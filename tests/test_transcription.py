from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import etpos_assistant.routers.transcription as router_module
import etpos_assistant.transcription as transcription_module
from etpos_assistant.transcription import TranscriptionResult, load_hotwords


def _request(
    body: bytes = b"audio",
    *,
    content_type: str = "audio/webm;codecs=opus",
    content_length: int | None = None,
) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"content-type", content_type.encode())]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/transcribe",
            "raw_path": b"/api/transcribe",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8787),
        },
        receive=receive,
    )


def test_load_hotwords_ignores_comments_empty_lines_and_duplicates(tmp_path):
    path = tmp_path / "hotwords.txt"
    path.write_text(
        "# commentaire\nETPOS\n\nVerifone\netpos\ncompte courant client\n",
        encoding="utf-8",
    )

    assert load_hotwords(path) == (
        "ETPOS",
        "Verifone",
        "compte courant client",
    )


def _install_fake_audio_decoder(monkeypatch, audio):
    package = ModuleType("faster_whisper")
    audio_module = ModuleType("faster_whisper.audio")
    audio_module.decode_audio = lambda _path, sampling_rate: audio
    package.audio = audio_module
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", audio_module)


def test_transcribe_audio_file_uses_french_vad_and_hotwords(monkeypatch, tmp_path):
    audio = [0.0] * transcription_module.AUDIO_SAMPLE_RATE
    _install_fake_audio_decoder(monkeypatch, audio)
    monkeypatch.setattr(
        transcription_module,
        "settings",
        SimpleNamespace(
            whisper_max_duration_seconds=60,
            whisper_language="fr",
        ),
    )
    monkeypatch.setattr(transcription_module, "hotwords_prompt", lambda: "ETPOS, Verifone")

    captured = {}

    class FakeModel:
        def transcribe(self, passed_audio, **kwargs):
            captured["audio"] = passed_audio
            captured["kwargs"] = kwargs
            return (
                [
                    SimpleNamespace(text="  Comment configurer "),
                    SimpleNamespace(text="ETPOS ?  "),
                ],
                SimpleNamespace(),
            )

    monkeypatch.setattr(transcription_module, "get_transcription_model", lambda: FakeModel())

    result = transcription_module.transcribe_audio_file(tmp_path / "voice.webm")

    assert result == TranscriptionResult(
        text="Comment configurer ETPOS ?",
        duration_seconds=1.0,
    )
    assert captured["audio"] is audio
    assert captured["kwargs"] == {
        "language": "fr",
        "task": "transcribe",
        "beam_size": 5,
        "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": 500},
        "hotwords": "ETPOS, Verifone",
    }


def test_transcribe_audio_file_rejects_overlong_audio_before_model_load(monkeypatch, tmp_path):
    audio = [0.0] * (transcription_module.AUDIO_SAMPLE_RATE + 1)
    _install_fake_audio_decoder(monkeypatch, audio)
    monkeypatch.setattr(
        transcription_module,
        "settings",
        SimpleNamespace(whisper_max_duration_seconds=1),
    )

    def should_not_load_model():
        raise AssertionError("model should not load for an overlong recording")

    monkeypatch.setattr(transcription_module, "get_transcription_model", should_not_load_model)

    with pytest.raises(transcription_module.TranscriptionInputError, match="limite de 1 seconde"):
        transcription_module.transcribe_audio_file(tmp_path / "voice.webm")


@pytest.mark.asyncio
async def test_transcribe_accepts_browser_audio_and_deletes_tempfile(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "settings",
        SimpleNamespace(whisper_max_upload_bytes=1024),
    )
    monkeypatch.setattr(
        router_module,
        "require_api_session",
        lambda _request: {"user_id": 1, "csrf_token": "token"},
    )
    monkeypatch.setattr(router_module, "require_csrf", lambda _request, _session: None)

    captured: dict[str, Path] = {}

    def fake_transcribe(path: Path):
        assert path.exists()
        assert path.read_bytes() == b"browser-audio"
        captured["path"] = path
        return TranscriptionResult(
            text="Comment configurer ETPOS ?",
            duration_seconds=1.2345,
        )

    monkeypatch.setattr(router_module, "transcribe_audio_file", fake_transcribe)

    response = await router_module.transcribe(
        _request(body=b"browser-audio", content_type="audio/webm;codecs=opus")
    )

    assert response == {
        "text": "Comment configurer ETPOS ?",
        "duration_seconds": 1.234,
    }
    assert not captured["path"].exists()


@pytest.mark.asyncio
async def test_transcribe_rejects_unsupported_media_type(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "require_api_session",
        lambda _request: {"user_id": 1, "csrf_token": "token"},
    )
    monkeypatch.setattr(router_module, "require_csrf", lambda _request, _session: None)

    with pytest.raises(HTTPException) as exc_info:
        await router_module.transcribe(
            _request(body=b"not-audio", content_type="application/octet-stream")
        )

    assert exc_info.value.status_code == 415


@pytest.mark.asyncio
async def test_transcribe_rejects_declared_oversize_before_reading_body(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "settings",
        SimpleNamespace(whisper_max_upload_bytes=8),
    )
    monkeypatch.setattr(
        router_module,
        "require_api_session",
        lambda _request: {"user_id": 1, "csrf_token": "token"},
    )
    monkeypatch.setattr(router_module, "require_csrf", lambda _request, _session: None)

    with pytest.raises(HTTPException) as exc_info:
        await router_module.transcribe(
            _request(
                body=b"123456789",
                content_type="audio/webm",
                content_length=9,
            )
        )

    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_transcribe_rejects_stream_that_exceeds_limit(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "settings",
        SimpleNamespace(whisper_max_upload_bytes=8),
    )
    monkeypatch.setattr(
        router_module,
        "require_api_session",
        lambda _request: {"user_id": 1, "csrf_token": "token"},
    )
    monkeypatch.setattr(router_module, "require_csrf", lambda _request, _session: None)

    with pytest.raises(HTTPException) as exc_info:
        await router_module.transcribe(
            _request(body=b"123456789", content_type="audio/webm")
        )

    assert exc_info.value.status_code == 413
