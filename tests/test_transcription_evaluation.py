from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from etpos_assistant.transcription import (
    AUDIO_SAMPLE_RATE,
    TranscriptionInputError,
    TranscriptionProfile,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionTimings,
)
from etpos_assistant.transcription_evaluation import (
    BenchmarkRun,
    InferenceMeasurement,
    benchmark_profiles,
    benchmark_run_to_dict,
    compare_pause_finalization,
    extract_audio_windows,
    measure_inference,
    parse_seconds_csv,
    run_incremental_benchmark,
    simulate_incremental_arrival,
    simulation_report_to_dict,
    summarize_benchmark_runs,
)


def _result(
    *,
    text: str,
    profile: TranscriptionProfile,
    duration_seconds: float = 1.0,
    segment_count: int = 1,
) -> TranscriptionResult:
    segments = tuple(
        TranscriptionSegment(
            text=f"segment-{index}",
            start_seconds=float(index),
            end_seconds=float(index + 1),
        )
        for index in range(segment_count)
    )
    return TranscriptionResult(
        text=text,
        duration_seconds=duration_seconds,
        timings=TranscriptionTimings(
            decode_ms=0.0,
            model_ready_ms=1.0,
            queue_wait_ms=2.0,
            model_call_ms=3.0,
            segment_iteration_ms=4.0,
            reconstruction_ms=5.0,
            total_ms=15.0,
            model_cached_before_call=True,
            profile=profile,
        ),
        segments=segments,
        profile=profile,
    )


def _measurement(
    profile: TranscriptionProfile,
    *,
    text: str,
    total_ms: float,
    cpu_seconds: float = 0.01,
    error: str | None = None,
) -> InferenceMeasurement:
    return InferenceMeasurement(
        profile=profile,
        text=text,
        error=error,
        total_ms=total_ms,
        process_cpu_seconds=cpu_seconds,
        model_ready_ms=1.0 if error is None else None,
        queue_wait_ms=2.0 if error is None else None,
        model_call_ms=3.0 if error is None else None,
        segment_iteration_ms=4.0 if error is None else None,
        reconstruction_ms=5.0 if error is None else None,
        segment_count=1 if error is None else 0,
        model_cached_before_call=True if error is None else None,
    )


def test_parse_seconds_csv_validates_positive_sorted_unique_values():
    assert parse_seconds_csv("1,2,4,6,10", label="durations") == (
        1.0,
        2.0,
        4.0,
        6.0,
        10.0,
    )

    for value in ("", "1,,2", "0,1", "2,1", "1,1", "1,nan", "a,2"):
        with pytest.raises(ValueError):
            parse_seconds_csv(value, label="durations")


def test_extract_audio_windows_uses_exact_prefixes_and_rejects_short_audio():
    audio = list(range(40))
    windows = extract_audio_windows(
        audio,
        (1.0, 2.5),
        sample_rate=4,
    )

    assert windows == (
        (1.0, [0, 1, 2, 3]),
        (2.5, list(range(10))),
    )

    with pytest.raises(ValueError, match="Audio trop court"):
        extract_audio_windows(audio, (11.0,), sample_rate=4)


def test_measure_inference_separates_wall_clock_cpu_and_engine_timings():
    wall_values = iter((10.0, 10.25))
    cpu_values = iter((3.0, 3.1))

    def engine(_audio, *, profile):
        return _result(text="ETPOS", profile=profile, segment_count=2)

    measurement = measure_inference(
        [0.0] * AUDIO_SAMPLE_RATE,
        TranscriptionProfile.PREVIEW,
        engine=engine,
        wall_clock=lambda: next(wall_values),
        cpu_clock=lambda: next(cpu_values),
    )

    assert measurement.profile is TranscriptionProfile.PREVIEW
    assert measurement.total_ms == pytest.approx(250.0)
    assert measurement.process_cpu_seconds == pytest.approx(0.1)
    assert measurement.model_ready_ms == 1.0
    assert measurement.queue_wait_ms == 2.0
    assert measurement.model_call_ms == 3.0
    assert measurement.segment_iteration_ms == 4.0
    assert measurement.segment_count == 2


