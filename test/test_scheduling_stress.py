"""Scheduling stress tests for the BOC runtime.

These tests exercise the distributed-scheduling hot path under load.
They use **only** BOC primitives — no OS threads — because mixing OS threads
with @when behaviors is brittle (workers run in sub-interpreters and the
test thread cannot directly observe per-cown state).

Each test ships its results out via send/receive so the test thread can
synchronize with completion.
"""

import os
from unittest import mock

import pytest

import bocpy
from bocpy import _core
from bocpy import Cown, drain, receive, send, TIMEOUT, wait, when
import bocpy.behaviors as _behaviors


RECEIVE_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Helpers (module-level so workers can import them)
# ---------------------------------------------------------------------------


def _drain_done():
    """Drop any leftover 'done' messages between tests."""
    drain("done")


def _collect_done(expected: int, timeout: int = RECEIVE_TIMEOUT):
    """Block until `expected` 'done' messages arrive; return their payloads.

    Fails the test with a clear message on timeout instead of hanging.
    """
    payloads = []
    timed_out = False
    try:
        for _ in range(expected):
            tag, payload = receive("done", timeout)
            if tag == TIMEOUT:
                timed_out = True
                break
            payloads.append(payload)
    finally:
        drain("done")
    assert not timed_out, (
        f"Timed out waiting for 'done' messages: got {len(payloads)} of "
        f"{expected}. A behavior likely failed to schedule or run."
    )
    return payloads


class Counter:
    """Plain integer counter wrapped in a Cown.

    No locking is needed: BOC guarantees exclusive access to a cown's value
    inside a behavior, so a per-cown int is a sound oracle for fan-out tests.
    """

    __slots__ = ("count",)

    def __init__(self):
        """Initialize the counter at zero."""
        self.count = 0


# ---------------------------------------------------------------------------
# Fan-out: N behaviors over M cowns, disjoint and overlapping
# ---------------------------------------------------------------------------


class TestSchedulingFanOut:
    """N behaviors fan out across M cowns; each cown's count is an oracle."""

    @classmethod
    def teardown_class(cls):
        wait()
        _drain_done()

    @pytest.mark.parametrize("n,m", [(1000, 32), (200, 4), (500, 1)])
    def test_disjoint_fan_out(self, n: int, m: int):
        """N behaviors target round-robin across M cowns; sum must equal N."""
        cowns = [Cown(Counter()) for _ in range(m)]

        for i in range(n):
            target = cowns[i % m]

            @when(target)
            def _(c):
                c.value.count += 1

        # Read each counter back through a behavior and report it.
        for idx, c in enumerate(cowns):
            @when(c)
            def _(c):
                send("done", (idx, c.value.count))  # noqa: B023

        results = _collect_done(m)

        per_cown = {idx: count for idx, count in results}
        assert sum(per_cown.values()) == n, per_cown
        # Each cown should see exactly its round-robin share.
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

        for idx, c in enumerate(cowns):
            @when(c)
            def _(c):
                send("done", (idx, c.value.count))  # noqa: B023

        results = _collect_done(m)
        total = sum(count for _, count in results)
        assert total == 2 * n, results


# ---------------------------------------------------------------------------
# Sustained load: long-running schedule that must drain via wait()
# ---------------------------------------------------------------------------


