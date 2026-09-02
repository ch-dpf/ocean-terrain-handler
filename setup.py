"""Build the CTB meshing/encoding Cython extension (no system zlib)."""

from __future__ import annotations

from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

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
)


class BuildExt(build_ext):
    """MSVC vs gcc/clang flags so the same sources build on Win/macOS/Linux."""

    def build_extensions(self) -> None:
        msvc = self.compiler.compiler_type == "msvc"
        compile_args = ["/O2", "/std:c++17", "/EHsc"] if msvc else ["-O3", "-std=c++17"]
        for extension in self.extensions:
            extension.extra_compile_args = list(compile_args)
            extension.libraries = []
        super().build_extensions()


def _extensions() -> list:
    from Cython.Build import cythonize

    return cythonize(
        [ext],
        language_level=3,
        compiler_directives={"boundscheck": False, "wraparound": False},
    )


setup(ext_modules=_extensions(), cmdclass={"build_ext": BuildExt})
