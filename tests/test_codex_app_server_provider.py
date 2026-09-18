from __future__ import annotations

import stat
from pathlib import Path

from etpos_assistant.providers.codex_app_server import (
    _app_server_command,
    _forbidden_item_type,
    _prepare_isolated_codex_home,
    _token_metrics,
    _validate_dedicated_codex_home,
)


def test_app_server_command_disables_web_and_shell_tools():
    command = _app_server_command()

    assert command[:3] == ["codex", "app-server", "--stdio"]
    assert 'web_search="disabled"' in command
    assert command.count("--disable") >= 2
    assert "shell_tool" in command
    assert "unified_exec" in command


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


def test_validate_dedicated_codex_home_rejects_runtime_configuration(tmp_path: Path):
    (tmp_path / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    _validate_dedicated_codex_home(tmp_path)

    (tmp_path / "config.toml").write_text('model="x"', encoding="utf-8")
    try:
        _validate_dedicated_codex_home(tmp_path)
    except RuntimeError as exc:
        assert "config.toml" in str(exc)
    else:
        raise AssertionError("A dedicated App Server home must reject config.toml")


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
