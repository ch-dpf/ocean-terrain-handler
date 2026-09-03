"""CTB runner helpers."""

from pathlib import Path

import pytest

from app.services.ctb_runner import format_docker_bind_source, resolve_ctb_volume_source


def test_format_docker_bind_source_windows_drive():
    assert (
        format_docker_bind_source(Path("D:/workspace/ocean-terrain-handler/data"))
        == "//d/workspace/ocean-terrain-handler/data"
    )


def test_format_docker_bind_source_unix():
    assert format_docker_bind_source(Path("/data/workspace")) == "/data/workspace"


def test_resolve_ctb_volume_source_prefers_named_volume():
    assert (
        resolve_ctb_volume_source(
            workspace_dir=Path("/data/workspace"),
            host_workspace_dir=Path("D:/workspace/ocean-terrain-handler/data"),
            workspace_docker_volume="ocean-terrain-handler_workspace_data",
        )
        == "ocean-terrain-handler_workspace_data"
    )


def test_resolve_ctb_volume_source_falls_back_to_host_bind():
    assert (
        resolve_ctb_volume_source(
            workspace_dir=Path("/data/workspace"),
            host_workspace_dir=Path("D:/workspace/ocean-terrain-handler/data"),
            workspace_docker_volume=None,
        )
        == "//d/workspace/ocean-terrain-handler/data"
    )


def test_resolve_ctb_volume_source_rejects_path_like_volume_name():
    with pytest.raises(ValueError, match="Invalid workspace_docker_volume"):
        resolve_ctb_volume_source(
            workspace_dir=Path("/data/workspace"),
            workspace_docker_volume="D:/not/a/volume",
        )
