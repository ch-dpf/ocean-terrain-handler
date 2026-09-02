"""CTB runner helpers and backend selection."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.ctb_runner import (
    CtbError,
    format_docker_bind_source,
    resolve_ctb_backend,
    resolve_ctb_volume_source,
)


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


def test_auto_backend_uses_native_when_bar_is_met():
    with patch("app.services.ctb_runner.native_meets_bar", return_value=(True, "ok")):
        backend, reason = resolve_ctb_backend(
            "auto",
            docker_image="cesium-terrain-builder:local",
            workspace_dir=Path("/data/workspace"),
        )
    assert backend == "native"
    assert reason == "ok"


def test_auto_backend_falls_back_to_docker_when_native_misses_bar():
    with (
        patch("app.services.ctb_runner.native_meets_bar", return_value=(False, "too slow")),
        patch("app.services.ctb_runner.shutil.which", return_value="/usr/bin/docker"),
    ):
        backend, reason = resolve_ctb_backend(
            "auto",
            docker_image="cesium-terrain-builder:local",
            workspace_dir=Path("/data/workspace"),
        )
    assert backend == "docker"
    assert "too slow" in reason


def test_auto_backend_errors_when_native_and_docker_unavailable():
    with (
        patch("app.services.ctb_runner.native_meets_bar", return_value=(False, "missing")),
        patch("app.services.ctb_runner.shutil.which", return_value=None),
    ):
        with pytest.raises(CtbError, match="Docker ctb-tile is not available"):
            resolve_ctb_backend(
                "auto",
                docker_image="cesium-terrain-builder:local",
                workspace_dir=Path("/data/workspace"),
            )
