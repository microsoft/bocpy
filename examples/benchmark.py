"""Chain-ring microbenchmark for the BOC runtime.

This benchmark measures *BOC runtime scaling* (scheduler, 2PL, message
queue, sub-interpreter crossings, return-cown allocation) in isolation
from any application-specific serial work.  It is **not** a measure of
how well your own application will scale: real applications carry
serial costs (data structure construction, scheduling logic,
result drainage) that this benchmark deliberately eliminates.

A few load-bearing caveats baked into the design:

* Each behavior allocates a fresh return ``Cown`` (the auto-generated
  one returned by ``@when``).  At thousands of behaviors per second
  this is a real, version-dependent constant in every sample.
* ``ChainState`` crosses the interpreter boundary via XIData on every
  reschedule; for tiny payloads, marshaling can rival the useful work.
* The ``group-size`` sweep varies acquired-set cardinality and CPU work
  together (the inner loop multiplies every window slot into
  ``window[0]``, ``iters * group_size`` matrix multiplies per
  behavior).  It is not an isolated 2PL-cost knob.
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

from bocpy import (Cown, Matrix, noticeboard, notice_write, receive, send,
                   start, wait, when)
from bocpy import _core

# Sentinels for the parent/child JSON protocol.  Uppercase so the
# transpiler keeps them as module-level constants in the worker export.
SENTINEL_BEGIN = "---BOCPY-BENCH-BEGIN---"
SENTINEL_END = "---BOCPY-BENCH-END---"
SCHEMA_VERSION = 1


def _physical_cpu_count() -> int:
    """Return physical core count, falling back to logical or 1.

    Used as the oversubscription threshold so warnings fire when the
    requested worker count starts using SMT siblings rather than
    waiting until logical cores are exhausted.

    :return: A positive integer.
    """
    n = _core.physical_cpu_count()
    if n > 0:
        return n
    return os.cpu_count() or 1


# ---------------------------------------------------------------------------
# Behavior code (chain workload)
# ---------------------------------------------------------------------------


class ChainState:
    """Per-chain mutable state carried inside a ``Cown[ChainState]``.

    Holds ints only.  The chain's ring of ``Cown[Matrix]`` lives in the
    noticeboard under ``f"ring_{ring_id}"`` so it is materialized once
    per worker (and cached for the lifetime of ``NB_VERSION``) instead
    of being marshaled through XIData on every reschedule.
    """

    def __init__(self, chain_id: int, ring_id: int, head_idx: int,
                 iters: int, stride: int, ring_size: int):
        """Initialize a chain state.

        :param chain_id: A unique id within the workload.
        :param ring_id: Index of the ring this chain runs on.  Must
            correspond to a ``f"ring_{ring_id}"`` entry already
            written to the noticeboard.
        :param head_idx: Initial head position on the ring.
        :param iters: Inner-loop matrix multiplications per window slot.
        :param stride: Step between successive windows.
        :param ring_size: Number of cowns on the ring.
        """
        self.chain_id = chain_id
        self.ring_id = ring_id
        self.head_idx = head_idx
        self.count = 0
        self.iters = iters
        self.stride = stride
        self.ring_size = ring_size


def next_window(cs: "ChainState", group_size: int) -> list:
    """Compute the next sliding window of cowns for a chain.

    Reads the chain's ring from the noticeboard.  Must be called from
    inside a behavior so that ``noticeboard()`` returns the cached
    snapshot for the current ``NB_VERSION``.

    :param cs: The chain state.
    :param group_size: Number of adjacent cowns in the window.
    :return: ``list[Cown[Matrix]]`` for the next acquired set.
    """
    ring = noticeboard()[f"ring_{cs.ring_id}"]
    return [ring[(cs.head_idx + i * cs.stride) % cs.ring_size]
            for i in range(group_size)]


def schedule_step(state_cown: Cown, window_list: list, group_size: int) -> None:
    """Schedule one chain step with the given window.

    The static ``@when`` decorator inside this helper is rewritten by
    the transpiler into a ``whencall`` invocation, so this function
    works correctly when called from a worker sub-interpreter (where
    the Python ``when`` decorator is not wired up).

    :param state_cown: The chain's state cown.
    :param window_list: Adjacent cowns to acquire for this step.
    :param group_size: Window size, captured into the behavior.
    """
    @when(state_cown, window_list)
    def _step(state, window):
        cs = state.value
        # When ``cr_null`` is set, skip the matmul loop entirely.  The
        # behavior still acquires its window of cowns, mutates
        # ``ChainState``, and reschedules itself — so the measured
        # throughput reflects pure BOC runtime overhead (2PL, queue
        # ops, sub-interpreter crossings, return-cown allocation)
        # with the application work removed.
        if not noticeboard().get("cr_null", False):
            # The inner loop's first slot multiplies window[0] by itself.
            # Intentional — it keeps the per-behavior multiply count
            # exactly `iters * group_size`.
            for _ in range(cs.iters):
                for c in window:
                    window[0].value = window[0].value @ c.value

        cs.count += 1
        cs.head_idx = (cs.head_idx + cs.stride) % cs.ring_size
        if not noticeboard().get("cr_stop", False):
            # Pass the already-acquired `state` cown wrapper directly
            # rather than the closure-captured `state_cown` to keep the
            # capture set minimal.
            schedule_step(state, next_window(cs, group_size), group_size)


# ---------------------------------------------------------------------------
# Configuration and result types (plain data only; no Cowns)
# ---------------------------------------------------------------------------


@dataclass
class BenchConfig:
    """Plain-data benchmark configuration.

    Holds only ints / floats / strings / lists of the same so that an
    instance can stay live in ``main()``'s frame across ``wait()``
    without ``stop_workers`` finding any bare Cowns to acquire.
    """

    workers: int = 1
    duration: float = 5.0
    warmup: float = 1.0
    iters: int = 2000
    group_size: int = 2
    stride: int = 1
    rings: Optional[int] = None
    chains_per_ring: Optional[int] = None
    ring_size: int = 128
    payload_rows: int = 16
    payload_cols: int = 16
    repeats: int = 1
    null_payload: bool = False


@dataclass
class RepeatResult:
    """Plain-data result for a single repeat of one sweep point."""

    repeat_index: int
    completed_behaviors: int
    elapsed_s: float
    throughput: float
    wall_clock_ns_start: int
    scheduler_stats: Optional[list] = None
    queue_stats: Optional[list] = None
    # ``derived`` holds the post-processed metrics computed from the
    # per-window scheduler-stats delta (see
    # ``compute_derived_metrics``).
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
# Sizing / validation helpers (parent-side, no BOC required)
# ---------------------------------------------------------------------------


def derive_sizes(cfg: BenchConfig) -> BenchConfig:
    """Auto-size ``rings`` and ``chains_per_ring`` if not overridden.

    :param cfg: An input config (mutated and returned).
    :return: The same config with ``rings`` / ``chains_per_ring`` set.
    """
    if cfg.chains_per_ring is None:
        # Use a small per-ring chain count (4) so chains never collide
        # on adjacent slots as they advance. Independent rings carry
        # the load instead.
        cfg.chains_per_ring = max(
            1, cfg.ring_size // (cfg.group_size * cfg.stride * 8))
    if cfg.rings is None:
        # Bias toward more *rings* rather than more chains-per-ring:
        # chains on the same ring contend for adjacent slots as they
        # advance, so per-ring concurrency is bounded well below
        # ``chains_per_ring``. Independent rings, by contrast, never
        # collide. Provision at least ``workers * 4`` rings so every
        # worker sees a deep, independent supply of ready chains and
        # the measured throughput reflects scheduler scaling rather
        # than workload starvation.
        cfg.rings = max(cfg.workers * 16 // cfg.chains_per_ring,
                        cfg.workers * 4)
    return cfg


def validate_config(cfg: BenchConfig) -> Optional[str]:
    """Validate a fully-derived config; return an error string or None.

    Hard errors only.  Soft warnings (``duration < 1.0``, oversubscribed
    workers) are emitted by the caller rather than failing here.

    :param cfg: A config with ``rings`` and ``chains_per_ring`` set.
    :return: An error message, or ``None`` if the config is valid.
    """
    if cfg.group_size * cfg.stride * 2 > cfg.ring_size:
        return (f"group_size*stride*2 ({cfg.group_size}*{cfg.stride}*2) "
                f"> ring_size ({cfg.ring_size}); chains would collide")
    if cfg.workers < 1:
        return f"workers must be >= 1, got {cfg.workers}"
    if cfg.iters < 1:
        return f"iters must be >= 1, got {cfg.iters}"
    if cfg.payload_rows < 1 or cfg.payload_cols < 1:
        return "payload dimensions must be >= 1"
    if cfg.duration <= 0 or cfg.warmup < 0:
        return "duration must be > 0 and warmup must be >= 0"
    return None


def emit_soft_warnings(cfg: BenchConfig, cpu_count: int) -> None:
    """Print soft warnings for unusual configs to stderr.

    :param cfg: The fully-derived config.
    :param cpu_count: Detected CPU count for oversubscription check.
    """
    if cfg.duration < 1.0:
        print(f"warning: duration={cfg.duration}s is short; results will "
              "be noisy", file=sys.stderr)
    if cfg.workers > cpu_count:
        print(f"warning: workers={cfg.workers} exceeds physical core "
              f"count={cpu_count}; oversubscribed (SMT siblings or "
              f"hyperthreads will be used)", file=sys.stderr)


# ---------------------------------------------------------------------------
# Workload construction
# ---------------------------------------------------------------------------


def build_workload(cfg: BenchConfig):
    """Build per-ring cowns and per-chain state cowns.

    Each ring is published to the noticeboard under ``f"ring_{r}"``.
    Workers read it back via ``noticeboard()`` inside ``_step``; the
    noticeboard's per-worker version-cache means the ring is
    materialized once per worker per ``NB_VERSION`` instead of being
    marshaled through XIData on every reschedule.

    :param cfg: A fully-derived config.
    :return: A ``(rings, state_cowns)`` tuple.  ``rings`` is
        ``list[list[Cown[Matrix]]]``; ``state_cowns`` is
        ``list[Cown[ChainState]]``.  Both containers are invisible to
        ``stop_workers`` (it does not recurse into containers).
    """
    rings = []
    state_cowns = []
    chain_id = 0
    for r in range(cfg.rings):
        ring = [Cown(Matrix.uniform(0.0, 1.0,
                                    (cfg.payload_rows, cfg.payload_cols)))
                for _ in range(cfg.ring_size)]
        rings.append(ring)
        notice_write(f"ring_{r}", ring)
        # Spread chains evenly across the ring so adjacent chains'
        # initial windows don't overlap.
        spacing = max(1, cfg.ring_size // cfg.chains_per_ring)
        for k in range(cfg.chains_per_ring):
            head = (k * spacing) % cfg.ring_size
            cs = ChainState(chain_id=chain_id, ring_id=r, head_idx=head,
                            iters=cfg.iters, stride=cfg.stride,
                            ring_size=cfg.ring_size)
            state_cowns.append(Cown(cs))
            chain_id += 1
    return rings, state_cowns


# ---------------------------------------------------------------------------
# Snapshot helpers (used by the measurement flow)
# ---------------------------------------------------------------------------


def schedule_snap(state_cowns: list) -> None:
    """Schedule the final snapshot + publish behaviors.

    See the module docstring for the snap ordering invariant.  This
    helper is structured so that the bare ``snap`` and ``_publish``
    return-cown locals fall out of scope at its return boundary,
    satisfying the no-bare-Cowns-in-main rule before ``wait()`` runs.

    :param state_cowns: Every chain's state cown.
    """
    @when(state_cowns)
    def snap(states):
        return sum(s.value.count for s in states)

    notice_write("cr_stop", True)

    @when(snap)
    def _publish(s):
        send("snap", s.value)


def emit_chain_snapshot(state_cown: Cown, tag: str) -> None:
    """Send a chain's ``(count, head_idx)`` over the queue under ``tag``.

    Used by tests that need to inspect chain progress directly.  The
    helper lives in this module so the ``@when`` decorator runs through
    the transpiler that registered ``schedule_step``.

    :param state_cown: The chain's state cown.
    :param tag: The tag to ``send`` the snapshot under.
    """
    @when(state_cown)
    def _emit(s):
        send(tag, (s.value.count, s.value.head_idx))


# ---------------------------------------------------------------------------
# Single-point measurement (in-process; one BOC start/wait cycle)
# ---------------------------------------------------------------------------


def run_single_point_body(cfg: BenchConfig, repeat_index: int) -> RepeatResult:
    """Run one chain-ring measurement in a fresh BOC runtime.

    Snapshots ``_core.scheduler_stats()`` after warmup, then captures
    the post-session snapshot via ``wait(stats=True)``. The **delta**
    of the two is stored in ``RepeatResult.scheduler_stats`` so warmup
    pushes do not pollute the per-window counters consumed by
    ``compute_derived_metrics``.

    :param cfg: The fully-derived config.
    :param repeat_index: Index of this repeat for reporting.
    :return: A ``RepeatResult`` with no Cown references.
    """
    # Start the runtime first: ``build_workload`` writes rings to the
    # noticeboard, and noticeboard writes require the runtime to be
    # running.
    start(worker_count=cfg.workers)
    rings, state_cowns = build_workload(cfg)
    # Publish the null-payload toggle so worker behaviors can read it
    # from their per-behavior noticeboard snapshot.  Written before the
    # warmup sleep so the noticeboard thread has flushed it well
    # before t_measure_start.
    notice_write("cr_null", cfg.null_payload)
    payload_bytes = cfg.payload_rows * cfg.payload_cols * 8
    total_bytes = cfg.rings * cfg.ring_size * payload_bytes
    print(f"workload: chain rings={cfg.rings} ring_size={cfg.ring_size} "
          f"chains={cfg.rings * cfg.chains_per_ring} "
          f"payload={cfg.payload_rows}x{cfg.payload_cols} "
          f"(~{total_bytes / 1024:.1f} KiB matrix data)",
          file=sys.stderr)

    try:
        # Kick off one chain per (ring, chain-slot) pair.  Recompute the
        # head positions exactly the way `build_workload` chose them:
        # we cannot read `cs_cown.value` from the main thread because
        # Cowns are released to the runtime on construction.
        spacing = max(1, cfg.ring_size // cfg.chains_per_ring)
        chain_idx = 0
        for r in range(cfg.rings):
            for k in range(cfg.chains_per_ring):
                cs_cown = state_cowns[chain_idx]
                head = (k * spacing) % cfg.ring_size
                window = [rings[r][(head + i * cfg.stride) % cfg.ring_size]
                          for i in range(cfg.group_size)]
                schedule_step(cs_cown, window, cfg.group_size)
                chain_idx += 1

        time.sleep(cfg.warmup)
        from bocpy import _core
        sched_stats_warm = _core.scheduler_stats()
        wall_clock_ns_start = time.time_ns()
        t_measure_start = time.perf_counter()
        time.sleep(cfg.duration)

        schedule_snap(state_cowns)
        msg = receive(["snap"], 60.0 + cfg.duration)
        t_snap_received = time.perf_counter()
        if msg is None or msg[0] != "snap":
            raise RuntimeError("snap behavior did not publish in time")
        _, total = msg
        elapsed_s = t_snap_received - t_measure_start

        # Snapshot tagged-queue counters BEFORE wait() tears the
        # runtime down. Per-tag assignments are rebound on the next
        # start(), so capture here while they still reflect this run.
        queue_stats_snap = (
            _core.queue_stats() if hasattr(_core, "queue_stats") else None
        )
    finally:
        # Drop bare-Cown locals before wait().
        del rings
        del state_cowns
        # ``wait(stats=True)`` returns the per-worker scheduler_stats
        # snapshot captured AFTER all behaviors completed but BEFORE
        # the per-worker array is freed -- the only correct moment
        # for a session-final snapshot.
        sched_stats_end = wait(stats=True)

    sched_stats_delta = _delta_scheduler_stats(sched_stats_warm,
                                               sched_stats_end)
    throughput = total / elapsed_s if elapsed_s > 0 else 0.0
    return RepeatResult(repeat_index=repeat_index,
                        completed_behaviors=int(total),
                        elapsed_s=elapsed_s,
                        throughput=throughput,
                        wall_clock_ns_start=wall_clock_ns_start,
                        scheduler_stats=sched_stats_delta,
                        queue_stats=queue_stats_snap,
                        derived=compute_derived_metrics(sched_stats_delta,
                                                        int(total)))


# ---------------------------------------------------------------------------
# Stats-delta + derived metrics
# ---------------------------------------------------------------------------


# Counter fields in ``_core.scheduler_stats()`` that are monotonically
# increasing per-worker counters and therefore subtractable across two
# snapshots.  Non-counter fields (``last_steal_attempt_ns``,
# ``parked``) are carried over from the end-of-window snapshot
# unchanged because subtracting them is meaningless.
_COUNTER_FIELDS = (
    "pushed_local",
    "dispatched_to_pending",
    "pushed_remote",
    "popped_local",
    "popped_via_steal",
    "enqueue_cas_retries",
    "dequeue_cas_retries",
    "batch_resets",
    "steal_attempts",
    "steal_failures",
    "fairness_arm_fires",
)


def _delta_scheduler_stats(warm: Optional[list],
                           end: Optional[list]) -> Optional[list]:
    """Return per-worker ``end - warm`` for the monotonic counter fields.

    Non-counter fields (``parked``, ``last_steal_attempt_ns``) are
    copied from ``end`` unchanged.  If either snapshot is missing or
    the worker counts disagree (for example because the runtime tore
    down between snapshots), returns the end snapshot unchanged.

    :param warm: End-of-warmup snapshot (per-worker dicts).
    :param end: End-of-measurement-window snapshot.
    :return: Per-worker delta dicts.
    """
    if not end:
        return end
    if not warm or len(warm) != len(end):
        return end
    out = []
    for w, e in zip(warm, end):
        d = dict(e)
        for k in _COUNTER_FIELDS:
            if k in e and k in w:
                d[k] = int(e[k]) - int(w[k])
        out.append(d)
    return out


def compute_derived_metrics(stats: Optional[list],
                            completed_behaviors: int) -> dict:
    """Compute the dispatch-contention metrics from a stats delta.

    :param stats: Per-worker delta stats from ``_delta_scheduler_stats``.
    :param completed_behaviors: Total completed behaviors over the
        measurement window (matches the throughput numerator).
    :return: A dict with ``producer_worker_index``,
        ``enq_retry_ratio``, ``steal_yield``, ``idle_ratio``, and
        ``producer_pushed_local`` so callers can reconstruct the
        ratio's numerator / denominator without re-walking ``stats``.
    """
    out = {
        "producer_worker_index": None,
        "producer_pushed_local": 0,
        "producer_enqueue_cas_retries": 0,
        "enq_retry_ratio": None,
        "steal_yield": None,
        "idle_ratio": None,
    }
    if not stats:
        return out
    # Producer worker = the worker with the most local pushes over
    # the measurement window. For chain that is whichever worker's
    # queue saw the most ``schedule_fifo`` evicts of ``pending`` to
    # ``q``.
    pushed_local = [int(w.get("pushed_local", 0)) for w in stats]
    if not pushed_local or max(pushed_local) == 0:
        return out
    p_idx = max(range(len(pushed_local)), key=lambda i: pushed_local[i])
    p_pushed = pushed_local[p_idx]
    p_enq_r = int(stats[p_idx].get("enqueue_cas_retries", 0))
    out["producer_worker_index"] = p_idx
    out["producer_pushed_local"] = p_pushed
    out["producer_enqueue_cas_retries"] = p_enq_r
    out["enq_retry_ratio"] = (p_enq_r / p_pushed) if p_pushed > 0 else None

    total_steal = sum(int(w.get("popped_via_steal", 0)) for w in stats)
    if completed_behaviors > 0:
        out["steal_yield"] = total_steal / completed_behaviors

    total_attempts = sum(int(w.get("steal_attempts", 0)) for w in stats)
    total_failures = sum(int(w.get("steal_failures", 0)) for w in stats)
    if total_attempts > 0:
        out["idle_ratio"] = total_failures / total_attempts
    return out


# ---------------------------------------------------------------------------
# Subprocess orchestration
# ---------------------------------------------------------------------------


def cfg_to_argv(cfg: BenchConfig) -> list:
    """Render a ``BenchConfig`` as CLI args for a child invocation.

    :param cfg: The config to serialize.
    :return: A list of CLI arguments suitable for child invocation.
    """
    args = [
        "--workers", str(cfg.workers),
        "--duration", str(cfg.duration),
        "--warmup", str(cfg.warmup),
        "--iters", str(cfg.iters),
        "--group-size", str(cfg.group_size),
        "--stride", str(cfg.stride),
        "--ring-size", str(cfg.ring_size),
        "--payload-rows", str(cfg.payload_rows),
        "--payload-cols", str(cfg.payload_cols),
        "--repeats", "1",
        "--sweep-axis", "none",
    ]
    if cfg.rings is not None:
        args += ["--rings", str(cfg.rings)]
    if cfg.chains_per_ring is not None:
        args += ["--chains-per-ring", str(cfg.chains_per_ring)]
    if cfg.null_payload:
        args += ["--null-payload"]
    return args


# Sidechannel: the parent passes its --emit-scheduler-stats flag down
# to the child via an env var so cfg_to_argv stays a pure function of
# BenchConfig (the flag is a reporting concern, not a workload knob).
BOCPY_BENCH_EMIT_SCHED_STATS_ENV = "BOCPY_BENCH_EMIT_SCHED_STATS"


def run_in_subprocess(cfg: BenchConfig, repeat_index: int,
                      git_sha: Optional[str]) -> RepeatResult:
    """Run one repeat in a fresh subprocess and return its result.

    On non-zero exit / timeout / missing sentinel, raises
    ``RuntimeError`` with a stderr-tail diagnostic so the caller can
    record an ``error`` entry on the point.

    :param cfg: A fully-derived config with ``repeats`` ignored.
    :param repeat_index: Index into the parent's ``repeats[]`` list.
    :param git_sha: Optional git sha to forward to the child.
    """
    env = dict(os.environ)
    if git_sha is not None:
        env["BOCPY_BENCH_GIT_SHA"] = git_sha

    extra = []
    if env.get(BOCPY_BENCH_EMIT_SCHED_STATS_ENV) == "1":
        extra.append("--emit-scheduler-stats")

    cmd = [sys.executable, "-m", "bocpy.examples.benchmark",
           "--json-stdout"] + cfg_to_argv(cfg) + extra
    timeout = max(cfg.duration * 3 + 30, cfg.duration + cfg.warmup + 60)
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
        completed_behaviors=int(payload["completed_behaviors"]),
        elapsed_s=float(payload["elapsed_s"]),
        throughput=float(payload["throughput"]),
        wall_clock_ns_start=int(payload["wall_clock_ns_start"]),
        scheduler_stats=payload.get("scheduler_stats"),
        queue_stats=payload.get("queue_stats"),
        derived=payload.get("derived"))


def _extract_sentinel_payload(stdout: str) -> Optional[dict]:
    """Find and parse exactly one sentinel-framed JSON object.

    :param stdout: The captured child stdout.
    :return: The parsed payload, or ``None`` if no valid frame.
    """
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
# Sweep orchestration (parent side)
# ---------------------------------------------------------------------------


def cfg_for_axis(base: BenchConfig, axis: str, value) -> BenchConfig:
    """Clone ``base`` with one axis varied to ``value``.

    :param base: The base config.
    :param axis: One of ``workers``, ``iters``, ``group-size``,
        ``payload``, ``none``.
    :param value: The axis value (an ``int`` for most axes; a
        ``(rows, cols)`` tuple for ``payload``).
    :return: A fresh ``BenchConfig`` with that axis applied.
    """
    cfg = BenchConfig(**asdict(base))
    # Reset auto-sized fields so each point recomputes.
    cfg.rings = base.rings
    cfg.chains_per_ring = base.chains_per_ring
    if axis == "workers":
        cfg.workers = int(value)
        cfg.rings = None
        cfg.chains_per_ring = None
    elif axis == "iters":
        cfg.iters = int(value)
    elif axis == "group-size":
        cfg.group_size = int(value)
        cfg.chains_per_ring = None
        cfg.rings = None
    elif axis == "payload":
        cfg.payload_rows, cfg.payload_cols = value
    elif axis == "none":
        pass
    else:
        raise ValueError(f"unknown axis: {axis}")
    return derive_sizes(cfg)


def summarize_repeats(reps: list) -> dict:
    """Compute mean/stdev/min/max across repeats with the null-stdev rule.

    With fewer than 2 repeats, ``stdev`` / ``min`` / ``max`` are
    emitted as JSON null rather than zero, to avoid false zero-height
    error bars in downstream plots.

    :param reps: A list of ``RepeatResult``.
    :return: A dict with mean, stdev, min, max.
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


