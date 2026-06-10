r"""Micro-benchmark for the bocpy ``Matrix`` C extension.

Standalone script (no pytest, no harness). Run from the repo root with
the appropriate venv active:

    source .env314/bin/activate
    python scripts/bench_matrix.py > bench-results.txt

Optionally also dump structured results to a JSON file (better for
archival and tooling than the human-readable text):

    python scripts/bench_matrix.py --json bench-results.json

The file uses the schema ``{'runs': [{'environment': ..., 'results':
[...]}, ...]}``. Passing ``--json PATH`` against a path that does not
yet exist creates the file with one run; passing the same path again
appends another entry. This is convenient for capturing best-of-N
baselines without external merge tooling.

After capturing N runs, summarise them with the lowest-median-per-row
statistic (the canonical baseline statistic for this script):

    python scripts/bench_matrix.py --report-median bench-results.json

Report mode does not run any benches; it only reads the JSON and emits
an aligned text table to stdout. Redirect to a file (``> baseline.txt``)
to persist the summary.

The text and JSON outputs are intended as point-in-time references
for tracking Matrix performance regressions across versions. Numbers
are CPU / compiler / Python-version specific; paste the relevant
lines into PR descriptions rather than committing the result file.

Measurement contract:
- ``gc.collect(); gc.disable()`` around the measurement window.
- ``time.perf_counter_ns()`` only; ``perf_counter()`` is forbidden.
- Auto-tune ``batch`` so one batched iteration takes >= 1 ms; divide
  the elapsed time by ``batch`` for the per-call cost.
- Discard the first measured rep (warm-up).
- Take N=11 measurements after warm-up; report ``min / median / mean
  / max``.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import gc
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time

import bocpy
from bocpy import Matrix


REPS = 11
MIN_BATCH_NS = 1_000_000  # 1 ms: long enough to dwarf perf_counter resolution
# 200 ms warm-up pushes past the Intel PL2->PL1 boost transient (50-150 ms on
# the reference CPU) so every timed rep samples the sustained P-state.
WARMUP_NS = 200_000_000


def _cpu_model() -> str:
    """Best-effort CPU model from /proc/cpuinfo, with platform fallback."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _collect_env() -> dict[str, object]:
    """Capture facts about the loaded bocpy and the host (Python, CPU)."""
    return {
        "bocpy_version": bocpy.__version__,
        "bocpy_path": bocpy.__file__,
        "python": sys.version.replace("\n", " "),
        "cpu": _cpu_model(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reps": REPS,
        "min_batch_ns": MIN_BATCH_NS,
        "warmup_ns": WARMUP_NS,
    }


def _print_header(env: dict[str, object]) -> None:
    """Print the markdown-style header documenting the bench environment."""
    print("# bocpy Matrix micro-benchmark results")
    print()
    print(f"- bocpy version: {env['bocpy_version']}")
    print(f"- bocpy loaded from: {env['bocpy_path']}")
    print(f"- Python: {env['python']}")
    print(f"- CPU: {env['cpu']}")
    print(f"- Timestamp: {env['timestamp']}")
    print(
        f"- Reps after warm-up: {env['reps']}; min batch window: {env['min_batch_ns']} ns;"
        f" per-thunk warmup: {env['warmup_ns']} ns"
    )
    print()
    print("All times below are per-call costs in nanoseconds.")
    print()


def _tune_batch(thunk: Callable[[], None]) -> int:
    """Double ``batch`` until one batched run takes at least MIN_BATCH_NS."""
    batch = 1
    while True:
        t0 = time.perf_counter_ns()
        for _ in range(batch):
            thunk()
        elapsed = time.perf_counter_ns() - t0
        if elapsed >= MIN_BATCH_NS:
            return batch
        batch *= 2
        if batch > 1 << 24:  # 16M: sanity guard against a no-op thunk
            return batch


def measure(label: str, thunk: Callable[[], None]) -> dict[str, float]:
    """Run the contract-compliant measurement loop and return ns stats."""
    batch = _tune_batch(thunk)

    gc.collect()
    gc.disable()
    try:
        warmup_deadline = time.perf_counter_ns() + WARMUP_NS
        while time.perf_counter_ns() < warmup_deadline:
            for _ in range(batch):
                thunk()

        per_call_ns: list[float] = []
        for _ in range(REPS):
            t0 = time.perf_counter_ns()
            for _ in range(batch):
                thunk()
            elapsed = time.perf_counter_ns() - t0
            per_call_ns.append(elapsed / batch)
    finally:
        gc.enable()

    return {
        "label": label,
        "batch": batch,
        "min": min(per_call_ns),
        "median": statistics.median(per_call_ns),
        "mean": statistics.fmean(per_call_ns),
        "max": max(per_call_ns),
    }


def _print_row(stats: dict[str, float]) -> None:
    """Format and print one measurement row."""
    print(
        f"  {stats['label']:<48}"
        f"  min={stats['min']:>10.1f}"
        f"  med={stats['median']:>10.1f}"
        f"  mean={stats['mean']:>10.1f}"
        f"  max={stats['max']:>10.1f}"
        f"  (batch={int(stats['batch'])})"
    )


def _print_section(title: str) -> None:
    """Print a markdown-style section header."""
    print()
    print(f"## {title}")
    print()


def _make_matrix(rows: int, cols: int, seed: int = 0) -> Matrix:
    """Construct a deterministic Matrix populated by a simple LCG."""
    rng_state = seed
    values: list[float] = []
    n = rows * cols
    for _ in range(n):
        rng_state = (rng_state * 1103515245 + 12345) & 0x7FFFFFFF
        values.append((rng_state / 0x7FFFFFFF) * 2.0 - 1.0)
    return Matrix(rows, cols, values)


def bench_length(results: list[dict[str, float]]) -> None:
    """Bench length getter vs magnitude() across three shapes."""
    _print_section("length (getter, no axis support) vs magnitude()")

    for shape in [(1, 3), (1, 1000), (1000, 3)]:
        m = _make_matrix(*shape, seed=1)
        results.append(measure(f"length            shape={shape}", lambda m=m: m.length))
        _print_row(results[-1])
        results.append(measure(f"magnitude()       shape={shape}", lambda m=m: m.magnitude()))
        _print_row(results[-1])


def bench_magnitude_squared(results: list[dict[str, float]]) -> None:
    """Bench magnitude_squared against magnitude to expose the sqrt cost."""
    _print_section("magnitude_squared vs magnitude (sqrt cost)")

    for shape in [(1, 3), (1000, 3)]:
        m = _make_matrix(*shape, seed=2)
        for axis_label, axis in [("axis=None", None), ("axis=1", 1), ("axis=0", 0)]:
            if axis is None:
                results.append(measure(
                    f"magnitude()         shape={shape} {axis_label}",
                    lambda m=m: m.magnitude(),
                ))
                _print_row(results[-1])
                results.append(measure(
                    f"magnitude_squared() shape={shape} {axis_label}",
                    lambda m=m: m.magnitude_squared(),
                ))
                _print_row(results[-1])
            else:
                results.append(measure(
                    f"magnitude({axis})        shape={shape}",
                    lambda m=m, a=axis: m.magnitude(a),
                ))
                _print_row(results[-1])
                results.append(measure(
                    f"magnitude_squared({axis}) shape={shape}",
                    lambda m=m, a=axis: m.magnitude_squared(a),
                ))
                _print_row(results[-1])


def bench_negate(results: list[dict[str, float]]) -> None:
    """Bench negate(in_place=True) against negate to expose allocation cost."""
    _print_section("negate(in_place=True) vs negate (allocation cost)")

    shape = (1000, 3)
    m1 = _make_matrix(*shape, seed=3)
    m2 = _make_matrix(*shape, seed=3)

    results.append(measure(f"negate()                  shape={shape}", lambda m=m1: m.negate()))
    _print_row(results[-1])
    results.append(measure(f"negate(in_place=True)     shape={shape}", lambda m=m2: m.negate(in_place=True)))
    _print_row(results[-1])


def bench_vecdot(results: list[dict[str, float]]) -> None:
    """Bench vecdot against (a*b).sum(axis=k) to confirm the fusion win."""
    _print_section("vecdot vs (a*b).sum(axis=k) (fusion win)")

    a = _make_matrix(1000, 3, seed=4)
    b = _make_matrix(1000, 3, seed=5)
    for axis_label, axis in [("axis=None", None), ("axis=0", 0), ("axis=1", 1)]:
        if axis is None:
            results.append(measure(
                f"vecdot(b)              same-shape 1000x3 {axis_label}",
                lambda a=a, b=b: a.vecdot(b),
            ))
            _print_row(results[-1])
            results.append(measure(
                f"(a*b).sum()            same-shape 1000x3 {axis_label}",
                lambda a=a, b=b: (a * b).sum(),
            ))
            _print_row(results[-1])
        else:
            results.append(measure(
                f"vecdot(b, axis={axis})       same-shape 1000x3",
                lambda a=a, b=b, ax=axis: a.vecdot(b, ax),
            ))
            _print_row(results[-1])
            results.append(measure(
                f"(a*b).sum(axis={axis})       same-shape 1000x3",
                lambda a=a, b=b, ax=axis: (a * b).sum(ax),
            ))
            _print_row(results[-1])

    row = _make_matrix(1, 3, seed=6)
    results.append(measure(
        "vecdot(row)            row-broadcast 1000x3 . 1x3 axis=1",
        lambda a=a, r=row: a.vecdot(r, 1),
    ))
    _print_row(results[-1])
    results.append(measure(
        "(a*row).sum(axis=1)    row-broadcast 1000x3 . 1x3",
        lambda a=a, r=row: (a * r).sum(1),
    ))
    _print_row(results[-1])

    col = _make_matrix(1000, 1, seed=7)
    results.append(measure(
        "vecdot(col, axis=0)    col-broadcast 1000x3 . 1000x1",
        lambda a=a, c=col: a.vecdot(c, 0),
    ))
    _print_row(results[-1])
    results.append(measure(
        "(a*col).sum(axis=0)    col-broadcast 1000x3 . 1000x1",
        lambda a=a, c=col: (a * c).sum(0),
    ))
    _print_row(results[-1])


def bench_cross(results: list[dict[str, float]]) -> None:
    """Bench cross across scalar and batch shapes."""
    _print_section("cross at 1x3, 1000x2 (2D row batch), 1000x3 (3D row batch), 3x1000 (3D col batch)")

    a3 = _make_matrix(1, 3, seed=8)
    b3 = _make_matrix(1, 3, seed=9)
    results.append(measure("cross(b)           1x3", lambda a=a3, b=b3: a.cross(b)))
    _print_row(results[-1])

    a_nx2 = _make_matrix(1000, 2, seed=20)
    b_nx2 = _make_matrix(1000, 2, seed=21)
    results.append(measure(
        "cross(b)           1000x2 (2D row batch)",
        lambda a=a_nx2, b=b_nx2: a.cross(b),
    ))
    _print_row(results[-1])

    a_nx3 = _make_matrix(1000, 3, seed=22)
    b_nx3 = _make_matrix(1000, 3, seed=23)
    results.append(measure(
        "cross(b)           1000x3 (3D row batch)",
        lambda a=a_nx3, b=b_nx3: a.cross(b),
    ))
    _print_row(results[-1])

    a_3xn = _make_matrix(3, 1000, seed=24)
    b_3xn = _make_matrix(3, 1000, seed=25)
    results.append(measure(
        "cross(b)           3x1000 (3D col batch)",
        lambda a=a_3xn, b=b_3xn: a.cross(b),
    ))
    _print_row(results[-1])

    b_vec3 = _make_matrix(1, 3, seed=26)
    results.append(measure(
        "cross(vec3)        1000x3 (3D row batch, broadcast 1x3)",
        lambda a=a_nx3, b=b_vec3: a.cross(b),
    ))
    _print_row(results[-1])
    results.append(measure(
        "cross(vec3)        3x1000 (3D col batch, broadcast 3x1)",
        lambda a=a_3xn, b=b_vec3: a.cross(b),
    ))
    _print_row(results[-1])
    b_vec2 = _make_matrix(1, 2, seed=27)
    results.append(measure(
        "cross(vec2)        1000x2 (2D row batch, broadcast 1x2)",
        lambda a=a_nx2, b=b_vec2: a.cross(b),
    ))
    _print_row(results[-1])


def bench_normalize(results: list[dict[str, float]]) -> None:
    """Bench normalize and normalize(in_place=True) at 1000x3."""
    _print_section("normalize and normalize(in_place=True) at 1000x3")

    shape = (1000, 3)
    for axis_label, axis in [("axis=None", None), ("axis=0", 0), ("axis=1", 1)]:
        m = _make_matrix(*shape, seed=10)
        if axis is None:
            results.append(measure(
                f"normalize()                  shape={shape} {axis_label}",
                lambda m=m: m.normalize(),
            ))
        else:
            results.append(measure(
                f"normalize({axis})                 shape={shape}",
                lambda m=m, a=axis: m.normalize(a),
            ))
        _print_row(results[-1])

    m = _make_matrix(*shape, seed=11)
    results.append(measure(
        f"normalize(1, in_place=True)  shape={shape}",
        lambda m=m: m.normalize(1, in_place=True),
    ))
    _print_row(results[-1])
    m2 = _make_matrix(*shape, seed=11)
    results.append(measure(
        f"normalize(1)                 shape={shape}",
        lambda m=m2, a=1: m.normalize(a),
    ))
    _print_row(results[-1])


def bench_perpendicular(results: list[dict[str, float]]) -> None:
    """Bench perpendicular and perpendicular(in_place=True) at 10000x2."""
    _print_section("perpendicular and perpendicular(in_place=True) at 10000x2")

    shape = (10000, 2)
    m = _make_matrix(*shape, seed=12)
    results.append(measure(
        f"perpendicular()                  shape={shape}",
        lambda m=m: m.perpendicular(),
    ))
    _print_row(results[-1])
    m2 = _make_matrix(*shape, seed=12)
    results.append(measure(
        f"perpendicular(in_place=True)     shape={shape}",
        lambda m=m2: m.perpendicular(in_place=True),
    ))
    _print_row(results[-1])


def bench_angle(results: list[dict[str, float]]) -> None:
    """Bench angle at 10000x2 against a Python math.atan2 loop."""
    _print_section("angle at 10000x2 vs Python math.atan2 loop")

    shape = (10000, 2)
    m = _make_matrix(*shape, seed=13)
    results.append(measure(
        f"angle()                 shape={shape}",
        lambda m=m: m.angle(),
    ))
    _print_row(results[-1])

    rows = shape[0]
    xs = [m[i, 0] for i in range(rows)]
    ys = [m[i, 1] for i in range(rows)]

    def py_angle_loop() -> list[float]:
        out = [0.0] * rows
        for i in range(rows):
            out[i] = math.atan2(ys[i], xs[i])
        return out

    results.append(measure(
        f"math.atan2 loop         shape={shape}",
        py_angle_loop,
    ))
    _print_row(results[-1])


def bench_properties(results: list[dict[str, float]]) -> None:
    """Bench cheap property getters (rows, columns, T) and __len__."""
    _print_section("property getters (rows / columns / T / len)")

    m = _make_matrix(1000, 100, seed=100)
    results.append(measure("rows                  shape=(1000, 100)", lambda m=m: m.rows))
    _print_row(results[-1])
    results.append(measure("columns               shape=(1000, 100)", lambda m=m: m.columns))
    _print_row(results[-1])
    results.append(measure("len(m)                shape=(1000, 100)", lambda m=m: len(m)))
    _print_row(results[-1])
    results.append(measure("T                     shape=(1000, 100)", lambda m=m: m.T))
    _print_row(results[-1])


def bench_unary(results: list[dict[str, float]]) -> None:
    """Bench element-wise unary ops (ceil / floor / round / abs)."""
    _print_section("unary element-wise ops (ceil / floor / round / abs)")

    shape = (1000, 100)
    m = _make_matrix(*shape, seed=101)
    for label, fn in [
        ("ceil()", lambda m=m: m.ceil()),
        ("floor()", lambda m=m: m.floor()),
        ("round()", lambda m=m: m.round()),
        ("abs()", lambda m=m: m.abs()),
        ("negate()", lambda m=m: m.negate()),
    ]:
        results.append(measure(f"{label:<22} shape={shape}", fn))
        _print_row(results[-1])


def bench_aggregations(results: list[dict[str, float]]) -> None:
    """Bench sum / mean / min / max across axis=None and axis=1."""
    _print_section("aggregations (sum / mean / min / max) at 1000x100")

    shape = (1000, 100)
    m = _make_matrix(*shape, seed=102)
    for label, fn in [
        ("sum()", lambda m=m: m.sum()),
        ("sum(1)", lambda m=m: m.sum(1)),
        ("mean()", lambda m=m: m.mean()),
        ("mean(1)", lambda m=m: m.mean(1)),
        ("min()", lambda m=m: m.min()),
        ("min(1)", lambda m=m: m.min(1)),
        ("max()", lambda m=m: m.max()),
        ("max(1)", lambda m=m: m.max(1)),
    ]:
        results.append(measure(f"{label:<22} shape={shape}", fn))
        _print_row(results[-1])


def bench_binary_arithmetic(results: list[dict[str, float]]) -> None:
    """Bench the four broadcast paths of binary arithmetic across +/-/*//."""
    _print_section(
        "binary arithmetic (add/sub/mul/div) — scalar / same-shape / row-bcast / col-bcast"
    )

    shape = (1000, 100)
    a = _make_matrix(*shape, seed=104)
    b = _make_matrix(*shape, seed=105)
    row = _make_matrix(1, 100, seed=106)
    col = _make_matrix(1000, 1, seed=107)

    results.append(measure(
        f"add scalar              shape={shape} + 1.5",
        lambda a=a: a + 1.5,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"add same-shape          shape={shape} + {shape}",
        lambda a=a, b=b: a + b,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"add row-broadcast       shape={shape} + (1, 100)",
        lambda a=a, r=row: a + r,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"add col-broadcast       shape={shape} + (1000, 1)",
        lambda a=a, c=col: a + c,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"sub scalar              shape={shape} - 1.5",
        lambda a=a: a - 1.5,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"sub same-shape          shape={shape} - {shape}",
        lambda a=a, b=b: a - b,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"sub row-broadcast       shape={shape} - (1, 100)",
        lambda a=a, r=row: a - r,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"sub col-broadcast       shape={shape} - (1000, 1)",
        lambda a=a, c=col: a - c,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"mul scalar              shape={shape} * 1.5",
        lambda a=a: a * 1.5,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"mul same-shape          shape={shape} * {shape}",
        lambda a=a, b=b: a * b,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"mul row-broadcast       shape={shape} * (1, 100)",
        lambda a=a, r=row: a * r,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"mul col-broadcast       shape={shape} * (1000, 1)",
        lambda a=a, c=col: a * c,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"div scalar              shape={shape} / 1.5",
        lambda a=a: a / 1.5,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"div same-shape          shape={shape} / {shape}",
        lambda a=a, b=b: a / b,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"div row-broadcast       shape={shape} / (1, 100)",
        lambda a=a, r=row: a / r,
    ))
    _print_row(results[-1])
    results.append(measure(
        f"div col-broadcast       shape={shape} / (1000, 1)",
        lambda a=a, c=col: a / c,
    ))
    _print_row(results[-1])
    a_ip = _make_matrix(*shape, seed=104)
    results.append(measure(
        f"iadd same-shape         shape={shape}",
        lambda a=a_ip, b=b: a.__iadd__(b),
    ))
    _print_row(results[-1])


def bench_matmul(results: list[dict[str, float]]) -> None:
    """Bench matmul across small, square, tall-thin, and fat-short shapes."""
    _print_section("matmul (@) across shape regimes")

    cases = [
        ("small", (16, 16), (16, 16)),
        ("square mid", (128, 128), (128, 128)),
        ("square big", (256, 256), (256, 256)),
        ("tall-thin", (1000, 4), (4, 1000)),
        ("fat-short", (4, 1000), (1000, 4)),
        ("rect mxn @ nxk", (500, 64), (64, 500)),
    ]
    for label, ash, bsh in cases:
        a = _make_matrix(*ash, seed=hash(label) & 0xFFFF)
        b = _make_matrix(*bsh, seed=(hash(label) >> 16) & 0xFFFF)
        results.append(measure(
            f"{label:<18} {ash} @ {bsh}",
            lambda a=a, b=b: a @ b,
        ))
        _print_row(results[-1])


def bench_transpose(results: list[dict[str, float]]) -> None:
    """Bench transpose vs transpose(in_place=True) to expose the allocation tax."""
    _print_section("transpose vs transpose(in_place=True) at 1000x100")

    shape = (1000, 100)
    m1 = _make_matrix(*shape, seed=103)
    m2 = _make_matrix(*shape, seed=103)
    results.append(measure(
        f"transpose()                  shape={shape}",
        lambda m=m1: m.transpose(),
    ))
    _print_row(results[-1])
    results.append(measure(
        f"transpose(in_place=True)     shape={shape}",
        lambda m=m2: m.transpose(in_place=True),
    ))
    _print_row(results[-1])


def bench_select(results: list[dict[str, float]]) -> None:
    """Bench row and column gather across small and large index lists."""
    _print_section("select (row / column gather)")

    shape = (1000, 100)
    m = _make_matrix(*shape, seed=108)
    row_idx = list(range(0, 1000, 10))
    col_idx = list(range(0, 100, 2))
    results.append(measure(
        f"select rows (100/1000)  shape={shape}",
        lambda m=m, i=row_idx: m.select(i, 0),
    ))
    _print_row(results[-1])
    results.append(measure(
        f"select cols (50/100)    shape={shape}",
        lambda m=m, i=col_idx: m.select(i, 1),
    ))
    _print_row(results[-1])


def bench_copy_clip_allclose(results: list[dict[str, float]]) -> None:
    """Bench copy (pure memcpy baseline), clip, and allclose."""
    _print_section("copy (baseline memcpy) / clip / allclose at 1000x100")

    shape = (1000, 100)
    m = _make_matrix(*shape, seed=109)
    results.append(measure(
        f"copy()                  shape={shape}",
        lambda m=m: m.copy(),
    ))
    _print_row(results[-1])
    results.append(measure(
        f"clip(-0.5, 0.5)         shape={shape}",
        lambda m=m: m.clip(-0.5, 0.5),
    ))
    _print_row(results[-1])

    n = _make_matrix(*shape, seed=109)
    results.append(measure(
        f"allclose(m, n)          shape={shape}",
        lambda m=m, n=n: Matrix.allclose(m, n),
    ))
    _print_row(results[-1])


def bench_construction(results: list[dict[str, float]]) -> None:
    """Bench Matrix construction from sequences and concat."""
    _print_section("construction (Matrix(rows, cols, list)) and concat")

    rows, cols = 1000, 100
    flat = [0.5] * (rows * cols)
    results.append(measure(
        f"Matrix(rows, cols, list)  shape=({rows}, {cols}) (Python list)",
        lambda flat=flat, r=rows, c=cols: Matrix(r, c, flat),
    ))
    _print_row(results[-1])
    results.append(measure(
        f"Matrix(rows, cols, 0.5)   shape=({rows}, {cols}) (scalar fill)",
        lambda r=rows, c=cols: Matrix(r, c, 0.5),
    ))
    _print_row(results[-1])

    pieces_rows = [_make_matrix(100, cols, seed=200 + i) for i in range(10)]
    pieces_cols = [_make_matrix(rows, 10, seed=300 + i) for i in range(10)]
    results.append(measure(
        "concat axis=0  10 x (100, 100) -> (1000, 100)",
        lambda p=pieces_rows: Matrix.concat(p, 0),
    ))
    _print_row(results[-1])
    results.append(measure(
        "concat axis=1  10 x (1000, 10) -> (1000, 100)",
        lambda p=pieces_cols: Matrix.concat(p, 1),
    ))
    _print_row(results[-1])


def bench_factories(results: list[dict[str, float]]) -> None:
    """Bench zeros / ones / normal / uniform classmethod factories."""
    _print_section("RNG factories (normal / uniform)")

    shape = (1000, 100)
    results.append(measure(
        f"Matrix.zeros(({shape[0]}, {shape[1]}))",
        lambda s=shape: Matrix.zeros(s),
    ))
    _print_row(results[-1])
    results.append(measure(
        f"Matrix.ones(({shape[0]}, {shape[1]}))",
        lambda s=shape: Matrix.ones(s),
    ))
    _print_row(results[-1])
    results.append(measure(
        f"Matrix.normal(0.0, 1.0, size=({shape[0]}, {shape[1]}))",
        lambda s=shape: Matrix.normal(0.0, 1.0, size=s),
    ))
    _print_row(results[-1])
    results.append(measure(
        f"Matrix.uniform(0.0, 1.0, size=({shape[0]}, {shape[1]}))",
        lambda s=shape: Matrix.uniform(0.0, 1.0, size=s),
    ))
    _print_row(results[-1])


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse CLI args for the bench driver."""
    parser = argparse.ArgumentParser(
        description="Run the bocpy Matrix micro-benchmark.",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Also write structured results to PATH as JSON. "
            "Creates the file with schema {'runs': [{environment, "
            "results}]} on first call; appends one entry to the "
            "'runs' array on every subsequent call against the same "
            "path. Human-readable output still goes to stdout."
        ),
    )
    parser.add_argument(
        "--report-median",
        dest="report_median_path",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Report-only mode: read the runs[] JSON file at PATH, "
            "compute the lowest-median-per-row across all runs, and "
            "emit an aligned text table to stdout. Does not run any "
            "benches. Mutually exclusive with --json."
        ),
    )
    return parser.parse_args(argv)


def _report_median(path: Path) -> int:
    """Summarise a runs[] JSON file as lowest-median-per-row."""
    data = json.loads(path.read_text())
    if "runs" not in data or not isinstance(data["runs"], list) or not data["runs"]:
        raise SystemExit(
            f"--report-median: {path} does not contain a non-empty 'runs' list."
        )
    runs = data["runs"]
    n_rows = len(runs[0]["results"])
    for k, run in enumerate(runs):
        if len(run["results"]) != n_rows:
            raise SystemExit(
                f"--report-median: run #{k + 1} has {len(run['results'])} "
                f"rows; run #1 has {n_rows}. Cannot merge runs with "
                "different harness shapes."
            )
    env0 = runs[0]["environment"]
    lines = [
        "# bench_matrix lowest-median-per-row summary",
        f"# Source: {path}",
        f"# Runs in source file: {len(runs)}",
        "# Timestamps: " + ", ".join(r["environment"]["timestamp"] for r in runs),
        f"# CPU: {env0.get('cpu', 'unknown')}",
        f"# Python: {env0.get('python', 'unknown')}",
        f"# bocpy_version: {env0.get('bocpy_version', 'unknown')}",
        "",
        f'{"label":<60} {"median_ns":>14} {"min_ns":>14} {"chosen_run":>10}',
    ]
    for i in range(n_rows):
        labels = [r["results"][i]["label"] for r in runs]
        if len(set(labels)) != 1:
            raise SystemExit(
                f"--report-median: label mismatch at row {i}: {labels}"
            )
        medians = [r["results"][i]["median"] for r in runs]
        best = min(range(len(runs)), key=lambda k: medians[k])
        row = runs[best]["results"][i]
        lines.append(
            f'{row["label"]:<60} {row["median"]:>14.3f} '
            f'{row["min"]:>14.3f} {best + 1:>10}'
        )
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Drive every bench_* section and return a process exit code."""
    args = _parse_args(argv)

    if args.report_median_path is not None:
        if args.json_path is not None:
            raise SystemExit("--report-median and --json are mutually exclusive")
        return _report_median(args.report_median_path)

    env = _collect_env()
    _print_header(env)

    results: list[dict[str, float]] = []

    bench_properties(results)
    bench_unary(results)
    bench_aggregations(results)
    bench_binary_arithmetic(results)
    bench_matmul(results)
    bench_transpose(results)
    bench_select(results)
    bench_copy_clip_allclose(results)
    bench_construction(results)
    bench_factories(results)

    bench_length(results)
    bench_magnitude_squared(results)
    bench_negate(results)
    bench_vecdot(results)
    bench_cross(results)
    bench_normalize(results)
    bench_perpendicular(results)
    bench_angle(results)

    if args.json_path is not None:
        payload = {"environment": env, "results": results}
        if args.json_path.exists():
            existing = json.loads(args.json_path.read_text())
            if "runs" not in existing or not isinstance(existing["runs"], list):
                raise SystemExit(
                    f"--json: {args.json_path} exists but is not in the "
                    "expected schema (missing top-level 'runs' list). "
                    "Delete it or pick a different path."
                )
            runs = existing["runs"]
        else:
            runs = []
        runs.append(payload)
        args.json_path.write_text(json.dumps({"runs": runs}, indent=2) + "\n")
        print(f"wrote run #{len(runs)} to {args.json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
