"""Application service behind the MCP tools."""

from __future__ import annotations

import asyncio

from iam_agent.errors import DependencyError
from iam_agent.models import (
    AnalysisPlan, CatalogRetrieval, ConversationTurn, EvidenceBundle, IAMDomain,
)
from iam_mcp.bigquery_executor import QueryExecutor
from iam_mcp.catalog import SemanticCatalog
from iam_mcp.compiler import QueryCompiler
from iam_mcp.validator import QueryPlanValidator
from iam_mcp.retrieval import CatalogRetriever, EvidenceCoverageValidator


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
        self.retriever = CatalogRetriever(catalog)
        self.coverage = EvidenceCoverageValidator(catalog)

    def search_catalog(
        self, *, principal: str, question: str, history: list[ConversationTurn]
    ) -> CatalogRetrieval:
        self.validator.authorization.require_query(principal)
        return self.retriever.retrieve(question, history)

    def list_domains(self) -> dict:
        return {
            "catalog_version": self.catalog.version,
            "domains": self.catalog.domain_summaries(),
        }

    def describe_domains(self, domains: list[str]) -> dict:
        selected = [IAMDomain(domain) for domain in domains]
        return self.catalog.planner_context(selected)

    async def run_query(
        self, *, principal: str, plan: AnalysisPlan,
        question: str, history: list[ConversationTurn],
    ) -> EvidenceBundle:
        authorized_users = self.validator.validate(plan, principal)
        # Recompute on the server from the original question. Never trust an LLM-supplied
        # domain, topic requirement, or claim that it already passed coverage validation.
        self.coverage.validate(plan, self.retriever.retrieve(question, history))
        compiled = [self.compiler.compile(step, authorized_users) for step in plan.steps]
        results = await asyncio.gather(
            *[
                self.executor.execute(query, step)
                for query, step in zip(compiled, plan.steps, strict=True)
            ],
            return_exceptions=True,
        )
        successful = []
        warnings = []
        for step, result in zip(plan.steps, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, DependencyError):
                    raise result
                warnings.append(f"Could not read {step.concept}: {result}")
            else:
                successful.append(result)
        if not successful:
            raise next(result for result in results if isinstance(result, DependencyError))
        return EvidenceBundle(
            objective=plan.objective,
            results=successful,
            queried_tables=sorted({result.source_table for result in successful}),
            warnings=warnings,
        )
