from __future__ import annotations

from pathlib import Path

import pytest

from iam_agent.answerer import AnswerAgent
from iam_agent.authorization import AuthorizationService, PrincipalGrant
from iam_agent.config import Settings
from iam_agent.conversation import ConversationStore
from iam_agent.mcp_client import MCPToolError
from iam_agent.models import (
    AnalysisPlan,
    AnalystAnswer,
    AnswerMode,
    ChatActor,
    EvidenceBundle,
    IAMDomain,
    QueryOperation,
    QueryStep,
    QueryStepResult,
    QuestionDecomposition,
    UserRole,
)
from iam_agent.orchestrator import IAMOrchestrator
from iam_agent.planner import QuestionPlanner
from iam_agent.policy_rag import PolicyStore


class FakeModel:
    def __init__(self, *, data_question: bool):
        self.data_question = data_question
        self.response_models: list[type] = []

    async def generate_structured(self, *, system_instruction, prompt, response_model):
        self.response_models.append(response_model)
        if response_model is QuestionDecomposition:
            if self.data_question:
                return QuestionDecomposition(
                    answer_mode=AnswerMode.DATA,
                    clarified_question="Rank identities by risk score",
                    domains=[IAMDomain.IDENTITY_RISK],
                    entities=[],
                    requires_project_data=True,
                )
            return QuestionDecomposition(
                answer_mode=AnswerMode.GENERAL,
                clarified_question="Explain least privilege",
                requires_project_data=False,
                requires_policy_context=True,
            )
        if response_model is AnalysisPlan:
            return AnalysisPlan(
                objective="Rank identities by risk score",
                steps=[
                    QueryStep(
                        step_id="rank_risk",
                        purpose="Return highest-risk identities",
                        concept="risk_scores",
                        operation=QueryOperation.RANK,
                        metrics=["maximum_risk_score"],
                        dimensions=["user"],
                        limit=10,
                    )
                ],
            )
        if response_model is AnalystAnswer:
            return AnalystAnswer(
                answer="user12 has the highest observed risk score.",
                key_findings=["user12: 91"],
                sources=["risk_scores", "not_queried"],
            )
        raise AssertionError(response_model)


class FakeMCPClient:
    def __init__(self, *, reject_first_plan: bool = False):
        self.described: list[list[IAMDomain]] = []
        self.plans: list[AnalysisPlan] = []
        self.reject_first_plan = reject_first_plan

    async def domain_summaries(self) -> dict[str, dict]:
        return {
            "identity_risk": {
                "description": "Risk scores and cross-signal findings",
                "synonyms": ["risky users"],
                "concepts": [{"name": "risk_scores", "description": "Per-user risk"}],
            }
        }

    async def describe_domains(self, domains: list[IAMDomain]) -> dict:
        self.described.append(domains)
        return {
            "domains": {"identity_risk": "Risk"},
            "concepts": [{"concept": "risk_scores", "metrics": ["maximum_risk_score"]}],
        }

    async def run_query(self, principal: str, plan: AnalysisPlan) -> EvidenceBundle:
        assert principal == "analyst@example.com"
        self.plans.append(plan)
        if self.reject_first_plan and len(self.plans) == 1:
            raise MCPToolError("Unknown metric 'risk' for concept 'risk_scores'")
        return EvidenceBundle(
            objective=plan.objective,
            queried_tables=["risk_scores"],
            results=[
                QueryStepResult(
                    step_id="rank_risk",
                    purpose="Return highest-risk identities",
                    concept="risk_scores",
                    source_table="risk_scores",
                    rows=[{"user": "user12", "maximum_risk_score": 91}],
                    row_count=1,
                )
            ],
        )


def build_orchestrator(tmp_path: Path, model: FakeModel, mcp: FakeMCPClient) -> IAMOrchestrator:
    (tmp_path / "policy.md").write_text(
        "# IAM Policy\n\n## Least privilege\nGrant only required access.",
        encoding="utf-8",
    )
    settings = Settings(_env_file=None)
    return IAMOrchestrator(
        settings=settings,
        authorization=AuthorizationService(
            [
                PrincipalGrant(
                    principal="analyst@example.com",
                    role=UserRole.ANALYST,
                    scope="/company",
                )
            ]
        ),
        policy_store=PolicyStore(tmp_path),
        mcp_client=mcp,
        planner=QuestionPlanner(model),
        answerer=AnswerAgent(model),
        conversations=ConversationStore(max_sessions=5, max_turns=3),
    )


@pytest.mark.asyncio
async def test_data_question_uses_mcp_plan_evidence_and_grounded_answer(tmp_path) -> None:
    model = FakeModel(data_question=True)
    mcp = FakeMCPClient()
    orchestrator = build_orchestrator(tmp_path, model, mcp)

    result = await orchestrator.answer(
        actor=ChatActor(principal="analyst@example.com"),
        question="Who is riskiest?",
        conversation_key="demo",
    )

    assert result.plan is not None
    assert result.evidence is not None
    assert result.answer.sources == ["risk_scores"]
    assert mcp.described == [[IAMDomain.IDENTITY_RISK]]
    assert len(mcp.plans) == 1
    assert model.response_models == [QuestionDecomposition, AnalysisPlan, AnalystAnswer]


@pytest.mark.asyncio
async def test_general_question_does_not_query_project_data(tmp_path) -> None:
    model = FakeModel(data_question=False)
    mcp = FakeMCPClient()
    orchestrator = build_orchestrator(tmp_path, model, mcp)

    result = await orchestrator.answer(
        actor=ChatActor(principal="analyst@example.com"),
        question="What is least privilege?",
        conversation_key="demo",
    )

    assert result.plan is None
    assert result.evidence is None
    assert result.answer.sources == []
    assert mcp.described == []
    assert mcp.plans == []
    assert model.response_models == [QuestionDecomposition, AnalystAnswer]


@pytest.mark.asyncio
async def test_rejected_plan_is_repaired_once_before_answering(tmp_path) -> None:
    model = FakeModel(data_question=True)
    mcp = FakeMCPClient(reject_first_plan=True)
    orchestrator = build_orchestrator(tmp_path, model, mcp)

    result = await orchestrator.answer(
        actor=ChatActor(principal="analyst@example.com"),
        question="Who is riskiest?",
        conversation_key="repair-demo",
    )

    assert result.evidence is not None
    assert len(mcp.plans) == 2
    assert model.response_models == [
        QuestionDecomposition,
        AnalysisPlan,
        AnalysisPlan,
        AnalystAnswer,
    ]


class EmptyDomainModel(FakeModel):
    async def generate_structured(self, *, system_instruction, prompt, response_model):
        if response_model is QuestionDecomposition:
            return QuestionDecomposition(
                answer_mode=AnswerMode.DATA,
                clarified_question="Tell me about user16",
                domains=[],
                entities=["user16"],
                requires_project_data=True,
            )
        return await super().generate_structured(
            system_instruction=system_instruction,
            prompt=prompt,
            response_model=response_model,
        )


@pytest.mark.asyncio
async def test_data_question_with_no_model_domains_falls_back_to_all_catalog_domains() -> None:
    planner = QuestionPlanner(EmptyDomainModel(data_question=True))
    cards = {domain.value: {"description": domain.value, "concepts": []} for domain in IAMDomain}

    decomposition = await planner.decompose(
        question="Tell me about user16",
        history=[],
        domain_summaries=cards,
    )

    assert decomposition.requires_project_data is True
    assert decomposition.domains == list(IAMDomain)
