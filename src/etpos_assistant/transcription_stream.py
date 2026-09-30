from __future__ import annotations

import asyncio
import json
import logging
import secrets
import struct
import threading
from dataclasses import dataclass
from time import perf_counter
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
    TranscriptionResult,
    transcribe_audio_samples,
)
from .transcription_vad import SileroPauseDetector, silero_contains_speech

logger = logging.getLogger(__name__)

STREAM_PROTOCOL_VERSION = 1
PCM_ENCODING = "pcm_s16le"
PCM_CHANNELS = 1
PCM_BYTES_PER_SAMPLE = 2
CHUNK_HEADER = struct.Struct("<IQ")
CHUNK_HEADER_BYTES = CHUNK_HEADER.size
TRANSPORT_MAX_MESSAGE_BYTES = 16 * 1024
TRANSPORT_MAX_QUEUE_MESSAGES = 4
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
_INFERENCE_TASKS: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True)
class StreamLimits:
    max_duration_seconds: float
    max_samples: int
    max_pcm_bytes: int
    max_message_bytes: int
    nominal_chunk_bytes: int
    max_queue_messages: int = TRANSPORT_MAX_QUEUE_MESSAGES
    finalization_timeout_seconds: float = 120.0


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

    def pcm_slice(self, start_sample: int, end_sample: int) -> bytes:
        if start_sample < 0 or end_sample < start_sample:
            raise ValueError("Plage PCM invalide.")
        if end_sample > self._next_sample_offset:
            raise ValueError("La plage PCM dépasse l'audio reçu.")
        start_byte = start_sample * PCM_BYTES_PER_SAMPLE
        end_byte = end_sample * PCM_BYTES_PER_SAMPLE
        return bytes(self._pcm[start_byte:end_byte])

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
        max_queue_messages=TRANSPORT_MAX_QUEUE_MESSAGES,
        finalization_timeout_seconds=settings.whisper_stream_finalization_timeout_seconds,
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


@dataclass(frozen=True)
class PreviewPolicy:
    first_samples: int
    interval_samples: int
    suspend_samples: int

    @classmethod
    def from_seconds(
        cls,
        *,
        first_seconds: float,
        interval_seconds: float,
        suspend_seconds: float,
    ) -> "PreviewPolicy":
        if first_seconds <= 0 or interval_seconds <= 0 or suspend_seconds <= 0:
            raise ValueError("Les paramètres d'aperçu doivent être strictement positifs.")
        if suspend_seconds < first_seconds:
            raise ValueError(
                "La suspension des aperçus ne peut pas précéder le premier aperçu."
            )
        return cls(
            first_samples=max(1, int(round(first_seconds * AUDIO_SAMPLE_RATE))),
            interval_samples=max(1, int(round(interval_seconds * AUDIO_SAMPLE_RATE))),
            suspend_samples=max(1, int(round(suspend_seconds * AUDIO_SAMPLE_RATE))),
        )


def preview_policy() -> PreviewPolicy:
    return PreviewPolicy.from_seconds(
        first_seconds=settings.whisper_stream_preview_first_seconds,
        interval_seconds=settings.whisper_stream_preview_interval_seconds,
        suspend_seconds=settings.whisper_stream_preview_suspend_seconds,
    )


@dataclass(frozen=True)
class PreviewRequest:
    pcm: bytes
    total_samples: int
    requested_at: float


@dataclass(frozen=True)
class PreviewOutcome:
    total_samples: int
    text: str
    elapsed_ms: float
    error: str | None = None


@dataclass(frozen=True)
class PortionRequest:
    pcm: bytes
    start_sample: int
    end_sample: int
    requested_at: float


@dataclass(frozen=True)
class PortionOutcome:
    start_sample: int
    end_sample: int
    text: str
    elapsed_ms: float
    error: str | None = None


@dataclass(frozen=True)
class FinalizedPortion:
    start_sample: int
    end_sample: int
    text: str
    elapsed_ms: float


@dataclass(frozen=True)
class PartialUpdate:
    revision: int
    text: str
    covered_samples: int


