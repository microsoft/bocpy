"""Build script for the bocpy C-ABI consumer smoke test.

Doubles as the canonical downstream template: any extension that wants
to interoperate with bocpy at the C level can copy this file and the
neighbouring ``pyproject.toml``, change the module name, and replace
``_bocpy_probe.c`` with their own sources.
"""

from setuptools import Extension, setup

import bocpy

setup(
    ext_modules=[
        Extension(
            "_bocpy_probe",
            sources=["src/_bocpy_probe.c"] + bocpy.get_sources(),
            include_dirs=[bocpy.get_include()],
        ),
    ],
)
