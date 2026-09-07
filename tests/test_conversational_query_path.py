"""Regression coverage through the real MCP protocol and BigQuery SDK parameter builder."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Forbidden, NotFound

from iam_agent.answerer import AnswerAgent
from iam_agent.authorization import AuthorizationService, IdentityDirectory, PrincipalGrant
from iam_agent.chat import GoogleChatHandler
from iam_agent.config import REPOSITORY_ROOT, Settings
from iam_agent.conversation import ConversationStore
from iam_agent.mcp_client import IAMEvidenceMCPClient
from iam_agent.models import AnalysisPlan, AnalystAnswer, QueryStep, QuestionDecomposition, UserRole
from iam_agent.orchestrator import IAMOrchestrator
from iam_agent.planner import QuestionPlanner
from iam_agent.policy_rag import PolicyStore
from iam_mcp.bigquery_executor import BigQueryExecutor
from iam_mcp.catalog import SemanticCatalog
from iam_mcp.compiler import QueryCompiler
from iam_mcp.server import create_mcp_server
from iam_mcp.service import IAMEvidenceService
from iam_mcp.validator import QueryPlanValidator


class BigQueryTransport:
    def __init__(self, *, forbidden=False, missing_timeline=False):
        self.queries = []
        self.forbidden = forbidden
        self.missing_timeline = missing_timeline

    def query(self, sql, *, job_config):
        self.queries.append(sql)
        if self.forbidden:
            raise Forbidden("The runtime principal cannot read this dataset")
        if self.missing_timeline and ".identity_timelines`" in sql:
            raise NotFound("The timeline output is missing")
        # Serialize real SDK query parameters, including array values and any dates.
        job_config.to_api_repr()
        return SimpleNamespace(
            total_bytes_processed=50,
            result=lambda: [
                {
                    "user": "user12",
                    "risk_score": 91,
                    "risk_factors": ["MFA bypass", "stale access"],
                    "event_date": date(2026, 9, 7),
                    "detected_at": datetime(2026, 9, 7, tzinfo=UTC),
                }
            ],
        )


class PlanningModel:
    def __init__(self, *, target, wrong_first=False, always_wrong=False, general=False):
        self.target = target
        self.wrong_first = wrong_first
        self.always_wrong = always_wrong
        self.general = general
        self.plans = 0

    async def generate_structured(self, *, response_model, prompt, **kwargs):
        if response_model is QuestionDecomposition:
            # Deliberately choose the wrong nonempty domain to exercise deterministic recovery.
            assert "retrieved_candidates" in prompt
            return QuestionDecomposition(
                answer_mode="general" if self.general else "data",
                clarified_question=self.target,
                domains=["access_and_provisioning"],
                requires_project_data=not self.general,
            )
        if response_model is AnalysisPlan:
            self.plans += 1
            wrong = self.always_wrong or (self.wrong_first and self.plans == 1)
            concepts = (
                ["stale_access"]
                if wrong
                else (
                    ["risk_scores", "identity_timelines"]
                    if "user12" in self.target
                    else ["risk_scores"]
                )
            )
            if self.target.startswith("Show MFA") and not wrong:
                concepts = ["mfa_bypass_events"]
            return AnalysisPlan(
                objective=self.target,
                steps=[
                    QueryStep(
                        step_id=f"query_{index}",
                        purpose=self.target,
                        concept=concept,
                        operation="records",
                        limit=5,
                        filters=[{"field": "user", "operator": "equals", "value": "user12"}]
                        if "user12" in self.target
                        else [],
                    )
                    for index, concept in enumerate(concepts)
                ],
            )
        return AnalystAnswer(answer="Based on the returned evidence.", sources=["risk_scores"])


def handler(model, transport):
    settings = Settings(_env_file=None)
    catalog = SemanticCatalog.from_yaml(settings.semantic_catalog_config)
    identities = IdentityDirectory.from_yaml(settings.identity_directory_config)
    authorization = AuthorizationService(
        [PrincipalGrant(principal="analyst@example.com", role=UserRole.ANALYST, scope="/company")]
    )
    service = IAMEvidenceService(
        catalog=catalog,
        validator=QueryPlanValidator(
            settings=settings, catalog=catalog, authorization=authorization, identities=identities
        ),
        compiler=QueryCompiler(settings=settings, catalog=catalog, identities=identities),
        executor=BigQueryExecutor(settings, client=transport),
    )
    orchestrator = IAMOrchestrator(
        settings=settings,
        authorization=authorization,
        policy_store=PolicyStore(REPOSITORY_ROOT / "policies"),
        mcp_client=IAMEvidenceMCPClient(create_mcp_server(service)),
        planner=QuestionPlanner(model),
        answerer=AnswerAgent(model),
        conversations=ConversationStore(max_sessions=5, max_turns=3),
    )
    return GoogleChatHandler(orchestrator)


async def ask(chat, question):
    return await chat.handle(
        {
            "type": "MESSAGE",
            "user": {"email": "analyst@example.com"},
            "message": {"text": question},
        },
        include_reports=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    ["Which users are riskiest, and why?", "tell me about user12", "Show MFA bypass for user12"],
)
async def test_screenshot_questions_and_wrong_domain_repair_use_real_mcp(question):
    model = PlanningModel(target=question, wrong_first=True)
    transport = BigQueryTransport()
    response = await ask(handler(model, transport), question)
    assert "error" not in response, response
    assert model.plans == 2
    assert transport.queries
    assert not any(".stale_access`" in sql for sql in transport.queries)


@pytest.mark.asyncio
async def test_repeated_wrong_domain_stops_after_one_repair_without_querying():
    question = "Show MFA bypass for user12"
    model = PlanningModel(target=question, always_wrong=True)
    transport = BigQueryTransport()
    response = await ask(handler(model, transport), question)
    assert response["error"]["code"] == "plan_rejected"
    assert model.plans == 2
    assert not transport.queries


@pytest.mark.asyncio
async def test_cloud_permission_failure_is_named_and_does_not_trigger_replanning():
    question = "Which users are riskiest, and why?"
    model = PlanningModel(target=question)
    response = await ask(handler(model, BigQueryTransport(forbidden=True)), question)
    assert response["error"]["code"] == "bigquery_permission"
    assert response["error"]["stage"] == "evidence_query"
    assert model.plans == 1


@pytest.mark.asyncio
async def test_missing_profile_source_preserves_successful_evidence_with_explicit_caveat():
    question = "tell me about user12"
    model = PlanningModel(target=question)
    response = await ask(handler(model, BigQueryTransport(missing_timeline=True)), question)
    assert "error" not in response, response
    assert "Could not read identity_timelines" in response["text"]
    assert "Sources: `risk_scores`" in response["text"]
    assert model.plans == 1


@pytest.mark.asyncio
async def test_greeting_has_no_bigquery_access():
    transport = BigQueryTransport()
    response = await ask(handler(PlanningModel(target="hello", general=True), transport), "hello")
    assert "error" not in response
    assert transport.queries == []
