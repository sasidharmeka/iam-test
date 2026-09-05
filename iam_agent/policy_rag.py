"""Small, deterministic RAG retriever for version-controlled IAM policies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9]{3,}")


@dataclass(frozen=True)
class PolicyChunk:
    chunk_id: str
    source: str
    text: str


class PolicyStore:
    """Loads Markdown policies and retrieves chunks by transparent lexical overlap."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._chunks = self._load_chunks()

    def _load_chunks(self) -> list[PolicyChunk]:
        chunks: list[PolicyChunk] = []
        for path in sorted(self.directory.glob("*.md")):
            sections = re.split(r"(?=^##?\s)", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
            for index, section in enumerate(filter(str.strip, sections), start=1):
                chunks.append(
                    PolicyChunk(
                        chunk_id=f"{path.stem}:{index}",
                        source=path.name,
                        text=section.strip(),
                    )
                )
        return chunks

    def retrieve(self, query: str, limit: int = 5) -> list[PolicyChunk]:
        query_tokens = {token.casefold() for token in TOKEN_PATTERN.findall(query)}

        def score(chunk: PolicyChunk) -> tuple[int, str]:
            chunk_tokens = {token.casefold() for token in TOKEN_PATTERN.findall(chunk.text)}
            return (len(query_tokens & chunk_tokens), chunk.chunk_id)

        ranked = sorted(self._chunks, key=score, reverse=True)
        return [chunk for chunk in ranked if score(chunk)[0] > 0][:limit]
