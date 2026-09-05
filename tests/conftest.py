from __future__ import annotations

import pytest

from iam_agent.models import (
    ActionRecord,
    AgentAnalysis,
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
)
from iam_agent.repository import SQLActionRepository


@pytest.fixture
def repository(tmp_path) -> SQLActionRepository:
    repo = SQLActionRepository(f"sqlite+pysqlite:///{tmp_path / 'actions.db'}")
    repo.create_schema()
    return repo


@pytest.fixture
def analysis() -> AgentAnalysis:
    return AgentAnalysis(
        investigation=InvestigationReport(
            summary="Unrequested privileged membership detected",
            assessed_risk_level="HIGH",
            findings=[
                InvestigationFinding(
                    source_table="shadow_access_events",
                    finding_id="finding-1",
                    severity="HIGH",
                    explanation="No approved request matches the directory grant",
                    evidence=["privileged_group=Domain Admins"],
                )
            ],
            confidence=0.95,
        ),
        policy=PolicyAssessment(
            summary="Least privilege control applies",
            applicable_controls=[
                PolicyControl(
                    control_id="IAM-02",
                    title="Least privilege",
                    requirement="Remove unsupported privilege after review",
                    source_chunk_id="iam_policy:2",
                )
            ],
        ),
        impact=ImpactAssessment(
            business_impact="Administrator access will be removed",
            affected_systems=["active_directory"],
            blast_radius="Single identity and group",
            urgency="high",
        ),
    )


@pytest.fixture
def pending_action(analysis) -> ActionRecord:
    return ActionRecord(
        requestor="analyst@example.com",
        target_user="user1",
        target_scope="/company/finance/manager",
        evidence_snapshot=EvidenceBundle(
            target_user="user1",
            risk_profile={"risk_level": "HIGH"},
            findings=[
                EvidenceRecord(
                    table="shadow_access_events",
                    data={"finding_id": "finding-1", "privileged_group": "Domain Admins"},
                )
            ],
            queried_tables=["risk_scores", "shadow_access_events"],
        ),
        recommendation=RemediationRecommendation(
            operation=Operation.REMOVE_AD_GROUP,
            target_system=TargetSystem.ACTIVE_DIRECTORY,
            target_user="user1",
            target_resource="Domain Admins",
            rationale="The membership has no matching approved request",
            risk_level="HIGH",
            verification_checks=["Confirm user is no longer a member"],
        ),
        analysis=analysis,
        source_event_id="spaces/demo/messages/1",
    )
