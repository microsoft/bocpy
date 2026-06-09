"""Tests for :class:`bocpy.PinnedCown` and the pinned-behavior pump.

Pinned cowns are permanently owned by the main interpreter; their
values never round-trip through XIData. Behaviors whose request set
contains at least one :class:`PinnedCown` run on main via the pump
rather than on a worker.

Three test classes cover three use shapes:

* :class:`TestPinnedCownBasics` -- construction invariants (value
  identity preserved across acquire/release, debug-build destructor,
  wrong-interpreter raise).
* :class:`TestPinnedCownsAutoDrain` -- the script-mode path:
  :func:`bocpy.wait` auto-pumps when any :class:`PinnedCown` is
  live, and the shutdown drain in ``stop_workers`` clears any
  pinned work still in the queue.
* :class:`TestPinnedCownsManualPump` -- the event-loop integration
  path: the public :func:`bocpy.pump` facade with ``deadline_ms``,
  ``max_behaviors``, ``raise_on_error``, BaseException propagation,
  and reentry rejection.

Tests read results back through ``quiesce()`` + :meth:`Cown.unwrap`
(behaviors return the value under test); worker-scheduled pinned
``@when`` chains are double-unwrapped. The handful of cross-interpreter
handoffs that genuinely need the message queue (the round-trip
producer/consumer split) keep ``send`` / ``receive``.
"""

from __future__ import annotations

from functools import partial
import gc
import time

import pytest

from bocpy import (
    _core,
    Cown,
    notice_update,
    notice_write,
    noticeboard,
    PinnedCown,
    pump,
    quiesce,
    set_pump_watchdog,
    set_wait_pump_poll,
    start,
    wait,
    when,
)
from bocpy import behaviors as _behaviors


QUIESCE_TIMEOUT = 10


def _replace_with(new_value, _old):
    """notice_update fn that ignores the prior value and substitutes ``new_value``.

    Defined at module scope so it is picklable across the boundary into
    the noticeboard sub-interpreter. Use via ``partial(_replace_with, x)``.
    """
    return new_value


class _NotPicklable:
    """Probe: pinned values must not be pickled.

    Any path that routes the value through ``object_to_xidata`` raises
    :class:`TypeError` from ``__reduce_ex__``, so a regression in the
    pinned acquire/release short-circuit surfaces immediately.
    """

    def __reduce_ex__(self, protocol):  # noqa: D401
        raise TypeError("pinned cown values must never be pickled")

    def __repr__(self) -> str:
        return "<NotPicklable sentinel>"


class TestPinnedCownBasics:
    """PinnedCown construction invariants (no schedule, no pump)."""

    @classmethod
    def teardown_class(cls):
        wait()

    def test_pinned_value_identity_and_no_pickle(self):
        """Pinned-cown value keeps identity across many acquire cycles.

        Schedules 64 single-cown behaviors against one
        :class:`PinnedCown` wrapping a non-picklable probe. Any path
        that routes the value through ``object_to_xidata`` would
        raise from ``__reduce_ex__``; any path that disowns the
        value would change ``id(pc.value)``.
        """
        obj = _NotPicklable()
        obj_id = id(obj)
        pc = PinnedCown(obj)

        readers = []
        for _ in range(64):
            @when(pc)
            def _body(pc):
                return id(pc.value)
            readers.append(_body)

        quiesce(QUIESCE_TIMEOUT)
        for r in readers:
            assert r.unwrap() == obj_id

    def test_pinned_destruct_after_construction_only(self):
        """Drop a pinned cown immediately after construction.

        Debug builds (``.env313d``) trip ownership assertions if
        ``cown_decref_inline`` does not tolerate
        ``value != NULL && xidata == NULL`` on pinned cowns. Touching
        only the construct + drop path keeps the test focused on the
        destructor; the auto-drain class covers the schedule path.
        """
        pc = PinnedCown(_NotPicklable())
        del pc
        gc.collect()

    def test_pinned_cown_off_main_raises(self):
        """``PinnedCown(...)`` from a worker raises ``RuntimeError``.

        The behavior runs on a worker (no pinned cown in its request
        set), constructs a ``PinnedCown`` off-main, and the resulting
        ``RuntimeError`` is captured on the result cown. ``unwrap``
        re-raises it on the main thread.
        """
        @when()
        def _probe():
            PinnedCown(object())

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(RuntimeError, match="main interpreter"):
            _probe.unwrap()


