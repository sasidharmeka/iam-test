"""Official MCP server exposing read-only IAM discovery and query tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from iam_agent.authorization import AuthorizationError
from iam_agent.errors import DependencyError
from iam_agent.models import AnalysisPlan, CatalogRetrieval, ConversationTurn, EvidenceBundle
from iam_mcp.catalog import CatalogError
from iam_mcp.service import IAMEvidenceService
from iam_mcp.validator import QueryPlanError


class DomainListResponse(BaseModel):
    catalog_version: int
    domains: dict[str, dict]


class DomainDescriptionResponse(BaseModel):
    catalog_version: int
    domains: dict[str, str]
    concepts: list[dict]
    rules: list[str]


class QueryToolResponse(BaseModel):
    ok: bool
    evidence: EvidenceBundle | None = None
    error: str | None = None
    error_code: str | None = None


def create_mcp_server(service: IAMEvidenceService) -> MCPServer:
    server = MCPServer(
        "iam-evidence",
        description="Read-only semantic discovery and bounded BigQuery access for IAM analytics",
        instructions=(
            "All tools are read-only. Discover domains and concepts before submitting a "
            "semantic AnalysisPlan. Never submit SQL or physical table identifiers."
        ),
        version="2.0.0",
    )

    @server.tool(
        name="list_iam_domains",
        description="List the compact IAM business domains available for question planning.",
        structured_output=True,
    )
    def list_iam_domains() -> DomainListResponse:
        return DomainListResponse.model_validate(service.list_domains())

    @server.tool(
        name="describe_iam_domains",
        description=(
            "Return the approved concepts, fields, dimensions, metrics, and grains for selected "
            "IAM domains. This reads metadata only and does not query BigQuery."
        ),
        structured_output=True,
    )
    def describe_iam_domains(domains: list[str]) -> DomainDescriptionResponse:
        return DomainDescriptionResponse.model_validate(service.describe_domains(domains))

    @server.tool(
        name="search_iam_catalog",
        description=(
            "Search all IAM catalog concepts before selecting domains. Return multiple candidates, "
            "their semantic schemas, and deterministic evidence-coverage requirements. Metadata "
            "only; no BigQuery rows are scanned."
        ),
        structured_output=True,
    )
    def search_iam_catalog(
        principal: str, question: str, history: list[ConversationTurn]
    ) -> CatalogRetrieval:
        return service.search_catalog(principal=principal, question=question, history=history)

    @server.tool(
        name="run_iam_query",
        description=(
            "Validate and execute a bounded semantic AnalysisPlan against allowlisted IAM "
            "BigQuery outputs. The server, not the caller, resolves tables and compiles SQL."
        ),
        structured_output=True,
    )
    async def run_iam_query(
        principal: str, plan: AnalysisPlan, question: str, history: list[ConversationTurn]
    ) -> QueryToolResponse:
        try:
            evidence = await service.run_query(
                principal=principal, plan=plan, question=question, history=history
            )
        except DependencyError as exc:
            return QueryToolResponse(ok=False, error=str(exc), error_code=exc.code)
        except (AuthorizationError, CatalogError, QueryPlanError, ValueError) as exc:
            # Expected plan rejections are data, not an MCP transport failure. This gives the
            # orchestrator a precise reason it can use for one bounded repair attempt.
            return QueryToolResponse(ok=False, error=str(exc), error_code="plan_rejected")
        return QueryToolResponse(ok=True, evidence=evidence)

    return server
