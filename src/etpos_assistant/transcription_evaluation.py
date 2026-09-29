from __future__ import annotations

import math
import os
import platform
import statistics
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter, process_time
from typing import Any, Callable, Sequence

from .config import settings
from .transcription import (
    AUDIO_SAMPLE_RATE,
    TranscriptionInputError,
    TranscriptionProfile,
    TranscriptionResult,
    get_transcription_model,
    transcribe_audio_samples,
)

DEFAULT_WINDOW_SECONDS = (1.0, 2.0, 4.0, 6.0, 10.0)
DEFAULT_BLOCK_SECONDS = 0.25

TranscriptionEngine = Callable[..., TranscriptionResult]


@dataclass(frozen=True)
class InferenceMeasurement:
    profile: TranscriptionProfile
    text: str
    error: str | None
    total_ms: float
    process_cpu_seconds: float
    model_ready_ms: float | None
    queue_wait_ms: float | None
    model_call_ms: float | None
    segment_iteration_ms: float | None
    reconstruction_ms: float | None
    segment_count: int
    model_cached_before_call: bool | None


@dataclass(frozen=True)
class BenchmarkRun:
    duration_seconds: float
    repeat: int
    measurement: InferenceMeasurement


@dataclass(frozen=True)
class SimulationEvent:
    sequence: int
    event: str
    at_seconds: float
    audio_covered_seconds: float | None = None
    audio_lag_seconds: float | None = None
    text: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SimulationReport:
    block_seconds: float
    stop_at_seconds: float
    preview_request_times_seconds: tuple[float, ...]
    preview_requests: int
    preview_executed: int
    preview_requests_obsolete: int
    preview_results_obsolete: int
    inference_active_at_stop: bool
    cumulative_inference_ms: float
    cumulative_process_cpu_seconds: float
    max_audio_lag_seconds: float
    time_to_first_result_seconds: float | None
    time_to_first_preview_result_seconds: float | None
    time_to_first_text_seconds: float | None
    final_started_at_seconds: float
    final_completed_at_seconds: float
    final_wait_after_stop_seconds: float
    time_after_stop_seconds: float
    final_text: str
    final_error: str | None
    events: tuple[SimulationEvent, ...]


@dataclass
class _PendingPreview:
    requested_at_seconds: float
    audio_covered_seconds: float


@dataclass
class _ActiveInference:
    kind: str
    started_at_seconds: float
    completes_at_seconds: float
    audio_covered_seconds: float
    measurement: InferenceMeasurement


def parse_seconds_csv(value: str, *, label: str) -> tuple[float, ...]:
    raw_values = [item.strip() for item in value.split(",")]
    if not raw_values or any(not item for item in raw_values):
        raise ValueError(f"{label} doit contenir des secondes séparées par des virgules.")
    try:
        values = tuple(float(item) for item in raw_values)
    except ValueError as exc:
        raise ValueError(f"{label} contient une valeur non numérique.") from exc
    if any(not math.isfinite(item) or item <= 0 for item in values):
        raise ValueError(f"{label} doit contenir uniquement des durées positives finies.")
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{label} doit être strictement croissant et sans doublon.")
    return values


def extract_audio_windows(
    audio: Any,
    durations_seconds: Sequence[float],
    *,
    sample_rate: int = AUDIO_SAMPLE_RATE,
) -> tuple[tuple[float, Any], ...]:
    if sample_rate <= 0:
        raise ValueError("sample_rate doit être strictement positif.")
    if not durations_seconds:
        raise ValueError("Au moins une durée audio est requise.")

    normalized = tuple(float(item) for item in durations_seconds)
    if any(not math.isfinite(item) or item <= 0 for item in normalized):
        raise ValueError("Les durées audio doivent être positives et finies.")
    if tuple(sorted(set(normalized))) != normalized:
        raise ValueError("Les durées audio doivent être strictement croissantes et sans doublon.")

    available_samples = len(audio)
    windows: list[tuple[float, Any]] = []
    for duration in normalized:
        required_samples = int(round(duration * sample_rate))
        if required_samples <= 0:
            raise ValueError("Une fenêtre audio ne peut pas être vide.")
        if required_samples > available_samples:
            available_seconds = available_samples / sample_rate
            raise ValueError(
                f"Audio trop court pour {duration:g} s : {available_seconds:.3f} s disponibles."
            )
        windows.append((duration, audio[:required_samples]))
    return tuple(windows)