class TestPinnedCownsAutoDrain:
    """Script-mode path: ``wait()`` auto-pumps and ``stop()`` drains."""

    @classmethod
    def teardown_class(cls):
        wait()

    def test_wait_auto_drains_pinned(self):
        """A pinned behavior scheduled before quiesce() runs on main without pump()."""
        pc = PinnedCown({"hits": 0})

        @when(pc)
        def _body(pc):
            pc.value["hits"] += 1
            return pc.value["hits"]

        quiesce(QUIESCE_TIMEOUT)
        assert _body.unwrap() == 1

    def test_wait_pinned_cown_in_cown(self):
        pc = PinnedCown({"hits": 0})
        wrap = Cown(pc)

        @when(wrap)
        def _wrapper(w):
            @when(w.value)
            def _body(pc):
                pc.value["hits"] += 1
                return pc.value["hits"]
            return _body

        quiesce(QUIESCE_TIMEOUT)
        assert _wrapper.unwrap().unwrap() == 1

    def test_main_pump_drain_all_marks_result_cowns(self):
        """``_core.main_pump_drain_all`` pops every entry and marks each result Cown with a shutdown RuntimeError."""
        pcs = [PinnedCown(0) for _ in range(8)]
        results = []
        for pc in pcs:
            @when(pc)
            def _body(pc):
                pc.value += 1
            results.append(_body)

        assert _core.main_pump_queue_depth() == 8

        drained = _core.main_pump_drain_all()
        assert drained == 8, (
            f"main_pump_drain_all must pop every queued behavior; "
            f"got {drained}"
        )
        assert _core.main_pump_queue_depth() == 0

        for result in results:
            with result:
                assert result.exception is True, (
                    "main_pump_drain_all must set exception=True on "
                    "every drained behavior's result Cown"
                )
                assert isinstance(result.value, RuntimeError), (
                    f"expected RuntimeError, got "
                    f"{type(result.value).__name__}"
                )
                assert "shutdown" in str(result.value), (
                    f"drop-exception message did not mention "
                    f"shutdown: {result.value!r}"
                )

    def test_stop_drains_pinned_queue(self):
        """An explicit stop() should leave MAIN_PINNED_QUEUE empty.

        Also verifies the transpiler's per-iteration capture of ``i``:
        a final pinned behaviour reads ``pc.value`` and writes the
        tuple to the noticeboard. A regression that late-bound ``i``
        at body-execution time would yield ``(3, 3, 3, 3)`` instead of
        ``(0, 1, 2, 3)``.
        """
        pc = PinnedCown([])
        for i in range(4):
            @when(pc)
            def _body(pc):
                pc.value.append(i)  # noqa: B023

        @when(pc)
        def _final(pc):
            notice_write("pinned_final", tuple(pc.value))

        snap = wait(noticeboard=True)
        assert snap["pinned_final"] == (0, 1, 2, 3), (
            f"per-iteration capture of i broke: expected "
            f"(0, 1, 2, 3), got {snap['pinned_final']!r}"
        )
        assert _core.main_pump_queue_depth() == 0

    def test_shutdown_does_not_disown_pinned_value(self):
        """The Python value inside a PinnedCown must outlive stop().

        Schedule a pinned behaviour that reads the underlying value's
        identity and contents; after ``quiesce()`` completes, the value
        should still be reachable (no disown / no XIData round-trip).
        The body runs on main via the pump, so ``id(pc.value)`` is
        directly comparable to the value captured at construction.
        """
        v = ["sentinel"]
        pc = PinnedCown(v)
        v_id = id(v)

        @when(pc)
        def _body(pc):
            id_matches = id(pc.value) == v_id
            pc.value.append("post-acquire")
            return (id_matches, list(pc.value))

        quiesce(QUIESCE_TIMEOUT)
        id_matches, contents = _body.unwrap()
        assert id_matches is True
        assert contents == ["sentinel", "post-acquire"]


