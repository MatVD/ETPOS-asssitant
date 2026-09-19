from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from etpos_assistant.providers.codex_app_server import (
    _app_server_command,
    _forbidden_item_type,
    _prepare_isolated_codex_home,
    _reply_to_server_request,
    _thread_start_params,
    _token_metrics,
    _validate_dedicated_codex_home,
)


def test_app_server_command_disables_web_and_shell_tools():
    command = _app_server_command()

    assert command[:3] == ["codex", "app-server", "--stdio"]
    assert 'web_search="disabled"' in command
    assert "mcp_servers={}" in command
    assert command.count("--disable") >= 2
    for feature in {
        "apps",
        "browser_use",
        "browser_use_full_cdp_access",
        "computer_use",
        "hooks",
        "image_generation",
        "enable_mcp_apps",
        "mcp_2026_07_28",
        "memories",
        "multi_agent",
        "non_prefixed_mcp_tool_names",
        "plugins",
        "remote_plugin",
        "shell_tool",
        "skill_search",
        "tool_call_mcp_elicitation",
        "tool_suggest",
        "unified_exec",
        "view_image",
        "workspace_dependencies",
    }:
        assert feature in command


def test_thread_start_params_disable_dynamic_and_environment_capabilities(tmp_path: Path):
    params = _thread_start_params(str(tmp_path))

    assert params["ephemeral"] is True
    assert params["approvalPolicy"] == "never"
    assert params["sandbox"] == "read-only"
    assert params["dynamicTools"] == []
    assert params["environments"] == []
    assert params["runtimeWorkspaceRoots"] == []
    assert params["selectedCapabilityRoots"] == []
    assert params["config"]["web_search"] == "disabled"
    assert params["config"]["mcp_servers"] == {}


def test_prepare_isolated_codex_home_copies_only_auth(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    (source / "config.toml").write_text("[mcp_servers.demo]\nenabled=true\n", encoding="utf-8")
    (source / "AGENTS.md").write_text("untrusted instructions", encoding="utf-8")

    _prepare_isolated_codex_home(source, target)

    assert (target / "auth.json").read_text(encoding="utf-8") == '{"token":"secret"}'
    assert not (target / "config.toml").exists()
    assert not (target / "AGENTS.md").exists()
    assert stat.S_IMODE((target / "auth.json").stat().st_mode) == 0o600


def test_validate_dedicated_codex_home_allows_codex_runtime_state(tmp_path: Path):
    (tmp_path / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    (tmp_path / "config.toml").write_text('model="gpt-5.6-sol"', encoding="utf-8")
    skills = tmp_path / "skills" / ".system" / "openai-docs"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("system skill", encoding="utf-8")
    (tmp_path / "state_5.sqlite").write_bytes(b"runtime-state")

    _validate_dedicated_codex_home(tmp_path)


def test_validate_dedicated_codex_home_requires_authentication(tmp_path: Path):
    try:
        _validate_dedicated_codex_home(tmp_path)
    except RuntimeError as exc:
        assert "Authentification Codex App Server introuvable" in str(exc)
    else:
        raise AssertionError("A dedicated App Server home must contain auth.json")


@pytest.mark.asyncio
async def test_app_server_declines_command_and_file_approval_requests():
    class FakeStdin:
        def __init__(self):
            self.writes: list[bytes] = []

        def write(self, payload: bytes) -> None:
            self.writes.append(payload)

        async def drain(self) -> None:
            return None

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()

    process = FakeProcess()
    await _reply_to_server_request(
        process,
        {"id": 10, "method": "item/commandExecution/requestApproval"},
    )
    await _reply_to_server_request(
        process,
        {"id": 11, "method": "item/fileChange/requestApproval"},
    )

    replies = [json.loads(payload) for payload in process.stdin.writes]
    assert replies == [
        {"id": 10, "result": {"decision": "decline"}},
        {"id": 11, "result": {"decision": "decline"}},
    ]


@pytest.mark.asyncio
async def test_app_server_rejects_unknown_server_requests():
    class FakeStdin:
        def __init__(self):
            self.writes: list[bytes] = []

        def write(self, payload: bytes) -> None:
            self.writes.append(payload)

        async def drain(self) -> None:
            return None

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()

    process = FakeProcess()
    await _reply_to_server_request(process, {"id": 12, "method": "unknown/request"})

    reply = json.loads(process.stdin.writes[0])
    assert reply["id"] == 12
    assert reply["error"]["code"] == -32601


def test_token_metrics_uses_last_turn_usage():
    payload = {
        "tokenUsage": {
            "last": {
                "inputTokens": 120,
                "cachedInputTokens": 80,
                "outputTokens": 30,
                "reasoningOutputTokens": 5,
            }
        }
    }

    assert _token_metrics(payload) == {
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "output_tokens": 30,
        "reasoning_output_tokens": 5,
    }


def test_forbidden_item_type_fails_closed_on_tools():
    assert (
        _forbidden_item_type(
            {
                "method": "item/started",
                "params": {"item": {"type": "commandExecution"}},
            }
        )
        == "commandExecution"
    )
    assert (
        _forbidden_item_type(
            {
                "method": "item/started",
                "params": {"item": {"type": "agentMessage"}},
            }
        )
        is None
    )
    assert (
        _forbidden_item_type(
            {
                "method": "item/started",
                "params": {"item": {"type": "reasoning"}},
            }
        )
        is None
    )