def test_measure_inference_records_expected_empty_final_error():
    wall_values = iter((1.0, 1.02))
    cpu_values = iter((2.0, 2.01))

    def engine(_audio, *, profile):
        assert profile is TranscriptionProfile.FINAL
        raise TranscriptionInputError("Aucune parole exploitable n'a été détectée.")

    measurement = measure_inference(
        [0.0] * AUDIO_SAMPLE_RATE,
        TranscriptionProfile.FINAL,
        engine=engine,
        wall_clock=lambda: next(wall_values),
        cpu_clock=lambda: next(cpu_values),
    )

    assert measurement.text == ""
    assert measurement.error == "Aucune parole exploitable n'a été détectée."
    assert measurement.total_ms == pytest.approx(20.0)
    assert measurement.process_cpu_seconds == pytest.approx(0.01)
    assert measurement.segment_count == 0
    assert measurement.model_call_ms is None


def test_benchmark_profiles_reuses_same_window_for_final_and_preview():
    audio = [0.0] * (2 * AUDIO_SAMPLE_RATE)
    calls: list[tuple[int, int, TranscriptionProfile]] = []

    def engine(window, *, profile):
        calls.append((len(window), id(window), profile))
        return _result(
            text=f"{profile.value}-{len(window)}",
            profile=profile,
            duration_seconds=len(window) / AUDIO_SAMPLE_RATE,
        )

    runs = benchmark_profiles(
        audio,
        durations_seconds=(1.0, 2.0),
        repeats=2,
        engine=engine,
    )

    assert len(runs) == 8
    assert [call[2] for call in calls[:4]] == [
        TranscriptionProfile.FINAL,
        TranscriptionProfile.PREVIEW,
        TranscriptionProfile.PREVIEW,
        TranscriptionProfile.FINAL,
    ]
    for duration_samples in (AUDIO_SAMPLE_RATE, 2 * AUDIO_SAMPLE_RATE):
        ids = {
            window_id
            for length, window_id, _profile in calls
            if length == duration_samples
        }
        assert len(ids) == 1


def test_summarize_benchmark_runs_publishes_median_distribution_and_texts():
    runs = (
        BenchmarkRun(
            duration_seconds=1.0,
            repeat=1,
            measurement=_measurement(
                TranscriptionProfile.FINAL,
                text="texte final",
                total_ms=100.0,
                cpu_seconds=0.2,
            ),
        ),
        BenchmarkRun(
            duration_seconds=1.0,
            repeat=2,
            measurement=_measurement(
                TranscriptionProfile.FINAL,
                text="texte final",
                total_ms=140.0,
                cpu_seconds=0.4,
            ),
        ),
        BenchmarkRun(
            duration_seconds=1.0,
            repeat=1,
            measurement=_measurement(
                TranscriptionProfile.PREVIEW,
                text="texte aperçu",
                total_ms=60.0,
                cpu_seconds=0.1,
            ),
        ),
        BenchmarkRun(
            duration_seconds=1.0,
            repeat=2,
            measurement=_measurement(
                TranscriptionProfile.PREVIEW,
                text="",
                total_ms=20.0,
                cpu_seconds=0.05,
                error="silence",
            ),
        ),
    )

    summaries = summarize_benchmark_runs(runs)
    final = next(item for item in summaries if item["profile"] == "final")
    preview = next(item for item in summaries if item["profile"] == "preview")

    assert final["samples"] == 2
    assert final["median_total_ms"] == 120.0
    assert final["min_total_ms"] == 100.0
    assert final["max_total_ms"] == 140.0
    assert final["median_process_cpu_seconds"] == pytest.approx(0.3)
    assert final["texts"] == ["texte final"]

    assert preview["samples"] == 2
    assert preview["successful_samples"] == 1
    assert preview["error_samples"] == 1
    assert preview["texts"] == ["texte aperçu"]
    assert preview["errors"] == ["silence"]


