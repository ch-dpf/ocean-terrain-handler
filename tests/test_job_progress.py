"""Job progress parsing and mapping tests."""

import subprocess
import sys
import time
from datetime import UTC, datetime

from app.schemas import JobProgress
from app.services.job_progress import (
    JobProgressTracker,
    ThrottledProgressWriter,
    compute_elapsed_seconds,
    gdal_progress_flag_unsupported,
    parse_gdal_progress_chunk,
    parse_zoom_level,
    progress_to_store_fields,
    run_gdal_command,
)


def test_parse_gdal_dot_progress():
    current = 0.0
    current = parse_gdal_progress_chunk("0...10...20", current)
    assert current == 20.0
    current = parse_gdal_progress_chunk("...30...40", current)
    assert current == 40.0
    current = parse_gdal_progress_chunk("100 - done.", current)
    assert current == 100.0


def test_parse_gdal_percent_progress():
    current = parse_gdal_progress_chunk("Processing: 42.5%", 0.0)
    assert current == 42.5


def test_parse_zoom_level():
    assert parse_zoom_level("Generating overview for zoom level 12") == 12
    assert parse_zoom_level("zoom=8") == 8
    assert parse_zoom_level("no zoom here") is None


def test_tracker_monotonic_bytes():
    tracker = JobProgressTracker(stage="ctb_tile", bytes_planned=1000)
    first = tracker.set_bytes_done(100, message="Starting")
    second = tracker.set_bytes_done(50, message="Should not regress")
    assert second.percent >= first.percent
    assert second.bytes_done == 100


def test_tracker_zoom_does_not_invent_percent():
    tracker = JobProgressTracker(
        stage="ctb_tile",
        bytes_planned=1000,
        min_zoom=0,
        max_zoom=10,
    )
    progress = tracker.set_bytes_done(0, current_zoom=5)
    assert progress.percent == 0.0
    assert progress.current_zoom == 5


def test_throttled_writer_forces_final_emit():
    emitted: list[JobProgress] = []

    writer = ThrottledProgressWriter(
        lambda progress: emitted.append(progress),
        min_interval_seconds=60.0,
        min_percent_delta=50.0,
    )
    progress = JobProgress(percent=1.0, phase="gdal_preprocess", message="a")
    writer.emit(progress)
    writer.emit(progress)
    writer.emit(progress, force=True)
    assert len(emitted) == 2


def test_gdal_progress_flag_unsupported():
    stderr = "ERROR 1: Unknown argument: -progress\nUsage: gdalwarp ..."
    assert gdal_progress_flag_unsupported(stderr) is True
    assert gdal_progress_flag_unsupported("gdalwarp failed: out of memory") is False


def test_tracker_exposes_byte_progress():
    tracker = JobProgressTracker(
        stage="gdal_preprocess",
        bytes_planned=200,
        weight_source="bytes",
    )
    progress = tracker.set_bytes_done(50, message="Halfway")
    assert progress.weight_source == "bytes"
    assert progress.bytes_done == 50
    assert progress.bytes_planned == 200
    assert progress.percent == 25.0


def test_progress_to_store_fields():
    payload = progress_to_store_fields(
        JobProgress(percent=33.3, phase="ctb_tile", message="Generating tiles")
    )
    assert payload["progress"]["percent"] == 33.3
    assert payload["progress"]["phase"] == "ctb_tile"


def test_run_gdal_command_streams_stdout_progress_before_exit():
    """Progress dots arrive mid-run; must not wait for process EOF."""
    script = (
        "import sys, time\n"
        "parts = ['0...', '10...', '20...', '30...', '40...', "
        "'50...', '60...', '70...', '80...', '90...', '100 - done.\\n']\n"
        "for part in parts:\n"
        "    sys.stdout.buffer.write(part.encode())\n"
        "    sys.stdout.buffer.flush()\n"
        "    time.sleep(0.12)\n"
    )
    events: list[tuple[float, float]] = []
    started = time.monotonic()

    def on_subprogress(percent: float, _message: str | None) -> None:
        events.append((time.monotonic() - started, percent))

    # -u keeps child stdout unbuffered so parent can parse mid-run progress.
    run_gdal_command(
        [sys.executable, "-u", "-c", script],
        on_subprogress=on_subprogress,
    )

    assert events, f"expected progress updates, got {events!r}"
    assert events[-1][1] == 100.0

    # Linux workers (Docker) deliver mid-run chunks; Windows pipe buffering may coalesce.
    if sys.platform != "win32":
        mid = [(elapsed, percent) for elapsed, percent in events if 0.0 < percent < 100.0]
        assert mid, f"expected mid-run progress updates, got {events!r}"
        assert mid[0][0] < 0.8
        assert any(percent >= 20.0 for _, percent in mid)


def test_run_gdal_command_failure_preserves_stderr_text():
    script = "import sys; sys.stderr.write('ERROR 1: boom\\n'); sys.exit(2)\n"
    try:
        run_gdal_command([sys.executable, "-c", script])
        raise AssertionError("expected CalledProcessError")
    except subprocess.CalledProcessError as exc:
        assert "boom" in (exc.stderr or "")


def test_compute_elapsed_seconds_running():
    data = {
        "created_at": "2026-08-28T07:00:00+00:00",
        "status": "tiling",
    }
    elapsed = compute_elapsed_seconds(
        data,
        now=datetime(2026, 8, 28, 7, 0, 42, tzinfo=UTC),
    )
    assert elapsed == 42.0


def test_compute_elapsed_seconds_completed():
    data = {
        "created_at": "2026-08-28T07:00:00+00:00",
        "completed_at": "2026-08-28T07:01:30+00:00",
        "status": "completed",
    }
    assert compute_elapsed_seconds(data) == 90.0
