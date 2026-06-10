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

_headers = [
    "src/bocpy/include/bocpy/bocpy.h",
    "src/bocpy/include/bocpy/xidata.h",
    "src/bocpy/boc_compat.h",
    "src/bocpy/boc_cown.h",
    "src/bocpy/boc_noticeboard.h",
    "src/bocpy/boc_sched.h",
    "src/bocpy/boc_tags.h",
    "src/bocpy/boc_terminator.h",
]

# Both directories are on the include path:
#   - ``src/bocpy/include`` is the public root, so internal C files
#     refer to public headers as ``<bocpy/bocpy.h>`` and downstream
#     consumers resolved via ``bocpy.get_include()`` see exactly the
#     same surface.
#   - ``src/bocpy`` is the private root, scoped by the ``boc_`` prefix
#     to avoid colliding with system headers (``sched.h``, ``tags.h``,
#     etc.) when this directory ends up on a downstream ``-I`` path.
_include_dirs = ["src/bocpy/include", "src/bocpy"]

# Numeric kernels in _math.c are stamped as small noinline helpers so the
# autovectoriser sees a clean inner loop. GCC's distutils default is
# ``-O2``, which sets ``-fvect-cost-model=very-cheap`` and declines to
# vectorise these loops; ``-O3`` promotes the model to ``dynamic`` and
# enables loop unrolling on top. Apple clang on macOS already vectorises
# at ``-O2`` but accepts ``-O3`` for parity. MSVC has no ``-O3``; its
# ``/O2`` is the autovectorising default that cibuildwheel already uses,
# included here explicitly so the build invariant is documented in one
# place. Per-extension scope: only ``_math`` opts in. The scheduler /
# messaging code in ``_core`` has no compute-bound inner loops and we do
# not want to perturb concurrency-critical code with extra IPA passes.
#
# Stays at ``-O3``; never ``-Ofast``/``-ffast-math``, which would break
# IEEE semantics that ``fabs``, ``nearbyint``, and NaN handling depend on.
#
# ``-ffp-contract=off`` is required for bit-reproducible results. By
# default gcc and clang contract a ``a * b + c`` expression into a single
# fused-multiply-add (one rounding) on targets that have an FMA unit --
# notably every arm64 chip. x86-64 at the SSE2 baseline has no FMA, so
# the same source rounds twice there, and the matmul kernel's ascending-k
# accumulation then diverges by 1 ULP between architectures (see
# test_matmul_bitwise_reproducible). Turning contraction off makes the
# multiply and add round separately everywhere, matching the two-rounding
# reference. It does not inhibit autovectorisation -- NEON still vectorises
# the loop with separate FMUL/FADD lanes instead of fused FMLA. MSVC's
# default ``/fp:precise`` does not contract across statements, so the
# Windows build needs no equivalent flag.
if sys.platform == "win32":
    _math_extra_compile_args = ["/O2"]
else:
    _math_extra_compile_args = ["-O3", "-ffp-contract=off"]

_ext_modules = [
    Extension(
        name="bocpy._core",
        sources=["src/bocpy/_core.c", "src/bocpy/boc_compat.c", "src/bocpy/boc_noticeboard.c",
                 "src/bocpy/boc_sched.c", "src/bocpy/boc_tags.c", "src/bocpy/boc_terminator.c"],
        depends=_headers,
        include_dirs=_include_dirs,
    ),
    Extension(
        name="bocpy._math",
        sources=["src/bocpy/_math.c", "src/bocpy/boc_compat.c"],
        depends=_headers,
        include_dirs=_include_dirs,
        extra_compile_args=_math_extra_compile_args,
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
                "src/bocpy/_core.c",
                "src/bocpy/boc_compat.c",
                "src/bocpy/boc_noticeboard.c",
                "src/bocpy/boc_sched.c",
                "src/bocpy/boc_tags.c",
                "src/bocpy/boc_terminator.c",
            ],
            depends=_headers,
            include_dirs=_include_dirs,
        )
    )

setup(
    long_description=_readme,
    long_description_content_type="text/markdown",
    ext_modules=_ext_modules,
)
