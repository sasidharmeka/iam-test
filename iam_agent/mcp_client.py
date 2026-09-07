"""Orchestrator-facing client for the in-process official MCP server."""

from __future__ import annotations

import json
from typing import Any

from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from iam_agent.errors import DependencyError
from iam_agent.models import (
    AnalysisPlan,
    CatalogRetrieval,
    ConversationTurn,
    EvidenceBundle,
    IAMDomain,
)


class MCPToolError(DependencyError):
    def __init__(self, message: str, *, code: str = "plan_rejected"):
        super().__init__(code, message)


class IAMEvidenceMCPClient:
    """Uses MCP discovery/calls without adding a second Cloud Run service."""

    def __init__(self, server: MCPServer):
        self._server = server
        self._tool_names: tuple[str, ...] = ()
        self._domain_cache: dict[str, dict[str, Any]] = {}

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self._tool_names

    async def initialize(self) -> None:
        async with Client(self._server, raise_exceptions=True) as client:
            listing = await client.list_tools()
            self._tool_names = tuple(tool.name for tool in listing.tools)
            result = await client.call_tool("list_iam_domains")
        payload = self._payload(result)
        self._domain_cache = {
            str(name): dict(card) for name, card in payload.get("domains", {}).items()
        }
        required = {
            "list_iam_domains",
            "describe_iam_domains",
            "search_iam_catalog",
            "run_iam_query",
        }
        if not required.issubset(self._tool_names):
            missing = ", ".join(sorted(required - set(self._tool_names)))
            raise MCPToolError(f"IAM MCP server is missing required tools: {missing}")

    async def domain_summaries(self) -> dict[str, dict[str, Any]]:
        if not self._domain_cache:
            await self.initialize()
        return dict(self._domain_cache)

    async def describe_domains(self, domains: list[IAMDomain]) -> dict[str, Any]:
        result = await self._call(
            "describe_iam_domains",
            {"domains": [domain.value for domain in domains]},
        )
        return self._payload(result)

    async def search_catalog(
        self, principal: str, question: str, history: list[ConversationTurn]
    ) -> CatalogRetrieval:
        result = await self._call(
            "search_iam_catalog",
            {
                "principal": principal,
                "question": question,
                "history": [turn.model_dump(mode="json") for turn in history],
            },
        )
        return CatalogRetrieval.model_validate(self._payload(result))

    async def run_query(
        self,
        principal: str,
        plan: AnalysisPlan,
        *,
        question: str,
        history: list[ConversationTurn] | None = None,
    ) -> EvidenceBundle:
        result = await self._call(
            "run_iam_query",
            {
                "principal": principal,
                "plan": plan.model_dump(mode="json"),
                "question": question,
                "history": [turn.model_dump(mode="json") for turn in (history or [])],
            },
        )
        payload = self._payload(result)
        if payload.get("ok") is False:
            raise MCPToolError(
                str(payload.get("error") or "The query plan was rejected"),
                code=payload.get("error_code") or "plan_rejected",
            )
        evidence = payload.get("evidence", payload)
        return EvidenceBundle.model_validate(evidence)

    async def _call(self, name: str, arguments: dict[str, Any]):
        try:
            async with Client(self._server, raise_exceptions=True) as client:
                result = await client.call_tool(name, arguments)
        except Exception as exc:
            raise MCPToolError(
                "The IAM evidence service could not complete the tool call.", code="mcp_transport"
            ) from exc
        if getattr(result, "is_error", False):
            messages = [getattr(item, "text", "") for item in result.content]
            raise MCPToolError(" ".join(message for message in messages if message))
        return result

    @staticmethod
    def _payload(result) -> dict[str, Any]:
        payload = getattr(result, "structured_content", None)
        if isinstance(payload, dict):
            return payload
        for item in getattr(result, "content", []):
            text = getattr(item, "text", None)
            if text:
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    return decoded
        raise MCPToolError("IAM MCP tool returned no structured JSON object")
