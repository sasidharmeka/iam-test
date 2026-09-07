"""Bounded, owner-and-conversation-scoped snapshots of completed analyses."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4

from iam_agent.models import ChatWorkflowResult


@dataclass
class StoredResult:
    result_id: str
    owner: str
    conversation_key: str
    result: ChatWorkflowResult
    expires_at: float
    size: int


class ResultStore:
    def __init__(self, *, ttl_seconds: int = 1800, max_results: int = 100):
        self.ttl_seconds = ttl_seconds
        self.max_results = max_results
        self._items: OrderedDict[str, StoredResult] = OrderedDict()
        self._lock = asyncio.Lock()

    def _prune(self) -> None:
        for key in list(self._items):
            if self._items[key].expires_at <= time.monotonic():
                del self._items[key]
        while (
            len(self._items) > self.max_results
            or sum(i.size for i in self._items.values()) > 32_000_000
        ):
            self._items.popitem(last=False)

    async def save(self, owner: str, conversation_key: str, result: ChatWorkflowResult) -> str:
        async with self._lock:
            existing = self._items.get(result.result_id)
            if existing and (
                existing.owner != owner.casefold() or existing.conversation_key != conversation_key
            ):
                raise ValueError("Result ownership does not match this conversation")
            result_id = existing.result_id if existing else uuid4().hex
            result.result_id = result_id
            snapshot = result.model_copy(deep=True)
            snapshot.email_draft = None
            size = len(snapshot.model_dump_json().encode())
            if size > 32_000_000:
                raise ValueError("This result is too large to retain; narrow the analysis")
            self._items[result_id] = StoredResult(
                result_id, owner.casefold(), conversation_key, snapshot,
                time.monotonic() + self.ttl_seconds, size,
            )
            self._items.move_to_end(result_id)
            self._prune()
            return result_id

    async def list(self, owner: str, conversation_key: str) -> list[ChatWorkflowResult]:
        async with self._lock:
            self._prune()
            return [
                item.result.model_copy(deep=True) for item in self._items.values()
                if item.owner == owner.casefold() and item.conversation_key == conversation_key
            ][-8:]

    async def get(
        self, result_id: str, owner: str, conversation_key: str,
    ) -> ChatWorkflowResult | None:
        async with self._lock:
            self._prune()
            item = self._items.get(result_id)
            if (
                not item or item.owner != owner.casefold()
                or item.conversation_key != conversation_key
            ):
                return None
            return item.result.model_copy(deep=True)