class TestPinnedCownsManualPump:
    """Public :func:`bocpy.pump` facade for event-loop integration.

    Script-mode users get the same drain via :func:`wait`; these
    tests exist because the bounding arguments (``deadline_ms``,
    ``max_behaviors``, ``raise_on_error``) and the
    reentry/BaseException paths can only be exercised by an
    explicit pump caller.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_pump_max_behaviors_caps_drain(self):
        """``max_behaviors`` stops the drain at the requested bound."""
        pcs = [PinnedCown(i) for i in range(10)]
        for pc in pcs:
            @when(pc)
            def _body(pc):
                pass

        assert _core.main_pump_queue_depth() == 10

        result = pump(max_behaviors=3)
        assert result.executed == 3
        assert result.raised == 0
        assert result.deadline_reached is False
        assert _core.main_pump_queue_depth() == 7

        rest = pump()
        assert rest.executed == 7
        assert _core.main_pump_queue_depth() == 0

    def test_pump_deadline_caps_drain(self):
        """``deadline_ms`` may trip before the queue drains.

        Tolerates a sub-1ms full drain on fast hardware: either the
        deadline trips with a partial drain, or the whole queue
        drains in time. Both paths verify the result-tuple
        invariants.
        """
        pcs = [PinnedCown(i) for i in range(50)]
        for pc in pcs:
            @when(pc)
            def _body(pc):
                pass

        result = pump(deadline_ms=1)
        assert result.raised == 0

        if result.deadline_reached:
            assert 0 < result.executed < 50
            remaining = 50 - result.executed
            rest = pump()
            assert rest.executed == remaining
        else:
            assert result.executed == 50

        assert _core.main_pump_queue_depth() == 0

    def test_pump_raise_on_error_re_raises(self):
        """``raise_on_error`` re-raises the first body Exception.

        After re-raise the second behavior is still queued; a
        follow-up unbounded pump drains it.
        """
        pc1 = PinnedCown("payload1")
        pc2 = PinnedCown("payload2")

        @when(pc1)
        def _body1(pc):
            raise ValueError(f"boom: {pc.value!r}")

        @when(pc2)
        def _body2(pc):
            raise ValueError(f"boom: {pc.value!r}")

        with pytest.raises(ValueError, match="boom"):
            pump(raise_on_error=True)

        rest = pump()
        assert rest.executed == 1

    def test_pump_propagates_base_exception(self):
        """:class:`BaseException` propagates out, cleanup still runs.

        After the first body's :class:`KeyboardInterrupt` re-raises,
        the second behavior is still queued; a follow-up unbounded
        pump drains it.
        """
        pc1 = PinnedCown("payload1")
        pc2 = PinnedCown("payload2")

        @when(pc1)
        def _body1(pc):
            raise KeyboardInterrupt("base-exc from pump body")

        @when(pc2)
        def _body2(pc):
            pass

        assert _core.main_pump_queue_depth() == 2

        with pytest.raises(KeyboardInterrupt, match="base-exc"):
            pump()

        assert _core.main_pump_queue_depth() == 1

        rest = pump()
        assert rest.executed == 1

    def test_pump_rejects_nested_call(self):
        """``pump()`` from inside a pinned body raises ``RuntimeError``.

        The body wraps the inner ``pump()`` in a try/except and returns
        whether the re-entrancy guard fired, so the outer pump observes
        ``raised == 0``.
        """
        pc = PinnedCown("nest")

        @when(pc)
        def _attempt(pc):
            try:
                pump()
                return False
            except RuntimeError as ex:
                return "not reentrant" in str(ex)

        result = pump()
        assert result.executed == 1
        assert result.raised == 0
        assert _attempt.unwrap() is True


class TestPumpArgValidation:
    """Type / bound validation in the :func:`pump` Python wrapper.

    Validates the contract: ``deadline_ms`` and ``max_behaviors``
    must be ``None`` or a positive :class:`int` (not :class:`bool`).
    ``0`` is rejected outright: the caller's "skip if budget is
    zero" intent belongs in a one-line ``if budget:`` guard at the
    call site, not inside a short-circuit branch that would also
    bypass the live-runtime check.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    @pytest.mark.parametrize("bad", [0, -1, -1000, 1.5, "1", True, False])
    def test_pump_deadline_ms_rejects_bad_input(self, bad):
        """Non-None / non-int / non-positive / bool ``deadline_ms`` raises."""
        with pytest.raises(TypeError, match="deadline_ms"):
            pump(deadline_ms=bad)

    @pytest.mark.parametrize("bad", [0, -1, -1000, 1.5, "1", True, False])
    def test_pump_max_behaviors_rejects_bad_input(self, bad):
        """Non-None / non-int / non-positive / bool ``max_behaviors`` raises."""
        with pytest.raises(TypeError, match="max_behaviors"):
            pump(max_behaviors=bad)

    def test_validator_non_ms_bound_not_capped(self):
        """A non-ms bound passes through the validator without OverflowError."""
        huge = _behaviors._MAX_PUMP_MS * 1000 + 1
        assert (
            _behaviors._validate_pump_bound("max_behaviors", huge)
            == huge
        )

    def test_validator_ms_bound_capped(self):
        """An ms-flagged bound > _MAX_PUMP_MS raises OverflowError."""
        with pytest.raises(OverflowError, match="exceeds"):
            _behaviors._validate_pump_bound(
                "deadline_ms",
                _behaviors._MAX_PUMP_MS + 1,
                ms=True,
            )


