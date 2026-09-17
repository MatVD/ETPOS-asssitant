from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from .base import SourceContext
from .prompting import build_prompt
from ..config import settings


def _codex_command() -> list[str]:
    command = [
        settings.codex_binary,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
    ]
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


def parse_agent_message(raw_line: str) -> str | None:
    """Return only completed assistant messages from Codex JSONL output."""
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    if event.get("type") != "item.completed":
        return None
    item = event.get("item") or {}
    if item.get("type") != "agent_message":
        return None
    text = item.get("text")
    return text if isinstance(text, str) and text.strip() else None


class CodexCliProvider:
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

        prompt = build_prompt(question, sources, history)
        command = _codex_command()
        final_messages: list[str] = []

        with tempfile.TemporaryDirectory(prefix="etpos-codex-") as workdir:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workdir,
                env=codex_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
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
                        text = parse_agent_message(raw.decode("utf-8", errors="replace"))
                        if text:
                            final_messages.append(text)
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

        if not final_messages:
            raise RuntimeError("Codex CLI n'a retourné aucun message assistant exploitable.")

        # `codex exec --json` exposes completed agent messages, not token deltas.
        # Keep the provider interface asynchronous and emit only verified assistant text.
        yield final_messages[-1]
