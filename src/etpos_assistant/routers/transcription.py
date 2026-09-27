from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request, status

from ..config import settings
from ..transcription import (
    TranscriptionError,
    TranscriptionInputError,
    transcribe_audio_file,
)
from .deps import require_api_session, require_csrf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

ALLOWED_AUDIO_TYPES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
}


def _content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _store_limited_audio(request: Request, suffix: str) -> Path:
    declared = _declared_length(request)
    if declared is not None and declared > settings.whisper_max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Enregistrement audio trop volumineux.",
        )

    total = 0
    temp = tempfile.NamedTemporaryFile(
        prefix="etpos-voice-",
        suffix=suffix,
        delete=False,
    )
    path = Path(temp.name)
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > settings.whisper_max_upload_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Enregistrement audio trop volumineux.",
                )
            temp.write(chunk)
    except Exception:
        temp.close()
        path.unlink(missing_ok=True)
        raise
    else:
        temp.close()

    if total == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Enregistrement audio vide.",
        )
    return path


@router.post("/transcribe")
async def transcribe(request: Request):
    session = require_api_session(request)
    require_csrf(request, session)

    content_type = _content_type(request)
    suffix = ALLOWED_AUDIO_TYPES.get(content_type)
    if suffix is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Format audio non pris en charge.",
        )

    request_started = perf_counter()
    upload_started = perf_counter()
    path = await _store_limited_audio(request, suffix)
    upload_ms = (perf_counter() - upload_started) * 1000
    upload_bytes = path.stat().st_size
    try:
        processing_started = perf_counter()
        result = await asyncio.to_thread(transcribe_audio_file, path)
        processing_ms = (perf_counter() - processing_started) * 1000
    except TranscriptionInputError as exc:
        logger.info(
            "Transcription refusée status=422 content_type=%s upload_bytes=%s "
            "upload_ms=%.1f total_ms=%.1f detail=%s",
            content_type,
            upload_bytes,
            upload_ms,
            (perf_counter() - request_started) * 1000,
            str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except TranscriptionError as exc:
        logger.exception(
            "Erreur du moteur Whisper content_type=%s upload_bytes=%s upload_ms=%.1f total_ms=%.1f",
            content_type,
            upload_bytes,
            upload_ms,
            (perf_counter() - request_started) * 1000,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Le service de transcription est temporairement indisponible.",
        ) from exc
    finally:
        path.unlink(missing_ok=True)

    response_started = perf_counter()
    response = {
        "text": result.text,
        "duration_seconds": round(result.duration_seconds, 3),
    }
    response_build_ms = (perf_counter() - response_started) * 1000
    logger.info(
        "Transcription HTTP status=200 content_type=%s upload_bytes=%s upload_ms=%.1f "
        "processing_ms=%.1f response_build_ms=%.3f total_ms=%.1f",
        content_type,
        upload_bytes,
        upload_ms,
        processing_ms,
        response_build_ms,
        (perf_counter() - request_started) * 1000,
    )
    return response