def test_simulation_replaces_pending_previews_and_waits_for_active_call_on_stop():
    audio = [0.0] * (3 * AUDIO_SAMPLE_RATE)
    calls: list[tuple[TranscriptionProfile, int]] = []

    def engine(window, profile):
        calls.append((profile, len(window)))
        if profile is TranscriptionProfile.PREVIEW:
            return _measurement(
                profile,
                text=f"preview-{len(window)}",
                total_ms=1000.0,
            )
        return _measurement(profile, text="final", total_ms=500.0)

    report = simulate_incremental_arrival(
        audio,
        preview_request_times_seconds=(1.0, 1.5, 1.75, 2.0),
        stop_at_seconds=2.2,
        engine=engine,
        block_seconds=0.25,
    )

    assert report.preview_requests == 4
    assert report.preview_executed == 2
    assert report.preview_requests_obsolete == 2
    assert report.preview_results_obsolete == 1
    assert report.inference_active_at_stop is True
    assert report.max_audio_lag_seconds == pytest.approx(1.0)
    assert report.time_to_first_result_seconds == pytest.approx(2.0)
    assert report.time_to_first_preview_result_seconds == pytest.approx(2.0)
    assert report.time_to_first_text_seconds == pytest.approx(2.0)
    assert report.final_started_at_seconds == pytest.approx(3.0)
    assert report.final_wait_after_stop_seconds == pytest.approx(0.8)
    assert report.time_after_stop_seconds == pytest.approx(1.3)
    assert report.final_text == "final"

    assert calls == [
        (TranscriptionProfile.PREVIEW, 1 * AUDIO_SAMPLE_RATE),
        (TranscriptionProfile.PREVIEW, int(1.75 * AUDIO_SAMPLE_RATE)),
        (TranscriptionProfile.FINAL, int(2.2 * AUDIO_SAMPLE_RATE)),
    ]
    event_names = [event.event for event in report.events]
    assert event_names.index("stop") < event_names.index("preview_result_obsolete")
    assert event_names[-2:] == ["final_started", "final_result"]


def test_simulation_distinguishes_empty_preview_from_first_visible_text():
    audio = [0.0] * (2 * AUDIO_SAMPLE_RATE)

    def engine(_window, profile):
        if profile is TranscriptionProfile.PREVIEW:
            return _measurement(profile, text="", total_ms=200.0)
        return _measurement(profile, text="résultat final", total_ms=300.0)

    report = simulate_incremental_arrival(
        audio,
        preview_request_times_seconds=(1.0,),
        stop_at_seconds=2.0,
        engine=engine,
    )

    assert report.time_to_first_preview_result_seconds == pytest.approx(1.2)
    assert report.time_to_first_result_seconds == pytest.approx(1.2)
    assert report.time_to_first_text_seconds == pytest.approx(2.3)
    assert report.inference_active_at_stop is False
    assert report.time_after_stop_seconds == pytest.approx(0.3)


def test_simulation_validates_parameters():
    audio = [0.0] * (2 * AUDIO_SAMPLE_RATE)
    engine = lambda _window, profile: _measurement(
        profile,
        text="ok",
        total_ms=1.0,
    )

    with pytest.raises(ValueError, match="avant l'arrêt"):
        simulate_incremental_arrival(
            audio,
            preview_request_times_seconds=(2.0,),
            stop_at_seconds=2.0,
            engine=engine,
        )
    with pytest.raises(ValueError, match="strictement croissants"):
        simulate_incremental_arrival(
            audio,
            preview_request_times_seconds=(1.0, 0.5),
            stop_at_seconds=2.0,
            engine=engine,
        )
    with pytest.raises(ValueError, match="block_seconds"):
        simulate_incremental_arrival(
            audio,
            preview_request_times_seconds=(1.0,),
            stop_at_seconds=2.0,
            engine=engine,
            block_seconds=0.0,
        )


