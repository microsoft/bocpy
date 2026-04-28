"""Fanout microbenchmark for the BOC runtime.

Measures the dispatch-rate ceiling on a single producer worker for the
fanout workload. Each producer behavior runs on a
``Cown[ProducerState]`` and, on every step:

1. Allocates ``fanout_width`` **fresh** ``Cown[Matrix]`` consumers
   (the producer does not hold them).
2. Dispatches ``@when(consumer_i)`` per consumer; each child mutates
   its own cown and emits a ``"child"`` completion token.
3. Reschedules itself on the producer cown until ``producer_steps``
   steps have run.

Because the producer never holds the consumer cowns, every child
dispatch from the worker takes the producer-local arm of
``boc_sched_dispatch`` (``dispatched_to_pending`` then ``pushed_local``
once ``pending`` is occupied). Contention on the producer worker's
per-worker queue back-pointer is the failure mode the per-worker
``BOC_WSQ_N`` sub-queues address; this benchmark surfaces
``enqueue_cas_retries`` on the producer worker as the gating signal.

This file deliberately duplicates the harness scaffolding from
``benchmark.py`` (rule-of-three: chain and fanout are the only two
runtimes microbenchmarks today; refactoring is premature).
"""

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from bocpy import Cown, Matrix, receive, send, start, wait, when

SENTINEL_BEGIN = "---BOCPY-FANOUT-BEGIN---"
SENTINEL_END = "---BOCPY-FANOUT-END---"
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Behavior code (fanout workload, fresh-cown shape)
# ---------------------------------------------------------------------------


class ProducerState:
    """Per-producer state held inside a ``Cown[ProducerState]``.

    Holds plain ints only; the consumer cowns this producer dispatches
    against are allocated fresh inside ``schedule_producer`` on every
    step and never stored on the state.
    """

    def __init__(self, producer_id: int, fanout_width: int,
                 child_iters: int, target_steps: int,
                 payload_rows: int, payload_cols: int):
        """Initialize a producer state.

        :param producer_id: Unique id within the workload.
        :param fanout_width: Children dispatched per step (K).
        :param child_iters: Inner-loop matmul iterations per child.
        :param target_steps: Number of producer steps before this
            producer stops self-rescheduling.
        :param payload_rows: Rows of each fresh consumer matrix.
        :param payload_cols: Cols of each fresh consumer matrix.
        """
        self.producer_id = producer_id
        self.fanout_width = fanout_width
        self.child_iters = child_iters
        self.target_steps = target_steps
        self.payload_rows = payload_rows
        self.payload_cols = payload_cols
        self.dispatched = 0
        self.steps = 0


def schedule_child(consumer_cown: Cown, child_iters: int) -> None:
    """Schedule one child step on a fresh consumer cown.

    The child does ``child_iters`` in-place self-multiplications of
    its matrix, then emits a ``("child", 1)`` token so the parent
    can count completions.

    :param consumer_cown: The child's exclusively-acquired matrix cown.
    :param child_iters: Inner-loop matmul iterations, captured.
    """
    @when(consumer_cown)
    def _child(c):
        for _ in range(child_iters):
            c.value = c.value @ c.value
        send("child", 1)


def schedule_producer(p_cown: Cown) -> None:
    """Schedule one producer step on ``p_cown``.

    Allocates ``fanout_width`` fresh ``Cown[Matrix]`` consumers,
    dispatches one child per consumer, then either reschedules
    itself or emits ``("producer_done", producer_id)`` when
    ``target_steps`` is reached.

    The producer holds only ``p_cown``; the fresh consumer cowns are
    not in its acquired set, so each child dispatch takes the
    producer-local arm of ``boc_sched_dispatch`` and the producer
    worker is never blocked by a child.

    :param p_cown: The producer's ``Cown[ProducerState]``.
    """
    @when(p_cown)
    def _step(producer):
        ps = producer.value
        rows, cols = ps.payload_rows, ps.payload_cols
        k = ps.fanout_width
        for _ in range(k):
            consumer = Cown(Matrix.uniform(0.0, 1.0, (rows, cols)))
            schedule_child(consumer, ps.child_iters)
        ps.dispatched += k
        ps.steps += 1
        if ps.steps >= ps.target_steps:
            send("producer_done", (ps.producer_id, ps.dispatched))
            return
        # Pass the already-acquired wrapper rather than the
        # closure-captured ``p_cown`` to keep the capture set minimal.
        schedule_producer(producer)


