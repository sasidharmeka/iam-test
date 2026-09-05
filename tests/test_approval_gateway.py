from __future__ import annotations

import pytest

from iam_agent.authorization import AuthorizationError, AuthorizationService, PrincipalGrant
from iam_agent.execution import ApprovalGateway, DeterministicIAMExecutor
from iam_agent.models import ActionStatus, UserRole


@pytest.mark.asyncio
async def test_approved_action_executes_and_verifies(repository, pending_action) -> None:
    repository.create_action(pending_action)
    authorization = AuthorizationService(
        [
            PrincipalGrant(
                principal="approver@example.com",
                role=UserRole.APPROVER,
                scope="/company/finance",
            )
        ]
    )
    gateway = ApprovalGateway(repository, authorization, DeterministicIAMExecutor(repository))

    result = await gateway.decide(
        action_id=pending_action.action_id,
        approver="approver@example.com",
        approve=True,
        reason="Validated evidence",
    )

    assert result.action.status == ActionStatus.EXECUTED
    assert result.execution is not None and result.execution.verified
    assert result.execution.after_state == {"member": False}
    assert [item["stage"] for item in repository.list_audit(pending_action.action_id)] == [
        "ACTION_CREATED",
        "APPROVED",
        "EXECUTING",
        "EXECUTED",
    ]


@pytest.mark.asyncio
async def test_requestor_cannot_self_approve(repository, pending_action) -> None:
    repository.create_action(pending_action)
    authorization = AuthorizationService(
        [
            PrincipalGrant(
                principal="analyst@example.com",
                role=UserRole.ADMIN,
                scope="/",
            )
        ]
    )
    gateway = ApprovalGateway(repository, authorization, DeterministicIAMExecutor(repository))

    with pytest.raises(AuthorizationError):
        await gateway.decide(
            action_id=pending_action.action_id,
            approver="analyst@example.com",
            approve=True,
        )

    assert repository.get_action(pending_action.action_id).status == ActionStatus.PENDING_APPROVAL