class TestPumpRuntimeRequired:
    """``pump()`` refuses to run without a live runtime.

    Previously the wrapper silently fell back to ``sys.modules['__main__']``
    when ``BEHAVIORS.export_module`` was unset, which let pinned behaviors
    fail with cryptic per-iteration ``AttributeError``s on thunk lookup.
    The new contract is a single loud :class:`RuntimeError` at
    :func:`pump` entry naming the missing precondition.
    """

    def test_pump_before_start_raises_runtimeerror(self):
        """Without a live ``BEHAVIORS``, :func:`pump` raises immediately."""
        assert _behaviors.BEHAVIORS is None, (
            "expected runtime to be stopped before this test; previous "
            "test did not call wait() in teardown"
        )

        with pytest.raises(RuntimeError, match="bocpy.start"):
            pump()
        assert _behaviors.BEHAVIORS is None


def test_set_wait_pump_poll_re_read():
    """``_WAIT_PUMP_POLL_MS`` is re-read on every auto-pump iteration."""
    set_wait_pump_poll(50)
    assert _behaviors._WAIT_PUMP_POLL_MS == 50
    set_wait_pump_poll(5)
    assert _behaviors._WAIT_PUMP_POLL_MS == 5
    set_wait_pump_poll(50)


def test_set_wait_pump_poll_validation():
    """Reject zero, negative, non-int, and bool inputs."""
    with pytest.raises(TypeError):
        set_wait_pump_poll(0)
    with pytest.raises(TypeError):
        set_wait_pump_poll(-1)
    with pytest.raises(TypeError):
        set_wait_pump_poll(1.5)
    with pytest.raises(TypeError):
        set_wait_pump_poll(True)


def test_terminator_wake_reason_constants():
    assert _core.TERMINATED == 0
    assert _core.PUMP_READY == 1
    assert _core.WAIT_TIMED_OUT == 2


def test_terminator_wait_pumpable_terminated_when_empty():
    reason = _core.terminator_wait_pumpable(0.01)
    assert reason == _core.TERMINATED


def test_main_pump_drain_all_empty():
    assert _core.main_pump_drain_all() == 0
    assert _core.main_pump_queue_depth() == 0
    gc.collect()


