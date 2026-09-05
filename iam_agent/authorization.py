"""Authentication-adjacent identity mapping and hierarchy-based authorization."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel

from iam_agent.models import Permission, UserRole


class AuthorizationError(PermissionError):
    """Raised when an authenticated principal lacks a requested permission."""


class PrincipalGrant(BaseModel):
    principal: str
    role: UserRole
    scope: str


ROLE_PERMISSIONS: dict[UserRole, set[Permission]] = {
    UserRole.VIEWER: {Permission.VIEW_ACTION},
    UserRole.ANALYST: {Permission.INVESTIGATE, Permission.VIEW_ACTION},
    UserRole.APPROVER: {
        Permission.INVESTIGATE,
        Permission.VIEW_ACTION,
        Permission.APPROVE,
    },
    UserRole.ADMIN: set(Permission),
}


def normalize_scope(scope: str) -> str:
    normalized = "/" + str(PurePosixPath("/" + scope.strip("/"))).strip("/")
    return "/" if normalized == "/." else normalized.rstrip("/")


def scope_contains(granted_scope: str, target_scope: str) -> bool:
    granted_parts = PurePosixPath(normalize_scope(granted_scope)).parts
    target_parts = PurePosixPath(normalize_scope(target_scope)).parts
    return target_parts[: len(granted_parts)] == granted_parts


class IdentityDirectory:
    """Resolves an IAM identity to its organizational hierarchy path."""

    def __init__(self, identities: dict[str, str]):
        self._identities = {
            key.casefold(): normalize_scope(value) for key, value in identities.items()
        }

    @classmethod
    def from_yaml(cls, path: str | Path) -> IdentityDirectory:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(payload.get("identities", {}))

    def scope_for(self, target_user: str) -> str:
        return self._identities.get(
            target_user.casefold(), f"/company/unassigned/{target_user.casefold()}"
        )


class AuthorizationService:
    """Applies role permissions and ancestor-scope hierarchy checks."""

    def __init__(self, grants: list[PrincipalGrant]):
        self._grants = {grant.principal.casefold(): grant for grant in grants}

    @classmethod
    def from_yaml(cls, path: str | Path) -> AuthorizationService:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls([PrincipalGrant.model_validate(item) for item in payload.get("principals", [])])

    @staticmethod
    def _principals(value: str) -> list[str]:
        """Parse a comma-separated environment value without accepting empty entries."""

        return [principal.strip() for principal in value.split(",") if principal.strip()]

    @classmethod
    def from_environment_or_yaml(
        cls,
        path: str | Path,
        *,
        analysts: str = "",
        approvers: str = "",
        admins: str = "",
        default_scope: str = "/company",
    ) -> AuthorizationService:
        """Use deployment-provided principals, falling back to the versioned YAML file."""

        if not any((analysts.strip(), approvers.strip(), admins.strip())):
            return cls.from_yaml(path)

        grants: list[PrincipalGrant] = []
        for principal in cls._principals(analysts):
            grants.append(
                PrincipalGrant(principal=principal, role=UserRole.ANALYST, scope=default_scope)
            )
        for principal in cls._principals(approvers):
            grants.append(
                PrincipalGrant(principal=principal, role=UserRole.APPROVER, scope=default_scope)
            )
        for principal in cls._principals(admins):
            grants.append(PrincipalGrant(principal=principal, role=UserRole.ADMIN, scope="/"))
        return cls(grants)

    def grant_for(self, principal: str) -> PrincipalGrant:
        grant = self._grants.get(principal.casefold())
        if grant is None:
            raise AuthorizationError(f"Principal {principal!r} is not registered")
        return grant

    def require(self, principal: str, permission: Permission, target_scope: str) -> PrincipalGrant:
        grant = self.grant_for(principal)
        if permission not in ROLE_PERMISSIONS[grant.role]:
            raise AuthorizationError(
                f"Role {grant.role.value!r} does not allow {permission.value!r}"
            )
        if not scope_contains(grant.scope, target_scope):
            raise AuthorizationError(
                f"Scope {grant.scope!r} does not contain target scope {target_scope!r}"
            )
        return grant
