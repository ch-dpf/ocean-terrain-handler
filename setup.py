"""Build the CTB meshing/encoding Cython extension (no system zlib)."""

from __future__ import annotations

import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

NATIVE = "app/services/ctb/native"

ext = Extension(
    "app.services.ctb._ctb_core",
    sources=[
        f"{NATIVE}/_ctb_core.pyx",
        f"{NATIVE}/mesh_tile.cpp",
        f"{NATIVE}/resample.cpp",
    ],
    include_dirs=[NATIVE],
    depends=[
        f"{NATIVE}/mesh_tile.hpp",
        f"{NATIVE}/heightfield.hpp",
        f"{NATIVE}/encode.hpp",
        f"{NATIVE}/resample.hpp",
        f"{NATIVE}/fill_nodata.hpp",
    ],
    define_macros=[("NOMINMAX", "1")],
    language="c++",
)


class BuildExt(build_ext):
    """MSVC vs gcc/clang flags so the same sources build on Win/macOS/Linux."""

    def build_extensions(self) -> None:
        msvc = self.compiler.compiler_type == "msvc"
        compile_args = ["/O2", "/std:c++17", "/EHsc"] if msvc else ["-O3", "-std=c++17"]
        for extension in self.extensions:
            extension.extra_compile_args = list(compile_args)
            extension.extra_link_args = (
                ["-static-libstdc++", "-static-libgcc"]
                if not msvc and sys.platform.startswith("linux")
                else []
            )
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
