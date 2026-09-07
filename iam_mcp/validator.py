"""Deterministic semantic, authorization, and resource validation."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from iam_agent.authorization import AuthorizationService, IdentityDirectory, normalize_scope
from iam_agent.models import (
    AnalysisPlan,
    FilterOperator,
    QueryOperation,
    QueryStep,
)
from iam_mcp.catalog import ConceptDefinition, SemanticCatalog

if TYPE_CHECKING:
    from iam_agent.config import Settings


class QueryPlanError(ValueError):
    pass


class QueryPlanValidator:
    def __init__(
        self,
        *,
        settings: Settings,
        catalog: SemanticCatalog,
        authorization: AuthorizationService,
        identities: IdentityDirectory,
    ):
        self.settings = settings
        self.catalog = catalog
        self.authorization = authorization
        self.identities = identities

    def validate(self, plan: AnalysisPlan, principal: str) -> list[str]:
        grant = self.authorization.require_query(principal)
        if len(plan.steps) > self.settings.query_max_steps:
            raise QueryPlanError(
                f"The plan has {len(plan.steps)} steps; maximum is {self.settings.query_max_steps}"
            )
        authorized_users = self.identities.users_within(grant.scope)
        if not authorized_users:
            raise QueryPlanError("The authenticated principal has no visible identities")

        for step in plan.steps:
            concept = self.catalog.concept(step.concept)
            self._validate_step(step, concept, grant.scope, authorized_users)
        return authorized_users

    def _validate_step(
        self,
        step: QueryStep,
        concept: ConceptDefinition,
        granted_scope: str,
        authorized_users: list[str],
    ) -> None:
        if step.limit > self.settings.query_max_rows:
            raise QueryPlanError(
                f"Step {step.step_id!r} requests {step.limit} rows; "
                f"maximum is {self.settings.query_max_rows}"
            )
        if not concept.user_field and normalize_scope(granted_scope) not in {"/", "/company"}:
            raise QueryPlanError(
                f"Concept {step.concept!r} is organization-wide and cannot be safely scoped"
            )

        available_dimensions = {name for name, field in concept.fields.items() if field.dimension}
        if concept.user_field:
            available_dimensions.update(self.catalog.virtual_dimensions)
        self._require_known(step.dimensions, available_dimensions, "dimension", step.step_id)

        selectable_fields = {name for name, field in concept.fields.items() if field.selectable}
        self._require_known(step.fields, selectable_fields, "field", step.step_id)
        self._require_known(step.metrics, set(concept.metrics), "metric", step.step_id)

        if step.operation == QueryOperation.RECORDS:
            selected = step.fields or concept.default_fields
            self._require_known(selected, selectable_fields, "default field", step.step_id)
        elif step.fields:
            raise QueryPlanError(f"Step {step.step_id!r} cannot mix record fields and metrics")

        filterable_fields = {name for name, field in concept.fields.items() if field.filterable}
        if concept.user_field:
            filterable_fields.update(self.catalog.virtual_dimensions)
        for item in step.filters:
            if item.field not in filterable_fields:
                raise QueryPlanError(
                    f"Filter field {item.field!r} is not available for {step.concept!r}"
                )
            field_type = (
                concept.fields[item.field].type
                if item.field in concept.fields
                else self.catalog.virtual_dimensions[item.field].type
            )
            self._validate_filter_value(item.operator, item.value, field_type)
            if item.field == concept.user_field:
                self._validate_user_filter(item.operator, item.value, authorized_users)

        selected_aliases = set(step.fields or concept.default_fields)
        selected_aliases.update(step.dimensions)
        selected_aliases.update(step.metrics)
        for ordering in step.order_by:
            if ordering.field not in selected_aliases:
                raise QueryPlanError(
                    f"Sort field {ordering.field!r} is not selected by step {step.step_id!r}"
                )

    @staticmethod
    def _require_known(requested: list[str], allowed: set[str], label: str, step_id: str) -> None:
        unknown = sorted(set(requested) - allowed)
        if unknown:
            raise QueryPlanError(f"Unknown {label}(s) for step {step_id!r}: {', '.join(unknown)}")

    @staticmethod
    def _validate_user_filter(
        operator: FilterOperator, value: Any, authorized_users: list[str]
    ) -> None:
        if operator not in {FilterOperator.EQUALS, FilterOperator.IN}:
            return
        requested = value if isinstance(value, list) else [value]
        forbidden = sorted(
            str(user) for user in requested if str(user).casefold() not in authorized_users
        )
        if forbidden:
            raise QueryPlanError("Requested identities are outside the authenticated scope")

    @staticmethod
    def _validate_filter_value(operator: FilterOperator, value: Any, field_type: str) -> None:
        values = value if isinstance(value, list) else [value]
        if operator == FilterOperator.CONTAINS and field_type != "string":
            raise QueryPlanError("The contains operator is available only for string fields")
        ordered_operators = {
            FilterOperator.GREATER_THAN,
            FilterOperator.GREATER_THAN_OR_EQUAL,
            FilterOperator.LESS_THAN,
            FilterOperator.LESS_THAN_OR_EQUAL,
        }
        if operator in ordered_operators and field_type not in {
            "integer",
            "float",
            "date",
            "timestamp",
        }:
            raise QueryPlanError(f"Operator {operator.value!r} is not valid for {field_type}")

        def compatible(item: Any) -> bool:
            if field_type == "string":
                return isinstance(item, str)
            if field_type == "integer":
                return isinstance(item, int) and not isinstance(item, bool)
            if field_type == "float":
                return isinstance(item, (int, float)) and not isinstance(item, bool)
            if field_type == "boolean":
                return isinstance(item, bool)
            if field_type in {"date", "timestamp"}:
                return isinstance(item, (str, date, datetime))
            return False

        if not values or not all(compatible(item) for item in values):
            raise QueryPlanError(f"Filter value does not match field type {field_type!r}")
