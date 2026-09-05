"""FastAPI application factory for Cloud Run."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Header, HTTPException

from iam_agent.agents import (
    ImpactAnalysisAgent,
    InvestigationAgent,
    PolicyAgent,
    RemediationAgent,
)
from iam_agent.authentication import ChatAuthenticationError, GoogleChatRequestVerifier
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
    chat: GoogleChatHandler


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    repository = SQLActionRepository(settings.database_url)
    authorization = AuthorizationService.from_environment_or_yaml(
        settings.authorization_config,
        analysts=settings.chat_analyst_principals,
        approvers=settings.chat_approver_principals,
        admins=settings.chat_admin_principals,
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
