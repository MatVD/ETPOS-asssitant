from __future__ import annotations

import asyncio
from typing import AsyncIterator

from .base import SourceContext


class MockProvider:
    def __init__(self) -> None:
        self.last_answer_status: str | None = None

    async def stream_answer(
        self,
        *,
        question: str,
        sources: list[SourceContext],
        history: list[dict[str, str]],
    ) -> AsyncIterator[str]:
        if not sources:
            self.last_answer_status = "none"
            text = "La documentation ETPOS indexée ne permet pas de répondre avec certitude à cette question."
        else:
            self.last_answer_status = "full"
            primary = sources[0]
            excerpt = " ".join(primary.text.split())[:700]
            text = (
                "Mode diagnostic sans LLM externe. La recherche locale a identifié en priorité "
                f"« {primary.heading_path or primary.title} ». [S1]\n\n"
                f"Extrait retrouvé : {excerpt}"
            )
        for chunk in [text[i : i + 42] for i in range(0, len(text), 42)]:
            await asyncio.sleep(0)
            yield chunk
