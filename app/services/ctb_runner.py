"""Invoke cesium-terrain-builder via Docker."""

import logging
import subprocess
from pathlib import Path

from app.schemas import CtbOptions

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
    return str(path.resolve()).replace("\\", "/")


def build_ctb_command(
    input_path: Path,
    output_dir: Path,
    options: CtbOptions,
    docker_image: str,
    workspace_dir: Path,
    gdal_cachemax: int,
    host_workspace_dir: Path | None = None,
) -> list[str]:
    """Build docker run command for ctb-tile."""
    input_rel = input_path.resolve().relative_to(workspace_dir.resolve())
    output_rel = output_dir.resolve().relative_to(workspace_dir.resolve())

    container_input = f"/data/{input_rel.as_posix()}"
    container_output = f"/data/{output_rel.as_posix()}"

    # Host Docker daemon resolves -v paths on the host, not inside the worker.
    volume_source = format_docker_bind_source(host_workspace_dir or workspace_dir)

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
    if options.vertex_normals:
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
    )
    logger.info("Running CTB: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CtbError(
            f"ctb-tile failed ({result.returncode})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
