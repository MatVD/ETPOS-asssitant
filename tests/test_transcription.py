from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
            whisper_model="small",
            whisper_device="cpu",
            whisper_compute_type="int8",
            whisper_cpu_threads=0,
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
                    SimpleNamespace(
                        text="  Comment configurer ",
                        start=0.0,
                        end=0.55,
                    ),
                    SimpleNamespace(
                        text="ETPOS ?  ",
                        start=0.55,
                        end=1.0,
                    ),
                ],
                SimpleNamespace(),
            )

    monkeypatch.setattr(transcription_module, "get_transcription_model", lambda: FakeModel())

    result = transcription_module.transcribe_audio_file(tmp_path / "voice.webm")

    assert result.text == "Comment configurer ETPOS ?"
    assert result.duration_seconds == 1.0
    assert result.timings is not None
    assert result.timings.decode_ms >= 0
    assert result.timings.model_ready_ms >= 0
    assert result.timings.queue_wait_ms >= 0
    assert result.timings.model_call_ms >= 0
    assert result.timings.segment_iteration_ms >= 0
    assert result.timings.reconstruction_ms >= 0
    assert result.timings.total_ms >= 0
    assert captured["audio"] is audio
    assert captured["kwargs"] == {
        "language": "fr",
        "task": "transcribe",
        "beam_size": 5,
        "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": 500},
        "hotwords": "ETPOS, Verifone",
    }
    assert result.profile is transcription_module.TranscriptionProfile.FINAL
    assert result.timings.profile is transcription_module.TranscriptionProfile.FINAL
    assert result.segments == (
        transcription_module.TranscriptionSegment(
            text="Comment configurer",
            start_seconds=0.0,
            end_seconds=0.55,
        ),
        transcription_module.TranscriptionSegment(
            text="ETPOS ?",
            start_seconds=0.55,
            end_seconds=1.0,
        ),
    )


def _transcription_settings(*, max_duration: int = 60):
    return SimpleNamespace(
        whisper_max_duration_seconds=max_duration,
        whisper_language="fr",
        whisper_model="small",
        whisper_device="cpu",
        whisper_compute_type="int8",
        whisper_cpu_threads=6,
    )


def test_transcribe_audio_samples_uses_preview_profile(monkeypatch):
    audio = [0.0] * transcription_module.AUDIO_SAMPLE_RATE
    monkeypatch.setattr(
        transcription_module,
        "settings",
        _transcription_settings(),
    )
    monkeypatch.setattr(
        transcription_module,
        "hotwords_prompt",
        lambda: "ETPOS, Verifone",
    )
    captured = {}

    class FakeModel:
        def transcribe(self, passed_audio, **kwargs):
            captured["audio"] = passed_audio
            captured["kwargs"] = kwargs
            return (
                [
                    SimpleNamespace(
                        text="  compte courant client  ",
                        start=0.1,
                        end=0.9,
                    )
                ],
                SimpleNamespace(),
            )

    monkeypatch.setattr(
        transcription_module,
        "get_transcription_model",
        lambda: FakeModel(),
    )

    result = transcription_module.transcribe_audio_samples(
        audio,
        profile=transcription_module.TranscriptionProfile.PREVIEW,
    )

    assert result.text == "compte courant client"
    assert result.profile is transcription_module.TranscriptionProfile.PREVIEW
    assert result.timings is not None
    assert result.timings.profile is transcription_module.TranscriptionProfile.PREVIEW
    assert result.timings.decode_ms == 0.0
    assert result.segments == (
        transcription_module.TranscriptionSegment(
            text="compte courant client",
            start_seconds=0.1,
            end_seconds=0.9,
        ),
    )
    assert captured["audio"] is audio
    assert captured["kwargs"] == {
        "language": "fr",
        "task": "transcribe",
        "beam_size": 1,
        "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": 500},
        "hotwords": "ETPOS, Verifone",
        "temperature": 0.0,
        "best_of": 1,
    }


def test_preview_accepts_empty_transcript_while_final_rejects_it(monkeypatch):
    audio = [0.0] * transcription_module.AUDIO_SAMPLE_RATE
    monkeypatch.setattr(
        transcription_module,
        "settings",
        _transcription_settings(),
    )
    monkeypatch.setattr(transcription_module, "hotwords_prompt", lambda: None)

    class SilentModel:
        def transcribe(self, _audio, **_kwargs):
            return ([], SimpleNamespace())

    model = SilentModel()
    monkeypatch.setattr(
        transcription_module,
        "get_transcription_model",
        lambda: model,
    )

    preview = transcription_module.transcribe_audio_samples(
        audio,
        profile=transcription_module.TranscriptionProfile.PREVIEW,
    )
    assert preview.text == ""
    assert preview.segments == ()

    with pytest.raises(
        transcription_module.TranscriptionInputError,
        match="Aucune parole exploitable",
    ):
        transcription_module.transcribe_audio_samples(
            audio,
            profile=transcription_module.TranscriptionProfile.FINAL,
        )


def test_get_transcription_model_reuses_single_instance(monkeypatch):
    transcription_module.reset_transcription_model()
    package = ModuleType("faster_whisper")
    created = []

    class FakeWhisperModel:
        def __init__(self, model_name, **kwargs):
            self.model_name = model_name
            self.kwargs = kwargs
            created.append(self)

    package.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setattr(
        transcription_module,
        "settings",
        SimpleNamespace(
            whisper_model="small",
            whisper_device="cpu",
            whisper_compute_type="int8",
        ),
    )
    monkeypatch.setattr(transcription_module, "_model_kwargs", lambda: {})

    try:
        first = transcription_module.get_transcription_model()
        second = transcription_module.get_transcription_model()
    finally:
        transcription_module.reset_transcription_model()

    assert first is second
    assert len(created) == 1


def test_transcribe_audio_samples_serializes_inference(monkeypatch):
    audio = [0.0] * transcription_module.AUDIO_SAMPLE_RATE
    monkeypatch.setattr(
        transcription_module,
        "settings",
        _transcription_settings(),
    )
    monkeypatch.setattr(transcription_module, "hotwords_prompt", lambda: None)

    active = 0
    max_active = 0
    counter_lock = threading.Lock()

    class SlowModel:
        def transcribe(self, _audio, **_kwargs):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                return (
                    [SimpleNamespace(text="ok", start=0.0, end=1.0)],
                    SimpleNamespace(),
                )
            finally:
                with counter_lock:
                    active -= 1

    model = SlowModel()
    monkeypatch.setattr(
        transcription_module,
        "get_transcription_model",
        lambda: model,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: transcription_module.transcribe_audio_samples(
                    audio
                ),
                range(2),
            )
        )

    assert [result.text for result in results] == ["ok", "ok"]
    assert max_active == 1


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


def test_transcribe_audio_file_rejects_decode_error_before_model_load(
    monkeypatch,
    tmp_path,
):
    package = ModuleType("faster_whisper")
    audio_module = ModuleType("faster_whisper.audio")

    def fail_decode(_path, sampling_rate):
        assert sampling_rate == transcription_module.AUDIO_SAMPLE_RATE
        raise RuntimeError("invalid container")

    audio_module.decode_audio = fail_decode
    package.audio = audio_module
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", audio_module)

    def should_not_load_model():
        raise AssertionError("model should not load after a decode error")

    monkeypatch.setattr(
        transcription_module,
        "get_transcription_model",
        should_not_load_model,
    )

    with pytest.raises(
        transcription_module.TranscriptionInputError,
        match="n'a pas pu être décodé",
    ):
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
