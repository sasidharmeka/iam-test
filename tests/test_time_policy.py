from pathlib import Path

import pytest

from iam_agent.models import AnalysisPlan, ConversationTurn, QueryStep
from iam_agent.standard_plans import risk_ranking_plan
from iam_mcp.catalog import SemanticCatalog
from iam_mcp.retrieval import CatalogRetriever
from iam_mcp.time_policy import QueryTimePolicy, has_requested_time_range

CATALOG = SemanticCatalog.from_yaml(
    Path(__file__).resolve().parents[1] / "config/semantic_catalog.yaml"
)


def test_default_history_clears_both_model_date_filters_and_relative_windows_only():
    plan = AnalysisPlan(
        objective="Inspect stored events",
        steps=[QueryStep(
            step_id="mfa", purpose="MFA evidence", concept="mfa_bypass_events",
            operation="records", time_range_days=30,
            filters=[
                {"field": "user", "operator": "equals", "value": "user12"},
                {"field": "event_date", "operator": "greater_than", "value": "2026-01-01"},
                {"field": "detected_at", "operator": "greater_than", "value": "2026-01-01"},
            ],
        )],
    )
    normalized, note = QueryTimePolicy(CATALOG).apply(plan, "MFA bypasses for user12", [])
    assert normalized.steps[0].time_range_days is None
    assert [item.field for item in normalized.steps[0].filters] == ["user"]
    assert normalized.steps[0].filters[0].value == "user12"
    assert plan.steps[0].time_range_days == 30  # No mutation of the proposed model plan.
    assert "all retained dates" in note


@pytest.mark.parametrize("question", ["MFA in 2025", "MFA in the last 365 days", "MFA yesterday"])
def test_explicit_date_requests_remain_available(question):
    plan = AnalysisPlan(objective="History", steps=[QueryStep(
        step_id="mfa", purpose="MFA evidence", concept="mfa_bypass_events",
        operation="records", time_range_days=365,
    )])
    normalized, _ = QueryTimePolicy(CATALOG).apply(plan, question, [])
    assert normalized.steps[0].time_range_days == 365


def test_only_explicit_time_followups_reuse_prior_period():
    history = [ConversationTurn(user_message="MFA in 2025", assistant_summary="Historical events")]
    assert has_requested_time_range("Show stale access in the same period", history)
    assert not has_requested_time_range("Show stale access", history)
    assert not has_requested_time_range("Show all history instead of the last 30 days", history)
    assert not has_requested_time_range("Which users may be risky?", [])


@pytest.mark.parametrize("question", [
    "Which Finance users are riskiest?",
    "Which users are riskiest in 2025?",
    "Which users are riskiest in Salesforce?",
    "Which users are riskiest compared with user12?",
])
def test_standard_ranking_does_not_discard_additional_constraints(question):
    context = CatalogRetriever(CATALOG).retrieve(question).catalog_context
    assert risk_ranking_plan(question, context, 500) is None
