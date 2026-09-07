"""Loads and exposes the versioned IAM semantic catalog."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from iam_agent.models import IAMDomain


class CatalogError(ValueError):
    pass


class DomainDefinition(BaseModel):
    description: str
    synonyms: list[str] = Field(default_factory=list)


class RoutingTopic(BaseModel):
    description: str
    aliases: list[str]
    concepts: list[str]


class FieldDefinition(BaseModel):
    type: str
    description: str
    dimension: bool = False
    filterable: bool = True
    selectable: bool = True


class MetricDefinition(BaseModel):
    description: str
    expression: str
    result_type: str


class ConceptDefinition(BaseModel):
    table: str
    description: str
    grain: str
    domains: list[IAMDomain]
    entity: str
    user_field: str | None = None
    time_field: str = "event_date"
    include_common_fields: bool = True
    fields: dict[str, FieldDefinition]
    metrics: dict[str, MetricDefinition]
    default_fields: list[str]
    synonyms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def referenced_fields_exist(self) -> ConceptDefinition:
        references = [*self.default_fields]
        if self.user_field:
            references.append(self.user_field)
        if self.time_field:
            references.append(self.time_field)
        missing = sorted(set(references) - set(self.fields))
        if missing:
            raise ValueError(f"Unknown referenced fields: {', '.join(missing)}")
        return self


class CatalogPayload(BaseModel):
    version: int
    domains: dict[IAMDomain, DomainDefinition]
    virtual_dimensions: dict[str, FieldDefinition]
    common_fields: dict[str, FieldDefinition]
    concepts: dict[str, ConceptDefinition]
    routing_topics: dict[str, RoutingTopic] = Field(default_factory=dict)


class SemanticCatalog:
    """Trusted mapping from IAM language to allowlisted physical BigQuery metadata."""

    def __init__(self, payload: CatalogPayload):
        self.version = payload.version
        self.domains = payload.domains
        self.virtual_dimensions = payload.virtual_dimensions
        self.common_fields = payload.common_fields
        self.concepts = payload.concepts
        self.routing_topics = payload.routing_topics
        self._validate_physical_identifiers()
        for topic in self.routing_topics.values():
            if not topic.concepts or set(topic.concepts) - set(self.concepts):
                raise CatalogError("Routing topics must reference existing catalog concepts")

    @classmethod
    def from_yaml(cls, path: str | Path) -> SemanticCatalog:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        common = raw.get("common_fields", {})
        for concept in raw.get("concepts", {}).values():
            if concept.get("include_common_fields", True):
                concept["fields"] = {**common, **concept.get("fields", {})}
        try:
            return cls(CatalogPayload.model_validate(raw))
        except Exception as exc:
            raise CatalogError(f"Invalid semantic catalog: {exc}") from exc

    def _validate_physical_identifiers(self) -> None:
        identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for concept_name, concept in self.concepts.items():
            names = [concept_name, concept.table, *concept.fields, *concept.metrics]
            invalid = [name for name in names if not identifier.fullmatch(name)]
            if invalid:
                raise CatalogError(f"Unsafe identifiers in {concept_name}: {invalid}")

    def domain_summaries(self) -> dict[str, dict]:
        """Return compact but grounded cards for first-pass domain routing.

        Every concept is represented here so routing never depends on Gemini already knowing
        what an internal domain name means. Physical tables, fields, and metrics remain hidden
        until the selected-domain description call.
        """
        summaries: dict[str, dict] = {}
        for domain, definition in self.domains.items():
            summaries[domain.value] = {
                "description": definition.description,
                "synonyms": definition.synonyms,
                "concepts": [
                    {
                        "name": name,
                        "description": concept.description,
                        "entity": concept.entity,
                        "grain": concept.grain,
                        "synonyms": concept.synonyms,
                    }
                    for name, concept in self.concepts.items()
                    if domain in concept.domains
                ],
            }
        return summaries

    def concept(self, name: str) -> ConceptDefinition:
        try:
            return self.concepts[name]
        except KeyError as exc:
            raise CatalogError(f"Unknown IAM concept: {name}") from exc

    def planner_context(
        self, domains: list[IAMDomain], *, concept_names: list[str] | None = None
    ) -> dict:
        selected_domains = set(domains)
        concepts: list[dict] = []
        for name, definition in self.concepts.items():
            if concept_names is not None and name not in concept_names:
                continue
            if concept_names is None and not selected_domains.intersection(definition.domains):
                continue
            concepts.append(
                {
                    "concept": name,
                    "description": definition.description,
                    "grain": definition.grain,
                    "entity": definition.entity,
                    "domains": [domain.value for domain in definition.domains],
                    "field_types": {
                        field_name: field.type for field_name, field in definition.fields.items()
                    },
                    "user_field": definition.user_field,
                    "time_field": definition.time_field,
                    "default_fields": definition.default_fields,
                    "selectable_fields": {
                        field_name: field.description
                        for field_name, field in definition.fields.items()
                        if field.selectable
                    },
                    "dimensions": {
                        field_name: field.description
                        for field_name, field in definition.fields.items()
                        if field.dimension
                    }
                    | {
                        name: field.description
                        for name, field in self.virtual_dimensions.items()
                        if definition.user_field
                    },
                    "filterable_fields": [
                        field_name
                        for field_name, field in definition.fields.items()
                        if field.filterable
                    ]
                    + (list(self.virtual_dimensions) if definition.user_field else []),
                    "metrics": {
                        metric_name: metric.description
                        for metric_name, metric in definition.metrics.items()
                    },
                }
            )
        return {
            "catalog_version": self.version,
            "domains": {domain.value: self.domains[domain].description for domain in domains},
            "concepts": concepts,
            "rules": [
                "Each query step uses exactly one concept; use multiple steps for multiple tables.",
                "Record operations use fields and no metrics.",
                "Aggregate, rank, and trend operations use catalog metrics.",
                "Trend operations include event_date as a dimension.",
                "Physical table names and SQL never appear in an analysis plan.",
            ],
        }
