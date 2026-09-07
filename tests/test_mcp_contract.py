from __future__ import annotations

import pytest

from iam_agent.authorization import AuthorizationService, IdentityDirectory, PrincipalGrant
from iam_agent.config import REPOSITORY_ROOT, Settings
from iam_agent.mcp_client import IAMEvidenceMCPClient, MCPToolError
from iam_agent.models import (
    AnalysisPlan,
    QueryOperation,
    QueryStep,
    QueryStepResult,
    UserRole,
)
from iam_mcp.catalog import SemanticCatalog
from iam_mcp.compiler import QueryCompiler
from iam_mcp.server import create_mcp_server
from iam_mcp.service import IAMEvidenceService
from iam_mcp.validator import QueryPlanValidator


class FakeExecutor:
    async def execute(self, compiled, step) -> QueryStepResult:
        return QueryStepResult(
            step_id=step.step_id,
            purpose=step.purpose,
            concept=step.concept,
            source_table=compiled.table,
            rows=[{"user": "user12", "maximum_risk_score": 91}],
            row_count=1,
            bytes_processed=1024,
        )


def build_client() -> IAMEvidenceMCPClient:
    settings = Settings(_env_file=None)
    catalog = SemanticCatalog.from_yaml(REPOSITORY_ROOT / "config" / "semantic_catalog.yaml")
    identities = IdentityDirectory.from_yaml(REPOSITORY_ROOT / "config" / "identities.yaml")
    authorization = AuthorizationService(
        [
            PrincipalGrant(
                principal="analyst@example.com",
                role=UserRole.ANALYST,
                scope="/company",
            )
        ]
    )
    service = IAMEvidenceService(
        catalog=catalog,
        validator=QueryPlanValidator(
            settings=settings,
            catalog=catalog,
            authorization=authorization,
            identities=identities,
        ),
        compiler=QueryCompiler(settings=settings, catalog=catalog, identities=identities),
        executor=FakeExecutor(),
    )
    return IAMEvidenceMCPClient(create_mcp_server(service))


@pytest.mark.asyncio
async def test_official_mcp_client_discovers_and_calls_read_only_tools() -> None:
    client = build_client()
    await client.initialize()

    assert client.tool_names == (
        "list_iam_domains",
        "describe_iam_domains",
        "search_iam_catalog",
        "run_iam_query",
    )
    domains = await client.domain_summaries()
    assert "identity_risk" in domains
    assert domains["identity_risk"]["concepts"]
    assert any(concept["name"] == "risk_scores" for concept in domains["identity_risk"]["concepts"])

    plan = AnalysisPlan(
        objective="Rank users",
        steps=[
            QueryStep(
                step_id="rank_users",
                purpose="Rank users by risk",
                concept="risk_scores",
                operation=QueryOperation.RANK,
                metrics=["maximum_risk_score"],
                dimensions=["user"],
                limit=10,
            )
        ],
    )
    evidence = await client.run_query("analyst@example.com", plan, question="Rank users")

    assert evidence.queried_tables == ["risk_scores"]
    assert evidence.results[0].rows[0]["user"] == "user12"


@pytest.mark.asyncio
async def test_mcp_boundary_rejects_a_concept_absent_from_the_catalog() -> None:
    client = build_client()
    plan = AnalysisPlan(
        objective="Attempt an ungoverned query",
        steps=[
            QueryStep(
                step_id="ungoverned",
                purpose="This concept is not in the semantic catalog",
                concept="employee_salary",
                operation=QueryOperation.RECORDS,
            )
        ],
    )

    with pytest.raises(MCPToolError, match="Unknown IAM concept"):
        await client.run_query("analyst@example.com", plan, question="Show employee salary")
