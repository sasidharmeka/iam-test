"""Typed contracts for the read-only conversational IAM analyst."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class UserRole(StrEnum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"


class Permission(StrEnum):
    QUERY = "query"


class IAMDomain(StrEnum):
    IDENTITY_RISK = "identity_risk"
    AUTHENTICATION = "authentication"
    ACCESS_AND_PROVISIONING = "access_and_provisioning"
    APPLICATION_BEHAVIOR = "application_behavior"
    LIFECYCLE_AND_DATA_QUALITY = "lifecycle_and_data_quality"


class AnswerMode(StrEnum):
    GENERAL = "general"
    DATA = "data"
    MIXED = "mixed"


class QueryOperation(StrEnum):
    RECORDS = "records"
    AGGREGATE = "aggregate"
    RANK = "rank"
    TREND = "trend"


class FilterOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    IN = "in"
    CONTAINS = "contains"


class SortDirection(StrEnum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


class ChatActor(BaseModel):
    principal: str
    display_name: str = ""
    chat_user_name: str = ""


class QuestionDecomposition(BaseModel):
    """Gemini's first pass: decide whether project data is required."""

    answer_mode: AnswerMode
    clarified_question: str
    domains: list[IAMDomain] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    requires_project_data: bool
    requires_policy_context: bool = False

    @model_validator(mode="after")
    def data_questions_need_domains(self) -> QuestionDecomposition:
        if self.requires_project_data and not self.domains:
            raise ValueError("A project-data question must select at least one IAM domain")
        if self.answer_mode == AnswerMode.GENERAL and self.requires_project_data:
            raise ValueError("A general answer cannot require project data")
        return self


FilterValue = str | int | float | bool | date | datetime | list[str] | list[int] | list[float]


class QueryFilter(BaseModel):
    field: str
    operator: FilterOperator
    value: FilterValue

    @model_validator(mode="after")
    def list_operator_matches_value(self) -> QueryFilter:
        if self.operator == FilterOperator.IN and not isinstance(self.value, list):
            raise ValueError("The 'in' operator requires a list value")
        if self.operator != FilterOperator.IN and isinstance(self.value, list):
            raise ValueError("List values are allowed only with the 'in' operator")
        if self.operator == FilterOperator.CONTAINS and not isinstance(self.value, str):
            raise ValueError("The 'contains' operator requires a string value")
        return self


class QuerySort(BaseModel):
    field: str
    direction: SortDirection = SortDirection.DESCENDING


class QueryStep(BaseModel):
    """One table-level semantic query. Physical identifiers are deliberately absent."""

    step_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    purpose: str = Field(min_length=1, max_length=300)
    concept: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    operation: QueryOperation
    fields: list[str] = Field(default_factory=list, max_length=20)
    metrics: list[str] = Field(default_factory=list, max_length=10)
    dimensions: list[str] = Field(default_factory=list, max_length=10)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=12)
    time_range_days: int | None = Field(default=None, ge=1)
    order_by: list[QuerySort] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=100, ge=1)

    @field_validator("fields", "metrics", "dimensions")
    @classmethod
    def values_must_be_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Query fields, metrics, and dimensions must be unique")
        return values

    @model_validator(mode="after")
    def operation_has_required_parts(self) -> QueryStep:
        if self.operation == QueryOperation.RECORDS and (self.metrics or self.dimensions):
            raise ValueError("Record queries select fields, not aggregate metrics or dimensions")
        if self.operation != QueryOperation.RECORDS and not self.metrics:
            raise ValueError("Aggregate, rank, and trend queries require at least one metric")
        if self.operation == QueryOperation.TREND and "event_date" not in self.dimensions:
            raise ValueError("Trend queries must use event_date as a dimension")
        return self


class AnalysisPlan(BaseModel):
    """A bounded collection of independent, read-only BigQuery steps."""

    objective: str = Field(min_length=1, max_length=500)
    steps: list[QueryStep] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def step_ids_are_unique(self) -> AnalysisPlan:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Analysis-plan step IDs must be unique")
        return self


class QueryParameter(BaseModel):
    name: str
    bigquery_type: str
    value: Any
    is_array: bool = False


class CompiledQuery(BaseModel):
    step_id: str
    concept: str
    table: str
    sql: str
    parameters: list[QueryParameter]
    limit: int


class QueryStepResult(BaseModel):
    step_id: str
    purpose: str
    concept: str
    source_table: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int
    bytes_processed: int = 0
    truncated: bool = False


class EvidenceBundle(BaseModel):
    objective: str
    results: list[QueryStepResult]
    queried_tables: list[str]
    collected_at: datetime = Field(default_factory=utc_now)
    warnings: list[str] = Field(default_factory=list)


class AnalystAnswer(BaseModel):
    answer: str
    key_findings: list[str] = Field(default_factory=list, max_length=12)
    sources: list[str] = Field(default_factory=list, max_length=22)
    caveats: list[str] = Field(default_factory=list, max_length=8)
    suggested_questions: list[str] = Field(default_factory=list, max_length=4)


class ConversationTurn(BaseModel):
    user_message: str
    assistant_summary: str
    entities: list[str] = Field(default_factory=list)
    domains: list[IAMDomain] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ChatWorkflowResult(BaseModel):
    decomposition: QuestionDecomposition
    answer: AnalystAnswer
    plan: AnalysisPlan | None = None
    evidence: EvidenceBundle | None = None