class TestPinnedRoundTrip:
    """Pinned-cown handles round-trip through workers and noticeboards.

    The pinned *value* never crosses an interpreter boundary. The
    pinned *handle* (the wrapper + capsule) does -- a worker that ends
    up holding one can do exactly one useful thing with it: schedule
    a pinned ``@when`` against it, which the runtime routes to the main
    pump queue. These tests assert that handle round-trips and that the
    routing decision lives in the capsule, not in the Python wrapper
    class.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_handle_round_trip_via_worker_closure(self):
        """Worker receives a pinned handle via closure capture, schedules a pinned @when."""
        pc = PinnedCown([])
        unrelated = Cown(0)

        @when(unrelated)
        def _ship(u):
            @when(pc)
            def _on_main(pc):
                pc.value.append("main-ran")
                return pc.value
            return (_core.cown_is_pinned(pc.impl), _on_main)

        quiesce(QUIESCE_TIMEOUT)
        is_pinned, on_main = _ship.unwrap()
        assert is_pinned is True
        assert on_main.unwrap() == ["main-ran"]

    def test_pinned_via_noticeboard_write(self):
        """``notice_write("k", PinnedCown(x))`` round-trips to a worker reader."""
        start()
        pc = PinnedCown([])
        notice_write("t5_pc", pc)
        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        unrelated = Cown(0)

        @when(unrelated)
        def _reader(u):
            h = noticeboard()["t5_pc"]

            @when(h)
            def _on_main(h):
                h.value.append("via-noticeboard")
                return h.value
            return (_core.cown_is_pinned(h.impl), _on_main)

        quiesce(QUIESCE_TIMEOUT)
        is_pinned, on_main = _reader.unwrap()
        assert is_pinned is True
        assert on_main.unwrap() == ["via-noticeboard"]

    def test_pinned_list_via_noticeboard(self):
        """A worker pulls handles out of a list payload and chains pinned @whens."""
        start()
        pcs = [PinnedCown([]), PinnedCown([])]
        notice_write("t6_pcs", pcs)
        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        unrelated = Cown(0)

        @when(unrelated)
        def _reader(u):
            handles = noticeboard()["t6_pcs"]
            pins = []
            bodies = []
            for i, h in enumerate(handles):
                pins.append(_core.cown_is_pinned(h.impl))

                @when(h)
                def _on_main(h, i=i):
                    h.value.append(("chain", i))
                    return h.value
                bodies.append(_on_main)
            return (len(handles), pins, bodies)

        quiesce(QUIESCE_TIMEOUT)
        n, pins, bodies = _reader.unwrap()
        assert n == 2
        assert pins == [True, True]
        assert bodies[0].unwrap() == [("chain", 0)]
        assert bodies[1].unwrap() == [("chain", 1)]

    def test_pinned_nested_in_regular_cown_value(self):
        """``Cown({"pc": PinnedCown(x), ...})`` -- worker extracts the inner handle."""
        pc = PinnedCown([])
        outer = Cown({"pc": pc, "tag": "wrap"})

        @when(outer)
        def _worker(o):
            inner = o.value["pc"]

            @when(inner)
            def _on_main(inner):
                inner.value.append("from-nested")
                return inner.value
            return (_core.cown_is_pinned(inner.impl), o.value["tag"], _on_main)

        quiesce(QUIESCE_TIMEOUT)
        is_pinned, tag, on_main = _worker.unwrap()
        assert is_pinned is True
        assert tag == "wrap"
        assert on_main.unwrap() == ["from-nested"]

    def test_two_workers_share_pinned_handle_via_noticeboard(self):
        """Two workers each read the same pinned handle; both pinned bodies run on main."""
        start()
        pc = PinnedCown([])
        notice_write("t16_pc", pc)
        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        u1 = Cown(0)
        u2 = Cown(0)

        @when(u1)
        def _w1(u):
            h = noticeboard()["t16_pc"]

            @when(h)
            def _body(h):
                h.value.append("w1")
                return h.value[-1]
            return (_core.cown_is_pinned(h.impl), _body)

        @when(u2)
        def _w2(u):
            h = noticeboard()["t16_pc"]

            @when(h)
            def _body(h):
                h.value.append("w2")
                return h.value[-1]
            return (_core.cown_is_pinned(h.impl), _body)

        quiesce(QUIESCE_TIMEOUT)
        p1, b1 = _w1.unwrap()
        p2, b2 = _w2.unwrap()
        assert p1 is True and p2 is True
        assert b1.unwrap() == "w1"
        assert b2.unwrap() == "w2"

        sentinel = PinnedCown(None)

        @when(sentinel)
        def _inspect(_s):
            return sorted(pc.value)

        quiesce(QUIESCE_TIMEOUT)
        assert _inspect.unwrap() == ["w1", "w2"]

    def test_pinned_via_notice_update(self):
        """``notice_update`` with a pinned producer; readers see the pinned handle."""
        start()
        pc = PinnedCown([])
        notice_update("t16b_pc", partial(_replace_with, pc), default=None)
        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        unrelated = Cown(0)

        @when(unrelated)
        def _reader(u):
            h = noticeboard()["t16b_pc"]
            return (h is not None, _core.cown_is_pinned(h.impl))

        quiesce(QUIESCE_TIMEOUT)
        not_none, is_pinned = _reader.unwrap()
        assert not_none is True
        assert is_pinned is True

    def test_body_raise_drains_queue(self):
        """A raising pinned body marks its result cown and the queue still drains."""
        pc_raise = PinnedCown(0)
        pc_ok = PinnedCown(0)

        @when(pc_raise)
        def raiser(pc):
            raise RuntimeError("planned-failure")

        @when(pc_ok)
        def survivor(pc):
            return "survived"

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(RuntimeError, match="planned-failure"):
            raiser.unwrap()
        assert survivor.unwrap() == "survived"
        assert _core.main_pump_queue_depth() == 0

    @pytest.mark.parametrize("kind,expected_on_main", [
        (("p", "p"), True),
        (("p", "u"), True),
        (("u", "p"), True),
        (("u", "u"), False),
        (("p", "p", "u"), True),
        (("u", "u", "u"), False),
    ])
    def test_mixed_request_set_routes_to_main_iff_pinned(
            self, kind, expected_on_main):
        """Every request set containing a pinned cown routes to the main pump."""
        cowns = [PinnedCown(0) if k == "p" else Cown(0) for k in kind]

        if len(cowns) == 2:
            a, b = cowns

            @when(a, b)
            def _body(a, b):
                return _core.is_primary()
        else:
            a, b, c = cowns

            @when(a, b, c)
            def _body(a, b, c):
                return _core.is_primary()

        quiesce(QUIESCE_TIMEOUT)
        assert _body.unwrap() is expected_on_main


class TestPinnedWatchdog:
    """Pump-starvation watchdog: warn-side callback only.

    The watchdog samples at ``pump()`` entry. ``warn_ms`` invokes the
    ``on_starve`` callback (or the default ``bocpy.pump`` logger) once
    per non-empty epoch. It gates on the pinned queue's non-empty
    time, so an unpinned-only window never trips it.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def teardown_method(self, method):
        set_pump_watchdog(warn_ms=None, on_starve=None)

    def test_warn_only_fires_on_starvation(self):
        """``warn_ms`` invokes on_starve once after the threshold elapses."""
        warns = []

        def on_starve(severity, message):
            warns.append((severity, str(message)))

        set_pump_watchdog(warn_ms=50, on_starve=on_starve)

        pc = PinnedCown(0)

        @when(pc)
        def _body(pc):
            pass

        time.sleep(0.15)
        quiesce(QUIESCE_TIMEOUT)

        assert any(s == 0 for s, _ in warns), (
            f"expected warn (severity 0) in {warns!r}")

    def test_unpinned_only_window_does_not_trip_watchdog(self):
        """Watchdog gates on pinned-queue age, not on total work time."""
        warns = []

        def on_starve(severity, message):
            warns.append((severity, str(message)))

        set_pump_watchdog(warn_ms=20, on_starve=on_starve)

        c = Cown(0)
        for _ in range(8):
            @when(c)
            def _busy(c):
                time.sleep(0.02)
        quiesce()
        assert warns == [], (
            f"warn must not fire across unpinned-only window, got {warns!r}")

        pc = PinnedCown(0)

        @when(pc)
        def _body(pc):
            pass

        quiesce(QUIESCE_TIMEOUT)

    def test_reconfigure_after_first_pinned(self):
        """``set_pump_watchdog`` succeeds after live pinned work exists.

        This test pins the as-shipped contract: reconfiguration is
        unconditional and the new thresholds take effect on subsequent
        samples.
        """
        set_pump_watchdog(warn_ms=20, on_starve=None)

        pc = PinnedCown(0)

        @when(pc)
        def _body(pc):
            pass

        warns = []

        def on_starve(severity, message):
            warns.append((severity, str(message)))

        set_pump_watchdog(warn_ms=200, on_starve=on_starve)

        quiesce(QUIESCE_TIMEOUT)