# ---------------------------------------------------------------------------
# Configuration and result types
# ---------------------------------------------------------------------------


@dataclass
class FanoutConfig:
    """Plain-data fanout configuration (no Cowns)."""

    workers: int = 4
    producers: Optional[int] = None
    fanout_width: Optional[int] = None
    child_iters: int = 1
    producer_steps: int = 1000
    payload_rows: int = 16
    payload_cols: int = 16
    repeats: int = 1


@dataclass
class RepeatResult:
    """Plain-data result for a single repeat of one sweep point."""

    repeat_index: int
    completed_children: int
    elapsed_s: float
    throughput: float
    wall_clock_ns_start: int
    scheduler_stats: Optional[list] = None
    derived: Optional[dict] = None


@dataclass
class PointResult:
    """Plain-data result for a single sweep point."""

    inputs: dict
    repeats: list = field(default_factory=list)
    throughput_mean: Optional[float] = None
    throughput_stdev: Optional[float] = None
    throughput_min: Optional[float] = None
    throughput_max: Optional[float] = None
    error: Optional[dict] = None


# ---------------------------------------------------------------------------
# Sizing / validation
# ---------------------------------------------------------------------------


def derive_sizes(cfg: FanoutConfig) -> FanoutConfig:
    """Auto-size ``producers`` and ``fanout_width`` if not overridden.

    Defaults: one producer per ~4 workers (minimum 1), and
    ``K = 4 * workers`` children per producer step. These reproduce
    a contention-heavy operating point on the fanout workload.

    :param cfg: An input config (mutated and returned).
    :return: The same config.
    """
    if cfg.producers is None:
        cfg.producers = max(1, cfg.workers // 4)
    if cfg.fanout_width is None:
        cfg.fanout_width = max(1, 4 * cfg.workers)
    return cfg


def validate_config(cfg: FanoutConfig) -> Optional[str]:
    """Validate a fully-derived config.

    :param cfg: A config with ``producers`` and ``fanout_width`` set.
    :return: An error message, or ``None`` if valid.
    """
    if cfg.workers < 1:
        return f"workers must be >= 1, got {cfg.workers}"
    if cfg.producers is None or cfg.producers < 1:
        return f"producers must be >= 1, got {cfg.producers}"
    if cfg.fanout_width is None or cfg.fanout_width < 1:
        return f"fanout_width must be >= 1, got {cfg.fanout_width}"
    if cfg.child_iters < 1:
        return f"child_iters must be >= 1, got {cfg.child_iters}"
    if cfg.producer_steps < 1:
        return f"producer_steps must be >= 1, got {cfg.producer_steps}"
    if cfg.payload_rows < 1 or cfg.payload_cols < 1:
        return "payload dimensions must be >= 1"
    return None


# ---------------------------------------------------------------------------
# Single-point measurement
# ---------------------------------------------------------------------------


def run_single_point_body(cfg: FanoutConfig, repeat_index: int) -> RepeatResult:
    """Run one fanout measurement in a fresh BOC runtime.

    Total expected completions = ``producers * fanout_width *
    producer_steps``. The parent waits for that many ``child`` tokens
    and ``producers`` ``producer_done`` tokens before tearing the
    runtime down. ``wait(stats=True)`` returns the per-worker
    counters captured at shutdown.

    :param cfg: The fully-derived config.
    :param repeat_index: Repeat index for reporting.
    :return: A ``RepeatResult`` with no Cown references.
    """
    start(worker_count=cfg.workers)
    total_expected = cfg.producers * cfg.fanout_width * cfg.producer_steps
    payload_bytes = cfg.payload_rows * cfg.payload_cols * 8
    print(f"workload: fanout (fresh-cown) producers={cfg.producers} "
          f"fanout_width={cfg.fanout_width} "
          f"producer_steps={cfg.producer_steps} "
          f"child_iters={cfg.child_iters} "
          f"expected_children={total_expected} "
          f"payload={cfg.payload_rows}x{cfg.payload_cols} "
          f"(~{payload_bytes / 1024:.2f} KiB per consumer cown)",
          file=sys.stderr)

    # Allocate producer state cowns.
    producer_cowns = [
        Cown(ProducerState(
            producer_id=pid,
            fanout_width=cfg.fanout_width,
            child_iters=cfg.child_iters,
            target_steps=cfg.producer_steps,
            payload_rows=cfg.payload_rows,
            payload_cols=cfg.payload_cols))
        for pid in range(cfg.producers)
    ]

    # Generous wall-clock ceiling.
    timeout_s = max(60.0, total_expected * 0.001)

    try:
        wall_clock_ns_start = time.time_ns()
        t_measure_start = time.perf_counter()

        for p_cown in producer_cowns:
            schedule_producer(p_cown)

        # Drain child completions.
        completed = 0
        while completed < total_expected:
            msg = receive(["child"], timeout_s)
            if msg is None or msg[0] != "child":
                raise RuntimeError(
                    f"only {completed}/{total_expected} child tokens "
                    f"received within {timeout_s:.0f}s")
            completed += 1

        # Drain producer-done acks.
        producer_dispatched = 0
        for _ in range(cfg.producers):
            msg = receive(["producer_done"], timeout_s)
            if msg is None or msg[0] != "producer_done":
                raise RuntimeError(
                    f"producer_done not received within {timeout_s:.0f}s")
            _, (_pid, count) = msg
            producer_dispatched += count

        t_end = time.perf_counter()
        elapsed_s = t_end - t_measure_start

        if producer_dispatched != completed:
            raise RuntimeError(
                f"dispatched/completed mismatch: dispatched="
                f"{producer_dispatched} completed={completed}")
    finally:
        del producer_cowns
        sched_stats_end = wait(stats=True)

    throughput = completed / elapsed_s if elapsed_s > 0 else 0.0
    return RepeatResult(
        repeat_index=repeat_index,
        completed_children=int(completed),
        elapsed_s=elapsed_s,
        throughput=throughput,
        wall_clock_ns_start=wall_clock_ns_start,
        scheduler_stats=sched_stats_end,
        derived=compute_derived_metrics(sched_stats_end, int(completed)))


# ---------------------------------------------------------------------------
# Derived metrics (dispatch-contention signal)
# ---------------------------------------------------------------------------


def compute_derived_metrics(stats: Optional[list],
                            completed_children: int) -> dict:
    """Compute the dispatch-contention signal from a per-worker stats snapshot.

    The producer worker is identified as the worker with the largest
    ``pushed_local + dispatched_to_pending`` total over the session.
    The gate ratio is ``enqueue_cas_retries / (pushed_local +
    dispatched_to_pending)`` on that worker.

    Also computes a **fairness** signal: how evenly the work landed
    across workers, measured as the coefficient of variation of
    ``popped_local + popped_via_steal`` across all workers, plus the
    Gini coefficient of the same vector. Lower is fairer; perfectly
    balanced (every worker did the same number of behaviors) is
    ``fairness_cv = 0`` and ``fairness_gini = 0``.

    :param stats: Per-worker snapshot from ``wait(stats=True)``.
    :param completed_children: Total child completions over the run.
    :return: A dict with the gate inputs and outputs.
    """
    out = {
        "producer_worker_index": None,
        "producer_pushed_local": 0,
        "producer_dispatched_to_pending": 0,
        "producer_enqueue_cas_retries": 0,
        "enq_retry_ratio": None,
        "steal_yield": None,
        "idle_ratio": None,
        "fairness_cv": None,
        "fairness_gini": None,
        "worker_pop_min": None,
        "worker_pop_max": None,
        "worker_pop_mean": None,
        "worker_pop_counts": None,
    }
    if not stats:
        return out
    producer_pushes = [
        int(w.get("pushed_local", 0))
        + int(w.get("dispatched_to_pending", 0))
        for w in stats
    ]
    if max(producer_pushes) == 0:
        return out
    p_idx = max(range(len(producer_pushes)), key=lambda i: producer_pushes[i])
    p_local = int(stats[p_idx].get("pushed_local", 0))
    p_pending = int(stats[p_idx].get("dispatched_to_pending", 0))
    p_enq_r = int(stats[p_idx].get("enqueue_cas_retries", 0))
    p_total = p_local + p_pending
    out["producer_worker_index"] = p_idx
    out["producer_pushed_local"] = p_local
    out["producer_dispatched_to_pending"] = p_pending
    out["producer_enqueue_cas_retries"] = p_enq_r
    out["enq_retry_ratio"] = (p_enq_r / p_total) if p_total > 0 else None

    total_steal = sum(int(w.get("popped_via_steal", 0)) for w in stats)
    if completed_children > 0:
        out["steal_yield"] = total_steal / completed_children

    total_attempts = sum(int(w.get("steal_attempts", 0)) for w in stats)
    total_failures = sum(int(w.get("steal_failures", 0)) for w in stats)
    if total_attempts > 0:
        out["idle_ratio"] = total_failures / total_attempts

    # Fairness: distribution of work across workers. We count
    # popped_local + popped_via_steal per worker — this is what each
    # worker actually executed (regardless of who pushed it). For a
    # single-producer fanout the producer worker pushes everything;
    # fairness measures whether stealing redistributed evenly.
    pops = [
        int(w.get("popped_local", 0)) + int(w.get("popped_via_steal", 0))
        for w in stats
    ]
    n = len(pops)
    total = sum(pops)
    if n > 0 and total > 0:
        mean = total / n
        if n > 1:
            stdev = statistics.pstdev(pops)
            out["fairness_cv"] = stdev / mean if mean > 0 else None
        else:
            out["fairness_cv"] = 0.0
        # Gini: 0 is perfectly equal, 1 is maximally unequal.
        sorted_pops = sorted(pops)
        cum = 0
        weighted = 0
        for i, v in enumerate(sorted_pops, start=1):
            cum += v
            weighted += i * v
        if cum > 0:
            out["fairness_gini"] = (2 * weighted) / (n * cum) - (n + 1) / n
        out["worker_pop_min"] = min(pops)
        out["worker_pop_max"] = max(pops)
        out["worker_pop_mean"] = mean
        out["worker_pop_counts"] = pops
    return out


# ---------------------------------------------------------------------------
# Subprocess orchestration (one repeat per child, fresh runtime)
# ---------------------------------------------------------------------------


def cfg_to_argv(cfg: FanoutConfig) -> list:
    """Render a ``FanoutConfig`` as CLI args for a child invocation.

    :param cfg: The config to serialize.
    :return: A list of CLI arguments.
    """
    args = [
        "--workers", str(cfg.workers),
        "--child-iters", str(cfg.child_iters),
        "--producer-steps", str(cfg.producer_steps),
        "--payload-rows", str(cfg.payload_rows),
        "--payload-cols", str(cfg.payload_cols),
        "--repeats", "1",
        "--sweep-axis", "none",
    ]
    if cfg.producers is not None:
        args += ["--producers", str(cfg.producers)]
    if cfg.fanout_width is not None:
        args += ["--fanout-width", str(cfg.fanout_width)]
    return args


def run_in_subprocess(cfg: FanoutConfig, repeat_index: int,
                      git_sha: Optional[str]) -> RepeatResult:
    """Run one repeat in a fresh subprocess and return its result.

    :param cfg: A fully-derived config.
    :param repeat_index: Index into the parent's ``repeats[]`` list.
    :param git_sha: Optional git sha forwarded to the child.
    :return: A ``RepeatResult``.
    """
    env = dict(os.environ)
    if git_sha is not None:
        env["BOCPY_BENCH_GIT_SHA"] = git_sha
    cmd = [sys.executable, "-m", "bocpy.examples.fanout_benchmark",
           "--json-stdout"] + cfg_to_argv(cfg)
    total_expected = cfg.producers * cfg.fanout_width * cfg.producer_steps
    timeout = max(120.0, total_expected * 0.002 + 30)
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as ex:
        raise RuntimeError(
            f"subprocess timed out after {timeout}s; "
            f"stderr tail: {(ex.stderr or '')[-400:]!r}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"subprocess exited {proc.returncode}; "
            f"stderr tail: {proc.stderr[-400:]!r}")
    payload = _extract_sentinel_payload(proc.stdout)
    if payload is None:
        raise RuntimeError(
            "child produced no sentinel-framed JSON; "
            f"stderr tail: {proc.stderr[-400:]!r}")
    return RepeatResult(
        repeat_index=repeat_index,
        completed_children=int(payload["completed_children"]),
        elapsed_s=float(payload["elapsed_s"]),
        throughput=float(payload["throughput"]),
        wall_clock_ns_start=int(payload["wall_clock_ns_start"]),
        scheduler_stats=payload.get("scheduler_stats"),
        derived=payload.get("derived"))


def _extract_sentinel_payload(stdout: str) -> Optional[dict]:
    """Find and parse exactly one sentinel-framed JSON object."""
    begin = stdout.find(SENTINEL_BEGIN)
    end = stdout.find(SENTINEL_END)
    if begin < 0 or end < 0 or end < begin:
        return None
    inner = stdout[begin + len(SENTINEL_BEGIN):end].strip()
    try:
        return json.loads(inner)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------


def cfg_for_axis(base: FanoutConfig, axis: str, value) -> FanoutConfig:
    """Clone ``base`` with one axis varied to ``value``.

    :param base: The base config.
    :param axis: One of ``workers``, ``fanout-width``, ``producers``,
        ``child-iters``, ``producer-steps``, ``none``.
    :param value: The axis value.
    :return: A fresh ``FanoutConfig``.
    """
    cfg = FanoutConfig(**asdict(base))
    if axis == "workers":
        cfg.workers = int(value)
        # Re-derive producers/fanout-width when sweeping workers
        # unless the user explicitly pinned them at the base.
        if base.producers is None:
            cfg.producers = None
        if base.fanout_width is None:
            cfg.fanout_width = None
    elif axis == "fanout-width":
        cfg.fanout_width = int(value)
    elif axis == "producers":
        cfg.producers = int(value)
    elif axis == "child-iters":
        cfg.child_iters = int(value)
    elif axis == "producer-steps":
        cfg.producer_steps = int(value)
    elif axis == "none":
        pass
    else:
        raise ValueError(f"unknown axis: {axis}")
    return derive_sizes(cfg)


def summarize_repeats(reps: list) -> dict:
    """Compute mean/stdev/min/max across repeats.

    With <2 repeats, stdev/min/max are emitted as JSON null to avoid
    false zero-height error bars in plots.

    :param reps: A list of ``RepeatResult``.
    :return: A summary dict.
    """
    if not reps:
        return {"mean": None, "stdev": None, "min": None, "max": None}
    throughputs = [r.throughput for r in reps]
    if len(throughputs) < 2:
        return {"mean": throughputs[0], "stdev": None,
                "min": None, "max": None}
    return {
        "mean": statistics.fmean(throughputs),
        "stdev": statistics.stdev(throughputs),
        "min": min(throughputs),
        "max": max(throughputs),
    }


def run_sweep(axis: str, values: list, base: FanoutConfig,
              git_sha: Optional[str], output_path: str,
              metadata: dict) -> dict:
    """Run a sweep, flushing JSON to disk after every point."""
    points = []
    fixed = asdict(base)
    rendered_values = [list(v) if isinstance(v, tuple) else v for v in values]
    sweep_meta = {"axis": axis, "values": rendered_values, "fixed": fixed}

    interrupted = False
    for value in values:
        cfg = cfg_for_axis(base, axis, value)
        err = validate_config(cfg)
        inputs = asdict(cfg)
        if err is not None:
            point = PointResult(inputs=inputs,
                                error={"message": err, "stderr_tail": ""})
            points.append(asdict(point))
            print(f"point {axis}={value}: validation error: {err}",
                  file=sys.stderr)
            _flush_results(output_path, metadata, sweep_meta, points)
            continue

        repeats: list = []
        try:
            for r in range(base.repeats):
                print(f"point {axis}={value} repeat {r + 1}/{base.repeats}: "
                      "spawning child...", file=sys.stderr)
                try:
                    rep = run_in_subprocess(cfg, r, git_sha)
                    repeats.append(rep)
                    print(f"  -> {rep.throughput:.1f} children/s "
                          f"({rep.completed_children} in "
                          f"{rep.elapsed_s:.2f}s)", file=sys.stderr)
                except RuntimeError as ex:
                    point = PointResult(
                        inputs=inputs,
                        repeats=[asdict(r) for r in repeats],
                        error={"message": str(ex), "stderr_tail": ""})
                    points.append(asdict(point))
                    _flush_results(output_path, metadata, sweep_meta, points)
                    repeats = None
                    break
        except KeyboardInterrupt:
            interrupted = True
            metadata["interrupted"] = True
            if repeats:
                point = PointResult(
                    inputs=inputs,
                    repeats=[asdict(r) for r in repeats],
                    error={"message": "interrupted", "stderr_tail": ""})
                points.append(asdict(point))
            _flush_results(output_path, metadata, sweep_meta, points)
            break

        if repeats is None:
            continue

        summary = summarize_repeats(repeats)
        point = PointResult(
            inputs=inputs,
            repeats=[asdict(r) for r in repeats],
            throughput_mean=summary["mean"],
            throughput_stdev=summary["stdev"],
            throughput_min=summary["min"],
            throughput_max=summary["max"])
        points.append(asdict(point))
        _flush_results(output_path, metadata, sweep_meta, points)

    metadata["finished_at"] = datetime.now().isoformat(timespec="seconds")
    metadata["interrupted"] = interrupted or metadata.get("interrupted", False)
    final = _flush_results(output_path, metadata, sweep_meta, points)
    return final


def _flush_results(path: str, metadata: dict, sweep_meta: dict,
                   points: list) -> dict:
    """Atomic write of the results JSON; falls back to in-place on Windows."""
    document = {
        "schema_version": SCHEMA_VERSION,
        "metadata": metadata,
        "sweep": sweep_meta,
        "points": points,
    }
    serialized = json.dumps(document, indent=2, default=_json_default)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(serialized)
    delays = (0.05, 0.1, 0.2)
    for attempt, delay in enumerate(delays):
        try:
            os.replace(tmp, path)
            return document
        except PermissionError:
            if attempt == len(delays) - 1:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(serialized)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return document
            time.sleep(delay)
    return document


def _json_default(obj):
    """Coerce non-JSON-native objects for serialization."""
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    raise TypeError(f"object of type {type(obj).__name__} is not "
                    "JSON-serializable")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def collect_metadata(argv: list, git_sha: Optional[str]) -> dict:
    """Collect metadata for the top of the results JSON."""
    try:
        from importlib.metadata import version
        bocpy_version = version("bocpy")
    except Exception:
        bocpy_version = None
    free_threaded = bool(getattr(sys, "_is_gil_enabled",
                                 lambda: True)() is False)
    return {
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "cpu_count": os.cpu_count() or 0,
        "python_version": sys.version.split()[0],
        "python_implementation": sys.implementation.name,
        "free_threaded": free_threaded,
        "bocpy_version": bocpy_version,
        "git_sha": git_sha,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "argv": list(argv),
        "interrupted": False,
    }


def _git_sha() -> Optional[str]:
    """Read git sha if available; cheap-and-fail-quietly."""
    cached = os.environ.get("BOCPY_BENCH_GIT_SHA")
    if cached:
        return cached
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False)
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_sweep_values(axis: str, raw: Optional[str]) -> list:
    """Parse ``--sweep-values`` per-axis at argparse time.

    :param axis: The sweep axis.
    :param raw: The raw CSV string, or None.
    :return: A list of values appropriate for the axis.
    """
    if axis == "none":
        if raw:
            raise argparse.ArgumentTypeError(
                "--sweep-values must be empty when --sweep-axis is 'none'")
        return [None]
    if raw is None:
        return _default_sweep_values(axis)
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return _default_sweep_values(axis)
    out = []
    for t in tokens:
        try:
            out.append(int(t))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--sweep-values: token {t!r} is not an integer "
                f"(axis={axis})")
    return out


