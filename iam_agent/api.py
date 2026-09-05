"""FastAPI application factory for Cloud Run."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from iam_agent.agents import (
    ImpactAnalysisAgent,
    InvestigationAgent,
    PolicyAgent,
    RemediationAgent,
)
from iam_agent.authentication import (
    ChatAuthenticationError,
    GoogleChatRequestVerifier,
    GoogleWebIdentityVerifier,
    WebAuthenticationError,
)
from iam_agent.authorization import AuthorizationService, IdentityDirectory
from iam_agent.chat import GoogleChatHandler
from iam_agent.config import Settings, get_settings
from iam_agent.evidence import BigQueryEvidenceProvider
from iam_agent.execution import ApprovalGateway, DeterministicIAMExecutor
from iam_agent.gemini import GeminiClient
from iam_agent.orchestrator import IAMOrchestrator, IntentRouter
from iam_agent.policy_rag import PolicyStore
from iam_agent.repository import SQLActionRepository


@dataclass
class Container:
    settings: Settings
    repository: SQLActionRepository
    gemini: GeminiClient
    verifier: GoogleChatRequestVerifier
    web_verifier: GoogleWebIdentityVerifier
    chat: GoogleChatHandler


class WebMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class WebDecisionRequest(BaseModel):
    approve: bool


WEB_UI_PATH = Path(__file__).with_name("web_ui.html")


def _merge_principals(*values: str) -> str:
    return ",".join(value.strip(", ") for value in values if value.strip(", "))


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    repository = SQLActionRepository(settings.database_url)
    authorization = AuthorizationService.from_environment_or_yaml(
        settings.authorization_config,
        analysts=_merge_principals(
            settings.chat_analyst_principals,
            settings.web_analyst_principals,
        ),
        approvers=_merge_principals(
            settings.chat_approver_principals,
            settings.web_approver_principals,
        ),
        admins=_merge_principals(
            settings.chat_admin_principals,
            settings.web_admin_principals,
        ),
        default_scope=settings.chat_default_scope,
    )
    identities = IdentityDirectory.from_yaml(settings.identity_directory_config)
    gemini = GeminiClient(settings)
    evidence = BigQueryEvidenceProvider(settings)
    policy_store = PolicyStore(settings.policy_directory)
    orchestrator = IAMOrchestrator(
        evidence=evidence,
        authorization=authorization,
        identities=identities,
        policy_store=policy_store,
        repository=repository,
        intent_router=IntentRouter(gemini),
        investigation_agent=InvestigationAgent(gemini),
        policy_agent=PolicyAgent(gemini),
        impact_agent=ImpactAnalysisAgent(gemini),
        remediation_agent=RemediationAgent(gemini),
    )
    executor = DeterministicIAMExecutor(repository)
    approvals = ApprovalGateway(repository, authorization, executor)
    return Container(
        settings=settings,
        repository=repository,
        gemini=gemini,
        verifier=GoogleChatRequestVerifier(settings),
        web_verifier=GoogleWebIdentityVerifier(settings),
        chat=GoogleChatHandler(orchestrator, approvals),
    )


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container or build_container()
        await asyncio.to_thread(app.state.container.repository.create_schema)
        yield
        await app.state.container.gemini.close()

    app = FastAPI(title="IAM Agent Platform", version="2.0.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
        return await runtime.chat.handle(event)

    @app.post("/web/actions/{action_id}/decision")
    async def web_decision(
        action_id: str,
        request: WebDecisionRequest,
        authorization: str | None = Header(default=None),
    ) -> dict:
        runtime: Container = app.state.container
        identity = await asyncio.to_thread(web_identity, runtime, authorization)
        event = {
            "type": "CARD_CLICKED",
            "source": "Gmail web interface",
            "user": {
                "email": identity["email"],
                "displayName": identity["name"],
                "name": f"web-users/{identity['email']}",
            },
            "common": {
                "invokedFunction": "approve_action" if request.approve else "reject_action",
                "parameters": {"action_id": action_id},
            },
        }
        return await runtime.chat.handle(event)

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
