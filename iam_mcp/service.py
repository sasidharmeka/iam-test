"""Application service behind the MCP tools."""

from __future__ import annotations

import asyncio

from iam_agent.models import AnalysisPlan, EvidenceBundle, IAMDomain
from iam_mcp.bigquery_executor import QueryExecutor
from iam_mcp.catalog import SemanticCatalog
from iam_mcp.compiler import QueryCompiler
from iam_mcp.validator import QueryPlanValidator


class IAMEvidenceService:
    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        validator: QueryPlanValidator,
        compiler: QueryCompiler,
        executor: QueryExecutor,
    ):
        self.catalog = catalog
        self.validator = validator
        self.compiler = compiler
        self.executor = executor

    def list_domains(self) -> dict:
        return {
            "catalog_version": self.catalog.version,
            "domains": self.catalog.domain_summaries(),
        }

    def describe_domains(self, domains: list[str]) -> dict:
        selected = [IAMDomain(domain) for domain in domains]
        return self.catalog.planner_context(selected)

    async def run_query(self, *, principal: str, plan: AnalysisPlan) -> EvidenceBundle:
        authorized_users = self.validator.validate(plan, principal)
        compiled = [self.compiler.compile(step, authorized_users) for step in plan.steps]
        results = await asyncio.gather(
            *[
                self.executor.execute(query, step)
                for query, step in zip(compiled, plan.steps, strict=True)
            ]
        )
        return EvidenceBundle(
            objective=plan.objective,
            results=list(results),
            queried_tables=sorted({query.table for query in compiled}),
        )