def _default_sweep_values(axis: str) -> list:
    """Return the documented default sweep values for an axis."""
    cpu = os.cpu_count() or 1
    if axis == "workers":
        return sorted(set([1, 2, 4, 8, min(16, cpu)]))
    if axis == "fanout-width":
        return [1, 2, 4, 8, 16, 32]
    if axis == "producers":
        return [1, 2, 4, 8]
    if axis == "child-iters":
        return [1, 2, 4, 8]
    if axis == "producer-steps":
        return [100, 500, 1000, 5000]
    return []


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    p = argparse.ArgumentParser(
        prog="bocpy.examples.fanout_benchmark",
        description="Fanout microbenchmark for the BOC runtime.")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--sweep-axis",
                   choices=("workers", "fanout-width", "producers",
                            "child-iters", "producer-steps", "none"),
                   default="workers")
    p.add_argument("--sweep-values", default=None)
    p.add_argument("--producers", type=int, default=None)
    p.add_argument("--fanout-width", type=int, default=None,
                   dest="fanout_width")
    p.add_argument("--child-iters", type=int, default=1, dest="child_iters")
    p.add_argument("--producer-steps", type=int, default=1000,
                   dest="producer_steps")
    p.add_argument("--payload-rows", type=int, default=16,
                   dest="payload_rows")
    p.add_argument("--payload-cols", type=int, default=16,
                   dest="payload_cols")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--output", default=None)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json-stdout", action="store_true",
                   help="Run a single point and print sentinel-framed "
                        "JSON to stdout (subprocess internal).")
    return p