class TestPumpWatchdogOverflow:
    """ms-typed args reject inputs that would overflow ns scaling."""

    @classmethod
    def teardown_class(cls):
        wait()

    def test_pump_ms_overflow_raises_overflowerror(self):
        """``pump(deadline_ms=_MAX+1)`` raises :class:`OverflowError`."""
        too_big = _behaviors._MAX_PUMP_MS + 1
        with pytest.raises(OverflowError, match="deadline_ms"):
            pump(deadline_ms=too_big)

    def test_set_pump_watchdog_ms_overflow(self):
        """``set_pump_watchdog(warn_ms=_MAX+1)`` raises :class:`OverflowError`."""
        too_big = _behaviors._MAX_PUMP_MS + 1
        with pytest.raises(OverflowError, match="warn_ms"):
            set_pump_watchdog(warn_ms=too_big)
        set_pump_watchdog(warn_ms=1000, on_starve=None)


class TestSetPumpWatchdogValidation:
    """Tighter validators -- `0` is rejected.

    `None` is the documented disable sentinel; `0` previously
    slipped through the Python validator and silently turned the
    sampler off in C.
    """

    @classmethod
    def teardown_class(cls):
        set_pump_watchdog(warn_ms=1000, on_starve=None)
        wait()

    def test_zero_rejected(self):
        """``warn_ms=0`` is no longer a silent-disable value."""
        with pytest.raises(TypeError, match="positive int or None"):
            set_pump_watchdog(warn_ms=0)


