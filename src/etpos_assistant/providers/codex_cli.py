from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path

from .base import SourceContext
from .prompting import build_prompt
from ..config import settings


@dataclass(frozen=True)
class CodexRunMetrics:
    prompt_chars: int
    prompt_build_ms: float
    process_spawn_ms: float
    first_event_ms: float | None
    first_agent_message_ms: float | None
    total_ms: float
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    def as_dict(self) -> dict[str, int | float | None]:
        return asdict(self)


def _codex_command() -> list[str]:
    command = [
        settings.codex_binary,
        "--ask-for-approval",
        "never",
    ]
    if settings.codex_reasoning_effort:
        command.extend(
            [
                "--config",
                f"model_reasoning_effort={json.dumps(settings.codex_reasoning_effort)}",
            ]
        )
    if settings.codex_model_verbosity:
        command.extend(
            [
                "--config",
                f"model_verbosity={json.dumps(settings.codex_model_verbosity)}",
            ]
        )
    command.extend(
        [
            "exec",
            "--ephemeral",
            "--json",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
        ]
    )
    if settings.codex_model:
        command.extend(["--model", settings.codex_model])
    command.append("-")
    return command


def codex_environment() -> dict[str, str]:
    """Expose only what the Codex process needs, not the application environment."""
    allowed = {
        "PATH",
        "HOME",
        "CODEX_HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SSL_CERT_FILE",
        "CODEX_CA_CERTIFICATE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed and value}
    env["RUST_LOG"] = "error"
    env["NO_COLOR"] = "1"
    if settings.codex_home:
        env["CODEX_HOME"] = str(settings.codex_home)
    return env


def codex_auth_directory(env: dict[str, str] | None = None) -> Path | None:
    effective_env = codex_environment() if env is None else env
    explicit = effective_env.get("CODEX_HOME")
    if explicit:
        return Path(explicit).expanduser()
    home = effective_env.get("HOME")
    if home:
        return Path(home).expanduser() / ".codex"
    return None


def _parse_event(raw_line: str) -> dict | None:
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def parse_agent_message(raw_line: str) -> str | None:
    """Return only completed assistant messages from Codex JSONL output."""
    event = _parse_event(raw_line)
    if not event or event.get("type") != "item.completed":
        return None
    item = event.get("item") or {}
    if item.get("type") != "agent_message":
        return None
    text = item.get("text")
    return text if isinstance(text, str) and text.strip() else None


def parse_turn_usage(raw_line: str) -> dict[str, int] | None:
    """Return documented token usage from a completed Codex turn."""
    event = _parse_event(raw_line)
    if not event or event.get("type") != "turn.completed":
        return None
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    result: dict[str, int] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            result[key] = value
    return result or None


class CodexCliProvider:
    def __init__(self) -> None:
        self.last_metrics: CodexRunMetrics | None = None

    async def stream_answer(
        self,
        *,
        question: str,
        sources: list[SourceContext],
        history: list[dict[str, str]],
    ) -> AsyncIterator[str]:
        if shutil.which(settings.codex_binary) is None and not Path(settings.codex_binary).is_file():
            raise RuntimeError(
                f"Codex CLI introuvable ({settings.codex_binary}). Installe Codex puis exécute `codex login`."
            )

        run_started = time.perf_counter()
        prompt_started = time.perf_counter()
        prompt = build_prompt(question, sources, history)
        prompt_build_ms = (time.perf_counter() - prompt_started) * 1000.0
        command = _codex_command()
        final_messages: list[str] = []
        usage: dict[str, int] = {}
        process_spawn_ms = 0.0
        first_event_ms: float | None = None
        first_agent_message_ms: float | None = None

        with tempfile.TemporaryDirectory(prefix="etpos-codex-") as workdir:
            spawn_started = time.perf_counter()
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workdir,
                env=codex_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            process_spawn_ms = (time.perf_counter() - spawn_started) * 1000.0
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None

            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            stderr_task = asyncio.create_task(process.stderr.read())
            try:
                async with asyncio.timeout(settings.codex_timeout_seconds):
                    async for raw in process.stdout:
                        now_ms = (time.perf_counter() - run_started) * 1000.0
                        if first_event_ms is None:
                            first_event_ms = now_ms
                        raw_text = raw.decode("utf-8", errors="replace")
                        text = parse_agent_message(raw_text)
                        if text:
                            if first_agent_message_ms is None:
                                first_agent_message_ms = now_ms
                            final_messages.append(text)
                        turn_usage = parse_turn_usage(raw_text)
                        if turn_usage:
                            usage.update(turn_usage)
                    return_code = await process.wait()
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                stderr_task.cancel()
                raise RuntimeError("Codex CLI a dépassé le délai maximal autorisé.") from exc

            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            if return_code != 0:
                detail = stderr[-1000:] if stderr else f"code de sortie {return_code}"
                raise RuntimeError(f"Échec Codex CLI : {detail}")

        total_ms = (time.perf_counter() - run_started) * 1000.0
        self.last_metrics = CodexRunMetrics(
            prompt_chars=len(prompt),
            prompt_build_ms=prompt_build_ms,
            process_spawn_ms=process_spawn_ms,
            first_event_ms=first_event_ms,
            first_agent_message_ms=first_agent_message_ms,
            total_ms=total_ms,
            input_tokens=usage.get("input_tokens"),
            cached_input_tokens=usage.get("cached_input_tokens"),
            output_tokens=usage.get("output_tokens"),
            reasoning_output_tokens=usage.get("reasoning_output_tokens"),
        )

        if not final_messages:
            raise RuntimeError("Codex CLI n'a retourné aucun message assistant exploitable.")

        # `codex exec --json` exposes completed agent messages, not token deltas.
        # Keep the provider interface asynchronous and emit only verified assistant text.
        yield final_messages[-1]