def measure_inference(
    audio: Any,
    profile: TranscriptionProfile | str,
    *,
    engine: TranscriptionEngine = transcribe_audio_samples,
    wall_clock: Callable[[], float] = perf_counter,
    cpu_clock: Callable[[], float] = process_time,
) -> InferenceMeasurement:
    selected_profile = TranscriptionProfile(profile)
    wall_started = wall_clock()
    cpu_started = cpu_clock()
    try:
        result = engine(audio, profile=selected_profile)
    except TranscriptionInputError as exc:
        cpu_seconds = max(0.0, cpu_clock() - cpu_started)
        total_ms = max(0.0, (wall_clock() - wall_started) * 1000)
        return InferenceMeasurement(
            profile=selected_profile,
            text="",
            error=str(exc),
            total_ms=total_ms,
            process_cpu_seconds=cpu_seconds,
            model_ready_ms=None,
            queue_wait_ms=None,
            model_call_ms=None,
            segment_iteration_ms=None,
            reconstruction_ms=None,
            segment_count=0,
            model_cached_before_call=None,
        )

    cpu_seconds = max(0.0, cpu_clock() - cpu_started)
    total_ms = max(0.0, (wall_clock() - wall_started) * 1000)
    timings = result.timings
    return InferenceMeasurement(
        profile=selected_profile,
        text=result.text,
        error=None,
        total_ms=total_ms,
        process_cpu_seconds=cpu_seconds,
        model_ready_ms=timings.model_ready_ms if timings else None,
        queue_wait_ms=timings.queue_wait_ms if timings else None,
        model_call_ms=timings.model_call_ms if timings else None,
        segment_iteration_ms=timings.segment_iteration_ms if timings else None,
        reconstruction_ms=timings.reconstruction_ms if timings else None,
        segment_count=len(result.segments),
        model_cached_before_call=(
            timings.model_cached_before_call if timings else None
        ),
    )


def benchmark_profiles(
    audio: Any,
    *,
    durations_seconds: Sequence[float] = DEFAULT_WINDOW_SECONDS,
    profiles: Sequence[TranscriptionProfile | str] = (
        TranscriptionProfile.FINAL,
        TranscriptionProfile.PREVIEW,
    ),
    repeats: int = 3,
    engine: TranscriptionEngine = transcribe_audio_samples,
) -> tuple[BenchmarkRun, ...]:
    if repeats < 2:
        raise ValueError("repeats doit être supérieur ou égal à 2.")

    normalized_profiles = tuple(TranscriptionProfile(profile) for profile in profiles)
    if not normalized_profiles:
        raise ValueError("Au moins un profil de transcription est requis.")
    if len(set(normalized_profiles)) != len(normalized_profiles):
        raise ValueError("Les profils de transcription ne doivent pas être dupliqués.")

    windows = extract_audio_windows(audio, durations_seconds)
    runs: list[BenchmarkRun] = []
    for duration_seconds, window in windows:
        for repeat in range(1, repeats + 1):
            ordered_profiles = (
                normalized_profiles
                if repeat % 2 == 1
                else tuple(reversed(normalized_profiles))
            )
            for profile in ordered_profiles:
                runs.append(
                    BenchmarkRun(
                        duration_seconds=duration_seconds,
                        repeat=repeat,
                        measurement=measure_inference(
                            window,
                            profile,
                            engine=engine,
                        ),
                    )
                )
    return tuple(runs)


