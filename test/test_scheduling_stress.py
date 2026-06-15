"""Scheduling stress tests for the BOC runtime.

These tests exercise the distributed-scheduling hot path under load.
They use **only** BOC primitives — no OS threads — because mixing OS threads
with @when behaviors is brittle (workers run in sub-interpreters and the
test thread cannot directly observe per-cown state).

Each test reads its results back through reader behaviors and
``quiesce()`` + ``Cown.unwrap()`` so the test thread can synchronize
with completion.
"""

import os

import pytest

import bocpy
from bocpy import _core
from bocpy import Cown, quiesce, wait, when
import bocpy.behaviors as _behaviors


# Do NOT import ``mockreplacement`` (or ``unittest.mock``) at module scope: the
# transpiler exports this whole module into every worker sub-interpreter, where
# it is not on ``sys.path``. Tests that need it import it locally.
QUIESCE_TIMEOUT = 30


def _read_back(cowns):
    """Schedule one reader behavior per cown.

    Each reader returns ``(idx, count)`` for its cown; the readers run
    after every increment already queued on that cown (FIFO per cown),
    so unwrapping their result cowns after ``quiesce()`` yields the
    final counts. Returns the list of result cowns.
    """
    readers = []
    for idx, c in enumerate(cowns):
        @when(c)
        def _(c, idx=idx):
            return (idx, c.value.count)
        readers.append(_)
    return readers


class Counter:
    """Plain integer counter wrapped in a Cown.

    No locking is needed: BOC guarantees exclusive access to a cown's value
    inside a behavior, so a per-cown int is a sound oracle for fan-out tests.
    """

    __slots__ = ("count",)

    def __init__(self):
        """Initialize the counter at zero."""
        self.count = 0


class TestSchedulingFanOut:
    """N behaviors fan out across M cowns; each cown's count is an oracle."""

    @classmethod
    def teardown_class(cls):
        wait()

    @pytest.mark.parametrize("n,m", [(1000, 32), (200, 4), (500, 1)])
    def test_disjoint_fan_out(self, n: int, m: int):
        """N behaviors target round-robin across M cowns; sum must equal N."""
        cowns = [Cown(Counter()) for _ in range(m)]

        for i in range(n):
            target = cowns[i % m]

            @when(target)
            def _(c):
                c.value.count += 1

        readers = _read_back(cowns)
        quiesce(QUIESCE_TIMEOUT)
        results = [r.unwrap() for r in readers]

        per_cown = {idx: count for idx, count in results}
        assert sum(per_cown.values()) == n, per_cown
        for idx in range(m):
            expected_share = n // m + (1 if idx < n % m else 0)
            assert per_cown[idx] == expected_share, (idx, per_cown)

    @pytest.mark.parametrize("n,m", [(500, 8), (1000, 16)])
    def test_overlapping_fan_out(self, n: int, m: int):
        """Each behavior locks two adjacent cowns; both increment.

        Sum of all counters must equal 2 * N.
        """
        cowns = [Cown(Counter()) for _ in range(m)]

        for i in range(n):
            a = cowns[i % m]
            b = cowns[(i + 1) % m]

            @when(a, b)
            def _(a, b):
                a.value.count += 1
                b.value.count += 1

        readers = _read_back(cowns)
        quiesce(QUIESCE_TIMEOUT)
        results = [r.unwrap() for r in readers]
        total = sum(count for _, count in results)
        assert total == 2 * n, results


