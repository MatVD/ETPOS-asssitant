from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import etpos_assistant.routers.transcription as router_module
import etpos_assistant.transcription_stream as stream_module
from etpos_assistant.transcription import TranscriptionInputError, TranscriptionResult


class FakeWebSocket:
    def __init__(self, messages=()):
        self._messages = list(messages)
        self.headers = {"origin": "http://testserver", "host": "testserver"}
        self.cookies = {"etpos_session": "session-token"}
        self.url = SimpleNamespace(scheme="ws")
        self.client = SimpleNamespace(host="127.0.0.1")
        self.accepted = False
        self.sent = []
        self.closed = []

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if self._messages:
            return self._messages.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000):
        self.closed.append(code)


def _settings(**overrides):
    values = {
        "whisper_streaming_enabled": True,
        "whisper_max_duration_seconds": 60,
        "whisper_stream_max_message_bytes": 16 * 1024,
        "whisper_stream_nominal_chunk_bytes": 8000,
        "whisper_stream_init_timeout_seconds": 1.0,
        "whisper_stream_idle_timeout_seconds": 1.0,
        "whisper_stream_finalization_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _session():
    return {"session_id": 1, "user_id": 1, "csrf_token": "csrf-token"}


def _init(**overrides):
    payload = {
        "type": "init",
        "version": 1,
        "csrf_token": "csrf-token",
        "format": {
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
        },
    }
    payload.update(overrides)
    return {"type": "websocket.receive", "text": __import__("json").dumps(payload)}


def _finish(last_sequence: int, total_samples: int):
    return {
        "type": "websocket.receive",
        "text": __import__("json").dumps(
            {
                "type": "finish",
                "last_sequence": last_sequence,
                "total_samples": total_samples,
            }
        ),
    }


@pytest.fixture(autouse=True)
def reset_admission():
    stream_module.dictation_admission.reset_for_tests()
    yield
    stream_module.dictation_admission.reset_for_tests()


@pytest.mark.asyncio
async def test_stream_is_disabled_by_default_flag(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings(whisper_streaming_enabled=False))
    websocket = FakeWebSocket()

    await stream_module.handle_transcription_websocket(websocket)

    assert not websocket.accepted
    assert websocket.closed == [stream_module.POLICY_CLOSE_CODE]


@pytest.mark.asyncio
async def test_stream_rejects_origin_before_accept(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: False)
    websocket = FakeWebSocket()

    await stream_module.handle_transcription_websocket(websocket)

    assert not websocket.accepted
    assert websocket.closed == [stream_module.POLICY_CLOSE_CODE]


@pytest.mark.asyncio
async def test_stream_rejects_missing_session_before_accept(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    monkeypatch.setattr(stream_module, "get_session", lambda *_args, **_kwargs: None)
    websocket = FakeWebSocket()

    await stream_module.handle_transcription_websocket(websocket)

    assert not websocket.accepted
    assert websocket.closed == [stream_module.POLICY_CLOSE_CODE]


@pytest.mark.asyncio
async def test_stream_requires_csrf_in_first_message(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    monkeypatch.setattr(stream_module, "get_session", lambda *_args, **_kwargs: _session())
    websocket = FakeWebSocket([_init(csrf_token="wrong")])

    await stream_module.handle_transcription_websocket(websocket)

    assert websocket.accepted
    assert websocket.sent[-1]["type"] == "error"
    assert websocket.sent[-1]["code"] == "invalid_csrf"


@pytest.mark.asyncio
async def test_stream_transcribes_complete_pcm_with_final_profile(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    monkeypatch.setattr(stream_module, "get_session", lambda *_args, **_kwargs: _session())
    monkeypatch.setattr(stream_module, "pcm16le_to_float32", lambda pcm: ("samples", pcm))

    captured = {}

    def fake_transcribe(samples, *, profile):
        captured["samples"] = samples
        captured["profile"] = profile
        return TranscriptionResult(text="Créer un compte", duration_seconds=0.25)

    monkeypatch.setattr(stream_module, "transcribe_audio_samples", fake_transcribe)

    pcm = b"\x00\x00" * 4000
    frame = stream_module.encode_pcm_frame(0, 0, pcm)
    websocket = FakeWebSocket(
        [
            _init(),
            {"type": "websocket.receive", "bytes": frame},
            _finish(0, 4000),
        ]
    )

    await stream_module.handle_transcription_websocket(websocket)

    assert websocket.sent[0]["type"] == "ready"
    assert websocket.sent[0]["limits"]["nominal_chunk_bytes"] == 8000
    assert websocket.sent[-1] == {
        "type": "final",
        "dictation_id": websocket.sent[0]["dictation_id"],
        "text": "Créer un compte",
        "duration_seconds": 0.25,
        "covered_samples": 4000,
    }
    assert captured["samples"] == ("samples", pcm)
    assert captured["profile"] is stream_module.TranscriptionProfile.FINAL
    assert websocket.closed[-1] == stream_module.NORMAL_CLOSE_CODE
    assert not stream_module.dictation_admission.is_busy()


@pytest.mark.asyncio
async def test_stream_accepts_residual_last_block(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    monkeypatch.setattr(stream_module, "get_session", lambda *_args, **_kwargs: _session())
    monkeypatch.setattr(stream_module, "pcm16le_to_float32", lambda pcm: pcm)
    monkeypatch.setattr(
        stream_module,
        "transcribe_audio_samples",
        lambda *_args, **_kwargs: TranscriptionResult(text="ok", duration_seconds=0.3),
    )

    first_pcm = b"\x00\x00" * 4000
    residual_pcm = b"\x00\x00" * 800
    websocket = FakeWebSocket(
        [
            _init(),
            {
                "type": "websocket.receive",
                "bytes": stream_module.encode_pcm_frame(0, 0, first_pcm),
            },
            {
                "type": "websocket.receive",
                "bytes": stream_module.encode_pcm_frame(1, 4000, residual_pcm),
            },
            _finish(1, 4800),
        ]
    )

    await stream_module.handle_transcription_websocket(websocket)

    assert websocket.sent[-1]["type"] == "final"
    assert websocket.sent[-1]["covered_samples"] == 4800


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "expected_code"),
    [
        (
            [
                _init(),
                {
                    "type": "websocket.receive",
                    "bytes": stream_module.encode_pcm_frame(1, 0, b"\x00\x00" * 10),
                },
            ],
            "invalid_sequence",
        ),
        (
            [
                _init(),
                {
                    "type": "websocket.receive",
                    "bytes": stream_module.encode_pcm_frame(0, 2, b"\x00\x00" * 10),
                },
            ],
            "invalid_position",
        ),
        (
            [
                _init(),
                {
                    "type": "websocket.receive",
                    "bytes": stream_module.encode_pcm_frame(0, 0, b"\x00\x00" * 10),
                },
                _finish(0, 9),
            ],
            "incomplete_audio",
        ),
    ],
)
async def test_stream_rejects_incomplete_or_out_of_order_audio(
    monkeypatch,
    messages,
    expected_code,
):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    monkeypatch.setattr(stream_module, "get_session", lambda *_args, **_kwargs: _session())
    websocket = FakeWebSocket(messages)

    await stream_module.handle_transcription_websocket(websocket)

    assert websocket.sent[-1]["type"] == "error"
    assert websocket.sent[-1]["code"] == expected_code
    assert not stream_module.dictation_admission.is_busy()


@pytest.mark.asyncio
async def test_stream_rejects_invalid_format(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    monkeypatch.setattr(stream_module, "get_session", lambda *_args, **_kwargs: _session())
    websocket = FakeWebSocket(
        [
            _init(
                format={
                    "encoding": "pcm_s16le",
                    "sample_rate": 48000,
                    "channels": 1,
                }
            )
        ]
    )

    await stream_module.handle_transcription_websocket(websocket)

    assert websocket.sent[-1]["code"] == "invalid_format"


@pytest.mark.asyncio
async def test_stream_rechecks_session_while_connection_is_open(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    calls = 0

    def session_lookup(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return _session()
        return None

    monkeypatch.setattr(stream_module, "get_session", session_lookup)
    websocket = FakeWebSocket(
        [
            _init(),
            {
                "type": "websocket.receive",
                "bytes": stream_module.encode_pcm_frame(0, 0, b"\x00\x00" * 10),
            },
        ]
    )

    await stream_module.handle_transcription_websocket(websocket)

    assert websocket.sent[-1]["code"] == "session_expired"
    assert not stream_module.dictation_admission.is_busy()


@pytest.mark.asyncio
async def test_stream_does_not_return_final_if_session_expires_during_inference(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    calls = 0

    def session_lookup(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 5:
            return _session()
        return None

    monkeypatch.setattr(stream_module, "get_session", session_lookup)
    monkeypatch.setattr(stream_module, "pcm16le_to_float32", lambda pcm: pcm)
    monkeypatch.setattr(
        stream_module,
        "transcribe_audio_samples",
        lambda *_args, **_kwargs: TranscriptionResult(text="secret", duration_seconds=0.25),
    )
    pcm = b"\x00\x00" * 4000
    websocket = FakeWebSocket(
        [
            _init(),
            {
                "type": "websocket.receive",
                "bytes": stream_module.encode_pcm_frame(0, 0, pcm),
            },
            _finish(0, 4000),
        ]
    )

    await stream_module.handle_transcription_websocket(websocket)

    assert all(message.get("type") != "final" for message in websocket.sent)
    assert websocket.sent[-1]["code"] == "session_expired"
    assert not stream_module.dictation_admission.is_busy()


@pytest.mark.asyncio
async def test_stream_maps_silence_to_terminal_error(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    monkeypatch.setattr(stream_module, "get_session", lambda *_args, **_kwargs: _session())
    monkeypatch.setattr(stream_module, "pcm16le_to_float32", lambda pcm: pcm)

    def silent(*_args, **_kwargs):
        raise TranscriptionInputError("Aucune parole exploitable détectée.")

    monkeypatch.setattr(stream_module, "transcribe_audio_samples", silent)
    pcm = b"\x00\x00" * 4000
    websocket = FakeWebSocket(
        [
            _init(),
            {
                "type": "websocket.receive",
                "bytes": stream_module.encode_pcm_frame(0, 0, pcm),
            },
            _finish(0, 4000),
        ]
    )

    await stream_module.handle_transcription_websocket(websocket)

    assert websocket.sent[-1]["type"] == "error"
    assert websocket.sent[-1]["code"] == "unprocessable_audio"


@pytest.mark.asyncio
async def test_early_disconnect_releases_admission(monkeypatch):
    monkeypatch.setattr(stream_module, "settings", _settings())
    monkeypatch.setattr(stream_module, "same_origin_websocket", lambda _ws: True)
    monkeypatch.setattr(stream_module, "get_session", lambda *_args, **_kwargs: _session())
    websocket = FakeWebSocket([_init(), {"type": "websocket.disconnect"}])

    await stream_module.handle_transcription_websocket(websocket)

    assert websocket.sent[0]["type"] == "ready"
    assert not stream_module.dictation_admission.is_busy()


@pytest.mark.asyncio
async def test_inference_keeps_admission_after_waiter_cancellation():
    started = threading.Event()
    release = threading.Event()
    token = stream_module.dictation_admission.try_acquire()
    assert token is not None

    def slow_job():
        started.set()
        release.wait(timeout=2)
        return "done"

    waiter = asyncio.create_task(
        stream_module.run_admitted_inference(
            slow_job,
            admission_token=token,
        )
    )
    while not started.is_set():
        await asyncio.sleep(0.001)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert stream_module.dictation_admission.is_busy()
    release.set()
    for _ in range(200):
        if not stream_module.dictation_admission.is_busy():
            break
        await asyncio.sleep(0.005)
    assert not stream_module.dictation_admission.is_busy()


@pytest.mark.asyncio
async def test_http_and_websocket_share_admission_when_streaming_enabled(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "settings",
        SimpleNamespace(
            whisper_max_upload_bytes=1024,
            whisper_streaming_enabled=True,
        ),
    )
    monkeypatch.setattr(
        router_module,
        "require_api_session",
        lambda _request: {"user_id": 1, "csrf_token": "token"},
    )
    monkeypatch.setattr(router_module, "require_csrf", lambda *_args, **_kwargs: None)

    token = stream_module.dictation_admission.try_acquire()
    assert token is not None
    try:
        with pytest.raises(HTTPException) as exc_info:
            await router_module.transcribe(
                router_module.Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "scheme": "https",
                        "path": "/api/transcribe",
                        "raw_path": b"/api/transcribe",
                        "query_string": b"",
                        "headers": [(b"content-type", b"audio/webm")],
                        "client": ("127.0.0.1", 12345),
                        "server": ("127.0.0.1", 8787),
                    }
                )
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Une autre dictée est déjà en cours."
    finally:
        stream_module.dictation_admission.release(token)


def test_pcm_buffer_rejects_data_after_finish():
    limits = stream_module.StreamLimits(
        max_duration_seconds=1.0,
        max_samples=16000,
        max_pcm_bytes=32000,
        max_message_bytes=16 * 1024,
        nominal_chunk_bytes=8000,
    )
    buffer = stream_module.PCMStreamBuffer(limits)
    pcm = b"\x00\x00" * 100
    buffer.append_frame(stream_module.encode_pcm_frame(0, 0, pcm))
    buffer.finish(last_sequence=0, total_samples=100)

    with pytest.raises(stream_module.StreamProtocolError) as exc_info:
        buffer.append_frame(stream_module.encode_pcm_frame(1, 100, pcm))
    assert exc_info.value.code == "data_after_finish"


def test_pcm_buffer_enforces_message_and_duration_limits():
    limits = stream_module.StreamLimits(
        max_duration_seconds=0.01,
        max_samples=160,
        max_pcm_bytes=320,
        max_message_bytes=64,
        nominal_chunk_bytes=8000,
    )
    buffer = stream_module.PCMStreamBuffer(limits)

    with pytest.raises(stream_module.StreamProtocolError) as oversized:
        buffer.append_frame(
            stream_module.encode_pcm_frame(0, 0, b"\x00\x00" * 30)
        )
    assert oversized.value.code == "message_too_large"

    buffer = stream_module.PCMStreamBuffer(limits)
    with pytest.raises(stream_module.StreamProtocolError) as too_long:
        buffer.append_frame(
            stream_module.encode_pcm_frame(0, 0, b"\x00\x00" * 161)
        )
    assert too_long.value.code in {"message_too_large", "audio_too_long"}
