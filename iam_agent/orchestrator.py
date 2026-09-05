"""Coordinates evidence collection, specialized agents, and action creation."""

from __future__ import annotations

import asyncio
import json
import re

from iam_agent.agents import (
    ImpactAnalysisAgent,
    InvestigationAgent,
    PolicyAgent,
    RemediationAgent,
)
from iam_agent.agents.base import StructuredModel
from iam_agent.authorization import AuthorizationService, IdentityDirectory
from iam_agent.evidence import EvidenceProvider
from iam_agent.models import (
    ActionRecord,
    AgentAnalysis,
    AgentIntent,
    ChatActor,
    EvidenceBundle,
    IntentType,
    Operation,
    Permission,
    WorkflowResult,
)
from iam_agent.policy_rag import PolicyStore
from iam_agent.repository import SQLActionRepository


class UnsafeRecommendationError(ValueError):
    pass


class IntentRouter:
    """Uses deterministic commands first and Gemini for natural-language fallbacks."""

    STATUS_PATTERN = re.compile(r"^\s*status\s+([0-9a-fA-F-]{36})\s*$", re.IGNORECASE)
    USER_PATTERN = re.compile(r"\buser\d+\b", re.IGNORECASE)

    def __init__(self, model: StructuredModel):
        self.model = model

    async def route(self, message: str) -> AgentIntent:
        cleaned = message.strip()
        if not cleaned or cleaned.casefold() in {"help", "/help", "about"}:
            return AgentIntent(intent=IntentType.HELP)
        status_match = self.STATUS_PATTERN.match(cleaned)
        if status_match:
            return AgentIntent(intent=IntentType.STATUS, action_id=status_match.group(1))
        target_match = self.USER_PATTERN.search(cleaned)
        if target_match and any(
            keyword in cleaned.casefold()
            for keyword in ("investigate", "analyse", "analyze", "risk", "why")
        ):
            return AgentIntent(
                intent=IntentType.INVESTIGATE,
                target_user=target_match.group(0).casefold(),
            )
        return await self.model.generate_structured(
            system_instruction=(
                "Classify an IAM chat request as investigate, status, help, or unknown. "
                "Extract only an explicitly stated target_user or action_id. Never infer one."
            ),
            prompt=cleaned,
            response_model=AgentIntent,
        )


class IAMOrchestrator:
    """Runs the bounded agent workflow and persists reviewable actions."""

    def __init__(
        self,
        *,
        evidence: EvidenceProvider,
        authorization: AuthorizationService,
        identities: IdentityDirectory,
        policy_store: PolicyStore,
        repository: SQLActionRepository,
        intent_router: IntentRouter,
        investigation_agent: InvestigationAgent,
        policy_agent: PolicyAgent,
        impact_agent: ImpactAnalysisAgent,
        remediation_agent: RemediationAgent,
    ):
        self.evidence = evidence
        self.authorization = authorization
        self.identities = identities
        self.policy_store = policy_store
        self.repository = repository
        self.intent_router = intent_router
        self.investigation_agent = investigation_agent
        self.policy_agent = policy_agent
        self.impact_agent = impact_agent
        self.remediation_agent = remediation_agent

    @staticmethod
    def _supported_resources(bundle: EvidenceBundle, keys: set[str]) -> set[str]:
        values: set[str] = set()
        for record in bundle.findings:
            for key in keys:
                value = record.data.get(key)
                if isinstance(value, str) and value.strip():
                    values.add(value.casefold())
        return values

    def _validate_recommendation(self, bundle: EvidenceBundle, recommendation) -> None:
        if recommendation.target_user.casefold() != bundle.target_user.casefold():
            raise UnsafeRecommendationError("Gemini changed the target identity")
        if recommendation.operation == Operation.NO_ACTION:
            return
        if not recommendation.target_resource.strip():
            raise UnsafeRecommendationError("An executable operation needs a target resource")

        resource = recommendation.target_resource.casefold()
        if recommendation.operation == Operation.REMOVE_AD_GROUP:
            allowed = self._supported_resources(
                bundle, {"group", "privileged_group", "executed_group"}
            )
            if resource not in allowed:
                raise UnsafeRecommendationError("The AD group is absent from collected evidence")
        elif recommendation.operation == Operation.REVOKE_SAVIYNT_ACCESS:
            allowed = self._supported_resources(bundle, {"app", "requested_app"})
            if resource not in allowed:
                raise UnsafeRecommendationError("The application is absent from collected evidence")
        elif resource not in {"identity", bundle.target_user.casefold()}:
            raise UnsafeRecommendationError(
                "Identity-wide operations must target the investigated identity"
            )

    async def investigate(
        self,
        *,
        actor: ChatActor,
        target_user: str,
        source_event_id: str,
        chat_space: str,
        chat_thread: str,
        original_request: str,
    ) -> WorkflowResult:
        target_scope = self.identities.scope_for(target_user)
        self.authorization.require(actor.principal, Permission.INVESTIGATE, target_scope)

        existing = await asyncio.to_thread(self.repository.get_by_source_event, source_event_id)
        if existing:
            return WorkflowResult(
                evidence=existing.evidence_snapshot,
                analysis=existing.analysis,
                recommendation=existing.recommendation,
                action=existing,
            )

        bundle = await self.evidence.collect(target_user)
        retrieval_query = " ".join(
            [
                original_request,
                json.dumps(bundle.risk_profile, default=str),
                " ".join(record.table for record in bundle.findings),
            ]
        )
        chunks = self.policy_store.retrieve(retrieval_query)
        evidence_payload = bundle.model_dump(mode="json")
        investigation_task = self.investigation_agent.run({"evidence": evidence_payload})
        policy_task = self.policy_agent.run(
            {
                "target_user": target_user,
                "risk_profile": bundle.risk_profile,
                "policy_chunks": [
                    {
                        "chunk_id": chunk.chunk_id,
                        "source": chunk.source,
                        "text": chunk.text,
                    }
                    for chunk in chunks
                ],
            }
        )
        impact_task = self.impact_agent.run({"evidence": evidence_payload})
        investigation, policy, impact = await asyncio.gather(
            investigation_task, policy_task, impact_task
        )
        analysis = AgentAnalysis(
            investigation=investigation,
            policy=policy,
            impact=impact,
        )
        recommendation = await self.remediation_agent.run(
            {
                "target_user": target_user,
                "investigation": investigation.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
                "impact": impact.model_dump(mode="json"),
                "allowed_operations": [operation.value for operation in Operation],
            }
        )
        self._validate_recommendation(bundle, recommendation)

        if recommendation.operation == Operation.NO_ACTION:
            return WorkflowResult(
                evidence=bundle,
                analysis=analysis,
                recommendation=recommendation,
            )

        action = ActionRecord(
            requestor=actor.principal,
            target_user=target_user,
            target_scope=target_scope,
            evidence_snapshot=bundle,
            recommendation=recommendation,
            analysis=analysis,
            source_event_id=source_event_id,
            chat_space=chat_space,
            chat_thread=chat_thread,
        )
        action = await asyncio.to_thread(self.repository.create_action, action)
        return WorkflowResult(
            evidence=bundle,
            analysis=analysis,
            recommendation=recommendation,
            action=action,
        )

    async def get_status(self, actor: ChatActor, action_id: str) -> ActionRecord:
        action = await asyncio.to_thread(self.repository.get_action, action_id)
        self.authorization.require(actor.principal, Permission.VIEW_ACTION, action.target_scope)
        return action
