import os
import re
import sys
from pathlib import Path

from setuptools import Extension, setup

# Load the README and strip any sections marked as PyPI-skip. GitHub still
# renders the original; PyPI's long_description gets the filtered version so
# unsupported content (e.g. Mermaid code blocks) does not appear as raw text.
_readme = Path(__file__).parent.joinpath("README.md").read_text(encoding="utf-8")
_readme = re.sub(
    r"<!-- pypi-skip-start -->.*?<!-- pypi-skip-end -->\n?",
    "",
    _readme,
    flags=re.DOTALL,
)

# The `_internal_test` extension exposes private C primitives (atomics,
# work-stealing queue cursors, MPMC behaviour queue) used only by the
# pytest suite. It must NOT ship in distributed wheels. It is only built
# when BOCPY_BUILD_INTERNAL_TESTS is set to a truthy value (e.g. "1"),
# which the developer-facing test workflow / CI test job sets explicitly.
#
# As a hard backstop we also refuse to build it when setuptools is being
# invoked to produce a wheel or sdist (e.g. by `pypa/cibuildwheel` in
# `.github/workflows/build_wheels.yml`), regardless of the env var. This
# guarantees the extension cannot leak into a release artifact even if a
# future workflow accidentally inherits BOCPY_BUILD_INTERNAL_TESTS=1.
_building_distribution = any(
    cmd in sys.argv for cmd in ("bdist_wheel", "bdist_egg", "sdist")
)
_build_internal_tests = (
    os.environ.get("BOCPY_BUILD_INTERNAL_TESTS", "").lower()
    in ("1", "true", "yes", "on")
    and not _building_distribution
)

_ext_modules = [
    Extension(
        name="bocpy._core",
        sources=["src/bocpy/_core.c", "src/bocpy/compat.c", "src/bocpy/noticeboard.c",
                 "src/bocpy/sched.c", "src/bocpy/tags.c", "src/bocpy/terminator.c"],
    ),
    Extension(
        name="bocpy._math",
        sources=["src/bocpy/_math.c", "src/bocpy/compat.c"],
    ),
]

if _build_internal_tests:
    _ext_modules.append(
        Extension(
            name="bocpy._internal_test",
            sources=[
                "src/bocpy/_internal_test.c",
                "src/bocpy/_internal_test_atomics.c",
                "src/bocpy/_internal_test_bq.c",
                "src/bocpy/_internal_test_wsq.c",
                "src/bocpy/compat.c",
                "src/bocpy/sched.c",
            ],
        )
    )

setup(
    long_description=_readme,
    long_description_content_type="text/markdown",
    ext_modules=_ext_modules,
)
