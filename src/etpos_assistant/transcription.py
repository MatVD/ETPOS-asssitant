from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any

from .config import settings

logger = logging.getLogger(__name__)

AUDIO_SAMPLE_RATE = 16_000
DEFAULT_HOTWORDS_PATH = Path("config/transcription_hotwords.txt")
_MODEL_LOCK = threading.Lock()
_TRANSCRIBE_LOCK = threading.Lock()
_MODEL = None


class TranscriptionError(RuntimeError):
    """Erreur de chargement ou d'inférence du moteur de transcription."""


class TranscriptionInputError(ValueError):
    """Audio valide au niveau HTTP mais inutilisable pour la transcription."""


class TranscriptionProfile(StrEnum):
    FINAL = "final"
    PREVIEW = "preview"


@dataclass(frozen=True)
class TranscriptionSegment:
    text: str
    start_seconds: float | None = None
    end_seconds: float | None = None


@dataclass(frozen=True)
class TranscriptionTimings:
    decode_ms: float
    model_ready_ms: float
    queue_wait_ms: float
    model_call_ms: float
    segment_iteration_ms: float
    reconstruction_ms: float
    total_ms: float
    model_cached_before_call: bool
    profile: TranscriptionProfile = TranscriptionProfile.FINAL


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    duration_seconds: float
    timings: TranscriptionTimings | None = None
    segments: tuple[TranscriptionSegment, ...] = ()
    profile: TranscriptionProfile = TranscriptionProfile.FINAL


def _hotwords_path() -> Path:
    return settings.whisper_hotwords_path or DEFAULT_HOTWORDS_PATH


def load_hotwords(path: Path | None = None) -> tuple[str, ...]:
    selected = path or _hotwords_path()
    try:
        lines = selected.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        logger.warning("Fichier de hotwords Whisper absent : %s", selected)
        return ()
    except OSError as exc:
        logger.warning("Impossible de lire les hotwords Whisper %s : %s", selected, exc)
        return ()

    hotwords: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        term = raw.strip()
        if not term or term.startswith("#"):
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        hotwords.append(term)
    return tuple(hotwords)


def hotwords_prompt() -> str | None:
    hotwords = load_hotwords()
    return ", ".join(hotwords) if hotwords else None


def _model_kwargs() -> dict:
    kwargs: dict = {
        "device": settings.whisper_device,
        "compute_type": settings.whisper_compute_type,
        "cpu_threads": settings.whisper_cpu_threads,
        "local_files_only": settings.whisper_local_files_only,
    }
    if settings.whisper_download_root is not None:
        kwargs["download_root"] = str(settings.whisper_download_root)
    return kwargs


def get_transcription_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from faster_whisper import WhisperModel

            logger.info(
                "Chargement Whisper model=%s device=%s compute_type=%s",
                settings.whisper_model,
                settings.whisper_device,
                settings.whisper_compute_type,
            )
            _MODEL = WhisperModel(settings.whisper_model, **_model_kwargs())
        except Exception as exc:  # pragma: no cover - dépend de l'environnement ML
            raise TranscriptionError(
                "Le moteur de transcription Whisper n'a pas pu être chargé."
            ) from exc
    return _MODEL


def reset_transcription_model() -> None:
    """Réservé aux tests et aux outils de diagnostic."""
    global _MODEL
    with _MODEL_LOCK:
        _MODEL = None


def _normalize_profile(
    profile: TranscriptionProfile | str,
) -> TranscriptionProfile:
    try:
        return TranscriptionProfile(profile)
    except ValueError as exc:
        raise ValueError(f"Profil de transcription inconnu : {profile}") from exc


def _transcription_kwargs(profile: TranscriptionProfile) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "language": settings.whisper_language,
        "task": "transcribe",
        "beam_size": 5,
        "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": 500},
        "hotwords": hotwords_prompt(),
    }
    if profile is TranscriptionProfile.PREVIEW:
        kwargs.update(
            beam_size=1,
            temperature=0.0,
            best_of=1,
        )
    return kwargs


def _validate_audio_duration(audio: Any) -> float:
    duration = len(audio) / AUDIO_SAMPLE_RATE
    if duration < 0.15:
        raise TranscriptionInputError("L'enregistrement est trop court.")
    if duration > settings.whisper_max_duration_seconds:
        limit = settings.whisper_max_duration_seconds
        unit = "seconde" if limit == 1 else "secondes"
        raise TranscriptionInputError(
            f"L'enregistrement dépasse la limite de {limit} {unit}."
        )
    return duration