def args_to_base_cfg(args) -> FanoutConfig:
    """Build a base ``FanoutConfig`` from parsed CLI args."""
    return FanoutConfig(
        workers=args.workers,
        producers=args.producers,
        fanout_width=args.fanout_width,
        child_iters=args.child_iters,
        producer_steps=args.producer_steps,
        payload_rows=args.payload_rows,
        payload_cols=args.payload_cols,
        repeats=args.repeats,
    )


def child_main(args) -> int:
    """Run a single point and emit a sentinel-framed JSON object."""
    cfg = derive_sizes(args_to_base_cfg(args))
    err = validate_config(cfg)
    if err is not None:
        print(f"fanout_benchmark: invalid config: {err}", file=sys.stderr)
        return 2
    rep = run_single_point_body(cfg, repeat_index=0)
    payload = {
        "inputs": asdict(cfg),
        "completed_children": rep.completed_children,
        "elapsed_s": rep.elapsed_s,
        "throughput": rep.throughput,
        "wall_clock_ns_start": rep.wall_clock_ns_start,
        "scheduler_stats": rep.scheduler_stats or [],
    }
    if rep.derived is not None:
        payload["derived"] = rep.derived
    sys.stdout.write("\n" + SENTINEL_BEGIN + "\n")
    sys.stdout.write(json.dumps(payload, default=_json_default))
    sys.stdout.write("\n" + SENTINEL_END + "\n")
    sys.stdout.flush()
    return 0