@dataclass
class PreviewMetrics:
    preview_requests: int = 0
    preview_started: int = 0
    preview_replaced: int = 0
    preview_obsolete_results: int = 0
    preview_emitted: int = 0
    preview_empty: int = 0
    preview_errors: int = 0
    pending_dropped_on_finish: int = 0
    pending_dropped_on_suspend: int = 0
    preview_total_ms: float = 0.0
    preview_queue_wait_max_ms: float = 0.0
    final_wait_for_preview_ms: float = 0.0
    max_audio_lag_seconds: float = 0.0
    preview_suspended: bool = False


class PreviewScheduler:
    def __init__(
        self,
        *,
        policy: PreviewPolicy,
        engine: Callable[..., TranscriptionResult] | None = None,
        converter: Callable[[bytes], Any] | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.policy = policy
        self.engine = engine or transcribe_audio_samples
        self.converter = converter or pcm16le_to_float32
        self.clock = clock
        self.metrics = PreviewMetrics()
        self._active_task: asyncio.Task[Any] | None = None
        self._active_request: PreviewRequest | PortionRequest | None = None
        self._active_kind: str | None = None
        self._pending: PreviewRequest | None = None
        self._last_requested_samples: int | None = None
        self._revision = 0
        self._finalizing = False
        self._completion_event = asyncio.Event()
        self._finalized_portions: list[FinalizedPortion] = []
        self._portion_error: str | None = None
        self._portion_total_ms = 0.0

    @property
    def has_active_inference(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    @property
    def has_tracked_inference(self) -> bool:
        return self._active_task is not None

    @property
    def pending_samples(self) -> int | None:
        return self._pending.total_samples if self._pending is not None else None

    @property
    def finalized_portions(self) -> tuple[FinalizedPortion, ...]:
        return tuple(self._finalized_portions)

    @property
    def finalized_coverage_samples(self) -> int:
        if not self._finalized_portions:
            return 0
        return self._finalized_portions[-1].end_sample

    @property
    def portion_error(self) -> str | None:
        return self._portion_error

    def drop_pending_preview(self) -> None:
        if self._pending is not None:
            self._pending = None
            self.metrics.pending_dropped_on_finish += 1

    def start_portion(
        self,
        pcm: bytes,
        *,
        start_sample: int,
        end_sample: int,
    ) -> bool:
        if self._finalizing or self._active_task is not None:
            return False
        if start_sample < 0 or end_sample <= start_sample:
            raise ValueError("Portion finale invalide.")
        if start_sample != self.finalized_coverage_samples:
            raise ValueError("Les portions finales doivent être contiguës.")

        request = PortionRequest(
            pcm=bytes(pcm),
            start_sample=start_sample,
            end_sample=end_sample,
            requested_at=self.clock(),
        )
        task = asyncio.create_task(asyncio.to_thread(self._run_portion, request))
        _track_inference_task(task)
        self._active_task = task
        self._active_request = request
        self._active_kind = "portion"
        self._completion_event.clear()
        task.add_done_callback(lambda _task: self._completion_event.set())
        if self._pending is not None:
            self._pending = None
            self.metrics.pending_dropped_on_finish += 1
        return True

    def consider_snapshot(self, pcm: bytes, *, total_samples: int) -> bool:
        if self._finalizing or total_samples <= 0:
            return False

        if total_samples > self.policy.suspend_samples:
            self.metrics.preview_suspended = True
            if self._pending is not None:
                self._pending = None
                self.metrics.pending_dropped_on_suspend += 1
            return False

        if self._last_requested_samples is None:
            if total_samples < self.policy.first_samples:
                return False
        elif total_samples - self._last_requested_samples < self.policy.interval_samples:
            return False

        request = PreviewRequest(
            pcm=bytes(pcm),
            total_samples=total_samples,
            requested_at=self.clock(),
        )
        self.metrics.preview_requests += 1
        self._last_requested_samples = total_samples

        if self._active_task is None:
            self._start(request)
        else:
            if self._pending is not None:
                self.metrics.preview_replaced += 1
            self._pending = request
        return True

    def _start(self, request: PreviewRequest) -> None:
        if self._finalizing:
            return
        self.metrics.preview_started += 1
        queue_wait_ms = max(0.0, (self.clock() - request.requested_at) * 1000)
        self.metrics.preview_queue_wait_max_ms = max(
            self.metrics.preview_queue_wait_max_ms,
            queue_wait_ms,
        )
        task = asyncio.create_task(asyncio.to_thread(self._run_preview, request))
        _track_inference_task(task)
        self._active_task = task
        self._active_request = request
        self._active_kind = "preview"
        self._completion_event.clear()
        task.add_done_callback(lambda _task: self._completion_event.set())

    def _run_preview(self, request: PreviewRequest) -> PreviewOutcome:
        started = self.clock()
        try:
            samples = self.converter(request.pcm)
            result = self.engine(samples, profile=TranscriptionProfile.PREVIEW)
        except TranscriptionError as exc:
            return PreviewOutcome(
                total_samples=request.total_samples,
                text="",
                elapsed_ms=max(0.0, (self.clock() - started) * 1000),
                error=str(exc),
            )
        return PreviewOutcome(
            total_samples=request.total_samples,
            text=result.text,
            elapsed_ms=max(0.0, (self.clock() - started) * 1000),
        )

    def _run_portion(self, request: PortionRequest) -> PortionOutcome:
        started = self.clock()
        try:
            samples = self.converter(request.pcm)
            result = self.engine(samples, profile=TranscriptionProfile.FINAL)
        except (TranscriptionError, TranscriptionInputError) as exc:
            return PortionOutcome(
                start_sample=request.start_sample,
                end_sample=request.end_sample,
                text="",
                elapsed_ms=max(0.0, (self.clock() - started) * 1000),
                error=str(exc),
            )
        return PortionOutcome(
            start_sample=request.start_sample,
            end_sample=request.end_sample,
            text=result.text,
            elapsed_ms=max(0.0, (self.clock() - started) * 1000),
        )

    async def wait_for_completion(self) -> None:
        await self._completion_event.wait()

    def collect_completed(self, *, current_total_samples: int) -> PartialUpdate | None:
        task = self._active_task
        request = self._active_request
        kind = self._active_kind
        if task is None or request is None or kind is None or not task.done():
            return None

        self._active_task = None
        self._active_request = None
        self._active_kind = None
        self._completion_event.clear()

        if kind == "portion":
            assert isinstance(request, PortionRequest)
            try:
                outcome = task.result()
            except Exception as exc:
                outcome = PortionOutcome(
                    start_sample=request.start_sample,
                    end_sample=request.end_sample,
                    text="",
                    elapsed_ms=0.0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            assert isinstance(outcome, PortionOutcome)
            self._portion_total_ms += outcome.elapsed_ms
            if outcome.error is not None:
                self._portion_error = outcome.error
                logger.warning(
                    "Portion finale Whisper ignorée start_sample=%s end_sample=%s error=%s",
                    outcome.start_sample,
                    outcome.end_sample,
                    outcome.error,
                )
            elif outcome.start_sample != self.finalized_coverage_samples:
                self._portion_error = "Couverture non contiguë des portions finales."
            else:
                self._finalized_portions.append(
                    FinalizedPortion(
                        start_sample=outcome.start_sample,
                        end_sample=outcome.end_sample,
                        text=outcome.text,
                        elapsed_ms=outcome.elapsed_ms,
                    )
                )
            return None

        assert isinstance(request, PreviewRequest)
        try:
            outcome = task.result()
        except Exception as exc:  # la finale doit rester disponible même si un aperçu échoue
            outcome = PreviewOutcome(
                total_samples=request.total_samples,
                text="",
                elapsed_ms=0.0,
                error=f"{type(exc).__name__}: {exc}",
            )
        assert isinstance(outcome, PreviewOutcome)

        self.metrics.preview_total_ms += outcome.elapsed_ms
        lag_seconds = max(
            0.0,
            (current_total_samples - outcome.total_samples) / AUDIO_SAMPLE_RATE,
        )
        self.metrics.max_audio_lag_seconds = max(
            self.metrics.max_audio_lag_seconds,
            lag_seconds,
        )

        obsolete = (
            self._finalizing
            or (
                self._pending is not None
                and self._pending.total_samples > outcome.total_samples
            )
            or (
                self.metrics.preview_suspended
                and current_total_samples > outcome.total_samples
            )
        )
        update: PartialUpdate | None = None
        if outcome.error is not None:
            self.metrics.preview_errors += 1
            logger.warning(
                "Aperçu Whisper ignoré covered_samples=%s error=%s",
                outcome.total_samples,
                outcome.error,
            )
        elif obsolete:
            self.metrics.preview_obsolete_results += 1
        else:
            self._revision += 1
            self.metrics.preview_emitted += 1
            if not outcome.text:
                self.metrics.preview_empty += 1
            update = PartialUpdate(
                revision=self._revision,
                text=outcome.text,
                covered_samples=outcome.total_samples,
            )

        pending = self._pending
        self._pending = None
        if pending is not None and not self._finalizing:
            self._start(pending)
        return update

    def begin_finalization(self) -> None:
        if self._finalizing:
            return
        self._finalizing = True
        if self._pending is not None:
            self._pending = None
            self.metrics.pending_dropped_on_finish += 1

    async def wait_for_active_before_final(
        self,
        *,
        current_total_samples: int,
        timeout_seconds: float,
    ) -> None:
        self.begin_finalization()
        task = self._active_task
        if task is None:
            return

        started = self.clock()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except TimeoutError as exc:
            self.metrics.final_wait_for_preview_ms = max(
                0.0,
                (self.clock() - started) * 1000,
            )
            raise StreamProtocolError(
                "finalization_timeout",
                "Un aperçu Whisper en cours n'a pas terminé avant le délai de finalisation.",
            ) from exc

        self.metrics.final_wait_for_preview_ms = max(
            0.0,
            (self.clock() - started) * 1000,
        )
        self.collect_completed(current_total_samples=current_total_samples)

    def abandon_and_release_admission(self, admission_token: str) -> None:
        self.begin_finalization()
        task = self._active_task
        if task is None or task.done():
            dictation_admission.release(admission_token)
            return

        task.add_done_callback(
            lambda _task: dictation_admission.release(admission_token)
        )

    def metrics_snapshot(self, *, final_ms: float | None = None) -> dict[str, Any]:
        return {
            "preview_requests": self.metrics.preview_requests,
            "preview_started": self.metrics.preview_started,
            "preview_replaced": self.metrics.preview_replaced,
            "preview_obsolete_results": self.metrics.preview_obsolete_results,
            "preview_emitted": self.metrics.preview_emitted,
            "preview_empty": self.metrics.preview_empty,
            "preview_errors": self.metrics.preview_errors,
            "pending_dropped_on_finish": self.metrics.pending_dropped_on_finish,
            "pending_dropped_on_suspend": self.metrics.pending_dropped_on_suspend,
            "preview_queue_wait_max_ms": round(
                self.metrics.preview_queue_wait_max_ms,
                3,
            ),
            "final_wait_for_preview_ms": round(
                self.metrics.final_wait_for_preview_ms,
                3,
            ),
            "max_audio_lag_seconds": round(
                self.metrics.max_audio_lag_seconds,
                3,
            ),
            "preview_suspended": self.metrics.preview_suspended,
            "finalized_portions": len(self._finalized_portions),
            "finalized_coverage_samples": self.finalized_coverage_samples,
            "portion_error": self._portion_error,
            "profile_cost_ms": {
                "preview": round(self.metrics.preview_total_ms, 3),
                "portion_final": round(self._portion_total_ms, 3),
                "terminal_final": round(final_ms, 3) if final_ms is not None else None,
            },
        }


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
    admission_token: str | None = None,
) -> None:
    _INFERENCE_TASKS.add(task)

    def done(completed: asyncio.Task[Any]) -> None:
        _INFERENCE_TASKS.discard(completed)
        if admission_token is not None:
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
    tasks = tuple(task for task in _INFERENCE_TASKS if not task.done())
    if tasks:
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)


def finalize_stream_audio(
    buffer: PCMStreamBuffer,
    scheduler: PreviewScheduler,
    *,
    pause_enabled: bool,
) -> tuple[TranscriptionResult, str]:
    def global_final() -> tuple[TranscriptionResult, str]:
        return (
            transcribe_audio_samples(
                pcm16le_to_float32(buffer.pcm_bytes),
                profile=TranscriptionProfile.FINAL,
            ),
            "global",
        )

    portions = scheduler.finalized_portions
    if (
        not pause_enabled
        or not portions
        or scheduler.portion_error is not None
        or portions[0].start_sample != 0
    ):
        return global_final()

    expected_start = 0
    for portion in portions:
        if portion.start_sample != expected_start or portion.end_sample <= portion.start_sample:
            return global_final()
        expected_start = portion.end_sample

    coverage = expected_start
    if coverage > buffer.total_samples:
        return global_final()

    texts = [portion.text for portion in portions if portion.text.strip()]
    if coverage == buffer.total_samples:
        text = " ".join(texts).strip()
        if not text:
            return global_final()
        return (
            TranscriptionResult(
                text=text,
                duration_seconds=buffer.total_samples / AUDIO_SAMPLE_RATE,
                profile=TranscriptionProfile.FINAL,
            ),
            "portions",
        )

    remainder_pcm = buffer.pcm_slice(coverage, buffer.total_samples)
    remainder_samples = pcm16le_to_float32(remainder_pcm)
    if not silero_contains_speech(remainder_samples):
        text = " ".join(texts).strip()
        if not text:
            return global_final()
        return (
            TranscriptionResult(
                text=text,
                duration_seconds=buffer.total_samples / AUDIO_SAMPLE_RATE,
                profile=TranscriptionProfile.FINAL,
            ),
            "portions",
        )

    if len(remainder_samples) < int(0.15 * AUDIO_SAMPLE_RATE):
        return global_final()

    try:
        remainder = transcribe_audio_samples(
            remainder_samples,
            profile=TranscriptionProfile.FINAL,
        )
    except (TranscriptionError, TranscriptionInputError):
        return global_final()

    if remainder.text.strip():
        texts.append(remainder.text)
    text = " ".join(texts).strip()
    if not text:
        return global_final()

    return (
        TranscriptionResult(
            text=text,
            duration_seconds=buffer.total_samples / AUDIO_SAMPLE_RATE,
            profile=TranscriptionProfile.FINAL,
        ),
        "portions",
    )


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
    scheduler: PreviewScheduler | None = None
    receive_task: asyncio.Task[dict[str, Any]] | None = None

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
                    "max_queue_messages": limits.max_queue_messages,
                    "finalization_timeout_seconds": limits.finalization_timeout_seconds,
                },
            }
        )

        buffer = PCMStreamBuffer(limits)
        scheduler = PreviewScheduler(policy=preview_policy())
        pause_enabled = bool(
            getattr(settings, "whisper_stream_pause_finalization_enabled", False)
        )
        pause_detector = (
            SileroPauseDetector(
                pause_ms=int(getattr(settings, "whisper_stream_pause_ms", 600))
            )
            if pause_enabled
            else None
        )
        min_portion_samples = max(
            1,
            int(
                round(
                    float(
                        getattr(
                            settings,
                            "whisper_stream_min_portion_seconds",
                            2.0,
                        )
                    )
                    * AUDIO_SAMPLE_RATE
                )
            ),
        )
        pending_boundary_sample: int | None = None
        finished = False

        while not finished:
            if receive_task is None:
                receive_task = asyncio.create_task(
                    _receive_message(
                        websocket,
                        timeout_seconds=settings.whisper_stream_idle_timeout_seconds,
                    )
                )

            completion_task: asyncio.Task[None] | None = None
            wait_for: set[asyncio.Task[Any]] = {receive_task}
            if scheduler.has_tracked_inference:
                completion_task = asyncio.create_task(scheduler.wait_for_completion())
                wait_for.add(completion_task)

            done, _pending = await asyncio.wait(
                wait_for,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if receive_task in done:
                message = receive_task.result()
                receive_task = None

                if message.get("type") == "websocket.disconnect":
                    scheduler.begin_finalization()
                    return

                if get_session(raw_session, touch=False) is None:
                    raise StreamProtocolError(
                        "session_expired",
                        "La session n'est plus valide.",
                    )

                binary = message.get("bytes")
                if isinstance(binary, bytes):
                    buffer.append_frame(binary)

                    if pause_detector is not None and scheduler.portion_error is None:
                        window_start = max(
                            0,
                            buffer.total_samples - pause_detector.window_samples,
                        )
                        window_pcm = buffer.pcm_slice(
                            window_start,
                            buffer.total_samples,
                        )
                        confirmed = pause_detector.consider_window(
                            pcm16le_to_float32(window_pcm),
                            window_start_sample=window_start,
                            total_samples=buffer.total_samples,
                            minimum_boundary_sample=scheduler.finalized_coverage_samples,
                        )
                        if (
                            confirmed is not None
                            and confirmed.boundary_sample
                            - scheduler.finalized_coverage_samples
                            >= min_portion_samples
                        ):
                            pending_boundary_sample = max(
                                pending_boundary_sample or 0,
                                confirmed.boundary_sample,
                            )
                            scheduler.drop_pending_preview()

                    if (
                        pending_boundary_sample is not None
                        and not scheduler.has_tracked_inference
                        and scheduler.portion_error is None
                    ):
                        portion_start = scheduler.finalized_coverage_samples
                        portion_end = pending_boundary_sample
                        if scheduler.start_portion(
                            buffer.pcm_slice(portion_start, portion_end),
                            start_sample=portion_start,
                            end_sample=portion_end,
                        ):
                            pending_boundary_sample = None
                    elif pending_boundary_sample is None:
                        scheduler.consider_snapshot(
                            buffer.pcm_bytes,
                            total_samples=buffer.total_samples,
                        )
                else:
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
                    scheduler.begin_finalization()
                    finished = True

            update = scheduler.collect_completed(
                current_total_samples=buffer.total_samples,
            )
            if (
                not finished
                and pending_boundary_sample is not None
                and not scheduler.has_tracked_inference
                and scheduler.portion_error is None
            ):
                portion_start = scheduler.finalized_coverage_samples
                portion_end = pending_boundary_sample
                if scheduler.start_portion(
                    buffer.pcm_slice(portion_start, portion_end),
                    start_sample=portion_start,
                    end_sample=portion_end,
                ):
                    pending_boundary_sample = None

            if update is not None and not finished:
                await websocket.send_json(
                    {
                        "type": "partial",
                        "dictation_id": dictation_id,
                        "revision": update.revision,
                        "text": update.text,
                        "covered_samples": update.covered_samples,
                    }
                )

            if completion_task is not None and not completion_task.done():
                completion_task.cancel()
                await asyncio.gather(completion_task, return_exceptions=True)

        await scheduler.wait_for_active_before_final(
            current_total_samples=buffer.total_samples,
            timeout_seconds=settings.whisper_stream_finalization_timeout_seconds,
        )

        if get_session(raw_session, touch=False) is None:
            raise StreamProtocolError(
                "session_expired",
                "La session n'est plus valide.",
            )

        assert admission_token is not None
        token_for_job = admission_token
        admission_token = None
        final_started = perf_counter()
        result, final_mode = await run_admitted_inference(
            lambda: finalize_stream_audio(
                buffer,
                scheduler,
                pause_enabled=pause_enabled,
            ),
            admission_token=token_for_job,
            timeout_seconds=settings.whisper_stream_finalization_timeout_seconds,
        )
        final_ms = max(0.0, (perf_counter() - final_started) * 1000)

        if get_session(raw_session, touch=False) is None:
            raise StreamProtocolError(
                "session_expired",
                "La session n'est plus valide.",
            )

        metrics = scheduler.metrics_snapshot(final_ms=final_ms)
        logger.info(
            "Dictée WebSocket terminée dictation_id=%s covered_samples=%s "
            "preview_requests=%s preview_started=%s preview_replaced=%s "
            "preview_emitted=%s preview_obsolete=%s preview_errors=%s "
            "preview_wait_max_ms=%s final_wait_for_preview_ms=%s "
            "max_audio_lag_seconds=%s finalized_portions=%s "
            "finalized_coverage_samples=%s final_mode=%s "
            "preview_cost_ms=%s portion_final_cost_ms=%s terminal_final_cost_ms=%s",
            dictation_id,
            buffer.total_samples,
            metrics["preview_requests"],
            metrics["preview_started"],
            metrics["preview_replaced"],
            metrics["preview_emitted"],
            metrics["preview_obsolete_results"],
            metrics["preview_errors"],
            metrics["preview_queue_wait_max_ms"],
            metrics["final_wait_for_preview_ms"],
            metrics["max_audio_lag_seconds"],
            metrics["finalized_portions"],
            metrics["finalized_coverage_samples"],
            final_mode,
            metrics["profile_cost_ms"]["preview"],
            metrics["profile_cost_ms"]["portion_final"],
            metrics["profile_cost_ms"]["terminal_final"],
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
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
            await asyncio.gather(receive_task, return_exceptions=True)

        if admission_token is not None:
            if scheduler is not None and scheduler.has_active_inference:
                scheduler.abandon_and_release_admission(admission_token)
            else:
                dictation_admission.release(admission_token)
