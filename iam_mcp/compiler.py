"""Deterministically compiles validated semantic plans into BigQuery SQL."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from iam_agent.authorization import IdentityDirectory
from iam_agent.config import Settings
from iam_agent.models import (
    CompiledQuery,
    FilterOperator,
    QueryFilter,
    QueryOperation,
    QueryParameter,
    QuerySort,
    QueryStep,
    SortDirection,
)
from iam_mcp.catalog import ConceptDefinition, SemanticCatalog

OPERATOR_SQL = {
    FilterOperator.EQUALS: "=",
    FilterOperator.NOT_EQUALS: "!=",
    FilterOperator.GREATER_THAN: ">",
    FilterOperator.GREATER_THAN_OR_EQUAL: ">=",
    FilterOperator.LESS_THAN: "<",
    FilterOperator.LESS_THAN_OR_EQUAL: "<=",
}

BIGQUERY_TYPES = {
    "string": "STRING",
    "integer": "INT64",
    "float": "FLOAT64",
    "boolean": "BOOL",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
}


class QueryCompiler:
    """The only component allowed to create SQL text."""

    def __init__(
        self,
        *,
        settings: Settings,
        catalog: SemanticCatalog,
        identities: IdentityDirectory,
        now: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.catalog = catalog
        self.identities = identities
        self._now = now or (lambda: datetime.now(UTC))

    def compile(self, step: QueryStep, authorized_users: list[str]) -> CompiledQuery:
        concept = self.catalog.concept(step.concept)
        parameters: list[QueryParameter] = []
        select_parts: list[str] = []
        selected_aliases: set[str] = set()

        if step.operation == QueryOperation.RECORDS:
            for field_name in step.fields or concept.default_fields:
                select_parts.append(f"t.`{field_name}` AS `{field_name}`")
                selected_aliases.add(field_name)
        else:
            for dimension in step.dimensions:
                expression = self._dimension_expression(
                    dimension, concept, authorized_users, parameters
                )
                select_parts.append(f"{expression} AS `{dimension}`")
                selected_aliases.add(dimension)
            for metric_name in step.metrics:
                metric = concept.metrics[metric_name]
                select_parts.append(f"{metric.expression} AS `{metric_name}`")
                selected_aliases.add(metric_name)

        where_parts: list[str] = []
        if concept.user_field:
            parameters.append(
                QueryParameter(
                    name="authorized_users",
                    bigquery_type="STRING",
                    value=authorized_users,
                    is_array=True,
                )
            )
            where_parts.append(f"t.`{concept.user_field}` IN UNNEST(@authorized_users)")

        for index, item in enumerate(step.filters):
            expression = self._dimension_expression(
                item.field, concept, authorized_users, parameters
            )
            where_parts.append(
                self._compile_filter(
                    expression=expression,
                    item=item,
                    parameter_name=f"filter_{index}",
                    concept=concept,
                    parameters=parameters,
                )
            )

        if step.time_range_days:
            start_date = (self._now() - timedelta(days=step.time_range_days)).date()
            parameters.append(
                QueryParameter(name="start_date", bigquery_type="DATE", value=start_date)
            )
            where_parts.append(f"t.`{concept.time_field}` >= @start_date")

        table_name = (
            f"`{self.settings.bigquery_project}.{self.settings.bigquery_dataset}.{concept.table}`"
        )
        sql_parts = [f"SELECT {', '.join(select_parts)}", f"FROM {table_name} AS t"]
        if where_parts:
            sql_parts.append("WHERE " + " AND ".join(where_parts))

        if step.operation != QueryOperation.RECORDS and step.dimensions:
            sql_parts.append(
                "GROUP BY " + ", ".join(str(index) for index in range(1, len(step.dimensions) + 1))
            )

        ordering = step.order_by
        if not ordering and step.operation == QueryOperation.RANK and step.metrics:
            ordering = [QuerySort(field=step.metrics[0], direction=SortDirection.DESCENDING)]
        if ordering:
            clauses = [
                f"`{item.field}` "
                + ("ASC" if item.direction == SortDirection.ASCENDING else "DESC")
                for item in ordering
            ]
            sql_parts.append("ORDER BY " + ", ".join(clauses))
        elif step.operation == QueryOperation.RECORDS and concept.time_field in selected_aliases:
            sql_parts.append(f"ORDER BY `{concept.time_field}` DESC")

        parameters.append(
            QueryParameter(name="result_limit", bigquery_type="INT64", value=step.limit)
        )
        sql_parts.append("LIMIT @result_limit")

        return CompiledQuery(
            step_id=step.step_id,
            concept=step.concept,
            table=concept.table,
            sql="\n".join(sql_parts),
            parameters=parameters,
            limit=step.limit,
        )

    def _dimension_expression(
        self,
        name: str,
        concept: ConceptDefinition,
        authorized_users: list[str],
        parameters: list[QueryParameter],
    ) -> str:
        if name in concept.fields:
            return f"t.`{name}`"
        if name not in self.catalog.virtual_dimensions or not concept.user_field:
            raise ValueError(f"Unknown dimension {name!r}")

        cases: list[str] = []
        existing_names = {parameter.name for parameter in parameters}
        for index, user in enumerate(authorized_users):
            user_parameter = f"directory_user_{index}"
            value_parameter = f"directory_{name}_{index}"
            attributes = self.identities.attributes_for(user)
            if user_parameter not in existing_names:
                parameters.append(
                    QueryParameter(name=user_parameter, bigquery_type="STRING", value=user)
                )
                existing_names.add(user_parameter)
            if value_parameter not in existing_names:
                parameters.append(
                    QueryParameter(
                        name=value_parameter,
                        bigquery_type="STRING",
                        value=attributes[name],
                    )
                )
                existing_names.add(value_parameter)
            cases.append(f"WHEN @{user_parameter} THEN @{value_parameter}")
        return f"CASE t.`{concept.user_field}` {' '.join(cases)} ELSE 'unassigned' END"

    def _compile_filter(
        self,
        *,
        expression: str,
        item: QueryFilter,
        parameter_name: str,
        concept: ConceptDefinition,
        parameters: list[QueryParameter],
    ) -> str:
        field_type = self._field_type(item.field, concept)
        bigquery_type = BIGQUERY_TYPES[field_type]
        if item.operator == FilterOperator.IN:
            parameters.append(
                QueryParameter(
                    name=parameter_name,
                    bigquery_type=bigquery_type,
                    value=item.value,
                    is_array=True,
                )
            )
            return f"{expression} IN UNNEST(@{parameter_name})"
        if item.operator == FilterOperator.CONTAINS:
            parameters.append(
                QueryParameter(
                    name=parameter_name,
                    bigquery_type="STRING",
                    value=f"%{str(item.value).casefold()}%",
                )
            )
            return f"LOWER(CAST({expression} AS STRING)) LIKE @{parameter_name}"

        parameters.append(
            QueryParameter(
                name=parameter_name,
                bigquery_type=bigquery_type,
                value=item.value,
            )
        )
        return f"{expression} {OPERATOR_SQL[item.operator]} @{parameter_name}"

    def _field_type(self, name: str, concept: ConceptDefinition) -> str:
        if name in concept.fields:
            return concept.fields[name].type
        return self.catalog.virtual_dimensions[name].type
