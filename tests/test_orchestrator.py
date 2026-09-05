from __future__ import annotations

import pytest

from iam_agent.agents import (
    ImpactAnalysisAgent,
    InvestigationAgent,
    PolicyAgent,
    RemediationAgent,
)
from iam_agent.authorization import AuthorizationService, IdentityDirectory, PrincipalGrant
from iam_agent.models import (
    ActionStatus,
    ChatActor,
    EvidenceBundle,
    EvidenceRecord,
    ImpactAssessment,
    InvestigationFinding,
    InvestigationReport,
    Operation,
    PolicyAssessment,
    PolicyControl,
    RemediationRecommendation,
    TargetSystem,
    UserRole,
)
from iam_agent.orchestrator import IAMOrchestrator, IntentRouter
from iam_agent.policy_rag import PolicyStore


class FakeEvidence:
    async def collect(self, target_user: str) -> EvidenceBundle:
        return EvidenceBundle(
            target_user=target_user,
            risk_profile={"risk_level": "HIGH", "risk_score": 12},
            findings=[
                EvidenceRecord(
                    table="shadow_access_events",
                    data={
                        "finding_id": "finding-1",
                        "user": target_user,
                        "privileged_group": "Domain Admins",
                    },
                )
            ],
            queried_tables=["risk_scores", "shadow_access_events"],
        )


class FakeModel:
    def __init__(self):
        self.calls = 0

    async def generate_structured(self, *, system_instruction: str, prompt: str, response_model):
        self.calls += 1
        if response_model is InvestigationReport:
            return InvestigationReport(
                summary="Unsupported privilege",
                assessed_risk_level="HIGH",
                findings=[
                    InvestigationFinding(
                        source_table="shadow_access_events",
                        finding_id="finding-1",
                        severity="HIGH",
                        explanation="No request",
                    )
                ],
                confidence=0.9,
            )
        if response_model is PolicyAssessment:
            return PolicyAssessment(
                summary="Least privilege applies",
                applicable_controls=[
                    PolicyControl(
                        control_id="IAM-02",
                        title="Least privilege",
                        requirement="Review unsupported privilege",
                        source_chunk_id="policy:1",
                    )
                ],
            )
        if response_model is ImpactAssessment:
            return ImpactAssessment(
                business_impact="Admin access removed",
                affected_systems=["active_directory"],
                blast_radius="one membership",
                urgency="high",
            )
        if response_model is RemediationRecommendation:
            return RemediationRecommendation(
                operation=Operation.REMOVE_AD_GROUP,
                target_system=TargetSystem.ACTIVE_DIRECTORY,
                target_user="user1",
                target_resource="Domain Admins",
                rationale="Unsupported membership",
                risk_level="HIGH",
            )
        raise AssertionError(response_model)


@pytest.mark.asyncio
async def test_investigation_creates_one_idempotent_review_action(repository, tmp_path) -> None:
    (tmp_path / "policy.md").write_text(
        "# Policy\n\n## Least privilege\nReview unsupported privilege.", encoding="utf-8"
    )
    model = FakeModel()
    orchestrator = IAMOrchestrator(
        evidence=FakeEvidence(),
        authorization=AuthorizationService(
            [
                PrincipalGrant(
                    principal="analyst@example.com",
                    role=UserRole.ANALYST,
                    scope="/company/finance",
                )
            ]
        ),
        identities=IdentityDirectory({"user1": "/company/finance/manager"}),
        policy_store=PolicyStore(tmp_path),
        repository=repository,
        intent_router=IntentRouter(model),
        investigation_agent=InvestigationAgent(model),
        policy_agent=PolicyAgent(model),
        impact_agent=ImpactAnalysisAgent(model),
        remediation_agent=RemediationAgent(model),
    )

    request = dict(
        actor=ChatActor(principal="analyst@example.com"),
        target_user="user1",
        source_event_id="spaces/demo/messages/99",
        chat_space="spaces/demo",
        chat_thread="spaces/demo/threads/1",
        original_request="Investigate user1",
    )
    first = await orchestrator.investigate(**request)
    second = await orchestrator.investigate(**request)

    assert first.action is not None
    assert first.action.status == ActionStatus.PENDING_APPROVAL
    assert second.action is not None
    assert second.action.action_id == first.action.action_id
    assert model.calls == 4
