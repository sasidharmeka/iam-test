import pytest

from iam_agent.authorization import (
    AuthorizationError,
    AuthorizationService,
    PrincipalGrant,
    scope_contains,
)
from iam_agent.models import Permission, UserRole


def test_hierarchy_uses_path_segments() -> None:
    assert scope_contains("/company/finance", "/company/finance/payments")
    assert not scope_contains("/company/fin", "/company/finance")


def test_analyst_can_investigate_but_cannot_approve() -> None:
    service = AuthorizationService(
        [
            PrincipalGrant(
                principal="analyst@example.com",
                role=UserRole.ANALYST,
                scope="/company/finance",
            )
        ]
    )
    service.require("analyst@example.com", Permission.INVESTIGATE, "/company/finance/payments")
    with pytest.raises(AuthorizationError):
        service.require("analyst@example.com", Permission.APPROVE, "/company/finance/payments")


def test_approver_cannot_cross_organizational_scope() -> None:
    service = AuthorizationService(
        [
            PrincipalGrant(
                principal="approver@example.com",
                role=UserRole.APPROVER,
                scope="/company/finance",
            )
        ]
    )
    with pytest.raises(AuthorizationError):
        service.require("approver@example.com", Permission.APPROVE, "/company/engineering")


def test_environment_principals_replace_yaml_examples(tmp_path) -> None:
    config = tmp_path / "authorization.yaml"
    config.write_text(
        "principals:\n"
        "  - principal: placeholder@example.com\n"
        "    role: analyst\n"
        "    scope: /company\n",
        encoding="utf-8",
    )

    service = AuthorizationService.from_environment_or_yaml(
        config,
        analysts="analyst@demo.test",
        approvers="first@demo.test, second@demo.test",
        admins="admin@demo.test",
        default_scope="/company",
    )

    assert service.grant_for("analyst@demo.test").role == UserRole.ANALYST
    assert service.grant_for("second@demo.test").role == UserRole.APPROVER
    assert service.grant_for("admin@demo.test").scope == "/"
    with pytest.raises(AuthorizationError):
        service.grant_for("placeholder@example.com")


def test_yaml_remains_local_fallback_when_environment_is_empty(tmp_path) -> None:
    config = tmp_path / "authorization.yaml"
    config.write_text(
        "principals:\n"
        "  - principal: local@example.com\n"
        "    role: analyst\n"
        "    scope: /company\n",
        encoding="utf-8",
    )

    service = AuthorizationService.from_environment_or_yaml(config)

    assert service.grant_for("local@example.com").role == UserRole.ANALYST
