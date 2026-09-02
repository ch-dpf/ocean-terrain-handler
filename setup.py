"""Build the CTB meshing/encoding Cython extension in-place or via pip."""

from __future__ import annotations

from pathlib import Path

from setuptools import Extension, setup

ROOT = Path(__file__).resolve().parent
NATIVE = ROOT / "app" / "services" / "ctb" / "native"

ext = Extension(
    "app.services.ctb._ctb_core",
    sources=[
        str(NATIVE / "_ctb_core.pyx"),
        str(NATIVE / "mesh_tile.cpp"),
    ],
    include_dirs=[str(NATIVE)],
    language="c++",
    extra_compile_args=["-O3", "-std=c++17"],
    libraries=["z"],
)


def _extensions() -> list:
    from Cython.Build import cythonize

    return cythonize(
        [ext],
        language_level=3,
        compiler_directives={"boundscheck": False, "wraparound": False},
    )


setup(ext_modules=_extensions())
