"""Work-stealing tests.

These tests exercise the work-stealing path end-to-end through the
public ``@when`` surface and the ``_core.scheduler_stats`` accessor.
They are the integration-level coverage for stealing; the C-API
unit coverage of try_steal/steal lives in
``test_scheduler_pertask_queue.py``.

What the tests assert:

- **Token-work fairness sanity** — a fan-out workload whose size
  comfortably exceeds ``BATCH_SIZE`` must produce at least one
  ``steal_attempts`` entry across the worker set, demonstrating the
  fairness arm (or the empty-queue arm) of ``pop_slow`` fires under
  realistic load.
- **Empty-queue race** — starting the runtime with W workers and no
  work must converge to every worker parked and the process CPU/wall
  ratio must stay well below 1 (no busy-spinning thieves).
- **Spurious-failure stress** — placeholder; activated when bocpy is
  built with ``-DBOC_SCHED_SYSTEMATIC`` (Verona-style fault-injection
  in the queue links). The flag is off in default builds, so the
  test is skipped here.

Tests that asserted timing-dependent outcomes (``popped_via_steal >
0`` after a pinned fan-out, ``fairness_arm_fires >= N`` on a busy
worker) were removed because their pass/fail depends on OS scheduler
behaviour rather than on bocpy code under test; the underlying
mechanisms are exercised end-to-end by the benchmarks in
``examples/`` and at the data-structure level by
``test_internal_wsq.py``.

All tests follow the same module-level helper / receive-pattern
discipline as the other scheduler integration tests (see
``test_scheduler_integration.py``), because behaviours run on
worker sub-interpreters that import this module to resolve symbols.
"""

import time

import pytest

import bocpy
from bocpy import _core
from bocpy import Cown, drain, receive, send, TIMEOUT, wait, when


RECEIVE_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Module-level helpers (must be importable by worker sub-interpreters)
# ---------------------------------------------------------------------------


class _Counter:
    """Plain counter used as cown payload in fan-out workloads."""

    __slots__ = ("count",)

    def __init__(self):
        """Initialise the counter at zero."""
        self.count = 0


def _ensure_quiesced():
    """Tear down any prior runtime so the test starts from a clean state."""
    bocpy.wait()


def _fanout_done(c_pin, marker):
    """Final ``@when`` extracted to a helper.

    Inlining inside ``_fanout_kickoff`` would trigger the transpiler
    nested-capture gap (outer ``marker`` not forwarded into the inner
    behaviour's capture tuple).
    """
    @when(c_pin)
    def _(c_pin):
        send("done", marker)


def _fanout_kickoff(c_pin, work_cowns, marker):
    """Fan ``len(work_cowns)`` independent behaviours onto the kickoff worker.

    The kickoff is dispatched from the main thread and lands on
    whichever worker the off-worker round-robin cursor points at.
    Inside the body the worker dispatches one trivial behaviour per
    ``work_cowns`` entry; because every entry is independent (no
    MCS contention) each dispatch reaches ``boc_sched_dispatch``
    immediately on the producer-local arm. The first lands in
    ``pending``; every subsequent dispatch evicts the prior pending
    into the worker's local queue. After roughly ``BATCH_SIZE``
    items the queue is the only source of work, and idle peers can
    steal from it.
    """
    @when(c_pin)
    def _(c_pin):
        for wc in work_cowns:
            @when(wc)
            def _(wc):
                wc.value.count += 1
        _fanout_done(c_pin, marker)


# ---------------------------------------------------------------------------
# Token-work fairness sanity
# ---------------------------------------------------------------------------


