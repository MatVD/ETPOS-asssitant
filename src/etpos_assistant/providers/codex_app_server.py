from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .base import SourceContext
from .codex_cli import (
    TEXT_ONLY_DISABLED_FEATURES,
    CodexRunMetrics,
    codex_auth_directory,
    codex_environment,
)
from .prompting import AnswerStatusGate, build_prompt
from ..config import settings


_TEXT_ONLY_BASE_INSTRUCTIONS = (
    "You are a text-only answer engine embedded in ETPOS Assistant. "
    "Do not use tools, shell commands, file operations, web search, MCP, plugins, "
    "skills, browser features, or external sources. Answer only from the input text."
)

_ALLOWED_PASSIVE_ITEM_TYPES = {
    "userMessage",
    "agentMessage",
    "reasoning",
    "plan",
    "contextCompaction",
}

_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}

def _app_server_command() -> list[str]:
    command = [
        settings.codex_binary,
        "app-server",
        "--stdio",
        "--config",
        'web_search="disabled"',
        "--config",
        "mcp_servers={}",
    ]
    for feature in TEXT_ONLY_DISABLED_FEATURES:
        command.extend(["--disable", feature])
    if settings.codex_model_verbosity:
        command.extend(
            [
                "--config",
                f"model_verbosity={json.dumps(settings.codex_model_verbosity)}",
            ]
        )
    return command


def _thread_start_params(workdir: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "ephemeral": True,
        "cwd": workdir,
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "baseInstructions": _TEXT_ONLY_BASE_INSTRUCTIONS,
        "developerInstructions": _TEXT_ONLY_BASE_INSTRUCTIONS,
        "personality": "none",
        "dynamicTools": [],
        "environments": [],
        "runtimeWorkspaceRoots": [],
        "selectedCapabilityRoots": [],
        "config": {"web_search": "disabled", "mcp_servers": {}},
    }
    if settings.codex_model:
        params["model"] = settings.codex_model
    return params


def _validate_dedicated_codex_home(home: Path) -> None:
    if not home.is_dir():
        raise RuntimeError(f"CODEX_HOME App Server introuvable : {home}")
    if not (home / "auth.json").is_file():
        raise RuntimeError(
            f"Authentification Codex App Server introuvable dans {home}. "
            "Authentifie directement ce répertoire avec CODEX_HOME=<chemin> codex login."
        )


def _prepare_isolated_codex_home(source_home: Path, isolated_home: Path) -> None:
    auth_source = source_home / "auth.json"
    if not auth_source.is_file():
        raise RuntimeError(
            f"Authentification Codex introuvable dans {source_home}. "
            "Exécute d'abord `codex login` avec le CODEX_HOME du service."
        )
    isolated_home.mkdir(parents=True, exist_ok=True)
    auth_target = isolated_home / "auth.json"
    shutil.copy2(auth_source, auth_target)
    auth_target.chmod(0o600)


