"""CTB tiling entry: in-process (Python I/O + C++ mesh/encode) or Docker ctb-tile."""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from app.schemas import CtbOptions, OutputFormat
from app.services.ctb.mesh_encode import native_meets_bar
from app.services.ctb.tiler import CtbError, run_ctb_tile as run_inprocess_ctb_tile
from app.services.job_progress import parse_zoom_level, run_streaming_command

logger = logging.getLogger(__name__)

CtbBackend = Literal["auto", "native", "docker"]
ResolvedBackend = Literal["native", "docker"]

ProgressCallback = Callable[[float, str | None], None]

__all__ = [
    "CtbError",
    "build_ctb_command",
    "format_docker_bind_source",
    "resolve_ctb_backend",
    "resolve_ctb_volume_source",
    "run_ctb_tile",
]


def format_docker_bind_source(path: Path) -> str:
    """Format host path for ``docker run -v source:target``.

    On Windows Docker Desktop, ``D:/foo:/data`` is misparsed because the drive
    colon is treated as the volume separator (host ``D``, mode ``/data``).
    Use ``//d/foo`` form instead.

    Do not ``resolve()`` Windows drive paths inside a Linux worker container;
    that incorrectly prefixes the current working directory.
    """
    text = str(path).replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        drive = text[0].lower()
        rest = text[2:].lstrip("/")
        return f"//{drive}/{rest}" if rest else f"//{drive}"
    if text.startswith("/"):
        return text
    return str(path.resolve()).replace("\\", "/")


def resolve_ctb_volume_source(
    *,
    workspace_dir: Path,
    host_workspace_dir: Path | None = None,
    workspace_docker_volume: str | None = None,
) -> str:
    """Return the left-hand side of ``docker run -v <source>:/data``."""
    if workspace_docker_volume:
        name = workspace_docker_volume.strip()
        if not name:
            raise ValueError("workspace_docker_volume is empty")
        if any(sep in name for sep in ("/", "\\", ":")):
            raise ValueError(f"Invalid workspace_docker_volume name: {name}")
        return name
    return format_docker_bind_source(host_workspace_dir or workspace_dir)


def docker_ctb_ready(
    *,
    docker_image: str | None,
    workspace_dir: Path | None,
) -> bool:
    if not docker_image or not str(docker_image).strip():
        return False
    if workspace_dir is None:
        return False
    return shutil.which("docker") is not None


def resolve_ctb_backend(
    requested: str,
    *,
    docker_image: str | None,
    workspace_dir: Path | None,
) -> tuple[ResolvedBackend, str]:
    """Pick native C++ path or Docker ``ctb-tile``.

    ``auto`` uses native when the Cython extension meets the functional/latency
    bar; otherwise Docker CTB if the daemon client is available.
    """
    choice = (requested or "auto").strip().lower()
    ok, detail = native_meets_bar()
    docker_ok = docker_ctb_ready(docker_image=docker_image, workspace_dir=workspace_dir)

    if choice == "docker":
        if not docker_ok:
            raise CtbError(
                "CTB_BACKEND=docker but docker/ctb image/workspace is not configured "
                f"(docker={shutil.which('docker')!r} image={docker_image!r})"
            )
        return "docker", "explicit docker backend"
    if choice == "native":
        if not ok:
            raise CtbError(f"CTB_BACKEND=native but the C++ extension is unusable: {detail}")
        return "native", detail
    if choice not in {"auto", "native", "docker"}:
        raise CtbError(f"Unknown CTB_BACKEND={requested!r}; use auto, native, or docker")

    if ok:
        return "native", detail
    if docker_ok:
        return "docker", f"native did not meet bar ({detail}); using docker ctb-tile"
    raise CtbError(
        "CTB native meshing/encoding did not meet the functional/performance bar "
        f"({detail}) and Docker ctb-tile is not available"
    )


def build_ctb_command(
    input_path: Path,
    output_dir: Path,
    options: CtbOptions,
    docker_image: str,
    workspace_dir: Path,
    gdal_cachemax: int,
    host_workspace_dir: Path | None = None,
    workspace_docker_volume: str | None = None,
) -> list[str]:
    """Build docker run command for ctb-tile."""
    input_rel = input_path.resolve().relative_to(workspace_dir.resolve())
    output_rel = output_dir.resolve().relative_to(workspace_dir.resolve())

    container_input = f"/data/{input_rel.as_posix()}"
    container_output = f"/data/{output_rel.as_posix()}"

    volume_source = resolve_ctb_volume_source(
        workspace_dir=workspace_dir,
        host_workspace_dir=host_workspace_dir,
        workspace_docker_volume=workspace_docker_volume,
    )

    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{volume_source}:/data",
        "-e",
        f"GDAL_CACHEMAX={gdal_cachemax}",
        docker_image,
        "ctb-tile",
        "-o",
        container_output,
        "-f",
        options.output_format.value,
        "-p",
        options.profile.value,
        "-r",
        options.resampling_method.value,
        "-z",
        str(options.error_threshold),
    ]

    if options.thread_count is not None:
        cmd.extend(["-c", str(options.thread_count)])
    if options.tile_size is not None:
        cmd.extend(["-t", str(options.tile_size)])
    if options.start_zoom is not None:
        cmd.extend(["-s", str(options.start_zoom)])
    if options.end_zoom is not None:
        cmd.extend(["-e", str(options.end_zoom)])
    if options.warp_memory is not None:
        cmd.extend(["-m", str(options.warp_memory)])
    if options.resume:
        cmd.append("-R")
    if options.mesh_qfactor != 1.0:
        cmd.extend(["-g", str(options.mesh_qfactor)])
    if options.layer_only:
        cmd.append("-l")
    if options.cesium_friendly:
        cmd.append("-C")
    if options.vertex_normals and options.output_format == OutputFormat.MESH:
        cmd.append("-N")
    if options.quiet:
        cmd.append("-q")
    if options.verbose:
        cmd.append("-v")
    for creation_option in options.creation_options:
        cmd.extend(["-n", creation_option])

    cmd.append(container_input)
    return cmd


