from pathlib import Path

from etpos_assistant.providers.codex_cli import (
    _codex_command,
    codex_auth_directory,
    codex_environment,
    parse_agent_message,
    parse_turn_usage,
)
from etpos_assistant.providers.prompting import SYSTEM_INSTRUCTIONS


def test_parse_agent_message_accepts_completed_agent_message():
    line = '{"type":"item.completed","item":{"type":"agent_message","text":"Bonjour [S1]"}}'
    assert parse_agent_message(line) == "Bonjour [S1]"


def test_parse_agent_message_ignores_other_events():
    assert parse_agent_message('{"type":"turn.started"}') is None
    assert parse_agent_message('{"type":"item.completed","item":{"type":"reasoning","text":"secret"}}') is None
    assert parse_agent_message('not-json') is None


def test_parse_turn_usage_accepts_documented_completed_turn_shape():
    line = (
        '{"type":"turn.completed","usage":{"input_tokens":24763,'
        '"cached_input_tokens":24448,"output_tokens":122,"reasoning_output_tokens":0}}'
    )
    assert parse_turn_usage(line) == {
        "input_tokens": 24763,
        "cached_input_tokens": 24448,
        "output_tokens": 122,
        "reasoning_output_tokens": 0,
    }
    assert parse_turn_usage('{"type":"turn.started"}') is None
    assert parse_turn_usage("not-json") is None


def test_prompt_prefers_supported_partial_answer_before_full_abstention():
    assert "informations utiles" in SYSTEM_INSTRUCTIONS
    assert "abstention complète uniquement" in SYSTEM_INSTRUCTIONS
    assert "absence d'un titre exactement identique" in SYSTEM_INSTRUCTIONS
    assert "concise et directe" in SYSTEM_INSTRUCTIONS


def test_codex_command_is_ephemeral_and_read_only(monkeypatch):
    from etpos_assistant import config

    previous = (
        config.settings.codex_model,
        config.settings.codex_reasoning_effort,
        config.settings.codex_model_verbosity,
    )
    try:
        object.__setattr__(config.settings, "codex_model", "")
        object.__setattr__(config.settings, "codex_reasoning_effort", "")
        object.__setattr__(config.settings, "codex_model_verbosity", "")
        command = _codex_command()
        assert command[:4] == [config.settings.codex_binary, "--ask-for-approval", "never", "exec"]
        assert "--ephemeral" in command
        assert "--json" in command
        assert "--ignore-user-config" in command
        assert "--ignore-rules" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[-1] == "-"
    finally:
        object.__setattr__(config.settings, "codex_model", previous[0])
        object.__setattr__(config.settings, "codex_reasoning_effort", previous[1])
        object.__setattr__(config.settings, "codex_model_verbosity", previous[2])


def test_codex_command_applies_explicit_performance_overrides():
    from etpos_assistant import config

    previous = (
        config.settings.codex_reasoning_effort,
        config.settings.codex_model_verbosity,
    )
    try:
        object.__setattr__(config.settings, "codex_reasoning_effort", "low")
        object.__setattr__(config.settings, "codex_model_verbosity", "low")
        command = _codex_command()
        assert 'model_reasoning_effort="low"' in command
        assert 'model_verbosity="low"' in command
        assert command.index("--config") < command.index("exec")
    finally:
        object.__setattr__(config.settings, "codex_reasoning_effort", previous[0])
        object.__setattr__(config.settings, "codex_model_verbosity", previous[1])


def test_codex_auth_directory_uses_explicit_codex_home_first():
    assert codex_auth_directory({"HOME": "/tmp/home", "CODEX_HOME": "/secure/codex"}) == Path(
        "/secure/codex"
    )
    assert codex_auth_directory({"HOME": "/tmp/home"}) == Path("/tmp/home/.codex")
    assert codex_auth_directory({}) is None


def test_codex_environment_does_not_forward_application_secrets(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ETPOS_PUBLIC_ORIGIN", "https://secret.example")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    env = codex_environment()

    assert env["HOME"] == "/tmp/home"
    assert env["PATH"] == "/usr/bin"
    assert "ETPOS_PUBLIC_ORIGIN" not in env
    assert "OPENAI_API_KEY" not in env