def _optional_segment_time(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _transcribe_decoded_audio(
    audio: Any,
    *,
    profile: TranscriptionProfile,
    decode_ms: float,
    total_started: float,
) -> TranscriptionResult:
    duration = _validate_audio_duration(audio)

    model_cached_before_call = _MODEL is not None
    model_started = perf_counter()
    model = get_transcription_model()
    model_ready_ms = (perf_counter() - model_started) * 1000

    queue_started = perf_counter()
    _TRANSCRIBE_LOCK.acquire()
    queue_wait_ms = (perf_counter() - queue_started) * 1000
    options: dict[str, Any] = {}
    try:
        options = _transcription_kwargs(profile)
        model_call_started = perf_counter()
        segments, _info = model.transcribe(audio, **options)
        model_call_ms = (perf_counter() - model_call_started) * 1000

        segment_iteration_started = perf_counter()
        transcript_segments: list[TranscriptionSegment] = []
        for segment in segments:
            text = getattr(segment, "text", "").strip()
            if not text:
                continue
            transcript_segments.append(
                TranscriptionSegment(
                    text=text,
                    start_seconds=_optional_segment_time(
                        getattr(segment, "start", None)
                    ),
                    end_seconds=_optional_segment_time(
                        getattr(segment, "end", None)
                    ),
                )
            )
        segment_iteration_ms = (perf_counter() - segment_iteration_started) * 1000
    except Exception as exc:  # pragma: no cover - dépend du runtime CTranslate2
        raise TranscriptionError(
            "La transcription Whisper a échoué."
        ) from exc
    finally:
        _TRANSCRIBE_LOCK.release()

    reconstruction_started = perf_counter()
    text = " ".join(segment.text for segment in transcript_segments).strip()
    reconstruction_ms = (perf_counter() - reconstruction_started) * 1000

    if not text and profile is TranscriptionProfile.FINAL:
        raise TranscriptionInputError(
            "Aucune parole exploitable n'a été détectée."
        )

    timings = TranscriptionTimings(
        decode_ms=decode_ms,
        model_ready_ms=model_ready_ms,
        queue_wait_ms=queue_wait_ms,
        model_call_ms=model_call_ms,
        segment_iteration_ms=segment_iteration_ms,
        reconstruction_ms=reconstruction_ms,
        total_ms=(perf_counter() - total_started) * 1000,
        model_cached_before_call=model_cached_before_call,
        profile=profile,
    )
    logger.info(
        "Transcription audio duration_s=%.3f decode_ms=%.1f model_ready_ms=%.1f "
        "queue_wait_ms=%.1f model_call_ms=%.1f segment_iteration_ms=%.1f "
        "reconstruction_ms=%.1f total_ms=%.1f model_cached=%s "
        "model=%s device=%s compute_type=%s cpu_threads=%s profile=%s "
        "beam_size=%s temperature=%s best_of=%s vad_filter=true",
        duration,
        timings.decode_ms,
        timings.model_ready_ms,
        timings.queue_wait_ms,
        timings.model_call_ms,
        timings.segment_iteration_ms,
        timings.reconstruction_ms,
        timings.total_ms,
        timings.model_cached_before_call,
        settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute_type,
        settings.whisper_cpu_threads,
        profile.value,
        options["beam_size"],
        options.get("temperature", "default"),
        options.get("best_of", "default"),
    )

    return TranscriptionResult(
        text=text,
        duration_seconds=duration,
        timings=timings,
        segments=tuple(transcript_segments),
        profile=profile,
    )


def transcribe_audio_samples(
    audio: Any,
    *,
    profile: TranscriptionProfile | str = TranscriptionProfile.FINAL,
) -> TranscriptionResult:
    total_started = perf_counter()
    selected_profile = _normalize_profile(profile)
    return _transcribe_decoded_audio(
        audio,
        profile=selected_profile,
        decode_ms=0.0,
        total_started=total_started,
    )


def transcribe_audio_file(path: Path) -> TranscriptionResult:
    total_started = perf_counter()
    try:
        from faster_whisper.audio import decode_audio
    except ImportError as exc:
        logger.error(
            "Dépendance faster-whisper indisponible pour la transcription: %s",
            exc.__class__.__name__,
        )
        raise TranscriptionError(
            "Le moteur de transcription Whisper n'est pas disponible."
        ) from exc

    decode_started = perf_counter()
    try:
        audio = decode_audio(str(path), sampling_rate=AUDIO_SAMPLE_RATE)
    except Exception as exc:
        logger.warning(
            "Décodage audio impossible path_suffix=%s error_type=%s",
            path.suffix.lower(),
            exc.__class__.__name__,
        )
        raise TranscriptionInputError(
            "Le fichier audio n'a pas pu être décodé."
        ) from exc
    decode_ms = (perf_counter() - decode_started) * 1000

    return _transcribe_decoded_audio(
        audio,
        profile=TranscriptionProfile.FINAL,
        decode_ms=decode_ms,
        total_started=total_started,
    )
