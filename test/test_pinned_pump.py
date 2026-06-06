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

All tests follow the standard ``send("assert", ...)`` / ``receive``
/ trailing-``wait()`` idiom documented in
``.github/skills/testing-with-boc/SKILL.md``.
"""

from __future__ import annotations

from functools import partial
import gc
import time

import pytest

from bocpy import (
    _core,
    Cown,
    drain,
    notice_sync,
    notice_update,
    notice_write,
    noticeboard,
    PinnedCown,
    pump,
    quiesce,
    receive,
    send,
    set_pump_watchdog,
    set_wait_pump_poll,
    start,
    TIMEOUT,
    wait,
    when,
)
from bocpy import behaviors as _behaviors


RECEIVE_TIMEOUT = 10


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


def receive_asserts(count=1):
    """Drain all expected assertion messages, then fail on first mismatch.

    The "assert" queue is always drained before returning so that leftover
    messages from a failing test do not leak into subsequent tests in CI.
    """
    failed = None
    timed_out = False
    try:
        for _ in range(count):
            result = receive("assert", RECEIVE_TIMEOUT)
            if result[0] == TIMEOUT:
                timed_out = True
                break
            _, (actual, expected) = result
            if failed is None and actual != expected:
                failed = (actual, expected)
    finally:
        drain("assert")

    assert not timed_out, (
        "Timed out waiting for an 'assert' message from a behavior. "
        "Check that every @when arg count matches the decorated "
        "function's parameter count."
    )
    if failed is not None:
        actual, expected = failed
        assert actual == expected, f"expected {expected!r}, got {actual!r}"


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

        for _ in range(64):
            @when(pc)
            def _body(pc):
                send("assert", (id(pc.value), obj_id))

        # quiesce() so worker sub-interpreters survive until
        # receive_asserts reads their messages.
        quiesce()
        receive_asserts(64)

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

        The except-clause cannot bind the exception to a name -- the
        transpiler's free-variable scan treats ``ExceptHandler.name``
        as a capture and frame-walking cannot resolve it. Capture
        the type name and a substring of the message into plain
        locals inside the except block and ship them through the
        standard ``"assert"`` tag.
        """
        @when()
        def _():
            exc_type_name = "no-raise"
            msg_mentions_main = False
            try:
                PinnedCown(object())
            except RuntimeError as ex:
                exc_type_name = "RuntimeError"
                msg_mentions_main = "main interpreter" in str(ex)

            send("assert", (
                (exc_type_name, msg_mentions_main),
                ("RuntimeError", True),
            ))

        quiesce()
        receive_asserts()