def run_sweep(axis: str, values: list, base: BenchConfig,
              git_sha: Optional[str], output_path: str,
              metadata: dict) -> dict:
    """Run a sweep, flushing JSON to disk after every point.

    :param axis: Sweep axis name.
    :param values: Per-axis values in order.
    :param base: Base configuration.
    :param git_sha: Optional git sha to forward to children.
    :param output_path: Destination JSON file.
    :param metadata: Initial metadata dict (will be updated with
        ``finished_at`` at end).
    :return: The final results dict (also written to disk).
    """
    points = []
    fixed = asdict(base)
    fixed.pop("workers", None) if axis == "workers" else None
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
                    print(f"  -> {rep.throughput:.1f} behaviors/s "
                          f"({rep.completed_behaviors} in "
                          f"{rep.elapsed_s:.2f}s)", file=sys.stderr)
                except RuntimeError as ex:
                    point = PointResult(
                        inputs=inputs,
                        repeats=[asdict(r) for r in repeats],
                        error={"message": str(ex), "stderr_tail": ""})
                    points.append(asdict(point))
                    _flush_results(output_path, metadata, sweep_meta, points)
                    repeats = None  # marker: already appended
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
    """Atomic write of the results JSON; falls back to in-place on Windows.

    :param path: Destination file path.
    :param metadata: Top-level metadata dict.
    :param sweep_meta: Sweep description dict.
    :param points: List of point dicts.
    :return: The full results document that was written.
    """
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
                print(f"warning: atomic rename failed after {len(delays)} "
                      "attempts; falling back to in-place overwrite",
                      file=sys.stderr)
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
    """Coerce non-JSON-native objects (e.g. tuples) for serialization.

    :param obj: An object json.dumps could not serialize natively.
    :return: A JSON-serializable representation.
    """
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    raise TypeError(f"object of type {type(obj).__name__} is not "
                    "JSON-serializable")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def collect_metadata(argv: list, git_sha: Optional[str]) -> dict:
    """Collect metadata for the top of the results JSON.

    :param argv: The parent's ``sys.argv``.
    :param git_sha: The git sha (or None).
    :return: A metadata dict.
    """
    try:
        bocpy_version = _read_bocpy_version()
    except Exception:
        bocpy_version = None

    free_threaded = bool(getattr(sys, "_is_gil_enabled",
                                 lambda: True)() is False)
    return {
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "cpu_count": os.cpu_count() or 0,
        "physical_cpu_count": _physical_cpu_count(),
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


def _read_bocpy_version() -> Optional[str]:
    """Best-effort read of bocpy's version from importlib.metadata.

    :return: Version string or None on failure.
    """
    try:
        from importlib.metadata import version
        return version("bocpy")
    except Exception:
        return None


def _git_sha() -> Optional[str]:
    """Read git sha if available; cheap-and-fail-quietly.

    :return: A 12-char abbreviated sha, or None.
    """
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
# ASCII table renderer
# ---------------------------------------------------------------------------


def render_table(document: dict) -> str:
    """Render a compact ASCII summary table from a results document.

    :param document: A loaded results JSON.
    :return: A multi-line string ready to print.
    """
    axis = document["sweep"]["axis"]
    points = document["points"]
    interrupted = document.get("metadata", {}).get("interrupted", False)

    lines = []
    show_speedup = axis == "workers"
    baseline = None
    if show_speedup and points:
        first = points[0]
        if interrupted or first.get("error") is not None \
                or first.get("throughput_mean") is None:
            show_speedup = False
            lines.append("note: speedup/efficiency suppressed (baseline "
                         "missing, errored, or interrupted run)")
        else:
            baseline = first["throughput_mean"]

    headers = [axis, "throughput", "stdev"]
    if show_speedup:
        headers += ["speedup", "efficiency"]
    rows = []
    for pt in points:
        if pt.get("error") is not None:
            row = [_axis_label(axis, pt), "ERROR", "-"]
            if show_speedup:
                row += ["-", "-"]
            rows.append(row)
            continue
        mean = pt.get("throughput_mean")
        stdev = pt.get("throughput_stdev")
        row = [
            _axis_label(axis, pt),
            f"{mean:.1f}" if mean is not None else "-",
            f"{stdev:.1f}" if stdev is not None else "-",
        ]
        if show_speedup:
            speedup = (mean / baseline) if mean and baseline else None
            workers = pt["inputs"]["workers"]
            efficiency = (speedup / workers) if speedup and workers else None
            row += [
                f"{speedup:.2f}x" if speedup is not None else "-",
                f"{efficiency:.0%}" if efficiency is not None else "-",
            ]
        rows.append(row)

    widths = [max(len(h), max((len(r[i]) for r in rows), default=0))
              for i, h in enumerate(headers)]
    sep = "-+-".join("-" * w for w in widths)
    lines.append(" | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    lines.append(sep)
    for r in rows:
        lines.append(" | ".join(r[i].ljust(widths[i]) for i in range(len(r))))
    return "\n".join(lines)


def _axis_label(axis: str, pt: dict) -> str:
    """Render the axis cell value for a point row.

    :param axis: Sweep axis name.
    :param pt: A point dict.
    :return: A string for the axis column.
    """
    inputs = pt.get("inputs", {})
    if axis == "workers":
        return str(inputs.get("workers"))
    if axis == "iters":
        return str(inputs.get("iters"))
    if axis == "group-size":
        return str(inputs.get("group_size"))
    if axis == "payload":
        return f"{inputs.get('payload_rows')}x{inputs.get('payload_cols')}"
    return "-"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_payload_token(token: str) -> tuple:
    """Parse a payload token of the form ``"<rows>x<cols>"``.

    :param token: The CLI token.
    :return: A ``(rows, cols)`` tuple.
    """
    if "x" not in token:
        raise argparse.ArgumentTypeError(
            f"payload value {token!r} must look like '<rows>x<cols>'")
    rs, cs = token.split("x", 1)
    try:
        rows, cols = int(rs), int(cs)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"payload value {token!r}: rows and cols must be integers")
    if rows < 1 or cols < 1:
        raise argparse.ArgumentTypeError(
            f"payload value {token!r}: rows and cols must be >= 1")
    return (rows, cols)


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
    if axis in ("workers", "iters", "group-size"):
        out = []
        for t in tokens:
            try:
                out.append(int(t))
            except ValueError:
                raise argparse.ArgumentTypeError(
                    f"--sweep-values: token {t!r} is not an integer "
                    f"(axis={axis})")
        return out
    if axis == "payload":
        return [parse_payload_token(t) for t in tokens]
    raise argparse.ArgumentTypeError(f"unknown axis: {axis}")


def _default_sweep_values(axis: str) -> list:
    """Return the documented default sweep values for an axis.

    :param axis: The sweep axis name.
    :return: A list of default values.
    """
    cpu = _physical_cpu_count()
    if axis == "workers":
        return sorted(set([1, 2, 4, 8, min(16, cpu)]))
    if axis == "iters":
        return [250, 500, 1000, 2000, 4000, 8000]
    if axis == "group-size":
        return [1, 2, 4, 8]
    if axis == "payload":
        return [(4, 4), (8, 8), (16, 16), (32, 32), (64, 64)]
    return []


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    :return: A configured ``argparse.ArgumentParser``.
    """
    p = argparse.ArgumentParser(
        prog="bocpy.examples.benchmark",
        description="Microbenchmark for the BOC runtime.")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--sweep-axis",
                   choices=("workers", "iters", "group-size", "payload",
                            "none"),
                   default="workers")
    p.add_argument("--sweep-values", default=None)
    p.add_argument("--duration", type=float, default=5.0)
    p.add_argument("--warmup", type=float, default=None)
    p.add_argument("--iters", type=int, default=2000)
    p.add_argument("--group-size", type=int, default=2, dest="group_size")
    p.add_argument("--stride", type=int, default=1)
    p.add_argument("--rings", type=int, default=None)
    p.add_argument("--chains-per-ring", type=int, default=None,
                   dest="chains_per_ring")
    p.add_argument("--ring-size", type=int, default=128, dest="ring_size")
    p.add_argument("--payload-rows", type=int, default=16,
                   dest="payload_rows")
    p.add_argument("--payload-cols", type=int, default=16,
                   dest="payload_cols")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--null-payload", dest="null_payload",
                   action="store_true", default=False,
                   help="Skip the matmul inner loop in each behavior. "
                        "Throughput then reflects pure BOC runtime "
                        "overhead with the application work removed.")
    p.add_argument("--output", default=None)
    p.add_argument("--table", dest="table", action="store_true", default=None)
    p.add_argument("--no-table", dest="table", action="store_false")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--emit-scheduler-stats", dest="emit_scheduler_stats",
                   action="store_true", default=False,
                   help="Capture _core.scheduler_stats() and "
                        "_core.queue_stats() snapshots after each "
                        "repeat and embed them in the result JSON.")
    p.add_argument("--json-stdout", action="store_true",
                   help="Run a single point and print sentinel-framed "
                        "JSON to stdout (subprocess internal).")
    p.add_argument("--print-table", default=None,
                   help="Print a table from an existing JSON file and exit.")
    return p


def args_to_base_cfg(args) -> BenchConfig:
    """Build a base ``BenchConfig`` from parsed CLI args.

    :param args: The parsed argparse namespace.
    :return: A ``BenchConfig`` (not yet derived).
    """
    workers = args.workers if args.workers is not None else 1
    warmup = args.warmup
    if warmup is None:
        warmup = min(1.0, args.duration * 0.1)
    return BenchConfig(
        workers=workers,
        duration=args.duration,
        warmup=warmup,
        iters=args.iters,
        group_size=args.group_size,
        stride=args.stride,
        rings=args.rings,
        chains_per_ring=args.chains_per_ring,
        ring_size=args.ring_size,
        payload_rows=args.payload_rows,
        payload_cols=args.payload_cols,
        repeats=args.repeats,
        null_payload=args.null_payload,
    )


def child_main(args) -> int:
    """Run a single point and emit a sentinel-framed JSON object.

    Used by ``run_in_subprocess``.  The child does **not** run the
    cross-worker validation gate — that runs once in the parent before
    any sweep child is spawned.

    :param args: The parsed argparse namespace.
    :return: Process exit code.
    """
    cfg = derive_sizes(args_to_base_cfg(args))
    err = validate_config(cfg)
    if err is not None:
        print(f"benchmark: invalid config: {err}", file=sys.stderr)
        return 2
    emit_soft_warnings(cfg, _physical_cpu_count())
    rep = run_single_point_body(cfg, repeat_index=0)
    payload = {
        "inputs": asdict(cfg),
        "completed_behaviors": rep.completed_behaviors,
        "elapsed_s": rep.elapsed_s,
        "throughput": rep.throughput,
        "wall_clock_ns_start": rep.wall_clock_ns_start,
    }
    if args.emit_scheduler_stats:
        # Read from the snapshot taken INSIDE run_single_point_body,
        # before wait() freed the per-worker array. Querying _core
        # here would return empty lists.
        payload["scheduler_stats"] = rep.scheduler_stats or []
        payload["queue_stats"] = rep.queue_stats or []
    # Always forward derived metrics (small dict; harmless when None).
    if rep.derived is not None:
        payload["derived"] = rep.derived
    sys.stdout.write("\n" + SENTINEL_BEGIN + "\n")
    sys.stdout.write(json.dumps(payload, default=_json_default))
    sys.stdout.write("\n" + SENTINEL_END + "\n")
    sys.stdout.flush()
    return 0


def parent_main(args) -> int:
    """Run a sweep across the requested axis.

    :param args: The parsed argparse namespace.
    :return: Process exit code.
    """
    base = args_to_base_cfg(args)
    try:
        sweep_values = parse_sweep_values(args.sweep_axis, args.sweep_values)
    except argparse.ArgumentTypeError as ex:
        print(f"benchmark: {ex}", file=sys.stderr)
        return 2

    # Pre-spawn validation across every sweep point.
    cpu = _physical_cpu_count()
    derived_points = []
    for value in sweep_values:
        cfg = cfg_for_axis(base, args.sweep_axis, value)
        err = validate_config(cfg)
        if err is not None:
            print(f"benchmark: sweep point {args.sweep_axis}={value} "
                  f"invalid: {err}", file=sys.stderr)
            return 2
        emit_soft_warnings(cfg, cpu)
        derived_points.append(cfg)

    git_sha = _git_sha()

    # Sidechannel: forward the emit-scheduler-stats flag to children
    # via an env var. cfg_to_argv stays a pure function of BenchConfig
    # because the flag is a reporting concern, not a workload knob.
    if args.emit_scheduler_stats:
        os.environ[BOCPY_BENCH_EMIT_SCHED_STATS_ENV] = "1"

    # Wall-clock estimate for sweep duration.
    startup_slack = 5.0
    est = sum((cfg.duration + cfg.warmup + startup_slack) * base.repeats
              for cfg in derived_points)
    print(f"sweep estimate: {len(derived_points)} points "
          f"x {base.repeats} repeats ~ {est:.0f}s wall clock",
          file=sys.stderr)

    output_path = args.output or _default_output_path()
    metadata = collect_metadata(sys.argv, git_sha)
    document = run_sweep(args.sweep_axis, sweep_values, base,
                         git_sha, output_path, metadata)

    if args.table is None:
        show_table = sys.stdout.isatty()
    else:
        show_table = args.table
    if show_table and not args.quiet:
        print(render_table(document))
    if not args.quiet:
        print(f"results: {output_path}", file=sys.stderr)
    return 0


def _default_output_path() -> str:
    """Compute the default output path under ``results/``.

    Uses ``%Y%m%dT%H%M%S`` rather than ``isoformat()`` so the filename
    is valid on Windows (no colons).

    :return: A path string.
    """
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    host = socket.gethostname().replace(os.sep, "_")
    return os.path.join("results", f"benchmark-{host}-{ts}.json")


def main() -> int:
    """CLI entry point.

    :return: Process exit code.
    """
    if sys.version_info < (3, 12):
        sys.exit("bocpy benchmarks require Python 3.12+ for "
                 "sub-interpreter parallelism")

    parser = build_arg_parser()
    args = parser.parse_args()

    if args.print_table is not None:
        with open(args.print_table, encoding="utf-8") as f:
            document = json.load(f)
        print(render_table(document))
        return 0

    if args.json_stdout:
        return child_main(args)

    return parent_main(args)


if __name__ == "__main__":
    sys.exit(main())
