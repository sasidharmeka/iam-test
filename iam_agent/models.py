"""Typed contracts exchanged between the chat app, agents, and approval gateway."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class UserRole(StrEnum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    APPROVER = "approver"
    ADMIN = "admin"


class Permission(StrEnum):
    INVESTIGATE = "investigate"
    VIEW_ACTION = "view_action"
    APPROVE = "approve"


class IntentType(StrEnum):
    INVESTIGATE = "investigate"
    STATUS = "status"
    HELP = "help"
    UNKNOWN = "unknown"


class ActionStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class Operation(StrEnum):
    NO_ACTION = "no_action"
    REMOVE_AD_GROUP = "remove_ad_group"
    REVOKE_SAVIYNT_ACCESS = "revoke_saviynt_access"
    REQUIRE_MFA_RESET = "require_mfa_reset"
    SUSPEND_IDENTITY = "suspend_identity"


class TargetSystem(StrEnum):
    NONE = "none"
    ACTIVE_DIRECTORY = "active_directory"
    SAVIYNT = "saviynt"
    OKTA = "okta"


class ChatActor(BaseModel):
    principal: str
    display_name: str = ""
    chat_user_name: str = ""


class AgentIntent(BaseModel):
    intent: IntentType
    target_user: str | None = None
    action_id: str | None = None

    @model_validator(mode="after")
    def validate_required_identifier(self) -> AgentIntent:
        if self.intent == IntentType.INVESTIGATE and not self.target_user:
            raise ValueError("target_user is required for an investigation")
        if self.intent == IntentType.STATUS and not self.action_id:
            raise ValueError("action_id is required for a status request")
        return self


class EvidenceRecord(BaseModel):
    table: str
    data: dict[str, Any]


class EvidenceBundle(BaseModel):
    target_user: str
    risk_profile: dict[str, Any] = Field(default_factory=dict)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[EvidenceRecord] = Field(default_factory=list)
    queried_tables: list[str] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=utc_now)


class InvestigationFinding(BaseModel):
    source_table: str
    finding_id: str
    severity: str
    explanation: str
    evidence: list[str] = Field(default_factory=list)


class InvestigationReport(BaseModel):
    summary: str
    assessed_risk_level: str
    findings: list[InvestigationFinding] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    data_gaps: list[str] = Field(default_factory=list)


class PolicyControl(BaseModel):
    control_id: str
    title: str
    requirement: str
    source_chunk_id: str


class PolicyAssessment(BaseModel):
    summary: str
    applicable_controls: list[PolicyControl] = Field(default_factory=list)
    policy_gaps: list[str] = Field(default_factory=list)


class ImpactAssessment(BaseModel):
    business_impact: str
    affected_systems: list[str] = Field(default_factory=list)
    blast_radius: str
    urgency: str
    risks_of_action: list[str] = Field(default_factory=list)
    risks_of_inaction: list[str] = Field(default_factory=list)


class RemediationRecommendation(BaseModel):
    operation: Operation
    target_system: TargetSystem
    target_user: str
    target_resource: str
    parameters: dict[str, str] = Field(default_factory=dict)
    rationale: str
    risk_level: str
    verification_checks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def system_must_match_operation(self) -> RemediationRecommendation:
        expected = {
            Operation.NO_ACTION: TargetSystem.NONE,
            Operation.REMOVE_AD_GROUP: TargetSystem.ACTIVE_DIRECTORY,
            Operation.REVOKE_SAVIYNT_ACCESS: TargetSystem.SAVIYNT,
            Operation.REQUIRE_MFA_RESET: TargetSystem.OKTA,
            Operation.SUSPEND_IDENTITY: TargetSystem.OKTA,
        }
        if expected[self.operation] != self.target_system:
            raise ValueError("target_system is not valid for the selected operation")
        return self


class AgentAnalysis(BaseModel):
    investigation: InvestigationReport
    policy: PolicyAssessment
    impact: ImpactAssessment


class ActionRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action_id: str = Field(default_factory=lambda: str(uuid4()))
    status: ActionStatus = ActionStatus.PENDING_APPROVAL
    requestor: str
    target_user: str
    target_scope: str
    evidence_snapshot: EvidenceBundle
    recommendation: RemediationRecommendation
    analysis: AgentAnalysis
    source_event_id: str
    chat_space: str = ""
    chat_thread: str = ""
    decision_by: str | None = None
    decision_reason: str | None = None
    execution_result: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ExecutionResult(BaseModel):
    action_id: str
    operation: Operation
    verified: bool
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    message: str


class WorkflowResult(BaseModel):
    evidence: EvidenceBundle
    analysis: AgentAnalysis
    recommendation: RemediationRecommendation
    action: ActionRecord | None = None


class ApprovalResult(BaseModel):
    action: ActionRecord
    execution: ExecutionResult | None = None
