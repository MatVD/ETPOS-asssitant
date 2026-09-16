from etpos_assistant.providers.codex_cli import _codex_command, parse_agent_message
from etpos_assistant.providers.prompting import SYSTEM_INSTRUCTIONS


def test_parse_agent_message_accepts_completed_agent_message():
    line = '{"type":"item.completed","item":{"type":"agent_message","text":"Bonjour [S1]"}}'
    assert parse_agent_message(line) == "Bonjour [S1]"


def test_parse_agent_message_ignores_other_events():
    assert parse_agent_message('{"type":"turn.started"}') is None
    assert parse_agent_message('{"type":"item.completed","item":{"type":"reasoning","text":"secret"}}') is None
    assert parse_agent_message('not-json') is None


def test_prompt_prefers_supported_partial_answer_before_full_abstention():
    assert "informations utiles" in SYSTEM_INSTRUCTIONS
    assert "abstention complète uniquement" in SYSTEM_INSTRUCTIONS
    assert "absence d'un titre exactement identique" in SYSTEM_INSTRUCTIONS


def test_codex_command_is_ephemeral_and_read_only(monkeypatch):
    from etpos_assistant import config

    object.__setattr__(config.settings, "codex_model", "")
    command = _codex_command()
    assert command[:4] == [config.settings.codex_binary, "--ask-for-approval", "never", "exec"]
    assert "--ephemeral" in command
    assert "--json" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"
