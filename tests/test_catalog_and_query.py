from __future__ import annotations

from datetime import UTC, datetime

import pytest

from iam_agent.authorization import AuthorizationService, IdentityDirectory, PrincipalGrant
from iam_agent.config import REPOSITORY_ROOT, Settings
from iam_agent.models import (
    AnalysisPlan,
    FilterOperator,
    QueryFilter,
    QueryOperation,
    QueryStep,
    UserRole,
)
from iam_mcp.catalog import SemanticCatalog
from iam_mcp.compiler import QueryCompiler
from iam_mcp.validator import QueryPlanError, QueryPlanValidator

EXPECTED_OUTPUTS = {
    "impossible_login_anomalies",
    "mfa_bypass_events",
    "mfa_bypass_events_all_apps",
    "impossible_travel_events",
    "privilege_escalation_without_request",
    "ad_volume_spikes",
    "rejected_but_executed",
    "high_risk_sequence",
    "excessive_data_transferred",
    "role_drift",
    "stale_access",
    "multi_app_single_session",
    "mfa_drift",
    "suspicious_approver_behavior",
    "ad_group_churn_rate",
    "orphan_event_detection",
    "orphan_user_detection",
    "cleanest_users",
    "shadow_access_events",
    "risk_scores",
    "identity_timelines",
    "suspicious_time_warp",
}


