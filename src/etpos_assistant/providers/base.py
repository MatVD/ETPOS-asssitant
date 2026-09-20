from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass(frozen=True)
class SourceContext:
    source_id: str
    title: str
    heading_path: str
    text: str
    source_type: str = "unknown"
    document_version: str | None = None
    revision_date: str | None = None


class LLMProvider(Protocol):
    async def stream_answer(
        self,
        *,
        question: str,
        sources: list[SourceContext],
        history: list[dict[str, str]],
    ) -> AsyncIterator[str]: ...
