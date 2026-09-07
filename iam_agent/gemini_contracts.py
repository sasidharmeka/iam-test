"""Simple generation contracts; the richer application validators still run afterward.

Google's controlled generation accepts a subset of JSON Schema. The internal FilterValue union
contains scalar types, dates and three array alternatives. A JSON-encoded string at this wire
boundary avoids that branching schema without accepting SQL or weakening QueryFilter validation.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from iam_agent.models import (
    AnalysisPlan,
    AnswerMode,
    FilterOperator,
    IAMDomain,
    QueryOperation,
    QuerySort,
    QuestionDecomposition,
)


class GeminiFilter(BaseModel):
    field: str
    operator: FilterOperator
    value_json: str = Field(description='JSON-encoded value: "user12", 80, true, or ["user12"].')


class GeminiStep(BaseModel):
    step_id: str
    purpose: str
    concept: str
    operation: QueryOperation
    fields: list[str]
    metrics: list[str]
    dimensions: list[str]
    filters: list[GeminiFilter]
    time_range_days: int | None
    order_by: list[QuerySort]
    limit: int


class GeminiAnalysisPlan(BaseModel):
    objective: str
    steps: list[GeminiStep]

    def to_application(self) -> AnalysisPlan:
        payload = self.model_dump(mode="json")
        for step in payload["steps"]:
            for item in step["filters"]:
                item["value"] = json.loads(item.pop("value_json"))
        return AnalysisPlan.model_validate(payload)


class GeminiDecomposition(BaseModel):
    answer_mode: AnswerMode
    clarified_question: str
    domains: list[str]
    entities: list[str]
    requires_project_data: bool
    requires_policy_context: bool

    def to_application(self) -> QuestionDecomposition:
        payload = self.model_dump(mode="json")
        allowed = {domain.value for domain in IAMDomain}
        payload["domains"] = list(dict.fromkeys(
            domain for domain in self.domains if domain in allowed
        ))
        return QuestionDecomposition.model_validate(payload)
