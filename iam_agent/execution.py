"""Approval boundary and deterministic simulated IAM execution tools."""

from __future__ import annotations

import asyncio

from iam_agent.authorization import AuthorizationError, AuthorizationService
from iam_agent.models import (
    ActionRecord,
    ActionStatus,
    ApprovalResult,
    ExecutionResult,
    Operation,
    Permission,
)
from iam_agent.repository import SQLActionRepository


class DeterministicIAMExecutor:
    """Maps a fixed operation enum to a fixed state mutation; no LLM is involved."""

    def __init__(self, repository: SQLActionRepository):
        self.repository = repository

    @staticmethod
    def _default_before(operation: Operation) -> dict:
        return {
            Operation.REMOVE_AD_GROUP: {"member": True},
            Operation.REVOKE_SAVIYNT_ACCESS: {"access_active": True},
            Operation.REQUIRE_MFA_RESET: {"mfa_reset_required": False},
            Operation.SUSPEND_IDENTITY: {"identity_active": True},
        }[operation]

    @staticmethod
    def _after(operation: Operation, before: dict) -> dict:
        after = dict(before)
        field, value = {
            Operation.REMOVE_AD_GROUP: ("member", False),
            Operation.REVOKE_SAVIYNT_ACCESS: ("access_active", False),
            Operation.REQUIRE_MFA_RESET: ("mfa_reset_required", True),
            Operation.SUSPEND_IDENTITY: ("identity_active", False),
        }[operation]
        after[field] = value
        return after

    @staticmethod
    def _verify(operation: Operation, state: dict) -> bool:
        field, expected = {
            Operation.REMOVE_AD_GROUP: ("member", False),
            Operation.REVOKE_SAVIYNT_ACCESS: ("access_active", False),
            Operation.REQUIRE_MFA_RESET: ("mfa_reset_required", True),
            Operation.SUSPEND_IDENTITY: ("identity_active", False),
        }[operation]
        return state.get(field) is expected

    def execute(self, action: ActionRecord) -> ExecutionResult:
        plan = action.recommendation
        if plan.operation == Operation.NO_ACTION:
            raise ValueError("no_action recommendations cannot be executed")
        before = self.repository.get_simulated_state(
            plan.target_system.value, plan.target_user, plan.target_resource
        ) or self._default_before(plan.operation)
        after = self._after(plan.operation, before)
        self.repository.set_simulated_state(
            plan.target_system.value, plan.target_user, plan.target_resource, after
        )
        observed = (
            self.repository.get_simulated_state(
                plan.target_system.value, plan.target_user, plan.target_resource
            )
            or {}
        )
        verified = self._verify(plan.operation, observed)
        return ExecutionResult(
            action_id=action.action_id,
            operation=plan.operation,
            verified=verified,
            before_state=before,
            after_state=observed,
            message=(
                "Simulated IAM change executed and verified"
                if verified
                else "Simulated IAM change executed but verification failed"
            ),
        )


class ApprovalGateway:
    """Enforces authorization, separation of duties, state transitions, and auditing."""

    def __init__(
        self,
        repository: SQLActionRepository,
        authorization: AuthorizationService,
        executor: DeterministicIAMExecutor,
    ):
        self.repository = repository
        self.authorization = authorization
        self.executor = executor

    async def decide(
        self,
        *,
        action_id: str,
        approver: str,
        approve: bool,
        reason: str = "",
    ) -> ApprovalResult:
        action = await asyncio.to_thread(self.repository.get_action, action_id)
        self.authorization.require(approver, Permission.APPROVE, action.target_scope)
        if action.requestor.casefold() == approver.casefold():
            raise AuthorizationError("The requestor cannot approve their own action")

        if action.status in {
            ActionStatus.REJECTED,
            ActionStatus.EXECUTED,
            ActionStatus.FAILED,
        }:
            return ApprovalResult(action=action)

        target_status = ActionStatus.APPROVED if approve else ActionStatus.REJECTED
        action = await asyncio.to_thread(
            self.repository.transition,
            action_id,
            expected=[ActionStatus.PENDING_APPROVAL],
            new_status=target_status,
            values={"decision_by": approver, "decision_reason": reason},
        )
        await asyncio.to_thread(
            self.repository.append_audit,
            action_id,
            target_status.value,
            approver,
            {"reason": reason},
        )
        if not approve:
            return ApprovalResult(action=action)

        action = await asyncio.to_thread(
            self.repository.transition,
            action_id,
            expected=[ActionStatus.APPROVED],
            new_status=ActionStatus.EXECUTING,
        )
        await asyncio.to_thread(
            self.repository.append_audit,
            action_id,
            ActionStatus.EXECUTING.value,
            "approval_gateway",
            {"operation": action.recommendation.operation.value},
        )
        try:
            execution = await asyncio.to_thread(self.executor.execute, action)
            terminal_status = ActionStatus.EXECUTED if execution.verified else ActionStatus.FAILED
            action = await asyncio.to_thread(
                self.repository.transition,
                action_id,
                expected=[ActionStatus.EXECUTING],
                new_status=terminal_status,
                values={"execution_result": execution.model_dump(mode="json")},
            )
            await asyncio.to_thread(
                self.repository.append_audit,
                action_id,
                terminal_status.value,
                "deterministic_executor",
                execution.model_dump(mode="json"),
            )
            return ApprovalResult(action=action, execution=execution)
        except Exception as exc:
            action = await asyncio.to_thread(
                self.repository.transition,
                action_id,
                expected=[ActionStatus.EXECUTING],
                new_status=ActionStatus.FAILED,
                values={"execution_result": {"error": type(exc).__name__}},
            )
            await asyncio.to_thread(
                self.repository.append_audit,
                action_id,
                ActionStatus.FAILED.value,
                "deterministic_executor",
                {"error": type(exc).__name__},
            )
            return ApprovalResult(action=action)