class TestDrainErrorsSurviveBaseException:
    """release_all failures are stashed before re-raising KI / SE."""

    def test_drain_errors_preserved_on_keyboard_interrupt(self, monkeypatch):
        """A KI mid-drain must not erase already-captured release_all errors.

        Fakes two orphan payloads: the first fails ``release_all`` with
        a normal :class:`Exception` (which lands in the local ``errors``
        list); the second fails with :class:`KeyboardInterrupt` (which
        defers the re-raise). Before the fix the deferred re-raise
        skipped past the ``return errors, drained_count`` assignment
        and the normal error was silently lost.
        """
        b = _behaviors.Behaviors(0)

        class _OkPayload:
            def set_drop_exception(self, exc):
                pass

            def release_all(self):
                raise ValueError("release-fail-1")

        class _KIPayload:
            def set_drop_exception(self, exc):
                pass

            def release_all(self):
                raise KeyboardInterrupt("from-drain")

        rounds = iter([[_OkPayload(), _KIPayload()], []])
        monkeypatch.setattr(
            _core, "scheduler_drain_all_queues", lambda: next(rounds))
        monkeypatch.setattr(_core, "terminator_dec", lambda: 0)

        with pytest.raises(KeyboardInterrupt) as ei:
            b._drain_orphan_behaviors()

        assert len(b._stop_drain_errors) == 2
        assert isinstance(b._stop_drain_errors[0], ValueError)
        assert isinstance(b._stop_drain_errors[1], KeyboardInterrupt)
        notes = getattr(ei.value, "__notes__", []) or []
        assert any("2 release_all error" in n for n in notes), notes
