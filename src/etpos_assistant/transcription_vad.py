from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .transcription import AUDIO_SAMPLE_RATE, TranscriptionError

DEFAULT_PAUSE_MS = 600
DEFAULT_WINDOW_SECONDS = 3.0


@dataclass(frozen=True)
class ConfirmedPause:
    speech_end_sample: int
    boundary_sample: int
    confirmed_at_sample: int
    silence_samples: int


TimestampDetector = Callable[..., Sequence[dict[str, int]]]


class SileroPauseDetector:
    """Détecte uniquement des pauses confirmées dans de l'audio déjà reçu."""

    def __init__(
        self,
        *,
        pause_ms: int = DEFAULT_PAUSE_MS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        timestamp_detector: TimestampDetector | None = None,
    ) -> None:
        if pause_ms <= 0:
            raise ValueError("pause_ms doit être strictement positif.")
        if window_seconds <= 0:
            raise ValueError("window_seconds doit être strictement positif.")

        self.pause_ms = int(pause_ms)
        self.pause_samples = max(
            1,
            int(round(self.pause_ms * AUDIO_SAMPLE_RATE / 1000)),
        )
        requested_window = int(round(window_seconds * AUDIO_SAMPLE_RATE))
        self.window_samples = max(
            requested_window,
            self.pause_samples + AUDIO_SAMPLE_RATE,
        )
        self._timestamp_detector = timestamp_detector
        self._last_boundary_sample = -1

    def _speech_timestamps(self, audio: Any) -> Sequence[dict[str, int]]:
        try:
            if self._timestamp_detector is not None:
                return self._timestamp_detector(
                    audio,
                    pause_ms=self.pause_ms,
                    sampling_rate=AUDIO_SAMPLE_RATE,
                )

            from faster_whisper.vad import VadOptions, get_speech_timestamps

            return get_speech_timestamps(
                audio,
                VadOptions(
                    min_speech_duration_ms=0,
                    min_silence_duration_ms=self.pause_ms,
                    speech_pad_ms=0,
                ),
                sampling_rate=AUDIO_SAMPLE_RATE,
            )
        except Exception as exc:
            raise TranscriptionError(
                "Le détecteur de pauses Silero n'est pas disponible."
            ) from exc

    def consider_window(
        self,
        audio: Any,
        *,
        window_start_sample: int,
        total_samples: int,
        minimum_boundary_sample: int = 0,
    ) -> ConfirmedPause | None:
        if window_start_sample < 0 or total_samples < 0:
            raise ValueError("Les positions audio doivent être positives.")

        audio_samples = len(audio)
        if window_start_sample + audio_samples != total_samples:
            raise ValueError(
                "La fenêtre VAD doit se terminer à la position audio courante."
            )
        if audio_samples == 0:
            return None

        speeches = tuple(self._speech_timestamps(audio))
        if not speeches:
            return None

        candidates: list[ConfirmedPause] = []

        for previous, following in zip(speeches, speeches[1:]):
            speech_end = int(previous["end"])
            next_start = int(following["start"])
            silence_samples = next_start - speech_end
            if silence_samples < self.pause_samples:
                continue
            boundary_local = speech_end + self.pause_samples // 2
            candidates.append(
                ConfirmedPause(
                    speech_end_sample=window_start_sample + speech_end,
                    boundary_sample=window_start_sample + boundary_local,
                    confirmed_at_sample=window_start_sample + next_start,
                    silence_samples=silence_samples,
                )
            )

        last_speech_end = int(speeches[-1]["end"])
        trailing_silence = audio_samples - last_speech_end
        if trailing_silence >= self.pause_samples:
            boundary_local = last_speech_end + self.pause_samples // 2
            candidates.append(
                ConfirmedPause(
                    speech_end_sample=window_start_sample + last_speech_end,
                    boundary_sample=window_start_sample + boundary_local,
                    confirmed_at_sample=total_samples,
                    silence_samples=trailing_silence,
                )
            )

        floor = max(self._last_boundary_sample, minimum_boundary_sample)
        eligible = [
            candidate
            for candidate in candidates
            if candidate.boundary_sample > floor
            and candidate.boundary_sample < total_samples
        ]
        if not eligible:
            return None

        selected = max(eligible, key=lambda candidate: candidate.boundary_sample)
        self._last_boundary_sample = selected.boundary_sample
        return selected

    def consider_audio(
        self,
        audio: Any,
        *,
        total_samples: int,
        minimum_boundary_sample: int = 0,
    ) -> ConfirmedPause | None:
        if len(audio) != total_samples:
            raise ValueError(
                "consider_audio attend l'audio complet jusqu'à total_samples."
            )
        window_start = max(0, total_samples - self.window_samples)
        return self.consider_window(
            audio[window_start:total_samples],
            window_start_sample=window_start,
            total_samples=total_samples,
            minimum_boundary_sample=minimum_boundary_sample,
        )


def silero_contains_speech(
    audio: Any,
    *,
    timestamp_detector: TimestampDetector | None = None,
) -> bool:
    if len(audio) == 0:
        return False

    try:
        if timestamp_detector is not None:
            speeches = timestamp_detector(
                audio,
                pause_ms=100,
                sampling_rate=AUDIO_SAMPLE_RATE,
            )
        else:
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            speeches = get_speech_timestamps(
                audio,
                VadOptions(
                    min_speech_duration_ms=0,
                    min_silence_duration_ms=100,
                    speech_pad_ms=0,
                ),
                sampling_rate=AUDIO_SAMPLE_RATE,
            )
    except Exception as exc:
        raise TranscriptionError(
            "Le détecteur de parole Silero n'est pas disponible."
        ) from exc

    return bool(speeches)
