from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    duration_seconds: float


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


def transcribe_audio_file(path: Path) -> TranscriptionResult:
    try:
        from faster_whisper.audio import decode_audio

        audio = decode_audio(str(path), sampling_rate=AUDIO_SAMPLE_RATE)
    except Exception as exc:
        raise TranscriptionInputError(
            "Le fichier audio n'a pas pu être décodé."
        ) from exc

    duration = len(audio) / AUDIO_SAMPLE_RATE
    if duration < 0.15:
        raise TranscriptionInputError("L'enregistrement est trop court.")
    if duration > settings.whisper_max_duration_seconds:
        limit = settings.whisper_max_duration_seconds
        unit = "seconde" if limit == 1 else "secondes"
        raise TranscriptionInputError(
            f"L'enregistrement dépasse la limite de {limit} {unit}."
        )

    model = get_transcription_model()
    try:
        with _TRANSCRIBE_LOCK:
            segments, _info = model.transcribe(
                audio,
                language=settings.whisper_language,
                task="transcribe",
                beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                hotwords=hotwords_prompt(),
            )
            text = " ".join(
                segment.text.strip()
                for segment in segments
                if getattr(segment, "text", "").strip()
            ).strip()
    except Exception as exc:  # pragma: no cover - dépend du runtime CTranslate2
        raise TranscriptionError(
            "La transcription Whisper a échoué."
        ) from exc

    if not text:
        raise TranscriptionInputError(
            "Aucune parole exploitable n'a été détectée."
        )

    return TranscriptionResult(text=text, duration_seconds=duration)