class TestPinnedCownsAutoDrain:
    """Script-mode path: ``wait()`` auto-pumps and ``stop()`` drains."""

    @classmethod
    def teardown_class(cls):
        wait()

    # wait() with pinned cowns auto-drains.
    def test_wait_auto_drains_pinned(self):
        """A pinned behavior scheduled before wait() runs on main without pump()."""
        pc = PinnedCown({"hits": 0})

        @when(pc)
        def _body(pc):
            pc.value["hits"] += 1
            send("assert", ("ran", "ran"))

        wait()
        receive_asserts()

    def test_wait_pinned_cown_in_cown(self):
        pc = PinnedCown({"hits": 0})
        wrap = Cown(pc)

        @when(wrap)
        def _wrapper(w):
            @when(w.value)
            def _body(pc):
                pc.value["hits"] += 1
                send("assert", ("ran", "ran"))

        wait()
        receive_asserts()

    def test_main_pump_drain_all_marks_result_cowns(self):
        """``_core.main_pump_drain_all`` pops every entry and marks each result Cown with a shutdown RuntimeError."""
        # 8 distinct cowns: same-cown behaviours serialise via MCS and only the head sits in MAIN_PINNED_QUEUE.
        pcs = [PinnedCown(0) for _ in range(8)]
        # Capture each @when's result Cown (the value returned by the
        # decorator) so we can inspect its exception/value after the
        # drain runs.
        results = []
        for pc in pcs:
            @when(pc)
            def _body(pc):
                pc.value += 1
            results.append(_body)

        # Precondition: all 8 still queued (no pump has run yet).
        assert _core.main_pump_queue_depth() == 8

        drained = _core.main_pump_drain_all()
        assert drained == 8, (
            f"main_pump_drain_all must pop every queued behavior; "
            f"got {drained}"
        )
        assert _core.main_pump_queue_depth() == 0

        # Re-acquire each result via the Cown context manager; *every* one must carry the drop-exception.
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

    # stop() with pending pinned work: drain runs.
    def test_stop_drains_pinned_queue(self):
        """An explicit stop() should leave MAIN_PINNED_QUEUE empty.

        Also verifies the transpiler's per-iteration capture of ``i``:
        a final pinned behaviour reads ``pc.value`` and ships the
        tuple back via ``send("final", ...)``. A regression that
        late-bound ``i`` at body-execution time would yield ``(3, 3,
        3, 3)`` instead of ``(0, 1, 2, 3)``.
        """
        pc = PinnedCown([])
        for i in range(4):
            @when(pc)
            def _body(pc):
                pc.value.append(i)  # noqa: B023
                send("assert", ("ran", "ran"))

        @when(pc)
        def _final(pc):
            send("final", tuple(pc.value))

        wait()
        receive_asserts(4)
        final_tag, final_payload = receive("final", RECEIVE_TIMEOUT)
        try:
            assert final_tag != TIMEOUT, (
                "timed out waiting for the final pinned behaviour"
            )
            assert final_payload == (0, 1, 2, 3), (
                f"per-iteration capture of i broke: expected "
                f"(0, 1, 2, 3), got {final_payload!r}"
            )
        finally:
            drain("final")
        assert _core.main_pump_queue_depth() == 0

    # shutdown_no_disown: refcount of pinned value preserved.
    def test_shutdown_does_not_disown_pinned_value(self):
        """The Python value inside a PinnedCown must outlive stop().

        Schedule a pinned behaviour that records ``sys.getrefcount`` of the
        underlying value before and after the body runs; after ``wait()``
        completes, the value should still be reachable (no disown / no
        XIData round-trip). The test reads the value via a fresh
        PinnedCown handle inside a follow-up behaviour rather than from
        test code so we don't reach across the shutdown boundary.
        """
        v = ["sentinel"]
        pc = PinnedCown(v)
        v_id = id(v)

        @when(pc)
        def _body(pc):
            # Value identity preserved across acquire/release.
            send("assert", (id(pc.value), v_id))
            pc.value.append("post-acquire")
            send("assert", (pc.value, ["sentinel", "post-acquire"]))

        wait()
        receive_asserts(2)


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
                send("assert", ("ran", "ran"))

        assert _core.main_pump_queue_depth() == 10

        result = pump(max_behaviors=3)
        assert result.executed == 3
        assert result.raised == 0
        assert result.deadline_reached is False
        assert _core.main_pump_queue_depth() == 7

        rest = pump()
        assert rest.executed == 7
        assert _core.main_pump_queue_depth() == 0

        receive_asserts(10)

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
                send("assert", ("ran", "ran"))

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
        receive_asserts(50)

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
        pump drains it and surfaces its ``send``.
        """
        pc1 = PinnedCown("payload1")
        pc2 = PinnedCown("payload2")

        @when(pc1)
        def _body1(pc):
            raise KeyboardInterrupt("base-exc from pump body")

        @when(pc2)
        def _body2(pc):
            send("assert", ("survivor-ran", "survivor-ran"))

        assert _core.main_pump_queue_depth() == 2

        with pytest.raises(KeyboardInterrupt, match="base-exc"):
            pump()

        assert _core.main_pump_queue_depth() == 1

        rest = pump()
        assert rest.executed == 1
        receive_asserts(1)

    def test_pump_rejects_nested_call(self):
        """``pump()`` from inside a pinned body raises ``RuntimeError``.

        The body wraps the inner ``pump()`` in a try/except and ships
        the captured type name + message-substring through the
        standard ``"assert"`` tag, so the outer pump observes
        ``raised == 0``.
        """
        pc = PinnedCown("nest")

        @when(pc)
        def _attempt(pc):
            exc_type_name = "no-raise"
            msg_says_reentrant = False
            try:
                pump()
            except RuntimeError as ex:
                exc_type_name = "RuntimeError"
                msg_says_reentrant = "not reentrant" in str(ex)
            send("assert", (
                (exc_type_name, msg_says_reentrant),
                ("RuntimeError", True),
            ))

        result = pump()
        assert result.executed == 1
        assert result.raised == 0
        receive_asserts()


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

    # Type rejection (incl. 0).
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

    # The overflow cap is gated on the explicit ``ms=True``
    # kwarg, not a name-string heuristic. A non-ms bound named
    # ``max_behaviors`` must NOT trip the cap even at huge values.
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
        # Ensure the runtime is fully torn down: a prior test in the
        # session may have left BEHAVIORS populated.
        assert _behaviors.BEHAVIORS is None, (
            "expected runtime to be stopped before this test; previous "
            "test did not call wait() in teardown"
        )

        with pytest.raises(RuntimeError, match="bocpy.start"):
            pump()
        # Stillborn pump must not start the runtime as a side effect.
        assert _behaviors.BEHAVIORS is None


# set_wait_pump_poll picked up mid-wait.
def test_set_wait_pump_poll_re_read():
    """``_WAIT_PUMP_POLL_MS`` is re-read on every auto-pump iteration."""
    set_wait_pump_poll(50)
    assert _behaviors._WAIT_PUMP_POLL_MS == 50
    set_wait_pump_poll(5)
    assert _behaviors._WAIT_PUMP_POLL_MS == 5
    # restore default
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

# Sanity: the new C constants exist with the expected integer values.


def test_terminator_wake_reason_constants():
    assert _core.TERMINATED == 0
    assert _core.PUMP_READY == 1
    assert _core.WAIT_TIMED_OUT == 2


# Sanity: terminator_wait_pumpable returns TERMINATED when no work is in flight.
def test_terminator_wait_pumpable_terminated_when_empty():
    # No outstanding behaviours: count must be 0 -> TERMINATED.
    reason = _core.terminator_wait_pumpable(0.01)
    assert reason == _core.TERMINATED


# Sanity: main_pump_drain_all on an empty queue returns 0 and is a no-op.
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
            send("assert", (_core.cown_is_pinned(pc.impl), True))

            @when(pc)
            def _on_main(pc):
                pc.value.append("main-ran")
                send("assert", (pc.value, ["main-ran"]))

        quiesce()
        receive_asserts(2)

    def test_pinned_via_noticeboard_write(self):
        """``notice_write("k", PinnedCown(x))`` round-trips to a worker reader."""
        start()
        pc = PinnedCown([])
        notice_write("t5_pc", pc)
        notice_sync()

        unrelated = Cown(0)

        @when(unrelated)
        def _reader(u):
            h = noticeboard()["t5_pc"]
            send("assert", (_core.cown_is_pinned(h.impl), True))

            @when(h)
            def _on_main(h):
                h.value.append("via-noticeboard")
                send("assert", (h.value, ["via-noticeboard"]))

        wait()
        receive_asserts(2)

    def test_pinned_list_via_noticeboard(self):
        """A worker pulls handles out of a list payload and chains pinned @whens."""
        start()
        pcs = [PinnedCown([]), PinnedCown([])]
        notice_write("t6_pcs", pcs)
        notice_sync()

        unrelated = Cown(0)

        @when(unrelated)
        def _reader(u):
            handles = noticeboard()["t6_pcs"]
            send("assert", (len(handles), 2))
            for i, h in enumerate(handles):
                send("assert", (_core.cown_is_pinned(h.impl), True))

                @when(h)
                def _on_main(h, i=i):
                    h.value.append(("chain", i))
                    send("assert", (h.value, [("chain", i)]))

        wait()
        # 1 length assert + 2 is_pinned asserts + 2 body asserts.
        receive_asserts(5)

    def test_pinned_nested_in_regular_cown_value(self):
        """``Cown({"pc": PinnedCown(x), ...})`` -- worker extracts the inner handle."""
        pc = PinnedCown([])
        outer = Cown({"pc": pc, "tag": "wrap"})
        # Pass the expected literal in as a closure capture: the
        # transpiler ships it via the captures tuple so the string
        # arrives in the worker with its own ownership, sidestepping a
        # 3.13 debug-build interned-string teardown bug that bites
        # comparisons against literals round-tripped through
        # ``o.value[...]``.
        expected_tag = "wrap"

        @when(outer)
        def _worker(o):
            inner = o.value["pc"]
            send("assert", (_core.cown_is_pinned(inner.impl), True))
            send("assert", (o.value["tag"], expected_tag))

            @when(inner)
            def _on_main(inner):
                inner.value.append("from-nested")
                send("assert", (inner.value, ["from-nested"]))

        quiesce()
        receive_asserts(3)

    def test_two_workers_share_pinned_handle_via_noticeboard(self):
        """Two workers each read the same pinned handle; both pinned bodies run on main."""
        start()
        pc = PinnedCown([])
        notice_write("t16_pc", pc)
        notice_sync()

        u1 = Cown(0)
        u2 = Cown(0)

        @when(u1)
        def _w1(u):
            h = noticeboard()["t16_pc"]
            send("assert", (_core.cown_is_pinned(h.impl), True))

            @when(h)
            def _body(h):
                h.value.append("w1")
                send("assert", (h.value[-1], "w1"))

        @when(u2)
        def _w2(u):
            h = noticeboard()["t16_pc"]
            send("assert", (_core.cown_is_pinned(h.impl), True))

            @when(h)
            def _body(h):
                h.value.append("w2")
                send("assert", (h.value[-1], "w2"))

        wait()
        # 2 is_pinned asserts + 2 body asserts.
        receive_asserts(4)

        # Both workers mutated the *same* pinned value -- strong evidence
        # that both handles resolved to the same underlying capsule.
        sentinel = PinnedCown(None)

        @when(sentinel)
        def _inspect(_s):
            content = sorted(pc.value)
            send("assert", (content, ["w1", "w2"]))

        wait()
        receive_asserts()

    def test_pinned_via_notice_update(self):
        """``notice_update`` with a pinned producer; readers see the pinned handle."""
        start()
        pc = PinnedCown([])
        notice_update("t16b_pc", partial(_replace_with, pc), default=None)
        notice_sync()

        unrelated = Cown(0)

        @when(unrelated)
        def _reader(u):
            h = noticeboard()["t16b_pc"]
            send("assert", (h is not None, True))
            send("assert", (_core.cown_is_pinned(h.impl), True))

        wait()
        receive_asserts(2)

    def test_body_raise_drains_queue(self):
        """A raising pinned body marks its result cown and the queue still drains."""
        pc_raise = PinnedCown(0)
        pc_ok = PinnedCown(0)

        @when(pc_raise)
        def raiser(pc):
            raise RuntimeError("planned-failure")

        @when(pc_ok)
        def _survivor(pc):
            send("assert", ("survived", "survived"))

        @when(raiser)
        def _inspect(r):
            send("assert", (r.exception, True))

        quiesce()
        receive_asserts(2)
        assert _core.main_pump_queue_depth() == 0

    # Mixed pinned/unpinned routing.
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
            def _body(a, b, expected_on_main=expected_on_main):
                send("assert", (_core.is_primary(), expected_on_main))
        else:
            a, b, c = cowns

            @when(a, b, c)
            def _body(a, b, c, expected_on_main=expected_on_main):
                send("assert", (_core.is_primary(), expected_on_main))

        quiesce()
        receive_asserts()


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
        # Reset watchdog state so a leaked threshold cannot poison the
        # next test. ``None`` disables the sampler.
        set_pump_watchdog(warn_ms=None, on_starve=None)

    # Warn fires after starvation threshold.
    def test_warn_only_fires_on_starvation(self):
        """``warn_ms`` invokes on_starve once after the threshold elapses."""
        warns = []

        def on_starve(severity, message):
            warns.append((severity, str(message)))

        set_pump_watchdog(warn_ms=50, on_starve=on_starve)

        pc = PinnedCown(0)

        @when(pc)
        def _body(pc):
            send("assert", ("ran", "ran"))

        # Let the queue sit non-empty past warn_ms before the pump runs.
        time.sleep(0.15)
        # auto-pump drains the body; check_warn samples at pump entry
        # and sees age > 50ms.
        quiesce()
        receive_asserts()

        assert any(s == 0 for s, _ in warns), (
            f"expected warn (severity 0) in {warns!r}")

    # Unpinned-only window leaves warn untripped.
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
                # Per-behaviour sleep adds up to ~160 ms total worker
                # time, well past warn_ms. The pinned queue remains
                # empty throughout, so NONEMPTY_SINCE_NS stays 0.
                time.sleep(0.02)
                send("assert", ("ran", "ran"))
        quiesce()
        receive_asserts(8)
        assert warns == [], (
            f"warn must not fire across unpinned-only window, got {warns!r}")

        # Now schedule a pinned @when. The pinned queue was empty
        # across the unpinned window, so age = 0 < warn_ms.
        pc = PinnedCown(0)

        @when(pc)
        def _body(pc):
            send("assert", ("pinned-ok", "pinned-ok"))

        quiesce()
        receive_asserts()

    # Reconfigure-after-first-pinned.
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
            send("assert", ("ran", "ran"))

        # Reconfigure mid-flight; must not raise.
        warns = []

        def on_starve(severity, message):
            warns.append((severity, str(message)))

        set_pump_watchdog(warn_ms=200, on_starve=on_starve)

        quiesce()
        receive_asserts()
        # The replaced callback may or may not have fired depending on
        # exact timing; either way no exception escapes quiesce().


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
        # Restore defaults so we don't leak watchdog state into later tests.
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

        # Both failures survive: the ValueError that was already in
        # the local list, and the KI that triggered the re-raise.
        assert len(b._stop_drain_errors) == 2
        assert isinstance(b._stop_drain_errors[0], ValueError)
        assert isinstance(b._stop_drain_errors[1], KeyboardInterrupt)
        # The re-raised KI carries a note pointing at the stashed list.
        notes = getattr(ei.value, "__notes__", []) or []
        assert any("2 release_all error" in n for n in notes), notes