class TestSchedulingSustainedLoad:
    """Schedule a large bounded workload and ensure it completes."""

    @classmethod
    def teardown_class(cls):
        wait()
        _drain_done()

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
                send("done", 1)

        # Use a generous timeout proportional to n; wait fails noisily if a
        # behavior is dropped.
        timeout = max(RECEIVE_TIMEOUT, n // 100)
        payloads = _collect_done(n, timeout=timeout)
        assert len(payloads) == n


# ---------------------------------------------------------------------------
# Dedup regression: @when(c, c) must run exactly once per scheduling
# ---------------------------------------------------------------------------


class TestSchedulingDedup:
    """A repeated cown in @when must not double-acquire or double-run."""

    @classmethod
    def teardown_class(cls):
        wait()
        _drain_done()

    def test_when_same_cown_twice_runs_once(self):
        """@when(c, c) schedules exactly one behavior invocation."""
        c = Cown(Counter())

        @when(c, c)
        def _(a, b):
            # a and b are separate Python wrappers but back the same cown,
            # so they observe the same underlying value object.
            a.value.count += 1
            send("done", a.value is b.value)

        payloads = _collect_done(1)
        # Both parameters should expose the same underlying value.
        assert payloads == [True]

        @when(c)
        def _(c):
            send("done", c.value.count)

        [count] = _collect_done(1)
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
        def _(c):
            send("done", c.value.count)

        [count] = _collect_done(1)
        assert count == n, f"expected {n}, got {count}"


# ---------------------------------------------------------------------------
# Drain-with-recycle-flush: terminator + recycle invariant after wait()
# ---------------------------------------------------------------------------


class TestSchedulingDrainRecycleFlush:
    """Verify the terminator and recycle queue invariants after ``wait()``.

    After a normal drain via ``wait()``, the C-level terminator counter
    must return to zero and a forced recycle-queue flush must be a no-op
    (no double-frees, no live entries left).

    An earlier draft of this test also wanted a per-BOCBehavior refcount
    assertion,
    but that counter is only exposed under the compile-time
    ``BOC_REF_TRACKING`` build flag. The terminator counter is a strict
    superset for the leak-detection purpose: every behavior takes one
    terminator hold via ``whencall`` and releases it on the worker thread
    after ``behavior_release_all``, so a behavior that is leaked (or whose
    release is dropped) keeps the count above zero.
    """

    @classmethod
    def teardown_class(cls):
        wait()
        _drain_done()

    def test_terminator_returns_to_zero_after_wait(self):
        """Schedule N disjoint behaviors; wait(); count must be 0."""
        n = 256
        cowns = [Cown(Counter()) for _ in range(8)]

        for i in range(n):
            target = cowns[i % len(cowns)]

            @when(target)
            def _(c):
                c.value.count += 1
                send("done", 1)

        payloads = _collect_done(n)
        assert len(payloads) == n
        # wait() drains and stops; terminator_count() should observe a
        # quiesced runtime. A non-zero value indicates a leaked hold.
        wait()
        assert _core.terminator_count() == 0

    def test_recycle_after_wait_is_idempotent(self):
        """Forced recycle-queue flush after wait() must not crash or leak."""
        cowns = [Cown(Counter()) for _ in range(4)]

        for c in cowns:
            @when(c)
            def _(c):
                c.value.count += 1
                send("done", 1)

        _collect_done(len(cowns))
        wait()
        # Two flushes back-to-back: the second must be a no-op.
        _core.recycle()
        _core.recycle()
        assert _core.terminator_count() == 0


# ---------------------------------------------------------------------------
# whencall rollback: a failed behavior_schedule must release the terminator
# ---------------------------------------------------------------------------


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
        _drain_done()

    def _baseline(self):
        # Drive the runtime to a quiesced state with no outstanding holds.
        wait()
        # Trigger a fresh start without scheduling anything. start()
        # leaves the terminator at (count=1, seeded=1) -- the seed
        # contribution that wait()/stop() drops via terminator_seed_dec.
        # We do not schedule a probe behavior here because the worker's
        # release/decrement happens after the behavior body returns and
        # there is no synchronisation point that proves the decrement
        # has landed before the test thread snapshots the count.
        from bocpy import start as _start_runtime
        _start_runtime()

    def test_rollback_after_schedule_raises(self):
        """A raising ``BehaviorCapsule.schedule`` must leave terminator_count at 0."""
        self._baseline()

        # After _baseline the runtime is alive (start() ran) but no
        # behaviors are in flight. The terminator still carries the
        # seed contribution (count == 1, seeded == 1) until stop().
        # whencall increments above the seed and a clean rollback must
        # bring count back to exactly the pre-call value.
        before = _core.terminator_count()

        sentinel = RuntimeError("synthetic schedule failure")
        fake_capsule = mock.MagicMock()
        fake_capsule.schedule.side_effect = sentinel
        with mock.patch.object(
            _behaviors._core, "BehaviorCapsule",
            return_value=fake_capsule,
        ):
            c = Cown(Counter())
            with pytest.raises(RuntimeError) as info:
                @when(c)
                def _(c):
                    c.value.count += 1
            assert info.value is sentinel

        # The mocked failure must not leave a dangling terminator hold:
        # whencall caught the raise and called terminator_dec.
        assert _core.terminator_count() == before
        # And the runtime should still be usable for fresh behaviors.
        c2 = Cown(Counter())

        @when(c2)
        def _(c):
            c.value.count += 1
            send("done", 1)

        _collect_done(1)
        wait()
        assert _core.terminator_count() == 0


# ---------------------------------------------------------------------------
# stop()-vs-schedule race: a closed terminator must reject new whencalls
# ---------------------------------------------------------------------------


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
        _drain_done()

    def test_terminator_inc_refuses_after_close(self):
        """``terminator_inc`` returns -1 once ``terminator_close`` has run."""
        # wait() quiesces the runtime and runs terminator_close internally,
        # leaving (count=0, seeded=0, closed=1). A direct terminator_inc
        # call from the test thread must therefore be refused.
        wait()
        rc = _core.terminator_inc()
        assert rc < 0, f"terminator_inc returned {rc}, expected -1"

        # The runtime must still be restartable on the next @when. The
        # Behaviors.start() path runs terminator_reset which raises drift
        # only if our refused inc somehow took effect (it must not have).
        c = Cown(Counter())

        @when(c)
        def _(c):
            send("done", 1)

        _collect_done(1)
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
        # First make sure the runtime is alive so @when does not try to
        # restart it during the patched call.
        c0 = Cown(Counter())

        @when(c0)
        def _(c):
            send("done", 1)

        _collect_done(1)

        with mock.patch.object(
            _behaviors._core, "terminator_inc",
            return_value=-1,
        ):
            c = Cown(Counter())
            with pytest.raises(RuntimeError, match="shutting down"):
                @when(c)
                def _(c):
                    c.value.count += 1

        # whencall short-circuited at terminator_inc; no hold leaked,
        # no behavior_schedule was called.
        wait()
        assert _core.terminator_count() == 0


# ---------------------------------------------------------------------------
# Worker error-path resilience: a failing behavior body must not strand
# wait() or take a worker out of rotation.
# ---------------------------------------------------------------------------


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
        _drain_done()

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
        a follow-up batch of well-behaved behaviors that emit on
        ``done``. If any worker had broken out of its loop, we would
        miss messages and ``_collect_done`` would time out.
        """
        n_raising = 200
        n_followup = 50

        raising_cowns = [Cown(Counter()) for _ in range(n_raising)]
        for c in raising_cowns:
            @when(c)
            def _(c):
                _raise_boom(c)

        followup_cowns = [Cown(Counter()) for _ in range(n_followup)]
        for i, c in enumerate(followup_cowns):
            @when(c)
            def _(c):
                send("done", i)  # noqa: B023

        payloads = _collect_done(n_followup)
        assert sorted(payloads) == list(range(n_followup))
        wait()
        assert _core.terminator_count() == 0


# ---------------------------------------------------------------------------
# Noticeboard startup handshake: a failed set_noticeboard_thread() must be
# surfaced on the calling thread, not silently strand the runtime.
# ---------------------------------------------------------------------------


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
        _drain_done()

    def test_failed_claim_raises_on_start(self):
        """``start()`` must raise if ``set_noticeboard_thread`` raises."""
        # Quiesce any prior runtime so the next @when triggers a fresh start.
        wait()

        sentinel = RuntimeError("synthetic claim failure")
        with mock.patch.object(
            _behaviors._core, "set_noticeboard_thread",
            side_effect=sentinel,
        ):
            c = Cown(Counter())
            with pytest.raises(RuntimeError, match="noticeboard thread"):
                @when(c)
                def _(c):
                    c.value.count += 1

        # The failed start must reset the global runtime slot so the
        # next @when triggers a fresh start() rather than reusing the
        # half-initialised Behaviors instance whose noticeboard thread
        # is already dead.
        assert _behaviors.BEHAVIORS is None

        # The runtime must be re-startable once the synthetic failure is
        # withdrawn. A successful @when proves the next start_noticeboard
        # claimed the slot cleanly.
        c2 = Cown(Counter())

        @when(c2)
        def _(c):
            send("done", 1)

        _collect_done(1)
        wait()
        assert _core.terminator_count() == 0


# ---------------------------------------------------------------------------
# Chain-ring stress, parameterised over worker_count
# ---------------------------------------------------------------------------


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
        _drain_done()

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

            # Read each counter back through a behaviour so the test
            # thread observes the final value after all increments
            # have committed.
            for idx, c in enumerate(cowns):
                @when(c)
                def _(c):
                    send("done", (idx, c.value.count))  # noqa: B023

            results = _collect_done(ring_size)
            total = sum(count for _, count in results)
            assert total == 2 * ring_length, (worker_count, results)
        finally:
            _drain_done()
            # `wait(stats=True)` returns the snapshot captured before
            # the per-worker array is freed, so we don't need a
            # pre-wait `_core.scheduler_stats()` call.
            stats = wait(stats=True)
            assert _core.terminator_count() == 0

        assert len(stats) == worker_count, stats
        total_local = sum(s["popped_local"] for s in stats)
        total_stolen = sum(s["popped_via_steal"] for s in stats)
        total_pops = total_local + total_stolen
        # Every behaviour that completes was popped exactly once, so
        # `total_pops` must reach the dispatched count. We don't need
        # an exact equality (last-mile read-back behaviours and the
        # warm-up handshake also count), only a sanity floor.
        assert total_pops >= ring_length, (
            f"W={worker_count}: only {total_pops} pops recorded "
            f"for {ring_length} dispatched behaviours"
        )


# ---------------------------------------------------------------------------
# Orphan-drain mitigation: set_drop_exception on stop()-orphaned results
# ---------------------------------------------------------------------------


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
        _drain_done()

    def test_set_drop_exception_marks_result_cown(self):
        """C-method: ``set_drop_exception`` writes value and flag, leaves cown released."""
        # Drive the runtime to a known state and ensure it is alive
        # (BehaviorCapsule construction touches per-module C state).
        wait()
        from bocpy import start as _start_runtime
        _start_runtime()

        result = Cown(None)
        arg = Cown(Counter())
        # Construct a BehaviorCapsule without scheduling it. The thunk
        # name does not need to resolve because we never call
        # ``execute`` — set_drop_exception only touches the result
        # cown.
        capsule = _core.BehaviorCapsule(
            "__behavior_never_called__",
            result.impl,
            [(1, arg.impl)],
            [],
        )

        drop = RuntimeError("orphaned during stop()")
        capsule.set_drop_exception(drop)

        # The result Cown must now be in the published-and-released
        # state with the exception flag set so a post-stop() consumer
        # can acquire it and observe the failure.
        result.acquire()
        try:
            assert result.exception is True, (
                "set_drop_exception must mark the result Cown's exception flag"
            )
            # Value goes through xidata pickle/unpickle on release/acquire,
            # so identity is not preserved; check type and message.
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

        # Build a real BehaviorCapsule wrapped in a MagicMock so the
        # orphan-drain path can call the documented methods on it
        # (``set_drop_exception``, ``release_all``).
        fake_capsule = mock.MagicMock()
        # Single-shot drain: first call returns one fake orphan, the
        # second call returns [] so the drain loop terminates.
        drain_results = [[fake_capsule], []]
        with mock.patch.object(
            _behaviors._core, "scheduler_drain_all_queues",
            side_effect=lambda: drain_results.pop(0),
        ), mock.patch.object(
            _behaviors._core, "terminator_dec",
            return_value=0,
        ):
            behaviors = bocpy.behaviors.BEHAVIORS
            assert behaviors is not None, (
                "runtime must be alive for _drain_orphan_behaviors test"
            )
            errors = behaviors._drain_orphan_behaviors()

        assert errors == [], (
            f"orphan drain reported unexpected errors: {errors!r}"
        )
        fake_capsule.set_drop_exception.assert_called_once()
        # The argument must be a RuntimeError carrying a stop()
        # diagnostic; the orphan drain UX contract requires the
        # message reference "stop()" so users can grep for it.
        sent_arg = fake_capsule.set_drop_exception.call_args[0][0]
        assert isinstance(sent_arg, RuntimeError), (
            f"expected RuntimeError, got {type(sent_arg).__name__}"
        )
        assert "stop()" in str(sent_arg), (
            f"drop exception message must mention stop(); got {sent_arg!r}"
        )
        fake_capsule.release_all.assert_called_once()


# ---------------------------------------------------------------------------
# Dispatch after runtime stop must surface
# ---------------------------------------------------------------------------


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
        # Ensure the runtime is up for any subsequent test class.
        wait()
        _drain_done()

    def test_schedule_after_runtime_stop_raises(self):
        """A ``@when`` after ``scheduler_runtime_stop`` raises and rolls back."""
        # Bring the runtime to a clean post-stop state.
        wait()

        # We need WORKER_COUNT == 0 at the C level. ``wait()`` ran
        # ``stop_workers`` which already called ``scheduler_runtime_stop``,
        # so the runtime is down. ``scheduler_stats()`` returns an
        # empty list iff the per-worker array has been freed.
        assert _core.scheduler_stats() == [], (
            "scheduler_runtime_stop should have left WORKER_COUNT == 0"
        )

        before_count = _core.terminator_count()
        before_seeded = _core.terminator_seeded()
        assert before_count == 0 and before_seeded == 0, (
            f"runtime should be quiesced; got count={before_count}, "
            f"seeded={before_seeded}"
        )

        # Bypass the auto-start in the @when fast path by reaching
        # whencall directly with a Cown whose runtime has been
        # explicitly stopped. The trick: re-close the terminator and
        # force WORKER_COUNT to 0 at the same time. We arm both by
        # going through the public start/wait cycle which leaves
        # exactly that state. Then we drive a behavior through
        # ``_core.BehaviorCapsule(...).schedule()`` directly so the
        # auto-start gate in ``behaviors.py`` cannot wake the
        # runtime back up between our setup and the dispatch.
        c = Cown(Counter())

        # Build a behavior capsule by hand so the auto-start path
        # in ``@when`` does not fire. ``_core.BehaviorCapsule``
        # takes (thunk_name, result_impl, cowns_with_groups,
        # captures); ``cowns_with_groups`` is a list of
        # (group_id, cown_impl) tuples mirroring whencall.
        result = Cown(None)
        capsule = _core.BehaviorCapsule(
            "__nonexistent_thunk__",
            result.impl,
            [(1, c.impl)],
            [],
        )

        # The terminator is closed after wait(); we must arm it for
        # this single dispatch attempt the same way whencall would,
        # then prove the dispatch failure rolls our hold back.
        # terminator_inc would refuse a closed terminator, so we
        # seed it via terminator_reset (count=1, seeded=1, closed=0)
        # to mimic an alive runtime, then forcibly bring
        # WORKER_COUNT back to 0 by NOT calling start().
        prior_count, prior_seeded = _core.terminator_reset()
        # The reset returned the post-wait quiesced state; arm the
        # terminator for our synthetic schedule attempt.
        rc = _core.terminator_inc()
        assert rc >= 0, f"terminator_inc unexpectedly refused: {rc}"

        try:
            # Direct schedule. With WORKER_COUNT == 0 the off-worker
            # dispatch arm in boc_sched_dispatch must surface a
            # RuntimeError rather than silently dropping the node.
            with pytest.raises(RuntimeError, match="bocpy runtime is not running"):
                capsule.schedule()
            # whencall's try/except in behaviors.py would now call
            # terminator_dec; we mirror that here so the count
            # returns to its pre-arm state.
            _core.terminator_dec()
        finally:
            # Drop the seed contribution from terminator_reset and
            # close the terminator so subsequent tests starting
            # fresh see a clean baseline.
            _core.terminator_seed_dec()
            _core.terminator_close()

        # All holds rolled back: count is back to 0 and the
        # surviving runtime state is clean.
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
        # Bring the runtime down to a clean baseline.
        wait()
        # Force a genuine runtime cycle: schedule one behaviour so
        # ``Behaviors.start()`` allocates ``WORKERS``, then ``wait()``
        # again so ``stop_workers`` performs the *first* real
        # ``scheduler_runtime_stop`` call. Without this step the
        # idempotency assertions below would all hit the
        # ``WORKERS == NULL`` early-out and pass vacuously.
        c = Cown(Counter())

        @when(c)
        def _(c):
            send("done", 1)

        _collect_done(1)
        # While the runtime is still alive, ``scheduler_stats()`` is
        # non-empty — this proves the runtime really did come up and
        # the next ``wait()`` will perform a load-bearing
        # ``scheduler_runtime_stop``.
        live_stats = _core.scheduler_stats()
        assert live_stats, (
            "runtime must be alive before tearing it down so the first "
            f"scheduler_runtime_stop has work to do; got {live_stats!r}"
        )
        wait()
        # First (real) call already happened inside ``stop_workers()``.
        # The array is freed and ``scheduler_stats()`` is empty.
        assert _core.scheduler_stats() == [], (
            "wait() should have left WORKER_COUNT == 0"
        )
        # A second explicit call must be a no-op (no crash, no error).
        _core.scheduler_runtime_stop()
        assert _core.scheduler_stats() == [], (
            "second scheduler_runtime_stop must leave WORKER_COUNT == 0"
        )
        # And a third, for good measure.
        _core.scheduler_runtime_stop()
        assert _core.scheduler_stats() == []