def _median_optional(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.median(present) if present else None


def summarize_benchmark_runs(
    runs: Sequence[BenchmarkRun],
) -> tuple[dict[str, Any], ...]:
    groups: dict[tuple[float, TranscriptionProfile], list[BenchmarkRun]] = {}
    for run in runs:
        key = (run.duration_seconds, run.measurement.profile)
        groups.setdefault(key, []).append(run)

    summaries: list[dict[str, Any]] = []
    for (duration_seconds, profile), grouped_runs in sorted(
        groups.items(),
        key=lambda item: (item[0][0], item[0][1].value),
    ):
        successful = [run for run in grouped_runs if run.measurement.error is None]
        errors = [run for run in grouped_runs if run.measurement.error is not None]
        texts = list(dict.fromkeys(run.measurement.text for run in successful))
        total_values = [run.measurement.total_ms for run in successful]
        cpu_values = [run.measurement.process_cpu_seconds for run in successful]
        summaries.append(
            {
                "duration_seconds": duration_seconds,
                "profile": profile.value,
                "samples": len(grouped_runs),
                "successful_samples": len(successful),
                "error_samples": len(errors),
                "median_total_ms": (
                    statistics.median(total_values) if total_values else None
                ),
                "min_total_ms": min(total_values) if total_values else None,
                "max_total_ms": max(total_values) if total_values else None,
                "median_process_cpu_seconds": (
                    statistics.median(cpu_values) if cpu_values else None
                ),
                "median_model_ready_ms": _median_optional(
                    [run.measurement.model_ready_ms for run in successful]
                ),
                "median_queue_wait_ms": _median_optional(
                    [run.measurement.queue_wait_ms for run in successful]
                ),
                "median_model_call_ms": _median_optional(
                    [run.measurement.model_call_ms for run in successful]
                ),
                "median_segment_iteration_ms": _median_optional(
                    [run.measurement.segment_iteration_ms for run in successful]
                ),
                "median_segment_count": (
                    statistics.median(
                        run.measurement.segment_count for run in successful
                    )
                    if successful
                    else None
                ),
                "texts": texts,
                "errors": list(
                    dict.fromkeys(
                        run.measurement.error
                        for run in errors
                        if run.measurement.error is not None
                    )
                ),
            }
        )
    return tuple(summaries)


def _available_audio_seconds(
    at_seconds: float,
    *,
    stop_at_seconds: float,
    block_seconds: float,
) -> float:
    if at_seconds >= stop_at_seconds:
        return stop_at_seconds
    completed_blocks = math.floor((at_seconds + 1e-12) / block_seconds)
    return min(stop_at_seconds, completed_blocks * block_seconds)


def _block_delivery_time(
    requested_at_seconds: float,
    *,
    block_seconds: float,
) -> float:
    blocks = math.ceil((requested_at_seconds - 1e-12) / block_seconds)
    return blocks * block_seconds


def simulate_incremental_arrival(
    audio: Any,
    *,
    preview_request_times_seconds: Sequence[float],
    stop_at_seconds: float,
    engine: Callable[[Any, TranscriptionProfile], InferenceMeasurement],
    block_seconds: float = DEFAULT_BLOCK_SECONDS,
    sample_rate: int = AUDIO_SAMPLE_RATE,
) -> SimulationReport:
    if not math.isfinite(block_seconds) or block_seconds <= 0:
        raise ValueError("block_seconds doit être strictement positif et fini.")
    if not math.isfinite(stop_at_seconds) or stop_at_seconds <= 0:
        raise ValueError("stop_at_seconds doit être strictement positif et fini.")
    if sample_rate <= 0:
        raise ValueError("sample_rate doit être strictement positif.")

    preview_times = tuple(float(item) for item in preview_request_times_seconds)
    if any(not math.isfinite(item) or item <= 0 for item in preview_times):
        raise ValueError("Les instants d'aperçu doivent être positifs et finis.")
    if tuple(sorted(set(preview_times))) != preview_times:
        raise ValueError(
            "Les instants d'aperçu doivent être strictement croissants et sans doublon."
        )
    if any(item >= stop_at_seconds for item in preview_times):
        raise ValueError("Chaque aperçu doit être demandé avant l'arrêt.")

    required_samples = int(round(stop_at_seconds * sample_rate))
    if required_samples > len(audio):
        raise ValueError("L'audio disponible ne couvre pas l'instant d'arrêt.")

    delivered_requests = tuple(
        (
            _block_delivery_time(item, block_seconds=block_seconds),
            item,
        )
        for item in preview_times
    )

    events: list[SimulationEvent] = []
    event_sequence = 0
    request_index = 0
    stopped = False
    stop_processed = False
    pending_preview: _PendingPreview | None = None
    active: _ActiveInference | None = None
    preview_requests = 0
    preview_executed = 0
    preview_requests_obsolete = 0
    preview_results_obsolete = 0
    inference_active_at_stop = False
    cumulative_inference_ms = 0.0
    cumulative_process_cpu_seconds = 0.0
    max_audio_lag_seconds = 0.0
    first_result_seconds: float | None = None
    first_preview_result_seconds: float | None = None
    first_text_seconds: float | None = None
    final_started_at_seconds: float | None = None
    final_completed_at_seconds: float | None = None
    final_text = ""
    final_error: str | None = None

    def record(
        event: str,
        at_seconds: float,
        *,
        audio_covered_seconds: float | None = None,
        audio_lag_seconds: float | None = None,
        text: str | None = None,
        error: str | None = None,
    ) -> None:
        nonlocal event_sequence
        event_sequence += 1
        events.append(
            SimulationEvent(
                sequence=event_sequence,
                event=event,
                at_seconds=at_seconds,
                audio_covered_seconds=audio_covered_seconds,
                audio_lag_seconds=audio_lag_seconds,
                text=text,
                error=error,
            )
        )

    def launch(
        kind: str,
        at_seconds: float,
        audio_covered_seconds: float,
    ) -> _ActiveInference:
        nonlocal preview_executed
        nonlocal cumulative_inference_ms
        nonlocal cumulative_process_cpu_seconds
        nonlocal final_started_at_seconds

        profile = (
            TranscriptionProfile.PREVIEW
            if kind == "preview"
            else TranscriptionProfile.FINAL
        )
        sample_count = int(round(audio_covered_seconds * sample_rate))
        measurement = engine(audio[:sample_count], profile)
        cumulative_inference_ms += measurement.total_ms
        cumulative_process_cpu_seconds += measurement.process_cpu_seconds
        if kind == "preview":
            preview_executed += 1
        else:
            final_started_at_seconds = at_seconds
        record(
            f"{kind}_started",
            at_seconds,
            audio_covered_seconds=audio_covered_seconds,
        )
        return _ActiveInference(
            kind=kind,
            started_at_seconds=at_seconds,
            completes_at_seconds=at_seconds + measurement.total_ms / 1000,
            audio_covered_seconds=audio_covered_seconds,
            measurement=measurement,
        )

    while final_completed_at_seconds is None:
        next_request_at = (
            delivered_requests[request_index][0]
            if request_index < len(delivered_requests)
            else math.inf
        )
        next_stop_at = stop_at_seconds if not stop_processed else math.inf
        next_completion_at = active.completes_at_seconds if active else math.inf
        next_at = min(next_completion_at, next_stop_at, next_request_at)
        if not math.isfinite(next_at):
            raise RuntimeError("Simulation bloquée sans événement terminal.")

        if active is not None and math.isclose(
            next_completion_at,
            next_at,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            completed = active
            active = None
            available = _available_audio_seconds(
                next_at,
                stop_at_seconds=stop_at_seconds,
                block_seconds=block_seconds,
            )
            lag = max(0.0, available - completed.audio_covered_seconds)
            max_audio_lag_seconds = max(max_audio_lag_seconds, lag)

            if completed.kind == "preview":
                if stopped:
                    preview_results_obsolete += 1
                    record(
                        "preview_result_obsolete",
                        next_at,
                        audio_covered_seconds=completed.audio_covered_seconds,
                        audio_lag_seconds=lag,
                        text=completed.measurement.text,
                        error=completed.measurement.error,
                    )
                else:
                    record(
                        "preview_result",
                        next_at,
                        audio_covered_seconds=completed.audio_covered_seconds,
                        audio_lag_seconds=lag,
                        text=completed.measurement.text,
                        error=completed.measurement.error,
                    )
                    if first_preview_result_seconds is None:
                        first_preview_result_seconds = next_at
                    if first_result_seconds is None:
                        first_result_seconds = next_at
                    if (
                        first_text_seconds is None
                        and completed.measurement.error is None
                        and completed.measurement.text
                    ):
                        first_text_seconds = next_at

                if stopped:
                    active = launch("final", next_at, stop_at_seconds)
                elif pending_preview is not None:
                    pending = pending_preview
                    pending_preview = None
                    active = launch(
                        "preview",
                        next_at,
                        pending.audio_covered_seconds,
                    )
            else:
                final_completed_at_seconds = next_at
                final_text = completed.measurement.text
                final_error = completed.measurement.error
                record(
                    "final_result",
                    next_at,
                    audio_covered_seconds=completed.audio_covered_seconds,
                    audio_lag_seconds=lag,
                    text=final_text,
                    error=final_error,
                )
                if first_result_seconds is None:
                    first_result_seconds = next_at
                if (
                    first_text_seconds is None
                    and final_error is None
                    and final_text
                ):
                    first_text_seconds = next_at
            continue

        if not stop_processed and math.isclose(
            next_stop_at,
            next_at,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            stop_processed = True
            stopped = True
            record("stop", next_at, audio_covered_seconds=stop_at_seconds)
            if pending_preview is not None:
                preview_requests_obsolete += 1
                record(
                    "preview_request_obsolete_on_stop",
                    next_at,
                    audio_covered_seconds=pending_preview.audio_covered_seconds,
                )
                pending_preview = None
            if active is not None:
                inference_active_at_stop = True
            else:
                active = launch("final", next_at, stop_at_seconds)
            continue

        delivery_at, requested_at = delivered_requests[request_index]
        request_index += 1
        preview_requests += 1
        coverage = min(
            stop_at_seconds,
            _available_audio_seconds(
                delivery_at,
                stop_at_seconds=stop_at_seconds,
                block_seconds=block_seconds,
            ),
        )
        record(
            "preview_requested",
            delivery_at,
            audio_covered_seconds=coverage,
        )
        pending = _PendingPreview(
            requested_at_seconds=requested_at,
            audio_covered_seconds=coverage,
        )
        if stopped:
            preview_requests_obsolete += 1
            record(
                "preview_request_obsolete_on_stop",
                delivery_at,
                audio_covered_seconds=coverage,
            )
        elif active is None:
            active = launch("preview", delivery_at, coverage)
        else:
            if pending_preview is not None:
                preview_requests_obsolete += 1
                record(
                    "preview_request_superseded",
                    delivery_at,
                    audio_covered_seconds=pending_preview.audio_covered_seconds,
                )
            pending_preview = pending

    if final_started_at_seconds is None or final_completed_at_seconds is None:
        raise RuntimeError("La simulation n'a pas produit de résultat final.")

    return SimulationReport(
        block_seconds=block_seconds,
        stop_at_seconds=stop_at_seconds,
        preview_request_times_seconds=preview_times,
        preview_requests=preview_requests,
        preview_executed=preview_executed,
        preview_requests_obsolete=preview_requests_obsolete,
        preview_results_obsolete=preview_results_obsolete,
        inference_active_at_stop=inference_active_at_stop,
        cumulative_inference_ms=cumulative_inference_ms,
        cumulative_process_cpu_seconds=cumulative_process_cpu_seconds,
        max_audio_lag_seconds=max_audio_lag_seconds,
        time_to_first_result_seconds=first_result_seconds,
        time_to_first_preview_result_seconds=first_preview_result_seconds,
        time_to_first_text_seconds=first_text_seconds,
        final_started_at_seconds=final_started_at_seconds,
        final_completed_at_seconds=final_completed_at_seconds,
        final_wait_after_stop_seconds=max(
            0.0,
            final_started_at_seconds - stop_at_seconds,
        ),
        time_after_stop_seconds=max(
            0.0,
            final_completed_at_seconds - stop_at_seconds,
        ),
        final_text=final_text,
        final_error=final_error,
        events=tuple(events),
    )


def _package_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _cpu_model() -> str | None:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        try:
            for line in cpuinfo.read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    value = platform.processor().strip()
    return value or None


def environment_metadata(project_root: Path) -> dict[str, Any]:
    git_sha: str | None = None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
        if completed.returncode == 0:
            git_sha = completed.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        git_sha = None

    return {
        "git_sha": git_sha,
        "python": platform.python_version(),
        "faster_whisper": _package_version("faster-whisper"),
        "ctranslate2": _package_version("ctranslate2"),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "logical_cpu_count": os.cpu_count(),
    }


def _measurement_to_dict(measurement: InferenceMeasurement) -> dict[str, Any]:
    data = asdict(measurement)
    data["profile"] = measurement.profile.value
    return data


def benchmark_run_to_dict(run: BenchmarkRun) -> dict[str, Any]:
    return {
        "duration_seconds": run.duration_seconds,
        "repeat": run.repeat,
        **_measurement_to_dict(run.measurement),
    }


def simulation_report_to_dict(report: SimulationReport) -> dict[str, Any]:
    return {
        "block_seconds": report.block_seconds,
        "stop_at_seconds": report.stop_at_seconds,
        "preview_request_times_seconds": list(
            report.preview_request_times_seconds
        ),
        "preview_requests": report.preview_requests,
        "preview_executed": report.preview_executed,
        "preview_requests_obsolete": report.preview_requests_obsolete,
        "preview_results_obsolete": report.preview_results_obsolete,
        "inference_active_at_stop": report.inference_active_at_stop,
        "cumulative_inference_ms": report.cumulative_inference_ms,
        "cumulative_process_cpu_seconds": report.cumulative_process_cpu_seconds,
        "max_audio_lag_seconds": report.max_audio_lag_seconds,
        "time_to_first_result_seconds": report.time_to_first_result_seconds,
        "time_to_first_preview_result_seconds": (
            report.time_to_first_preview_result_seconds
        ),
        "time_to_first_text_seconds": report.time_to_first_text_seconds,
        "final_started_at_seconds": report.final_started_at_seconds,
        "final_completed_at_seconds": report.final_completed_at_seconds,
        "final_wait_after_stop_seconds": report.final_wait_after_stop_seconds,
        "time_after_stop_seconds": report.time_after_stop_seconds,
        "final_text": report.final_text,
        "final_error": report.final_error,
        "events": [asdict(event) for event in report.events],
    }


def run_incremental_benchmark(
    path: Path,
    *,
    durations_seconds: Sequence[float] = DEFAULT_WINDOW_SECONDS,
    repeats: int = 3,
    preview_request_times_seconds: Sequence[float] | None = None,
    simulation_stop_at_seconds: float | None = None,
    block_seconds: float = DEFAULT_BLOCK_SECONDS,
    project_root: Path | None = None,
    engine: TranscriptionEngine = transcribe_audio_samples,
    model_loader: Callable[[], Any] = get_transcription_model,
    audio_decoder: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if audio_decoder is None:
        try:
            from faster_whisper.audio import decode_audio
        except ImportError as exc:
            raise RuntimeError(
                "Le décodeur faster-whisper n'est pas disponible."
            ) from exc
        audio_decoder = decode_audio

    decode_wall_started = perf_counter()
    decode_cpu_started = process_time()
    try:
        audio = audio_decoder(str(path), sampling_rate=AUDIO_SAMPLE_RATE)
    except Exception as exc:
        raise TranscriptionInputError(
            "Le fichier audio n'a pas pu être décodé."
        ) from exc
    decode_process_cpu_seconds = max(0.0, process_time() - decode_cpu_started)
    decode_ms = max(0.0, (perf_counter() - decode_wall_started) * 1000)

    source_duration_seconds = len(audio) / AUDIO_SAMPLE_RATE
    windows = extract_audio_windows(audio, durations_seconds)

    model_wall_started = perf_counter()
    model_cpu_started = process_time()
    model_loader()
    model_load_process_cpu_seconds = max(
        0.0,
        process_time() - model_cpu_started,
    )
    model_load_ms = max(0.0, (perf_counter() - model_wall_started) * 1000)

    warmup_duration, warmup_audio = windows[0]
    warmup = measure_inference(
        warmup_audio,
        TranscriptionProfile.PREVIEW,
        engine=engine,
    )

    runs = benchmark_profiles(
        audio,
        durations_seconds=durations_seconds,
        repeats=repeats,
        engine=engine,
    )
    successful_runs = [run for run in runs if run.measurement.error is None]

    root = project_root or Path.cwd()
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "incremental_profiles",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_audio": {
            "name": path.name,
            "duration_seconds": source_duration_seconds,
        },
        "configuration": {
            "sample_rate_hz": AUDIO_SAMPLE_RATE,
            "durations_seconds": [duration for duration, _window in windows],
            "profiles": [
                TranscriptionProfile.FINAL.value,
                TranscriptionProfile.PREVIEW.value,
            ],
            "repeats": repeats,
            "same_audio_per_duration": True,
            "model": settings.whisper_model,
            "device": settings.whisper_device,
            "compute_type": settings.whisper_compute_type,
            "cpu_threads": settings.whisper_cpu_threads,
            "language": settings.whisper_language,
        },
        "environment": environment_metadata(root),
        "performance_scope": {
            "measured_machine": "current_machine_only",
            "target_equivalence_implied": False,
        },
        "preparation": {
            "decode_ms": decode_ms,
            "decode_process_cpu_seconds": decode_process_cpu_seconds,
            "model_load_ms": model_load_ms,
            "model_load_process_cpu_seconds": model_load_process_cpu_seconds,
            "first_inference_after_model_load": {
                "duration_seconds": warmup_duration,
                "excluded_from_profile_comparison": True,
                **_measurement_to_dict(warmup),
            },
        },
        "runs": [benchmark_run_to_dict(run) for run in runs],
        "summaries": list(summarize_benchmark_runs(runs)),
        "aggregate": {
            "calls": len(runs),
            "successful_calls": len(successful_runs),
            "error_calls": len(runs) - len(successful_runs),
            "cumulative_inference_ms": sum(
                run.measurement.total_ms for run in runs
            ),
            "cumulative_process_cpu_seconds": sum(
                run.measurement.process_cpu_seconds for run in runs
            ),
        },
        "quality_review": {
            "note": (
                "Les textes sont conservés pour revue humaine. Une transcription "
                "identique entre plusieurs appels ne prouve pas sa correction."
            ),
            "dimensions": [
                "mots",
                "termes_etpos",
                "nombres",
                "negations",
                "omissions",
                "repetitions",
            ],
        },
    }

    if preview_request_times_seconds is not None:
        if simulation_stop_at_seconds is None:
            raise ValueError(
                "simulation_stop_at_seconds est requis avec les aperçus simulés."
            )

        def simulation_engine(
            samples: Any,
            profile: TranscriptionProfile,
        ) -> InferenceMeasurement:
            return measure_inference(samples, profile, engine=engine)

        simulation = simulate_incremental_arrival(
            audio,
            preview_request_times_seconds=preview_request_times_seconds,
            stop_at_seconds=simulation_stop_at_seconds,
            engine=simulation_engine,
            block_seconds=block_seconds,
        )
        report["simulation"] = simulation_report_to_dict(simulation)

    return report
