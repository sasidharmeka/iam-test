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
from pydantic import BaseModel, Field

from iam_agent.answerer import AnswerAgent
from iam_agent.authentication import (
    ChatAuthenticationError,
    GoogleChatRequestVerifier,
    GoogleWebIdentityVerifier,
    WebAuthenticationError,
)
from iam_agent.authorization import AuthorizationService, IdentityDirectory
from iam_agent.chat import GoogleChatHandler
from iam_agent.config import Settings, get_settings
from iam_agent.conversation import ConversationStore
from iam_agent.gemini import GeminiClient
from iam_agent.mcp_client import IAMEvidenceMCPClient
from iam_agent.orchestrator import IAMOrchestrator
from iam_agent.planner import QuestionPlanner
from iam_agent.policy_rag import PolicyStore
from iam_agent.reporting import ReportService, ReportStore
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


class WebMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


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
    orchestrator = IAMOrchestrator(
        settings=settings,
        authorization=authorization,
        policy_store=PolicyStore(settings.policy_directory),
        mcp_client=mcp_client,
        planner=QuestionPlanner(gemini),
        answerer=AnswerAgent(gemini),
        conversations=ConversationStore(
            max_sessions=settings.conversation_max_sessions,
            max_turns=settings.conversation_max_turns,
        ),
        report_service=ReportService(report_store),
    )
    return Container(
        settings=settings,
        gemini=gemini,
        verifier=GoogleChatRequestVerifier(settings),
        web_verifier=GoogleWebIdentityVerifier(settings),
        mcp_client=mcp_client,
        chat=GoogleChatHandler(orchestrator),
        report_store=report_store,
    )


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container or build_container()
        await app.state.container.mcp_client.initialize()
        yield
        await app.state.container.gemini.close()

    app = FastAPI(title="Read-only IAM Analytics Agent", version="3.2.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        runtime: Container = app.state.container
        return {
            "status": "ok",
            "mode": "read-only",
            "version": "3.2.0",
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
            },
            "space": {"name": "web-spaces/browser-demo"},
        }
        return await runtime.chat.handle(event, include_reports=True)

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
