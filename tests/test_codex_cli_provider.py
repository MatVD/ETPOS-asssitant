from pathlib import Path

from etpos_assistant.providers.codex_cli import (
    TEXT_ONLY_DISABLED_FEATURES,
    CodexRunMetrics,
    _codex_command,
    codex_auth_directory,
    codex_environment,
    parse_agent_message,
    parse_forbidden_item_type,
    parse_turn_usage,
)
from etpos_assistant.providers.base import SourceContext
from etpos_assistant.providers.prompting import (
    ABSTENTION_TEXT,
    AnswerStatusGate,
    SYSTEM_INSTRUCTIONS,
    build_prompt,
)


def test_parse_agent_message_accepts_completed_agent_message():
    line = '{"type":"item.completed","item":{"type":"agent_message","text":"Bonjour [S1]"}}'
    assert parse_agent_message(line) == "Bonjour [S1]"


def test_parse_agent_message_ignores_other_events():
    assert parse_agent_message('{"type":"turn.started"}') is None
    assert parse_agent_message('{"type":"item.completed","item":{"type":"reasoning","text":"secret"}}') is None
    assert parse_agent_message('not-json') is None


def test_parse_forbidden_item_type_fails_closed_on_exec_tools():
    assert (
        parse_forbidden_item_type(
            '{"type":"item.started","item":{"type":"command_execution","command":"pwd"}}'
        )
        == "command_execution"
    )
    assert (
        parse_forbidden_item_type(
            '{"type":"item.started","item":{"type":"mcp_tool_call","server":"docs"}}'
        )
        == "mcp_tool_call"
    )
    assert (
        parse_forbidden_item_type(
            '{"type":"item.started","item":{"type":"reasoning","text":"..."}}'
        )
        is None
    )


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


def test_codex_run_metrics_serializes_queue_wait():
    metrics = CodexRunMetrics(
        prompt_chars=100,
        prompt_build_ms=1.0,
        process_spawn_ms=2.0,
        first_event_ms=3.0,
        first_agent_message_ms=4.0,
        total_ms=10.0,
        queue_wait_ms=5.5,
    )

    assert metrics.as_dict()["queue_wait_ms"] == 5.5


def test_prompt_prefers_supported_partial_answer_before_full_abstention():
    assert "informations utiles" in SYSTEM_INSTRUCTIONS
    assert "abstention complète" in SYSTEM_INSTRUCTIONS
    assert "absence d'un titre exactement identique" in SYSTEM_INSTRUCTIONS
    assert "concise et directe" in SYSTEM_INSTRUCTIONS
    assert 'type="manual"' in SYSTEM_INSTRUCTIONS
    assert "source de vérité fonctionnelle" in SYSTEM_INSTRUCTIONS
    assert "[[ETPOS_STATUS:full]]" in SYSTEM_INSTRUCTIONS
    assert "[[ETPOS_STATUS:partial]]" in SYSTEM_INSTRUCTIONS
    assert "[[ETPOS_STATUS:none]]" in SYSTEM_INSTRUCTIONS


def test_prompt_includes_document_type_version_and_revision_metadata():
    prompt = build_prompt(
        "Comment sauvegarder ?",
        [
            SourceContext(
                source_id="S1",
                title="Sauvegarde",
                heading_path="SÉCURITÉ > Sauvegarde",
                text="Texte documentaire",
                source_type="manual",
                document_version="V5.34",
                revision_date="2026-01-30",
            )
        ],
        [],
    )

    assert 'type="manual"' in prompt
    assert 'version="V5.34"' in prompt
    assert 'revision_date="2026-01-30"' in prompt


def test_answer_status_gate_hides_marker_and_preserves_streaming():
    gate = AnswerStatusGate()

    assert gate.feed("[[ETPOS_") == []
    assert gate.feed("STATUS:full]]\nBon") == ["Bon"]
    assert gate.feed("jour [S1]") == ["jour [S1]"]
    assert gate.finish() == []
    assert gate.status == "full"


def test_answer_status_gate_canonicalizes_none():
    gate = AnswerStatusGate()

    assert gate.feed("[[ETPOS_STATUS:none]]") == []
    assert gate.finish() == [ABSTENTION_TEXT]
    assert gate.status == "none"


def test_answer_status_gate_falls_back_when_marker_is_missing():
    gate = AnswerStatusGate()

    assert gate.feed("Réponse sans marqueur\nSuite") == ["Réponse sans marqueur\nSuite"]
    assert gate.finish() == []
    assert gate.status == "unknown"


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
        assert command[:3] == [config.settings.codex_binary, "--ask-for-approval", "never"]
        assert "exec" in command
        assert 'web_search="disabled"' in command
        assert "mcp_servers={}" in command
        for feature in TEXT_ONLY_DISABLED_FEATURES:
            assert feature in command
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