async def _send_json(process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("stdin app-server indisponible")
    process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    await process.stdin.drain()


async def _read_json(process: asyncio.subprocess.Process) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("stdout app-server indisponible")
    raw = await process.stdout.readline()
    if not raw:
        raise RuntimeError("Codex app-server a fermé son flux de sortie.")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Codex app-server a produit un événement JSON invalide.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Codex app-server a produit un événement inattendu.")
    return value


async def _reply_to_server_request(
    process: asyncio.subprocess.Process,
    message: dict[str, Any],
) -> None:
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None or not isinstance(method, str):
        return
    if method in _APPROVAL_METHODS:
        await _send_json(process, {"id": request_id, "result": {"decision": "decline"}})
        return
    await _send_json(
        process,
        {
            "id": request_id,
            "error": {
                "code": -32601,
                "message": "Server request unsupported by ETPOS Assistant.",
            },
        },
    )


async def _wait_for_response(
    process: asyncio.subprocess.Process,
    request_id: int,
) -> dict[str, Any]:
    while True:
        message = await _read_json(process)
        if message.get("id") == request_id and "method" not in message:
            if "error" in message:
                raise RuntimeError(f"Codex app-server RPC error: {message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        if "id" in message and "method" in message:
            await _reply_to_server_request(process, message)


def _token_metrics(payload: dict[str, Any]) -> dict[str, int]:
    token_usage = payload.get("tokenUsage")
    if not isinstance(token_usage, dict):
        return {}
    last = token_usage.get("last")
    if not isinstance(last, dict):
        return {}
    mapping = {
        "input_tokens": "inputTokens",
        "cached_input_tokens": "cachedInputTokens",
        "output_tokens": "outputTokens",
        "reasoning_output_tokens": "reasoningOutputTokens",
    }
    result: dict[str, int] = {}
    for target, source in mapping.items():
        value = last.get(source)
        if isinstance(value, int) and value >= 0:
            result[target] = value
    return result


def _forbidden_item_type(message: dict[str, Any]) -> str | None:
    if message.get("method") != "item/started":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if not isinstance(item_type, str) or item_type in _ALLOWED_PASSIVE_ITEM_TYPES:
        return None
    return item_type


class CodexAppServerProvider:
    """Persistent app-server transport with ephemeral, text-only turns.

    One provider instance owns one app-server process. Calls are serialized on that
    process because the low-level stdio transport is consumed sequentially here.
    FastAPI reuses a single instance per worker, which keeps the runtime warm while
    the ETPOS conversation history remains owned exclusively by app.db.
    """

    def __init__(self) -> None:
        self.last_metrics: CodexRunMetrics | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[bytes] | None = None
        self._workdir: tempfile.TemporaryDirectory[str] | None = None
        self._isolated_home: tempfile.TemporaryDirectory[str] | None = None
        self._request_id = 0
        self._turn_lock = asyncio.Lock()

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _start_process(self) -> float:
        binary = shutil.which(settings.codex_binary)
        if binary is None and not Path(settings.codex_binary).is_file():
            raise RuntimeError(
                f"Codex CLI introuvable ({settings.codex_binary}). Installe Codex puis exécute `codex login`."
            )

        self._workdir = tempfile.TemporaryDirectory(prefix="etpos-app-server-runtime-")
        env = codex_environment()

        if settings.codex_app_home is not None:
            _validate_dedicated_codex_home(settings.codex_app_home)
            env["CODEX_HOME"] = str(settings.codex_app_home)
        elif settings.is_production:
            self._workdir.cleanup()
            self._workdir = None
            raise RuntimeError(
                "ETPOS_CODEX_APP_HOME est requis en production avec le transport app-server. "
                "Authentifie ce CODEX_HOME dédié directement avec codex login ; ne copie pas auth.json."
            )
        else:
            source_home = codex_auth_directory()
            if source_home is None:
                self._workdir.cleanup()
                self._workdir = None
                raise RuntimeError(
                    "Impossible de déterminer le répertoire d'authentification Codex."
                )
            self._isolated_home = tempfile.TemporaryDirectory(
                prefix="etpos-codex-home-runtime-"
            )
            _prepare_isolated_codex_home(source_home, Path(self._isolated_home.name))
            env["CODEX_HOME"] = self._isolated_home.name

        spawn_started = time.perf_counter()
        self._process = await asyncio.create_subprocess_exec(
            *_app_server_command(),
            cwd=self._workdir.name,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        spawn_ms = (time.perf_counter() - spawn_started) * 1000.0

        if self._process.stderr is None:
            await self.close()
            raise RuntimeError("stderr app-server indisponible")
        self._stderr_task = asyncio.create_task(self._process.stderr.read())

        init_id = self._next_request_id()
        await _send_json(
            self._process,
            {
                "method": "initialize",
                "id": init_id,
                "params": {
                    "clientInfo": {
                        "name": "etpos_assistant",
                        "title": "ETPOS Assistant",
                        "version": "1",
                    },
                    "capabilities": {},
                },
            },
        )
        await _wait_for_response(self._process, init_id)
        await _send_json(self._process, {"method": "initialized", "params": {}})
        return spawn_ms

    async def _ensure_process(self) -> float:
        if self._process is not None and self._process.returncode is None:
            return 0.0
        await self.close()
        return await self._start_process()

    async def _abort_process(self) -> str:
        process = self._process
        stderr_task = self._stderr_task
        self._process = None
        self._stderr_task = None

        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()

        stderr = ""
        if stderr_task is not None:
            try:
                stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            except asyncio.CancelledError:
                pass

        if self._workdir is not None:
            self._workdir.cleanup()
            self._workdir = None
        if self._isolated_home is not None:
            self._isolated_home.cleanup()
            self._isolated_home = None
        return stderr

    async def close(self) -> None:
        await self._abort_process()

    async def stream_answer(
        self,
        *,
        question: str,
        sources: list[SourceContext],
        history: list[dict[str, str]],
    ) -> AsyncIterator[str]:
        run_started = time.perf_counter()
        prompt_started = time.perf_counter()
        prompt = build_prompt(question, sources, history)
        prompt_build_ms = (time.perf_counter() - prompt_started) * 1000.0

        async with self._turn_lock:
            process_spawn_ms = 0.0
            first_event_ms: float | None = None
            first_delta_ms: float | None = None
            final_item_ids: set[str] = set()
            unknown_item_deltas: dict[str, list[str]] = {}
            streamed_chunks: list[str] = []
            raw_final_delta_seen = False
            status_gate = AnswerStatusGate()
            completed_final_text: str | None = None
            fallback_agent_texts: list[str] = []
            usage: dict[str, int] = {}
            turn_id: str | None = None
            completed = False
            turn_workdir = tempfile.TemporaryDirectory(prefix="etpos-app-server-turn-")

            try:
                async with asyncio.timeout(settings.codex_timeout_seconds):
                    process_spawn_ms = await self._ensure_process()
                    process = self._process
                    if process is None:
                        raise RuntimeError("Codex app-server n'a pas démarré.")

                    thread_params = _thread_start_params(turn_workdir.name)

                    thread_request_id = self._next_request_id()
                    await _send_json(
                        process,
                        {
                            "method": "thread/start",
                            "id": thread_request_id,
                            "params": thread_params,
                        },
                    )
                    thread_result = await _wait_for_response(process, thread_request_id)
                    thread = thread_result.get("thread")
                    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                        raise RuntimeError("Codex app-server n'a pas retourné de thread exploitable.")
                    thread_id = thread["id"]

                    turn_params: dict[str, Any] = {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt}],
                    }
                    if settings.codex_reasoning_effort:
                        turn_params["effort"] = settings.codex_reasoning_effort

                    turn_request_id = self._next_request_id()
                    await _send_json(
                        process,
                        {
                            "method": "turn/start",
                            "id": turn_request_id,
                            "params": turn_params,
                        },
                    )

                    while not completed:
                        message = await _read_json(process)
                        now_ms = (time.perf_counter() - run_started) * 1000.0
                        if first_event_ms is None:
                            first_event_ms = now_ms

                        if (
                            message.get("id") == turn_request_id
                            and "method" not in message
                        ):
                            if "error" in message:
                                raise RuntimeError(
                                    f"Codex app-server turn/start error: {message['error']}"
                                )
                            result = message.get("result")
                            turn = result.get("turn") if isinstance(result, dict) else None
                            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                                turn_id = turn["id"]
                            continue

                        if "id" in message and "method" in message:
                            await _reply_to_server_request(process, message)
                            continue

                        forbidden = _forbidden_item_type(message)
                        if forbidden:
                            raise RuntimeError(
                                "Codex app-server a tenté d'utiliser un outil interdit "
                                f"({forbidden})."
                            )

                        method = message.get("method")
                        params = message.get("params")
                        if not isinstance(params, dict):
                            params = {}

                        message_turn_id = params.get("turnId")
                        if (
                            turn_id is not None
                            and isinstance(message_turn_id, str)
                            and message_turn_id != turn_id
                        ):
                            continue

                        if method == "item/started":
                            item = params.get("item")
                            if isinstance(item, dict) and item.get("type") == "agentMessage":
                                item_id = item.get("id")
                                phase = item.get("phase")
                                if isinstance(item_id, str):
                                    if phase == "final_answer":
                                        final_item_ids.add(item_id)
                                    elif phase is None:
                                        unknown_item_deltas.setdefault(item_id, [])
                            continue

                        if method == "item/agentMessage/delta":
                            item_id = params.get("itemId")
                            delta = params.get("delta")
                            if not isinstance(item_id, str) or not isinstance(delta, str) or not delta:
                                continue
                            if item_id in final_item_ids:
                                raw_final_delta_seen = True
                                for visible in status_gate.feed(delta):
                                    if not visible:
                                        continue
                                    if first_delta_ms is None:
                                        first_delta_ms = now_ms
                                    streamed_chunks.append(visible)
                                    yield visible
                            elif item_id in unknown_item_deltas:
                                unknown_item_deltas[item_id].append(delta)
                            continue

                        if method == "item/completed":
                            item = params.get("item")
                            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                                continue
                            item_id = item.get("id")
                            phase = item.get("phase")
                            text = item.get("text")
                            if isinstance(text, str) and text.strip():
                                if phase == "final_answer":
                                    completed_final_text = text
                                    if (
                                        isinstance(item_id, str)
                                        and item_id in unknown_item_deltas
                                        and item_id not in final_item_ids
                                    ):
                                        buffered = unknown_item_deltas[item_id]
                                        if buffered:
                                            raw_final_delta_seen = True
                                            for delta in buffered:
                                                for visible in status_gate.feed(delta):
                                                    if not visible:
                                                        continue
                                                    if first_delta_ms is None:
                                                        first_delta_ms = now_ms
                                                    streamed_chunks.append(visible)
                                                    yield visible
                                elif phase is None:
                                    fallback_agent_texts.append(text)
                            continue

                        if method == "thread/tokenUsage/updated":
                            usage.update(_token_metrics(params))
                            continue

                        if method == "turn/completed":
                            turn = params.get("turn")
                            if not isinstance(turn, dict):
                                raise RuntimeError(
                                    "Codex app-server a retourné un turn/completed invalide."
                                )
                            if turn_id is not None and turn.get("id") != turn_id:
                                continue
                            status = turn.get("status")
                            if status != "completed":
                                raise RuntimeError(
                                    f"Codex app-server a terminé avec le statut {status!r}."
                                )
                            completed = True
                            continue

                    if not raw_final_delta_seen:
                        fallback = completed_final_text
                        if not fallback and fallback_agent_texts:
                            fallback = fallback_agent_texts[-1]
                        if fallback:
                            now_ms = (time.perf_counter() - run_started) * 1000.0
                            for visible in status_gate.feed(fallback):
                                if not visible:
                                    continue
                                if first_delta_ms is None:
                                    first_delta_ms = now_ms
                                streamed_chunks.append(visible)
                                yield visible

                    now_ms = (time.perf_counter() - run_started) * 1000.0
                    for visible in status_gate.finish():
                        if not visible:
                            continue
                        if first_delta_ms is None:
                            first_delta_ms = now_ms
                        streamed_chunks.append(visible)
                        yield visible

                    if not streamed_chunks:
                        raise RuntimeError(
                            "Codex app-server n'a retourné aucun message assistant final exploitable."
                        )

            except TimeoutError as exc:
                await self._abort_process()
                raise RuntimeError("Codex app-server a dépassé le délai maximal autorisé.") from exc
            except asyncio.CancelledError:
                await asyncio.shield(self._abort_process())
                raise
            except GeneratorExit:
                await asyncio.shield(self._abort_process())
                raise
            except Exception:
                await self._abort_process()
                raise
            finally:
                turn_workdir.cleanup()

            total_ms = (time.perf_counter() - run_started) * 1000.0
            self.last_metrics = CodexRunMetrics(
                prompt_chars=len(prompt),
                prompt_build_ms=prompt_build_ms,
                process_spawn_ms=process_spawn_ms,
                first_event_ms=first_event_ms,
                first_agent_message_ms=first_delta_ms,
                total_ms=total_ms,
                input_tokens=usage.get("input_tokens"),
                cached_input_tokens=usage.get("cached_input_tokens"),
                output_tokens=usage.get("output_tokens"),
                reasoning_output_tokens=usage.get("reasoning_output_tokens"),
                stream_chunks=len(streamed_chunks),
            )