class TestSchedulingSustainedLoad:
    """Schedule a large bounded workload and ensure it completes."""

    @classmethod
    def teardown_class(cls):
        wait()

    def test_bounded_completion(self):
        """Schedule many behaviors; each reports done; wait collects them all.

        This is the bounded-workload variant. The full ≥30 s sustained-load
        run is gated by the BOCPY_STRESS_LONG environment variable so CI
        stays fast; set it locally to exercise long runs.
        """
        n = 2000 if not os.environ.get("BOCPY_STRESS_LONG") else 100_000
        cowns = [Cown(Counter()) for _ in range(8)]

        for i in range(n):
            target = cowns[i % len(cowns)]

            @when(target)
            def _(c):
                c.value.count += 1

        timeout = max(QUIESCE_TIMEOUT, n // 100)
        readers = _read_back(cowns)
        quiesce(timeout)
        total = sum(count for _, count in (r.unwrap() for r in readers))
        assert total == n


class TestSchedulingDedup:
    """A repeated cown in @when must not double-acquire or double-run."""

    @classmethod
    def teardown_class(cls):
        wait()

    def test_when_same_cown_twice_runs_once(self):
        """@when(c, c) schedules exactly one behavior invocation."""
        c = Cown(Counter())

        @when(c, c)
        def identity(a, b):
            a.value.count += 1
            return a.value is b.value

        quiesce(QUIESCE_TIMEOUT)
        assert identity.unwrap() is True

        @when(c)
        def reader(c):
            return c.value.count

        quiesce(QUIESCE_TIMEOUT)
        count = reader.unwrap()
        assert count == 1, f"dedup failed: counter={count}"

    def test_when_repeated_cown_many_times(self):
        """Scheduling N copies of @when(c, c) yields exactly N increments."""
        c = Cown(Counter())
        n = 100

        for _ in range(n):
            @when(c, c)
            def _(a, b):
                a.value.count += 1

        @when(c)
        def reader(c):
            return c.value.count

        quiesce(QUIESCE_TIMEOUT)
        count = reader.unwrap()
        assert count == n, f"expected {n}, got {count}"


class TestSchedulingDrainRecycleFlush:
    """Verify the terminator and recycle queue invariants after ``wait()``.

    After a normal drain via ``wait()``, the C-level terminator counter
    must return to zero and a forced recycle-queue flush must be a no-op
    (no double-frees, no live entries left).

    A per-BOCBehavior refcount assertion is only exposed under the
    compile-time ``BOC_REF_TRACKING`` build flag. The terminator counter is
    a strict superset for the leak-detection purpose: every behavior takes
    one terminator hold via ``whencall`` and releases it on the worker
    thread after ``behavior_release_all``, so a behavior that is leaked (or
    whose release is dropped) keeps the count above zero.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_terminator_returns_to_zero_after_wait(self):
        """Schedule N disjoint behaviors; wait(); count must be 0."""
        n = 256
        cowns = [Cown(Counter()) for _ in range(8)]

        for i in range(n):
            target = cowns[i % len(cowns)]

            @when(target)
            def _(c):
                c.value.count += 1

        readers = _read_back(cowns)
        quiesce(QUIESCE_TIMEOUT)
        total = sum(count for _, count in (r.unwrap() for r in readers))
        assert total == n
        wait()
        assert _core.terminator_count() == 0

    def test_recycle_after_wait_is_idempotent(self):
        """Forced recycle-queue flush after wait() must not crash or leak."""
        cowns = [Cown(Counter()) for _ in range(4)]

        for c in cowns:
            @when(c)
            def _(c):
                c.value.count += 1

        readers = _read_back(cowns)
        quiesce(QUIESCE_TIMEOUT)
        total = sum(count for _, count in (r.unwrap() for r in readers))
        assert total == len(cowns)
        wait()
        _core.recycle()
        _core.recycle()
        assert _core.terminator_count() == 0


class TestWhencallRollback:
    """Verify that a failed ``behavior_schedule`` releases its terminator hold.

    The ``whencall`` helper takes a terminator hold via ``terminator_inc``
    before it dispatches to ``behavior_schedule``. If the schedule call
    raises (which is normally the unreachable post-prepare branch, but is
    reachable defensively if a future C-level invariant is violated), the
    Python ``try/except`` MUST drop the hold via ``terminator_dec`` so
    ``wait()`` can complete.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def _baseline(self):
        wait()
        from bocpy import start as _start_runtime
        _start_runtime()

    def test_rollback_after_schedule_raises(self):
        """A raising ``BehaviorCapsule.schedule`` must leave terminator_count at 0."""
        self._baseline()

        before = _core.terminator_count()

        from mockreplacement import patch_attr, Recorder

        sentinel = RuntimeError("synthetic schedule failure")
        fake_capsule = Recorder("FakeBehaviorCapsule")
        fake_capsule.schedule.side_effect = sentinel

        def _fake_capsule_ctor(*args, **kwargs):
            return fake_capsule

        with patch_attr(
            _behaviors._core, "BehaviorCapsule", _fake_capsule_ctor,
        ):
            c = Cown(Counter())
            with pytest.raises(RuntimeError) as info:
                @when(c)
                def _(c):
                    c.value.count += 1
            assert info.value is sentinel

        assert _core.terminator_count() == before
        c2 = Cown(Counter())

        @when(c2)
        def probe(c):
            c.value.count += 1
            return c.value.count

        quiesce(QUIESCE_TIMEOUT)
        assert probe.unwrap() == 1
        wait()
        assert _core.terminator_count() == 0


class TestStopVsScheduleRace:
    """Verify that ``stop()`` fences subsequent ``whencall`` attempts.

    ``stop()`` (called by ``wait()``) closes the terminator and any
    subsequent ``terminator_inc`` MUST return -1 so ``whencall`` raises
    ``RuntimeError("runtime is shutting down")`` rather than racing
    teardown. The runtime must then be restartable on the next ``@when``.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_terminator_inc_refuses_after_close(self):
        """``terminator_inc`` returns -1 once ``terminator_close`` has run."""
        wait()
        rc = _core.terminator_inc()
        assert rc < 0, f"terminator_inc returned {rc}, expected -1"

        c = Cown(Counter())

        @when(c)
        def probe(c):
            return True

        quiesce(QUIESCE_TIMEOUT)
        assert probe.unwrap() is True
        wait()
        assert _core.terminator_count() == 0

    def test_whencall_raises_after_close(self):
        """``@when`` directly after a refused inc must surface RuntimeError.

        We monkey-patch ``terminator_inc`` to return -1 (the refusal
        sentinel), since once a real ``terminator_close`` has fenced the
        runtime the entire @when path is shut and there is no test hook
        to reopen it without going through ``start()``. The patch
        targets the same underlying C function via the Python module
        binding the whencall helper actually consults.
        """
        c0 = Cown(Counter())

        @when(c0)
        def probe(c):
            return True

        quiesce(QUIESCE_TIMEOUT)
        assert probe.unwrap() is True

        from mockreplacement import patch_attr

        def _refuse_inc(*args, **kwargs):
            return -1

        with patch_attr(
            _behaviors._core, "terminator_inc", _refuse_inc,
        ):
            c = Cown(Counter())
            with pytest.raises(RuntimeError, match="shutting down"):
                @when(c)
                def _(c):
                    c.value.count += 1

        wait()
        assert _core.terminator_count() == 0


class _Boom(Exception):
    """Sentinel exception raised by the worker-resilience tests."""


def _raise_boom(c):
    """Behavior body that always raises ``_Boom``.

    Module-level so the worker can import it via the transpiler export.
    """
    raise _Boom("synthetic body failure")


class TestWorkerErrorPath:
    """Verify worker resilience when behavior bodies raise.

    A raising behavior body must:

    * have its terminator hold dropped (``wait()`` returns),
    * leave the worker in the receive loop (next @when on the same cown
      runs to completion), and
    * propagate the exception via the result Cown's ``.exception``.

    These properties hold because ``run_behavior`` wraps the body in
    its own ``try/except`` and ``do_work`` wraps each iteration in a
    ``try/except`` so a failure cannot break the worker loop.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_raising_body_does_not_strand_wait(self):
        """A single raising behavior must let ``wait()`` complete."""
        c = Cown(Counter())

        @when(c)
        def _(c):
            _raise_boom(c)

        wait()
        assert _core.terminator_count() == 0

    def test_raising_body_sets_exception_on_result(self):
        """The result Cown must carry the body's exception."""
        c = Cown(Counter())

        @when(c)
        def result(c):
            _raise_boom(c)

        wait()
        assert result.exception is True
        assert isinstance(result.value, _Boom)

    def test_workers_survive_many_raising_behaviors(self):
        """N raising behaviors must not take any worker out of rotation.

        Schedule far more raising behaviors than workers, then schedule
        a follow-up batch of well-behaved behaviors that return their
        index. If any worker had broken out of its loop, the follow-up
        result cowns would never resolve and ``quiesce`` would time out.
        """
        n_raising = 200
        n_followup = 50

        raising_cowns = [Cown(Counter()) for _ in range(n_raising)]
        for c in raising_cowns:
            @when(c)
            def _(c):
                _raise_boom(c)

        followup_cowns = [Cown(Counter()) for _ in range(n_followup)]
        readers = []
        for i, c in enumerate(followup_cowns):
            @when(c)
            def _(c, i=i):
                return i
            readers.append(_)

        quiesce(QUIESCE_TIMEOUT)
        payloads = sorted(r.unwrap() for r in readers)
        assert payloads == list(range(n_followup))
        wait()
        assert _core.terminator_count() == 0


class TestNoticeboardStartupHandshake:
    """Verify that a failed noticeboard claim surfaces on the starter thread.

    ``start_noticeboard`` waits until the thread either claims the
    C-level single-writer slot or captures the failure exception. A
    failed claim must propagate as ``RuntimeError`` rather than leave
    the runtime in a half-started state where ``notice_*`` writes
    enqueue forever with no consumer.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_failed_claim_raises_on_start(self):
        """``start()`` must raise if ``set_noticeboard_thread`` raises."""
        wait()

        from mockreplacement import patch_attr

        sentinel = RuntimeError("synthetic claim failure")

        def _raise_sentinel(*args, **kwargs):
            raise sentinel

        with patch_attr(
            _behaviors._core, "set_noticeboard_thread", _raise_sentinel,
        ):
            c = Cown(Counter())
            with pytest.raises(RuntimeError, match="noticeboard thread"):
                @when(c)
                def _(c):
                    c.value.count += 1

        assert _behaviors.BEHAVIORS is None

        c2 = Cown(Counter())

        @when(c2)
        def probe(c):
            return True

        quiesce(QUIESCE_TIMEOUT)
        assert probe.unwrap() is True
        wait()
        assert _core.terminator_count() == 0


class TestChainRingPerWorkerCount:
    """Long ring of overlapping pair-locks under varied worker counts.

    Schedules ``ring_length`` behaviours each locking an adjacent
    ``(c[i], c[(i+1) % ring_length])`` pair against a 64-cown ring.
    Two-phase locking over the worker-count parameterisation
    ({1, 2, 4, 8}) exercises the dispatch / pop / 2PL-handoff paths
    under both serialised and parallel regimes; a regression in the
    per-worker queue or MCS handoff would manifest as a leak or a
    missing increment.

    Each parameterised run quiesces the runtime first so the
    explicit ``worker_count`` actually takes effect — auto-start
    would otherwise reuse whatever ``WORKER_COUNT`` defaulted to.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    @pytest.mark.parametrize("worker_count", [1, 2, 4, 8])
    def test_chain_ring(self, worker_count: int):
        """Ring of pair-locks completes cleanly at every worker count.

        Each behaviour increments both adjacent counters; total sum
        across the ring must equal ``2 * ring_length``. After
        ``wait()`` the terminator must return to zero — any leaked
        hold from the dispatch path (forgotten ``terminator_inc``
        rollback, skipped ``terminator_dec`` on a worker error path,
        etc.) would surface here as a non-zero count.

        Also asserts the work-conservation floor on the stats
        snapshot: ``sum(popped_local + popped_via_steal) >=
        ring_length``. The proportion between local pops and steals
        is intentionally *not* asserted — that ratio depends on OS
        scheduler behaviour (worker wake-up order, sub-interpreter
        startup latency, kernel pre-emption) and was previously a
        source of CI flakiness.
        """
        wait()
        bocpy.start(worker_count=worker_count)
        try:
            ring_size = 64
            ring_length = 10_000
            cowns = [Cown(Counter()) for _ in range(ring_size)]

            for i in range(ring_length):
                a = cowns[i % ring_size]
                b = cowns[(i + 1) % ring_size]

                @when(a, b)
                def _(a, b):
                    a.value.count += 1
                    b.value.count += 1

            readers = _read_back(cowns)
            quiesce(QUIESCE_TIMEOUT)
            results = [r.unwrap() for r in readers]
            total = sum(count for _, count in results)
            assert total == 2 * ring_length, (worker_count, results)
        finally:
            stats = wait(stats=True)
            assert _core.terminator_count() == 0

        assert len(stats) == worker_count, stats
        total_local = sum(s["popped_local"] for s in stats)
        total_stolen = sum(s["popped_via_steal"] for s in stats)
        total_pops = total_local + total_stolen
        assert total_pops >= ring_length, (
            f"W={worker_count}: only {total_pops} pops recorded "
            f"for {ring_length} dispatched behaviours"
        )


class TestOrphanDropException:
    """Verify the orphan-drain mitigation surfaces RuntimeError on result Cowns.

    Behaviors orphaned during ``stop()`` surface a
    :class:`RuntimeError` on their result Cown so callers awaiting
    ``cown.value`` / ``cown.exception`` after teardown see a
    diagnostic instead of a permanent ``None``.

    Two layers of coverage:

    1. ``test_set_drop_exception_marks_result_cown`` — direct C-method
       unit test. Constructs a :class:`_core.BehaviorCapsule` without
       scheduling it, calls ``set_drop_exception`` on it, then verifies
       the result Cown's value/exception state matches the worker
       exception path (``acquire`` → set value → ``exception = True``
       → ``release``).

    2. ``test_drain_orphan_invokes_set_drop_exception`` — wiring test
       for ``Behaviors._drain_orphan_behaviors``. Mocks
       ``_core.scheduler_drain_all_queues`` to return a fake capsule
       once, then verifies the drain path invokes both
       ``set_drop_exception`` and ``release_all`` on it.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_set_drop_exception_marks_result_cown(self):
        """C-method: ``set_drop_exception`` writes value and flag, leaves cown released."""
        wait()
        from bocpy import start as _start_runtime
        _start_runtime()

        result = Cown(None)
        arg = Cown(Counter())
        capsule = _core.BehaviorCapsule(
            "__behavior_never_called__",
            result.impl,
            [(1, arg.impl)],
            [],
        )

        drop = RuntimeError("orphaned during stop()")
        capsule.set_drop_exception(drop)

        result.acquire()
        try:
            assert result.exception is True, (
                "set_drop_exception must mark the result Cown's exception flag"
            )
            assert isinstance(result.value, RuntimeError), (
                f"expected RuntimeError, got {type(result.value).__name__}"
            )
            assert "orphaned during stop()" in str(result.value), (
                f"unexpected message: {result.value!r}"
            )
        finally:
            result.release()

    def test_drain_orphan_invokes_set_drop_exception(self):
        """``_drain_orphan_behaviors`` calls ``set_drop_exception`` then ``release_all``."""
        wait()
        from bocpy import start as _start_runtime
        _start_runtime()

        from mockreplacement import patch_attr, Recorder

        fake_capsule = Recorder("orphan_capsule")
        drain_results = [[fake_capsule], []]

        def _fake_drain():
            return drain_results.pop(0)

        def _fake_terminator_dec(*args, **kwargs):
            return 0

        with patch_attr(
            _behaviors._core, "scheduler_drain_all_queues", _fake_drain,
        ), patch_attr(
            _behaviors._core, "terminator_dec", _fake_terminator_dec,
        ):
            behaviors = bocpy.behaviors.BEHAVIORS
            assert behaviors is not None, (
                "runtime must be alive for _drain_orphan_behaviors test"
            )
            errors, drained_count = behaviors._drain_orphan_behaviors()

        assert errors == [], (
            f"orphan drain reported unexpected errors: {errors!r}"
        )
        assert drained_count == 1, (
            f"expected exactly one capsule drained; got {drained_count}"
        )
        fake_capsule.set_drop_exception.assert_called_once()
        sent_arg = fake_capsule.set_drop_exception.call_args[0][0]
        assert isinstance(sent_arg, RuntimeError), (
            f"expected RuntimeError, got {type(sent_arg).__name__}"
        )
        assert "stop()" in str(sent_arg), (
            f"drop exception message must mention stop(); got {sent_arg!r}"
        )
        fake_capsule.release_all.assert_called_once()


class TestDispatchAfterRuntimeStop:
    """``boc_sched_dispatch`` must raise once the runtime is torn down.

    Earlier the off-worker dispatch arm silently dropped the node
    when ``WORKER_COUNT == 0``, leaving the ``whencall`` caller's
    ``terminator_inc`` un-rolled-back so a subsequent ``wait()``
    would hang. The fix:

    * ``boc_sched_dispatch`` now sets a ``RuntimeError`` and returns
      -1 on the no-runtime path,
    * ``behavior_resolve_one`` propagates the failure (rolling back
      the queue-owned ``BEHAVIOR_INCREF``),
    * ``BehaviorCapsule.schedule`` propagates to ``whencall``, whose
      ``try/except BaseException`` runs ``terminator_dec``,
    * ``boc_sched_shutdown`` publishes ``WORKER_COUNT = 0`` with a
      release fence and bumps ``INCARNATION`` so cached
      ``rr_nonlocal`` TLS in off-worker producers self-invalidates.

    This test exercises the full chain end-to-end.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_schedule_after_runtime_stop_raises(self):
        """A ``@when`` after ``scheduler_runtime_stop`` raises and rolls back."""
        wait()

        assert _core.scheduler_stats() == [], (
            "scheduler_runtime_stop should have left WORKER_COUNT == 0"
        )

        before_count = _core.terminator_count()
        before_seeded = _core.terminator_seeded()
        assert before_count == 0 and before_seeded == 0, (
            f"runtime should be quiesced; got count={before_count}, "
            f"seeded={before_seeded}"
        )

        c = Cown(Counter())

        result = Cown(None)
        capsule = _core.BehaviorCapsule(
            "__nonexistent_thunk__",
            result.impl,
            [(1, c.impl)],
            [],
        )

        prior_count, prior_seeded = _core.terminator_reset()
        rc = _core.terminator_inc()
        assert rc >= 0, f"terminator_inc unexpectedly refused: {rc}"

        try:
            with pytest.raises(RuntimeError, match="bocpy runtime is not running"):
                capsule.schedule()
            _core.terminator_dec()
        finally:
            _core.terminator_seed_dec()
            _core.terminator_close()

        assert _core.terminator_count() == 0, (
            "schedule failure must roll back the synthetic terminator hold"
        )

    def test_scheduler_runtime_stop_is_idempotent(self):
        """Calling ``scheduler_runtime_stop`` twice is a no-op the second time.

        ``Behaviors.start()`` includes a defence-in-depth ``except``
        arm that calls ``_core.scheduler_runtime_stop()`` even when an
        earlier abort path already called it. This is only safe if the
        C-side stop is idempotent: a double-free of the per-worker
        ``WORKERS`` array would corrupt the heap on the second call.

        The test must establish its own precondition (a real runtime
        has run and been torn down) so it does not pass vacuously
        under ``pytest -k`` or randomised test ordering. A bare
        ``wait()`` with ``BEHAVIORS is None`` and ``WORKERS == NULL``
        would short-circuit every assertion below without ever
        exercising the second-call path the docstring claims to
        defend.
        """
        wait()
        c = Cown(Counter())

        @when(c)
        def probe(c):
            return True

        quiesce(QUIESCE_TIMEOUT)
        assert probe.unwrap() is True
        live_stats = _core.scheduler_stats()
        assert live_stats, (
            "runtime must be alive before tearing it down so the first "
            f"scheduler_runtime_stop has work to do; got {live_stats!r}"
        )
        wait()
        assert _core.scheduler_stats() == [], (
            "wait() should have left WORKER_COUNT == 0"
        )
        _core.scheduler_runtime_stop()
        assert _core.scheduler_stats() == [], (
            "second scheduler_runtime_stop must leave WORKER_COUNT == 0"
        )
        _core.scheduler_runtime_stop()
        assert _core.scheduler_stats() == []