class TestStealFairnessSanity:
    """A workload bigger than BATCH_SIZE must exercise the steal arm."""

    @classmethod
    def teardown_class(cls):
        wait()
        drain("done")

    def test_fanout_exceeding_batch_size_provokes_steal_attempts(self):
        """K > BATCH_SIZE (=100) must produce non-zero steal_attempts.

        ``BATCH_SIZE`` is the consumer-side budget at which the
        fast-path bypasses ``pending`` to take from the queue. With
        K=300 fan-out items pinned to one worker, the kickoff worker
        cycles through its budget at least three times, and idle
        peers pass through ``pop_slow`` repeatedly looking for work.
        Every peer entry into the slow path bumps either the
        fairness arm or the empty-queue arm of ``boc_sched_steal``,
        which in turn calls ``boc_sched_try_steal`` (one attempt per
        ring victim per round). The aggregate must be non-zero.
        """
        _ensure_quiesced()
        W = 4  # noqa: N806
        K = 300  # > BOC_BQ_BATCH_SIZE (100)  # noqa: N806
        bocpy.start(worker_count=W)
        try:
            c_pin = Cown(_Counter())
            work_cowns = [Cown(_Counter()) for _ in range(K)]
            _fanout_kickoff(c_pin, work_cowns, "fairness-done")

            tag, _payload = receive("done", RECEIVE_TIMEOUT)
            assert tag != TIMEOUT, "kickoff failed to complete"

            stats = _core.scheduler_stats()
        finally:
            drain("done")
            wait()

        assert len(stats) == W, stats
        total_attempts = sum(s["steal_attempts"] for s in stats)
        # The exact distribution of attempts across workers depends
        # on scheduling races; we only assert the aggregate is
        # non-zero. ``last_steal_attempt_ns`` on at least one worker
        # must also be non-zero (it's stamped on every try_steal
        # entry).
        assert total_attempts > 0, (
            f"no steal_attempts recorded — fairness/empty-queue arms "
            f"never fired: {stats}"
        )
        nonzero_ts = [s for s in stats if s["last_steal_attempt_ns"] > 0]
        assert len(nonzero_ts) > 0, (
            f"no worker's last_steal_attempt_ns was set: {stats}"
        )


# ---------------------------------------------------------------------------
# Empty-queue race: workers with no work must park
# ---------------------------------------------------------------------------


class TestStealEmptyQueueNoSpin:
    """W workers, 0 work — every worker must park in cnd_wait."""

    @classmethod
    def teardown_class(cls):
        wait()

    @pytest.mark.skipif(
        not hasattr(time, "process_time"),
        reason="needs time.process_time for CPU accounting",
    )
    def test_empty_queue_does_not_spin(self):
        """Bring the runtime up with W=4 and dispatch no work.

        Every worker enters ``pop_slow``, finds its own queue empty,
        loops one round of ``boc_sched_steal`` against peers (also
        empty), and parks under ``cv_mu``. The process CPU/wall
        ratio over a fixed window must stay well below 1: a single
        spinning thief alone would push the ratio above 1, and four
        spinning thieves would push it toward W. We assert
        ``< 0.5`` to tolerate main-thread overhead, the noticeboard
        thread, and sub-interpreter startup costs.

        The cumulative ``parked`` counter on every worker must be
        non-zero at the end of the window (each worker reached the
        ``cnd_wait`` arm at least once).
        """
        _ensure_quiesced()
        W = 4  # noqa: N806
        bocpy.start(worker_count=W)
        try:
            # Brief warm-up so workers actually reach pop_slow and
            # commit to parking before we start measuring.
            time.sleep(0.05)

            wall_start = time.monotonic()
            cpu_start = time.process_time()
            time.sleep(0.30)
            wall_elapsed = time.monotonic() - wall_start
            cpu_elapsed = time.process_time() - cpu_start

            stats = _core.scheduler_stats()
        finally:
            wait()

        ratio = cpu_elapsed / wall_elapsed
        assert ratio < 0.5, (
            f"CPU/wall ratio = {ratio:.2f} (cpu={cpu_elapsed:.3f}s, "
            f"wall={wall_elapsed:.3f}s) over an idle window — "
            f"workers are not parking"
        )

        assert len(stats) == W, stats
        for s in stats:
            assert s["parked"] > 0, (
                f"worker {s['worker_index']} never reached cnd_wait "
                f"in an idle runtime: {s}"
            )


# ---------------------------------------------------------------------------
# Spurious-failure stress (gated on the systematic-test build flag)
# ---------------------------------------------------------------------------


class TestStealSpuriousFailureStress:
    """Reserved for ``-DBOC_SCHED_SYSTEMATIC`` builds.

    Verona's stealing path has three documented spurious-failure
    modes (fully empty victim, single-element victim, first link not
    yet visible). Verifying convergence under fault-injection
    requires building bocpy with the ``BOC_SCHED_SYSTEMATIC`` macro,
    which is off in the default editable install. When that build
    flavour exists the body of this test should run 100 fan-out
    iterations and assert each completes within ``RECEIVE_TIMEOUT``.
    """

    @pytest.mark.skip(
        reason="needs -DBOC_SCHED_SYSTEMATIC build flag",
    )
    def test_spurious_failure_stress(self):  # pragma: no cover
        """Placeholder; see class docstring."""
        pass
