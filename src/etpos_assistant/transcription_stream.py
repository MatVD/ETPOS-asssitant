from __future__ import annotations

import asyncio
import json
import logging
import secrets
import struct
import threading
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .config import settings
from .security import (
    SESSION_COOKIE,
    get_session,
    same_origin_websocket,
    secure_compare,
)
from .transcription import (
    AUDIO_SAMPLE_RATE,
    TranscriptionError,
    TranscriptionInputError,
    TranscriptionProfile,
    transcribe_audio_samples,
)

logger = logging.getLogger(__name__)

STREAM_PROTOCOL_VERSION = 1
PCM_ENCODING = "pcm_s16le"
PCM_CHANNELS = 1
PCM_BYTES_PER_SAMPLE = 2
CHUNK_HEADER = struct.Struct("<IQ")
CHUNK_HEADER_BYTES = CHUNK_HEADER.size
TRANSPORT_MAX_MESSAGE_BYTES = 16 * 1024
NORMAL_CLOSE_CODE = 1000
POLICY_CLOSE_CODE = 1008
TRY_AGAIN_CLOSE_CODE = 1013
INTERNAL_ERROR_CLOSE_CODE = 1011


class StreamProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DictationAdmission:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: str | None = None

    def try_acquire(self) -> str | None:
        token = secrets.token_urlsafe(18)
        with self._lock:
            if self._owner is not None:
                return None
            self._owner = token
        return token

    def release(self, token: str) -> bool:
        with self._lock:
            if self._owner != token:
                return False
            self._owner = None
            return True

    def is_busy(self) -> bool:
        with self._lock:
            return self._owner is not None

    def reset_for_tests(self) -> None:
        with self._lock:
            self._owner = None


dictation_admission = DictationAdmission()
_FINALIZATION_TASKS: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True)
class StreamLimits:
    max_duration_seconds: float
    max_samples: int
    max_pcm_bytes: int
    max_message_bytes: int
    nominal_chunk_bytes: int


class PCMStreamBuffer:
    def __init__(self, limits: StreamLimits):
        self.limits = limits
        self._pcm = bytearray()
        self._next_sequence = 0
        self._next_sample_offset = 0
        self._finished = False

    @property
    def total_samples(self) -> int:
        return self._next_sample_offset

    @property
    def last_sequence(self) -> int | None:
        if self._next_sequence == 0:
            return None
        return self._next_sequence - 1

    @property
    def pcm_bytes(self) -> bytes:
        return bytes(self._pcm)

    def append_frame(self, frame: bytes) -> None:
        if self._finished:
            raise StreamProtocolError(
                "data_after_finish",
                "Aucune donnée audio n'est acceptée après finish.",
            )
        if len(frame) > self.limits.max_message_bytes:
            raise StreamProtocolError(
                "message_too_large",
                "Bloc audio trop volumineux.",
            )
        if len(frame) <= CHUNK_HEADER_BYTES:
            raise StreamProtocolError(
                "invalid_chunk",
                "Bloc audio vide ou en-tête incomplet.",
            )

        sequence, sample_offset = CHUNK_HEADER.unpack_from(frame)
        pcm = frame[CHUNK_HEADER_BYTES:]
        if len(pcm) % PCM_BYTES_PER_SAMPLE:
            raise StreamProtocolError(
                "invalid_chunk",
                "Le PCM 16 bits doit contenir un nombre pair d'octets.",
            )
        if sequence != self._next_sequence:
            raise StreamProtocolError(
                "invalid_sequence",
                f"Séquence attendue {self._next_sequence}, reçue {sequence}.",
            )
        if sample_offset != self._next_sample_offset:
            raise StreamProtocolError(
                "invalid_position",
                (
                    f"Position attendue {self._next_sample_offset}, "
                    f"reçue {sample_offset}."
                ),
            )

        sample_count = len(pcm) // PCM_BYTES_PER_SAMPLE
        next_total = self._next_sample_offset + sample_count
        if next_total > self.limits.max_samples:
            raise StreamProtocolError(
                "audio_too_long",
                "La durée maximale de dictée est dépassée.",
            )
        if len(self._pcm) + len(pcm) > self.limits.max_pcm_bytes:
            raise StreamProtocolError(
                "audio_too_large",
                "Le tampon audio maximal est dépassé.",
            )

        self._pcm.extend(pcm)
        self._next_sequence += 1
        self._next_sample_offset = next_total

    def finish(self, *, last_sequence: int, total_samples: int) -> None:
        if self._finished:
            raise StreamProtocolError(
                "duplicate_finish",
                "La dictée est déjà terminée.",
            )
        if self.last_sequence is None:
            raise StreamProtocolError(
                "empty_audio",
                "Aucun bloc audio n'a été reçu.",
            )
        if last_sequence != self.last_sequence:
            raise StreamProtocolError(
                "incomplete_audio",
                (
                    f"Dernière séquence attendue {self.last_sequence}, "
                    f"reçue {last_sequence}."
                ),
            )
        if total_samples != self._next_sample_offset:
            raise StreamProtocolError(
                "incomplete_audio",
                (
                    f"Total attendu {self._next_sample_offset} échantillons, "
                    f"reçu {total_samples}."
                ),
            )
        self._finished = True


