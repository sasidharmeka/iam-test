from pathlib import Path

import pytest

from iam_agent.models import (
    AnalysisPlan,
    AnswerMode,
    ConversationTurn,
    IAMDomain,
    QueryStep,
    QuestionDecomposition,
)
from iam_agent.planner import QuestionPlanner
from iam_mcp.catalog import SemanticCatalog
from iam_mcp.retrieval import CatalogRetriever, EvidenceCoverageValidator
from iam_mcp.validator import QueryPlanError

CATALOG = SemanticCatalog.from_yaml(
    Path(__file__).resolve().parents[1] / "config/semantic_catalog.yaml"
)


def records(concept, *, user="user12", fields=None):
    return QueryStep(
        step_id=concept,
        purpose="Inspect requested evidence",
        concept=concept,
        operation="records",
        fields=fields or [],
        filters=[{"field": "user", "operator": "equals", "value": user}] if user else [],
        limit=5,
    )


@pytest.mark.parametrize(
    "question,topics,domains",
    [
        ("Which users are riskiest, and why?", {"risk", "risk_reasons"}, {"identity_risk"}),
        (
            "tell me about user12",
            {"risk_scores", "identity_timelines"},
            {"identity_risk", "lifecycle_and_data_quality"},
        ),
        ("Show authntication failures for user12", {"authentication"}, {"authentication"}),
        (
            "Show failed MFA and stale access for user12",
            {"mfa_bypass", "stale_access"},
            {"authentication", "access_and_provisioning"},
        ),
    ],
)
def test_catalog_retrieval_handles_screenshot_and_multi_topic_questions(question, topics, domains):
    retrieved = CatalogRetriever(CATALOG).retrieve(question)
    assert {item.topic for item in retrieved.requirements} == topics
    assert retrieved.requires_project_data
    assert domains.issubset({domain.value for domain in retrieved.candidate_domains})
    for item in retrieved.requirements:
        assert set(item.concepts).issubset(retrieved.candidate_concepts)


def test_low_confidence_widens_without_inventing_a_hard_domain_gate():
    retrieved = CatalogRetriever(CATALOG).retrieve("Investigate unusual activity")
    assert retrieved.low_confidence
    assert set(retrieved.candidate_concepts) == set(CATALOG.concepts)


def test_valid_access_table_does_not_satisfy_mfa_question():
    retrieval = CatalogRetriever(CATALOG).retrieve("Show MFA bypass for user12")
    validator = EvidenceCoverageValidator(CATALOG)
    with pytest.raises(QueryPlanError, match="Missing evidence"):
        validator.validate(
            AnalysisPlan(objective="Wrong domain", steps=[records("stale_access")]), retrieval
        )
    validator.validate(
        AnalysisPlan(objective="Correct evidence", steps=[records("mfa_bypass_events")]), retrieval
    )


def test_each_topic_and_requested_identity_must_be_preserved():
    retrieved = CatalogRetriever(CATALOG).retrieve("Show failed MFA and stale access for user12")
    validator = EvidenceCoverageValidator(CATALOG)
    with pytest.raises(QueryPlanError, match="stale_access"):
        validator.validate(
            AnalysisPlan(objective="Partial", steps=[records("mfa_bypass_events")]), retrieved
        )
    with pytest.raises(QueryPlanError, match="requested identities"):
        validator.validate(
            AnalysisPlan(
                objective="Wrong user",
                steps=[records("mfa_bypass_events", user="user16"), records("stale_access")],
            ),
            retrieved,
        )
    validator.validate(
        AnalysisPlan(
            objective="Both topics", steps=[records("mfa_bypass_events"), records("stale_access")]
        ),
        retrieved,
    )


def test_risk_score_alone_cannot_answer_why():
    retrieved = CatalogRetriever(CATALOG).retrieve("Which users are riskiest, and why?")
    with pytest.raises(QueryPlanError, match="risk_reasons"):
        EvidenceCoverageValidator(CATALOG).validate(
            AnalysisPlan(
                objective="Incomplete explanation",
                steps=[records("risk_scores", user=None, fields=["user", "risk_score"])],
            ),
            retrieved,
        )
    EvidenceCoverageValidator(CATALOG).validate(
        AnalysisPlan(objective="Risk with reasons", steps=[records("risk_scores", user=None)]),
        retrieved,
    )


def test_followup_uses_context_only_when_referenced():
    history = [
        ConversationTurn(
            user_message="Tell me about user12", assistant_summary="Profile", entities=["user12"]
        )
    ]
    retriever = CatalogRetriever(CATALOG)
    followup = retriever.retrieve("What about their MFA bypasses?", history)
    assert followup.entities == ["user12"]
    assert not any(
        item.topic in {"risk_scores", "identity_timelines"} for item in followup.requirements
    )
    assert retriever.retrieve("hello", history).entities == []
    assert not retriever.retrieve("What is MFA?", history).requires_project_data


@pytest.mark.asyncio
async def test_wrong_nonempty_gemini_domain_cannot_remove_authentication_candidates():
    retrieved = CatalogRetriever(CATALOG).retrieve("Show MFA bypass for user12")

    class WrongDomainModel:
        async def generate_structured(self, **kwargs):
            assert "mfa_bypass_events" in kwargs["prompt"]
            return QuestionDecomposition(
                answer_mode=AnswerMode.DATA,
                clarified_question="MFA bypass for user12",
                domains=[IAMDomain.ACCESS_AND_PROVISIONING],
                requires_project_data=True,
            )

    decomposition = await QuestionPlanner(WrongDomainModel()).decompose(
        question="Show MFA bypass for user12",
        history=[],
        domain_summaries=CATALOG.domain_summaries(),
        retrieval=retrieved,
    )
    assert IAMDomain.AUTHENTICATION in decomposition.domains
    assert set(retrieved.candidate_domains).issubset(decomposition.domains)