def _run_docker_ctb_tile(
    input_path: Path,
    output_dir: Path,
    options: CtbOptions,
    docker_image: str,
    workspace_dir: Path,
    gdal_cachemax: int,
    host_workspace_dir: Path | None = None,
    workspace_docker_volume: str | None = None,
    *,
    on_subprogress: ProgressCallback | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_ctb_command(
        input_path=input_path,
        output_dir=output_dir,
        options=options,
        docker_image=docker_image,
        workspace_dir=workspace_dir,
        gdal_cachemax=gdal_cachemax,
        host_workspace_dir=host_workspace_dir,
        workspace_docker_volume=workspace_docker_volume,
    )
    logger.info("Running CTB: %s", " ".join(cmd))

    def _forward(percent: float, message: str | None) -> None:
        if on_subprogress is None:
            return
        zoom = parse_zoom_level(message) if message else None
        if zoom is not None and (message is None or "zoom" not in message.lower()):
            message = f"Zoom {zoom}"
        on_subprogress(percent, message or "Generating terrain tiles")

    try:
        run_streaming_command(cmd, on_subprogress=_forward if on_subprogress else None)
    except subprocess.CalledProcessError as exc:
        raise CtbError(
            f"ctb-tile failed ({exc.returncode})\n"
            f"stdout: {exc.output}\nstderr: {exc.stderr}"
        ) from exc

    if on_subprogress is not None:
        on_subprogress(100.0, "Tiling complete")


def _clear_terrain_tiles(output_dir: Path) -> None:
    if not output_dir.is_dir():
        return
    for path in output_dir.rglob("*.terrain"):
        try:
            path.unlink()
        except OSError:
            logger.warning("Failed to remove partial tile %s", path)


def run_ctb_tile(
    input_path: Path,
    output_dir: Path,
    options: CtbOptions,
    *,
    cache_bytes: int | None = None,
    gdal_cachemax: int | None = None,
    on_subprogress: ProgressCallback | None = None,
    docker_image: str | None = None,
    workspace_dir: Path | None = None,
    host_workspace_dir: Path | None = None,
    workspace_docker_volume: str | None = None,
    ctb_backend: str = "auto",
    **_ignored: object,
) -> None:
    """Run CTB-compatible tiling.

    Preferred path: Python schedules tiles and reads rasters; Cython/C++ does
    meshing and quantized-mesh/heightmap encoding. If that path does not meet
    the functional/performance bar, fall back to Docker ``ctb-tile``.
    """
    backend, reason = resolve_ctb_backend(
        ctb_backend,
        docker_image=docker_image,
        workspace_dir=workspace_dir,
    )
    logger.info("CTB backend=%s (%s)", backend, reason)
    if backend == "docker":
        if docker_image is None or workspace_dir is None:
            raise CtbError("docker CTB requires docker_image and workspace_dir")
        _run_docker_ctb_tile(
            input_path=input_path,
            output_dir=output_dir,
            options=options,
            docker_image=docker_image,
            workspace_dir=workspace_dir,
            gdal_cachemax=gdal_cachemax if gdal_cachemax is not None else 512,
            host_workspace_dir=host_workspace_dir,
            workspace_docker_volume=workspace_docker_volume,
            on_subprogress=on_subprogress,
        )
        return

    try:
        run_inprocess_ctb_tile(
            input_path,
            output_dir,
            options,
            cache_bytes=cache_bytes,
            gdal_cachemax=gdal_cachemax,
            on_subprogress=on_subprogress,
        )
    except Exception as exc:
        docker_ok = docker_ctb_ready(docker_image=docker_image, workspace_dir=workspace_dir)
        if ctb_backend == "native" or not docker_ok:
            if isinstance(exc, CtbError):
                raise
            raise CtbError(str(exc)) from exc
        logger.warning(
            "In-process CTB failed (%s); retrying with docker ctb-tile",
            exc,
        )
        _clear_terrain_tiles(output_dir)
        if docker_image is None or workspace_dir is None:
            raise CtbError("docker CTB requires docker_image and workspace_dir") from exc
        _run_docker_ctb_tile(
            input_path=input_path,
            output_dir=output_dir,
            options=options,
            docker_image=docker_image,
            workspace_dir=workspace_dir,
            gdal_cachemax=gdal_cachemax if gdal_cachemax is not None else 512,
            host_workspace_dir=host_workspace_dir,
            workspace_docker_volume=workspace_docker_volume,
            on_subprogress=on_subprogress,
        )