def stream_limits() -> StreamLimits:
    max_samples = int(settings.whisper_max_duration_seconds * AUDIO_SAMPLE_RATE)
    return StreamLimits(
        max_duration_seconds=float(settings.whisper_max_duration_seconds),
        max_samples=max_samples,
        max_pcm_bytes=max_samples * PCM_BYTES_PER_SAMPLE,
        max_message_bytes=min(
            settings.whisper_stream_max_message_bytes,
            TRANSPORT_MAX_MESSAGE_BYTES,
        ),
        nominal_chunk_bytes=settings.whisper_stream_nominal_chunk_bytes,
    )


def encode_pcm_frame(sequence: int, sample_offset: int, pcm: bytes) -> bytes:
    return CHUNK_HEADER.pack(sequence, sample_offset) + pcm


def _json_message(raw: str, *, max_bytes: int) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > max_bytes:
        raise StreamProtocolError(
            "message_too_large",
            "Message de contrôle trop volumineux.",
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StreamProtocolError(
            "invalid_json",
            "Message JSON invalide.",
        ) from exc
    if not isinstance(payload, dict):
        raise StreamProtocolError(
            "invalid_message",
            "Le message de contrôle doit être un objet JSON.",
        )
    return payload


def _validate_init(payload: dict[str, Any], session: Any) -> None:
    if payload.get("type") != "init":
        raise StreamProtocolError(
            "init_required",
            "Le premier message doit être init.",
        )
    if payload.get("version") != STREAM_PROTOCOL_VERSION:
        raise StreamProtocolError(
            "unsupported_version",
            "Version du protocole non prise en charge.",
        )
    if not secure_compare(payload.get("csrf_token"), session["csrf_token"]):
        raise StreamProtocolError(
            "invalid_csrf",
            "Jeton CSRF invalide.",
        )

    audio_format = payload.get("format")
    if not isinstance(audio_format, dict):
        raise StreamProtocolError(
            "invalid_format",
            "Format audio manquant.",
        )
    expected = {
        "encoding": PCM_ENCODING,
        "sample_rate": AUDIO_SAMPLE_RATE,
        "channels": PCM_CHANNELS,
    }
    if any(audio_format.get(key) != value for key, value in expected.items()):
        raise StreamProtocolError(
            "invalid_format",
            "Le flux doit être PCM signé 16 bits little-endian, mono 16 kHz.",
        )


def _validate_finish(payload: dict[str, Any]) -> tuple[int, int]:
    if payload.get("type") != "finish":
        raise StreamProtocolError(
            "unexpected_control",
            "Seul le message finish est accepté après ready.",
        )
    last_sequence = payload.get("last_sequence")
    total_samples = payload.get("total_samples")
    if not isinstance(last_sequence, int) or last_sequence < 0:
        raise StreamProtocolError(
            "invalid_finish",
            "last_sequence doit être un entier positif ou nul.",
        )
    if not isinstance(total_samples, int) or total_samples < 0:
        raise StreamProtocolError(
            "invalid_finish",
            "total_samples doit être un entier positif ou nul.",
        )
    return last_sequence, total_samples


def pcm16le_to_float32(pcm: bytes):
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - dépend de faster-whisper
        raise TranscriptionError(
            "Le runtime numérique de transcription n'est pas disponible."
        ) from exc

    samples = np.frombuffer(pcm, dtype="<i2")
    return samples.astype(np.float32) / 32768.0


async def _receive_message(
    websocket: WebSocket,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(
            websocket.receive(),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise StreamProtocolError(
            "timeout",
            "Délai d'attente du flux dépassé.",
        ) from exc


async def _send_error(
    websocket: WebSocket,
    *,
    code: str,
    message: str,
    close_code: int = POLICY_CLOSE_CODE,
) -> None:
    try:
        await websocket.send_json(
            {
                "type": "error",
                "code": code,
                "message": message,
            }
        )
    except (RuntimeError, WebSocketDisconnect):
        pass
    try:
        await websocket.close(code=close_code)
    except (RuntimeError, WebSocketDisconnect):
        pass


def _track_inference_task(
    task: asyncio.Task[Any],
    *,
    admission_token: str,
) -> None:
    _FINALIZATION_TASKS.add(task)

    def done(completed: asyncio.Task[Any]) -> None:
        _FINALIZATION_TASKS.discard(completed)
        dictation_admission.release(admission_token)

    task.add_done_callback(done)


async def run_admitted_inference(
    call: Callable[[], Any],
    *,
    admission_token: str,
    timeout_seconds: float | None = None,
) -> Any:
    task = asyncio.create_task(asyncio.to_thread(call))
    _track_inference_task(task, admission_token=admission_token)
    awaited = asyncio.shield(task)
    if timeout_seconds is None:
        return await awaited
    try:
        return await asyncio.wait_for(awaited, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise StreamProtocolError(
            "finalization_timeout",
            "La finalisation de la dictée a dépassé le délai autorisé.",
        ) from exc


async def shutdown_transcription_stream_runtime() -> None:
    tasks = tuple(task for task in _FINALIZATION_TASKS if not task.done())
    if tasks:
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)


async def handle_transcription_websocket(websocket: WebSocket) -> None:
    if not settings.whisper_streaming_enabled:
        await websocket.close(code=POLICY_CLOSE_CODE)
        return

    if not same_origin_websocket(websocket):
        await websocket.close(code=POLICY_CLOSE_CODE)
        return

    raw_session = websocket.cookies.get(SESSION_COOKIE)
    session = get_session(raw_session)
    if session is None:
        await websocket.close(code=POLICY_CLOSE_CODE)
        return

    await websocket.accept()
    limits = stream_limits()
    admission_token: str | None = None
    inference_started = False

    try:
        init_message = await _receive_message(
            websocket,
            timeout_seconds=settings.whisper_stream_init_timeout_seconds,
        )
        if init_message.get("type") == "websocket.disconnect":
            return
        raw_init = init_message.get("text")
        if not isinstance(raw_init, str):
            raise StreamProtocolError(
                "init_required",
                "Le premier message doit être un objet JSON init.",
            )
        init_payload = _json_message(
            raw_init,
            max_bytes=limits.max_message_bytes,
        )
        _validate_init(init_payload, session)

        current_session = get_session(raw_session, touch=False)
        if current_session is None:
            raise StreamProtocolError(
                "session_expired",
                "La session n'est plus valide.",
            )

        admission_token = dictation_admission.try_acquire()
        if admission_token is None:
            await _send_error(
                websocket,
                code="busy",
                message="Une autre dictée est déjà en cours.",
                close_code=TRY_AGAIN_CLOSE_CODE,
            )
            return

        dictation_id = secrets.token_urlsafe(12)
        await websocket.send_json(
            {
                "type": "ready",
                "version": STREAM_PROTOCOL_VERSION,
                "dictation_id": dictation_id,
                "format": {
                    "encoding": PCM_ENCODING,
                    "sample_rate": AUDIO_SAMPLE_RATE,
                    "channels": PCM_CHANNELS,
                },
                "chunk_header_bytes": CHUNK_HEADER_BYTES,
                "limits": {
                    "max_duration_seconds": limits.max_duration_seconds,
                    "max_samples": limits.max_samples,
                    "max_message_bytes": limits.max_message_bytes,
                    "nominal_chunk_bytes": limits.nominal_chunk_bytes,
                },
            }
        )

        buffer = PCMStreamBuffer(limits)
        while True:
            message = await _receive_message(
                websocket,
                timeout_seconds=settings.whisper_stream_idle_timeout_seconds,
            )
            if message.get("type") == "websocket.disconnect":
                return

            if get_session(raw_session, touch=False) is None:
                raise StreamProtocolError(
                    "session_expired",
                    "La session n'est plus valide.",
                )

            binary = message.get("bytes")
            if isinstance(binary, bytes):
                buffer.append_frame(binary)
                continue

            text = message.get("text")
            if not isinstance(text, str):
                raise StreamProtocolError(
                    "invalid_message",
                    "Message WebSocket non pris en charge.",
                )
            payload = _json_message(
                text,
                max_bytes=limits.max_message_bytes,
            )
            last_sequence, total_samples = _validate_finish(payload)
            buffer.finish(
                last_sequence=last_sequence,
                total_samples=total_samples,
            )
            break

        if get_session(raw_session, touch=False) is None:
            raise StreamProtocolError(
                "session_expired",
                "La session n'est plus valide.",
            )

        samples = pcm16le_to_float32(buffer.pcm_bytes)
        token_for_job = admission_token
        inference_started = True
        result = await run_admitted_inference(
            lambda: transcribe_audio_samples(
                samples,
                profile=TranscriptionProfile.FINAL,
            ),
            admission_token=token_for_job,
            timeout_seconds=settings.whisper_stream_finalization_timeout_seconds,
        )
        admission_token = None

        if get_session(raw_session, touch=False) is None:
            raise StreamProtocolError(
                "session_expired",
                "La session n'est plus valide.",
            )

        await websocket.send_json(
            {
                "type": "final",
                "dictation_id": dictation_id,
                "text": result.text,
                "duration_seconds": round(result.duration_seconds, 3),
                "covered_samples": buffer.total_samples,
            }
        )
        await websocket.close(code=NORMAL_CLOSE_CODE)
    except StreamProtocolError as exc:
        close_code = (
            TRY_AGAIN_CLOSE_CODE
            if exc.code == "finalization_timeout"
            else POLICY_CLOSE_CODE
        )
        await _send_error(
            websocket,
            code=exc.code,
            message=str(exc),
            close_code=close_code,
        )
    except TranscriptionInputError as exc:
        await _send_error(
            websocket,
            code="unprocessable_audio",
            message=str(exc),
        )
    except TranscriptionError:
        logger.exception("Erreur du moteur Whisper pendant une dictée WebSocket")
        await _send_error(
            websocket,
            code="transcription_unavailable",
            message="Le service de transcription est temporairement indisponible.",
            close_code=INTERNAL_ERROR_CLOSE_CODE,
        )
    except (WebSocketDisconnect, asyncio.CancelledError):
        raise
    except Exception:
        logger.exception("Erreur inattendue du transport de dictée WebSocket")
        await _send_error(
            websocket,
            code="internal_error",
            message="Erreur interne du transport de dictée.",
            close_code=INTERNAL_ERROR_CLOSE_CODE,
        )
    finally:
        if admission_token is not None and not inference_started:
            dictation_admission.release(admission_token)
