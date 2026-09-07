"""FastAPI application factory for the read-only Cloud Run service."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from iam_agent.actions import ActionResolver, ReportAuthor
from iam_agent.answerer import AnswerAgent
from iam_agent.authentication import (
    ChatAuthenticationError,
    GoogleChatRequestVerifier,
    GoogleWebIdentityVerifier,
    WebAuthenticationError,
)
from iam_agent.authorization import AuthorizationError, AuthorizationService, IdentityDirectory
from iam_agent.chat import GoogleChatHandler
from iam_agent.config import Settings, get_settings
from iam_agent.conversation import ConversationStore
from iam_agent.conversation_actions import ConversationActions
from iam_agent.email_service import EmailError, EmailService
from iam_agent.gemini import GeminiClient
from iam_agent.mcp_client import IAMEvidenceMCPClient
from iam_agent.orchestrator import IAMOrchestrator
from iam_agent.planner import QuestionPlanner
from iam_agent.policy_rag import PolicyStore
from iam_agent.reporting import ReportService, ReportStore
from iam_agent.result_store import ResultStore
from iam_mcp.bigquery_executor import BigQueryExecutor
from iam_mcp.catalog import SemanticCatalog
from iam_mcp.compiler import QueryCompiler
from iam_mcp.server import create_mcp_server
from iam_mcp.service import IAMEvidenceService
from iam_mcp.validator import QueryPlanValidator


@dataclass
class Container:
    settings: Settings
    gemini: GeminiClient
    verifier: GoogleChatRequestVerifier
    web_verifier: GoogleWebIdentityVerifier
    mcp_client: IAMEvidenceMCPClient
    chat: GoogleChatHandler
    report_store: ReportStore
    email_service: EmailService | None = None
    authorization: AuthorizationService | None = None


class WebMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    conversation_id: str = Field(default="main", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    source_result_id: str = Field(default="", max_length=64)


class EmailPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)


class EmailSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_token: str = Field(min_length=1, max_length=128)
    confirmed: bool
    access_token: SecretStr = Field(min_length=1, max_length=8192)


WEB_UI_PATH = Path(__file__).with_name("web_ui.html")


def _merge_principals(*values: str) -> str:
    return ",".join(value.strip(", ") for value in values if value.strip(", "))


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    authorization = AuthorizationService.from_environment_or_yaml(
        settings.authorization_config,
        analysts=_merge_principals(
            settings.chat_analyst_principals,
            settings.web_analyst_principals,
        ),
        default_scope=settings.query_default_scope,
    )
    identities = IdentityDirectory.from_yaml(settings.identity_directory_config)
    catalog = SemanticCatalog.from_yaml(settings.semantic_catalog_config)
    gemini = GeminiClient(settings)
    validator = QueryPlanValidator(
        settings=settings,
        catalog=catalog,
        authorization=authorization,
        identities=identities,
    )
    compiler = QueryCompiler(settings=settings, catalog=catalog, identities=identities)
    evidence_service = IAMEvidenceService(
        catalog=catalog,
        validator=validator,
        compiler=compiler,
        executor=BigQueryExecutor(settings),
    )
    mcp_client = IAMEvidenceMCPClient(create_mcp_server(evidence_service))
    report_store = ReportStore()
    report_service = ReportService(report_store)
    email_service = EmailService(report_store)
    conversations = ConversationStore(
        max_sessions=settings.conversation_max_sessions,
        max_turns=settings.conversation_max_turns,
    )
    actions = ConversationActions(
        resolver=ActionResolver(gemini), author=ReportAuthor(gemini), results=ResultStore(),
        reports=report_service, emails=email_service, conversations=conversations,
    )
    orchestrator = IAMOrchestrator(
        settings=settings,
        authorization=authorization,
        policy_store=PolicyStore(settings.policy_directory),
        mcp_client=mcp_client,
        planner=QuestionPlanner(gemini),
        answerer=AnswerAgent(gemini),
        conversations=conversations,
        report_service=report_service,
        actions=actions,
    )
    return Container(
        settings=settings,
        gemini=gemini,
        verifier=GoogleChatRequestVerifier(settings),
        web_verifier=GoogleWebIdentityVerifier(settings),
        mcp_client=mcp_client,
        chat=GoogleChatHandler(orchestrator),
        report_store=report_store,
        email_service=email_service,
        authorization=authorization,
    )


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container or build_container()
        await app.state.container.mcp_client.initialize()
        yield
        await app.state.container.gemini.close()

    app = FastAPI(title="Read-only IAM Analytics Agent", version="3.4.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        runtime: Container = app.state.container
        return {
            "status": "ok",
            "mode": "read-only",
            "version": "3.4.0",
            "revision": os.environ.get("K_REVISION", "local"),
            "mcp_tools": list(runtime.mcp_client.tool_names),
        }

    def web_identity(runtime: Container, authorization: str | None) -> dict[str, str]:
        try:
            return runtime.web_verifier.verify(authorization)
        except WebAuthenticationError as exc:
            raise HTTPException(status_code=401, detail="Invalid Google sign-in") from exc

    @app.get("/", response_class=HTMLResponse)
    async def web_ui() -> HTMLResponse:
        runtime: Container = app.state.container
        if not runtime.settings.enable_web_ui:
            raise HTTPException(status_code=404, detail="Web UI is disabled")
        if not runtime.settings.web_google_client_id:
            raise HTTPException(status_code=503, detail="Google Sign-In is not configured")
        template = WEB_UI_PATH.read_text(encoding="utf-8")
        client_id = runtime.settings.web_google_client_id.replace("\\", "\\\\")
        client_id = client_id.replace('"', '\\"').replace("<", "\\u003c")
        return HTMLResponse(template.replace("__GOOGLE_CLIENT_ID__", client_id))

    @app.post("/web/messages")
    async def web_message(
        request: WebMessageRequest,
        authorization: str | None = Header(default=None),
    ) -> dict:
        runtime: Container = app.state.container
        identity = await asyncio.to_thread(web_identity, runtime, authorization)
        event = {
            "type": "MESSAGE",
            "user": {
                "email": identity["email"],
                "displayName": identity["name"],
                "name": f"web-users/{identity['email']}",
            },
            "message": {
                "text": request.text,
                "name": f"web-messages/{uuid4()}",
                "thread": {"name": request.conversation_id},
                "source_result_id": request.source_result_id,
            },
            "space": {"name": "web-spaces/browser-demo"},
        }
        return await runtime.chat.handle(event, include_reports=True)

    async def email_identity(authorization: str | None) -> tuple[Container, str]:
        runtime: Container = app.state.container
        identity = await asyncio.to_thread(web_identity, runtime, authorization)
        if runtime.email_service is None or runtime.authorization is None:
            raise HTTPException(status_code=503, detail="Email is not configured")
        try:
            runtime.authorization.require_query(identity["email"])
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail="Not authorized for this app") from exc
        return runtime, identity["email"]

    @app.post("/web/email/{draft_id}/preview")
    async def preview_email(
        draft_id: str, request: EmailPreviewRequest,
        authorization: str | None = Header(default=None),
    ) -> dict:
        runtime, owner = await email_identity(authorization)
        try:
            return await runtime.email_service.preview(
                draft_id=draft_id, owner=owner, **request.model_dump(),
            )
        except EmailError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.post("/web/email/{draft_id}/send")
    async def send_email(
        draft_id: str, request: EmailSendRequest,
        authorization: str | None = Header(default=None),
    ) -> dict:
        runtime, owner = await email_identity(authorization)
        try:
            return await runtime.email_service.send(
                draft_id=draft_id, owner=owner, review_token=request.review_token,
                confirmed=request.confirmed, access_token=request.access_token.get_secret_value(),
            )
        except EmailError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.get("/web/reports/{report_id}")
    async def download_report(
        report_id: str,
        authorization: str | None = Header(default=None),
    ) -> Response:
        runtime: Container = app.state.container
        identity = await asyncio.to_thread(web_identity, runtime, authorization)
        artifact = await runtime.report_store.get(report_id, identity["email"])
        if artifact is None:
            # Do not disclose whether a report exists for another signed-in identity.
            raise HTTPException(status_code=404, detail="Report not found or expired")
        return Response(
            content=artifact.content,
            media_type=artifact.mime_type,
            headers={
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''" + quote(artifact.filename, safe="")
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/google-chat/events")
    async def google_chat_event(
        event: dict,
        authorization: str | None = Header(default=None),
    ) -> dict:
        runtime: Container = app.state.container
        try:
            await asyncio.to_thread(runtime.verifier.verify, authorization)
        except ChatAuthenticationError as exc:
            raise HTTPException(status_code=401, detail="Invalid Google Chat request") from exc
        return await runtime.chat.handle(event)

    return app
