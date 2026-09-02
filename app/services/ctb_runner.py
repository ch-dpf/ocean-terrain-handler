"""Invoke cesium-terrain-builder via Docker."""

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from app.schemas import CtbOptions, OutputFormat
from app.services.job_progress import parse_zoom_level, run_streaming_command

logger = logging.getLogger(__name__)


class CtbError(RuntimeError):
    pass


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
    # Already-absolute POSIX paths must not be resolve()'d on Windows hosts
    # (``Path('/data')`` would become ``D:/data``).
    if text.startswith("/"):
        return text
    return str(path.resolve()).replace("\\", "/")


def resolve_ctb_volume_source(
    *,
    workspace_dir: Path,
    host_workspace_dir: Path | None = None,
    workspace_docker_volume: str | None = None,
) -> str:
    """Return the left-hand side of ``docker run -v <source>:/data``.

    Prefer a Docker named volume (fast on Docker Desktop). Fall back to a host
    bind path for local/dev setups that still mount ``./data`` into the worker.
    """
    if workspace_docker_volume:
        name = workspace_docker_volume.strip()
        if not name:
            raise ValueError("workspace_docker_volume is empty")
        if any(sep in name for sep in ("/", "\\", ":")):
            raise ValueError(f"Invalid workspace_docker_volume name: {name}")
        return name
    return format_docker_bind_source(host_workspace_dir or workspace_dir)


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

    # Host Docker daemon resolves -v sources on the host (path or volume name).
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


def run_ctb_tile(
    input_path: Path,
    output_dir: Path,
    options: CtbOptions,
    docker_image: str,
    workspace_dir: Path,
    gdal_cachemax: int,
    host_workspace_dir: Path | None = None,
    workspace_docker_volume: str | None = None,
    *,
    on_subprogress: Callable[[float, str | None], None] | None = None,
    **_ignored: object,
) -> None:
    """Run ``ctb-tile`` in Docker. Extra kwargs are ignored (legacy Python tiler args)."""
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
