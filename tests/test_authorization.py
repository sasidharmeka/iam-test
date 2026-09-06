import pytest

from iam_agent.authorization import (
    AuthorizationError,
    AuthorizationService,
    IdentityDirectory,
    PrincipalGrant,
    scope_contains,
)
from iam_agent.models import UserRole


def test_hierarchy_uses_path_segments() -> None:
    assert scope_contains("/company/finance", "/company/finance/payments")
    assert not scope_contains("/company/fin", "/company/finance")


def test_registered_analyst_can_query() -> None:
    service = AuthorizationService(
        [
            PrincipalGrant(
                principal="analyst@example.com",
                role=UserRole.ANALYST,
                scope="/company/finance",
            )
        ]
    )

    grant = service.require_query("analyst@example.com")

    assert grant.scope == "/company/finance"
    with pytest.raises(AuthorizationError):
        service.require_query("unknown@example.com")


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
        analysts="first@demo.test, second@demo.test",
        default_scope="/company",
    )

    assert service.grant_for("first@demo.test").role == UserRole.ANALYST
    assert service.grant_for("second@demo.test").scope == "/company"
    with pytest.raises(AuthorizationError):
        service.grant_for("placeholder@example.com")


def test_identity_directory_exposes_scoped_virtual_dimensions() -> None:
    directory = IdentityDirectory(
        {
            "user1": "/company/finance/manager",
            "user2": "/company/engineering/analyst",
        }
    )

    assert directory.users_within("/company/finance") == ["user1"]
    assert directory.attributes_for("user2") == {
        "department": "engineering",
        "job_role": "analyst",
        "organizational_scope": "/company/engineering/analyst",
    }
