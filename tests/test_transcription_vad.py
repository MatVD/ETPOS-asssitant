from __future__ import annotations

from etpos_assistant.transcription_vad import (
    SileroPauseDetector,
    silero_contains_speech,
)


def test_pause_detector_does_not_treat_buffer_end_as_confirmed_silence():
    def timestamps(audio, **_kwargs):
        return [{"start": 0, "end": len(audio)}]

    detector = SileroPauseDetector(
        pause_ms=500,
        timestamp_detector=timestamps,
    )
    audio = [0.0] * 16000

    assert (
        detector.consider_window(
            audio,
            window_start_sample=0,
            total_samples=len(audio),
        )
        is None
    )


def test_pause_detector_requires_real_received_silence_after_speech():
    def timestamps(_audio, **_kwargs):
        return [{"start": 0, "end": 6000}]

    detector = SileroPauseDetector(
        pause_ms=500,
        timestamp_detector=timestamps,
    )

    too_early = [0.0] * 13000
    assert (
        detector.consider_window(
            too_early,
            window_start_sample=0,
            total_samples=len(too_early),
        )
        is None
    )

    confirmed = [0.0] * 16000
    pause = detector.consider_window(
        confirmed,
        window_start_sample=0,
        total_samples=len(confirmed),
    )

    assert pause is not None
    assert pause.speech_end_sample == 6000
    assert pause.boundary_sample == 10000
    assert pause.confirmed_at_sample == 16000
    assert pause.silence_samples == 10000


def test_pause_detector_can_confirm_gap_after_speech_resumes():
    def timestamps(_audio, **_kwargs):
        return [
            {"start": 0, "end": 4000},
            {"start": 14000, "end": 18000},
        ]

    detector = SileroPauseDetector(
        pause_ms=500,
        timestamp_detector=timestamps,
    )
    audio = [0.0] * 18000

    pause = detector.consider_window(
        audio,
        window_start_sample=0,
        total_samples=len(audio),
    )

    assert pause is not None
    assert pause.speech_end_sample == 4000
    assert pause.boundary_sample == 8000
    assert pause.confirmed_at_sample == 14000
    assert pause.silence_samples == 10000


def test_pause_detector_preserves_absolute_positions_and_deduplicates_boundaries():
    calls = 0

    def timestamps(_audio, **_kwargs):
        nonlocal calls
        calls += 1
        return [{"start": 1000, "end": 6000}]

    detector = SileroPauseDetector(
        pause_ms=500,
        timestamp_detector=timestamps,
    )
    audio = [0.0] * 16000

    first = detector.consider_window(
        audio,
        window_start_sample=32000,
        total_samples=48000,
        minimum_boundary_sample=30000,
    )
    duplicate = detector.consider_window(
        audio,
        window_start_sample=32000,
        total_samples=48000,
        minimum_boundary_sample=30000,
    )

    assert first is not None
    assert first.speech_end_sample == 38000
    assert first.boundary_sample == 42000
    assert duplicate is None
    assert calls == 2


def test_silero_contains_speech_uses_same_small_adapter_contract():
    silent = lambda _audio, **_kwargs: []
    speech = lambda audio, **_kwargs: [{"start": 0, "end": len(audio)}]

    assert not silero_contains_speech([0.0] * 4000, timestamp_detector=silent)
    assert silero_contains_speech([0.0] * 4000, timestamp_detector=speech)
