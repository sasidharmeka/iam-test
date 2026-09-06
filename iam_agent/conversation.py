"""Small bounded in-memory context store for natural follow-up questions."""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from iam_agent.models import ConversationTurn


class ConversationStore:
    def __init__(self, *, max_sessions: int, max_turns: int):
        self.max_sessions = max_sessions
        self.max_turns = max_turns
        self._sessions: OrderedDict[str, list[ConversationTurn]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> list[ConversationTurn]:
        async with self._lock:
            turns = self._sessions.get(key, [])
            if key in self._sessions:
                self._sessions.move_to_end(key)
            return list(turns)

    async def append(self, key: str, turn: ConversationTurn) -> None:
        async with self._lock:
            turns = self._sessions.setdefault(key, [])
            turns.append(turn)
            self._sessions[key] = turns[-self.max_turns :]
            self._sessions.move_to_end(key)
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