def dependencies(scope: str = "/company"):
    settings = Settings(
        semantic_catalog_config=REPOSITORY_ROOT / "config" / "semantic_catalog.yaml",
        identity_directory_config=REPOSITORY_ROOT / "config" / "identities.yaml",
        _env_file=None,
    )
    catalog = SemanticCatalog.from_yaml(settings.semantic_catalog_config)
    identities = IdentityDirectory.from_yaml(settings.identity_directory_config)
    authorization = AuthorizationService(
        [
            PrincipalGrant(
                principal="analyst@example.com",
                role=UserRole.ANALYST,
                scope=scope,
            )
        ]
    )
    validator = QueryPlanValidator(
        settings=settings,
        catalog=catalog,
        authorization=authorization,
        identities=identities,
    )
    compiler = QueryCompiler(
        settings=settings,
        catalog=catalog,
        identities=identities,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    return settings, catalog, identities, validator, compiler


def test_catalog_documents_all_22_pipeline_outputs() -> None:
    _, catalog, _, _, _ = dependencies()

    assert set(catalog.concepts) == EXPECTED_OUTPUTS
    assert len(catalog.domains) == 5
    for name, concept in catalog.concepts.items():
        assert concept.table == name
        assert concept.description
        assert concept.grain
        assert {
            "detection_name",
            "finding_id",
            "event_date",
            "pipeline_run_id",
            "detected_at",
        }.issubset(concept.fields)
        assert concept.metrics

    domain_cards = catalog.domain_summaries()
    routed_concepts = {
        concept["name"]
        for card in domain_cards.values()
        for concept in card["concepts"]
    }
    assert routed_concepts == EXPECTED_OUTPUTS


def test_every_catalog_concept_and_metric_compiles_through_the_same_boundary() -> None:
    _, catalog, _, validator, compiler = dependencies()
    for concept_name, concept in catalog.concepts.items():
        record_step = QueryStep(
            step_id="records",
            purpose=f"Compile governed records for {concept_name}",
            concept=concept_name,
            operation=QueryOperation.RECORDS,
            limit=5,
        )
        record_plan = AnalysisPlan(objective="Compile records", steps=[record_step])
        users = validator.validate(record_plan, "analyst@example.com")
        record_query = compiler.compile(record_step, users)
        assert record_query.table == concept.table
        assert record_query.sql.startswith("SELECT ")

        for metric_name in concept.metrics:
            aggregate_step = QueryStep(
                step_id="aggregate",
                purpose=f"Compile {metric_name} for {concept_name}",
                concept=concept_name,
                operation=QueryOperation.AGGREGATE,
                metrics=[metric_name],
                limit=5,
            )
            aggregate_plan = AnalysisPlan(objective="Compile metric", steps=[aggregate_step])
            users = validator.validate(aggregate_plan, "analyst@example.com")
            aggregate_query = compiler.compile(aggregate_step, users)
            assert concept.metrics[metric_name].expression in aggregate_query.sql


def test_validator_and_compiler_create_only_allowlisted_read_query() -> None:
    _, _, identities, validator, compiler = dependencies()
    plan = AnalysisPlan(
        objective="Rank identities by risk",
        steps=[
            QueryStep(
                step_id="rank_risk",
                purpose="Return ten identities with the largest risk scores",
                concept="risk_scores",
                operation=QueryOperation.RANK,
                metrics=["maximum_risk_score"],
                dimensions=["user"],
                limit=10,
            )
        ],
    )

    authorized_users = validator.validate(plan, "analyst@example.com")
    compiled = compiler.compile(plan.steps[0], authorized_users)

    assert compiled.table == "risk_scores"
    assert "FROM `rapid-stage-506406-t4.iam_data_dev.risk_scores`" in compiled.sql
    assert "MAX(t.`risk_score`)" in compiled.sql
    assert "UNNEST(@authorized_users)" in compiled.sql
    assert "ORDER BY `maximum_risk_score` DESC" in compiled.sql
    assert all(
        forbidden not in compiled.sql.upper()
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ")
    )
    assert len(authorized_users) == len(identities.users_within("/company")) == 50


def test_virtual_department_filter_is_resolved_from_identity_directory() -> None:
    _, _, _, validator, compiler = dependencies("/company/finance")
    step = QueryStep(
        step_id="finance_risk",
        purpose="Summarize Finance risk",
        concept="risk_scores",
        operation=QueryOperation.AGGREGATE,
        metrics=["average_risk_score"],
        dimensions=["department"],
        filters=[
            QueryFilter(
                field="department",
                operator=FilterOperator.EQUALS,
                value="finance",
            )
        ],
        limit=10,
    )
    plan = AnalysisPlan(objective="Finance risk", steps=[step])

    users = validator.validate(plan, "analyst@example.com")
    compiled = compiler.compile(step, users)

    assert users
    assert "CASE t.`user`" in compiled.sql
    assert "AS `department`" in compiled.sql
    assert all(user.startswith("user") for user in users)


def test_validator_rejects_unknown_metric_and_cross_scope_user() -> None:
    _, _, _, validator, _ = dependencies("/company/finance")
    with pytest.raises(QueryPlanError, match="Unknown metric"):
        validator.validate(
            AnalysisPlan(
                objective="Unsupported",
                steps=[
                    QueryStep(
                        step_id="bad_metric",
                        purpose="Unsupported metric",
                        concept="risk_scores",
                        operation=QueryOperation.AGGREGATE,
                        metrics=["salary"],
                    )
                ],
            ),
            "analyst@example.com",
        )

    with pytest.raises(QueryPlanError, match="outside"):
        validator.validate(
            AnalysisPlan(
                objective="Cross scope",
                steps=[
                    QueryStep(
                        step_id="cross_scope",
                        purpose="Inspect engineering identity",
                        concept="risk_scores",
                        operation=QueryOperation.RECORDS,
                        filters=[
                            QueryFilter(
                                field="user",
                                operator=FilterOperator.EQUALS,
                                value="user5",
                            )
                        ],
                    )
                ],
            ),
            "analyst@example.com",
        )


def test_narrow_scope_cannot_query_unscoped_organization_table() -> None:
    _, _, _, validator, _ = dependencies("/company/finance")
    plan = AnalysisPlan(
        objective="AD volume",
        steps=[
            QueryStep(
                step_id="ad_spikes",
                purpose="List organization-wide AD volume spikes",
                concept="ad_volume_spikes",
                operation=QueryOperation.RECORDS,
            )
        ],
    )

    with pytest.raises(QueryPlanError, match="organization-wide"):
        validator.validate(plan, "analyst@example.com")


def test_validator_rejects_filter_type_mismatch() -> None:
    _, _, _, validator, _ = dependencies()
    plan = AnalysisPlan(
        objective="Invalid risk filter",
        steps=[
            QueryStep(
                step_id="bad_filter",
                purpose="Reject a string used as a numeric threshold",
                concept="risk_scores",
                operation=QueryOperation.RECORDS,
                filters=[
                    QueryFilter(
                        field="risk_score",
                        operator=FilterOperator.GREATER_THAN,
                        value="high",
                    )
                ],
            )
        ],
    )

    with pytest.raises(QueryPlanError, match="field type"):
        validator.validate(plan, "analyst@example.com")