def test_report_serialization_is_json_safe_without_real_whisper(tmp_path):
    audio = [0.0] * (2 * AUDIO_SAMPLE_RATE)
    calls = {"decode": 0, "model": 0, "engine": 0}
    audio_path = tmp_path / "reference.webm"
    audio_path.write_bytes(b"fake")

    def decoder(path, *, sampling_rate):
        assert Path(path) == audio_path
        assert sampling_rate == AUDIO_SAMPLE_RATE
        calls["decode"] += 1
        return audio

    def model_loader():
        calls["model"] += 1
        return object()

    def engine(window, *, profile):
        calls["engine"] += 1
        return _result(
            text=f"{profile.value}-{len(window)}",
            profile=profile,
            duration_seconds=len(window) / AUDIO_SAMPLE_RATE,
        )

    report = run_incremental_benchmark(
        audio_path,
        durations_seconds=(1.0, 2.0),
        repeats=2,
        project_root=tmp_path,
        engine=engine,
        model_loader=model_loader,
        audio_decoder=decoder,
    )

    assert calls == {"decode": 1, "model": 1, "engine": 9}
    assert report["schema_version"] == 1
    assert report["mode"] == "incremental_profiles"
    assert report["source_audio"]["name"] == "reference.webm"
    assert report["configuration"]["durations_seconds"] == [1.0, 2.0]
    assert report["configuration"]["profiles"] == ["final", "preview"]
    assert report["configuration"]["same_audio_per_duration"] is True
    assert report["performance_scope"] == {
        "measured_machine": "current_machine_only",
        "target_equivalence_implied": False,
    }
    assert report["preparation"]["first_inference_after_model_load"][
        "excluded_from_profile_comparison"
    ] is True
    assert len(report["runs"]) == 8
    assert len(report["summaries"]) == 4
    assert report["aggregate"]["calls"] == 8
    assert report["quality_review"]["dimensions"] == [
        "mots",
        "termes_etpos",
        "nombres",
        "negations",
        "omissions",
        "repetitions",
    ]

    payload = json.dumps(report, ensure_ascii=False)
    assert '"incremental_profiles"' in payload

    first_run = benchmark_run_to_dict(
        BenchmarkRun(
            duration_seconds=1.0,
            repeat=1,
            measurement=_measurement(
                TranscriptionProfile.PREVIEW,
                text="aperçu",
                total_ms=10.0,
            ),
        )
    )
    assert first_run["profile"] == "preview"

    simulation = simulate_incremental_arrival(
        audio,
        preview_request_times_seconds=(1.0,),
        stop_at_seconds=1.5,
        engine=lambda _window, profile: _measurement(
            profile,
            text="ok",
            total_ms=10.0,
        ),
    )
    serialized_simulation = simulation_report_to_dict(simulation)
    assert serialized_simulation["events"][-1]["event"] == "final_result"
    json.dumps(serialized_simulation, ensure_ascii=False)


def test_compare_pause_finalization_reports_global_portions_and_terminal_separately():
    audio = [0.0] * (3 * AUDIO_SAMPLE_RATE)
    calls: list[tuple[TranscriptionProfile, int]] = []

    def engine(window, *, profile):
        calls.append((profile, len(window)))
        return _result(
            text=f"{profile.value}-{len(window)}",
            profile=profile,
            duration_seconds=len(window) / AUDIO_SAMPLE_RATE,
        )

    class FakeDetector:
        window_samples = 3 * AUDIO_SAMPLE_RATE

        def __init__(self, *, pause_ms):
            self.pause_ms = pause_ms
            self.emitted = False

        def consider_window(
            self,
            _audio,
            *,
            window_start_sample,
            total_samples,
            minimum_boundary_sample,
        ):
            assert window_start_sample >= 0
            assert minimum_boundary_sample >= 0
            if not self.emitted and total_samples >= 2 * AUDIO_SAMPLE_RATE:
                self.emitted = True
                return SimpleNamespace(
                    boundary_sample=int(1.5 * AUDIO_SAMPLE_RATE)
                )
            return None

    report = compare_pause_finalization(
        audio,
        pause_ms_values=(500, 700),
        min_portion_seconds=1.0,
        block_seconds=0.25,
        engine=engine,
        detector_factory=FakeDetector,
    )

    assert report["reference_global_final"]["text"] == f"final-{3 * AUDIO_SAMPLE_RATE}"
    assert [item["pause_ms"] for item in report["strategies"]] == [500, 700]
    for strategy in report["strategies"]:
        assert strategy["boundaries_samples"] == [int(1.5 * AUDIO_SAMPLE_RATE)]
        assert len(strategy["portion_finalizations"]) == 1
        assert strategy["terminal_final"]["mode"] == "remainder"
        assert strategy["terminal_text"] == (
            f"final-{int(1.5 * AUDIO_SAMPLE_RATE)} "
            f"final-{int(1.5 * AUDIO_SAMPLE_RATE)}"
        )
        assert strategy["final_call_count"] == 2

    assert report["quality_review"]["requires_human_review"] is True
    assert "corrections_orales" in report["quality_review"]["dimensions"]
