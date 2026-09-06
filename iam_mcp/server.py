"""Official MCP server exposing read-only IAM discovery and query tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from iam_agent.models import AnalysisPlan, EvidenceBundle
from iam_mcp.service import IAMEvidenceService


class DomainListResponse(BaseModel):
    catalog_version: int
    domains: dict[str, str]


class DomainDescriptionResponse(BaseModel):
    catalog_version: int
    domains: dict[str, str]
    concepts: list[dict]
    rules: list[str]


def create_mcp_server(service: IAMEvidenceService) -> MCPServer:
    server = MCPServer(
        "iam-evidence",
        description="Read-only semantic discovery and bounded BigQuery access for IAM analytics",
        instructions=(
            "All tools are read-only. Discover domains and concepts before submitting a "
            "semantic AnalysisPlan. Never submit SQL or physical table identifiers."
        ),
        version="1.0.0",
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
        name="run_iam_query",
        description=(
            "Validate and execute a bounded semantic AnalysisPlan against allowlisted IAM "
            "BigQuery outputs. The server, not the caller, resolves tables and compiles SQL."
        ),
        structured_output=True,
    )
    async def run_iam_query(principal: str, plan: AnalysisPlan) -> EvidenceBundle:
        return await service.run_query(principal=principal, plan=plan)

    return server
