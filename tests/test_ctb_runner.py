"""CTB runner helpers."""

from pathlib import Path

from app.services.ctb_runner import format_docker_bind_source


def test_format_docker_bind_source_windows_drive():
    assert (
        format_docker_bind_source(Path("D:/workspace/ocean-terrain-handler/data"))
        == "//d/workspace/ocean-terrain-handler/data"
    )


def test_format_docker_bind_source_unix():
    assert format_docker_bind_source(Path("/data/workspace")) == "/data/workspace"