def parent_main(args) -> int:
    """Run a sweep across the requested axis."""
    base = args_to_base_cfg(args)
    try:
        sweep_values = parse_sweep_values(args.sweep_axis, args.sweep_values)
    except argparse.ArgumentTypeError as ex:
        print(f"fanout_benchmark: {ex}", file=sys.stderr)
        return 2
    for value in sweep_values:
        cfg = cfg_for_axis(base, args.sweep_axis, value)
        err = validate_config(cfg)
        if err is not None:
            print(f"fanout_benchmark: sweep point {args.sweep_axis}={value} "
                  f"invalid: {err}", file=sys.stderr)
            return 2
    git_sha = _git_sha()
    output_path = args.output or _default_output_path()
    metadata = collect_metadata(sys.argv, git_sha)
    run_sweep(args.sweep_axis, sweep_values, base, git_sha,
              output_path, metadata)
    if not args.quiet:
        print(f"results: {output_path}", file=sys.stderr)
    return 0


def _default_output_path() -> str:
    """Compute the default output path under ``results/``."""
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    host = socket.gethostname().replace(os.sep, "_")
    return os.path.join("results", f"fanout-{host}-{ts}.json")


def main() -> int:
    """CLI entry point."""
    if sys.version_info < (3, 12):
        sys.exit("bocpy benchmarks require Python 3.12+ for "
                 "sub-interpreter parallelism")
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.json_stdout:
        return child_main(args)
    return parent_main(args)


if __name__ == "__main__":
    sys.exit(main())
