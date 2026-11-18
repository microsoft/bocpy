"""Regression tests for non-closing zero-transition quiescence."""

import threading
import time

import pytest

from bocpy import (
    _core, Cown, PinnedCown, pump, quiesce, receive, root_scope, Scope,
    send, start, wait, when,
)
from bocpy import behaviors as _behaviors


WAIT_TIMEOUT = 10


@pytest.mark.parametrize("entry", ["quiesce", "wait", "runtime_quiesce", "runtime_stop"])
@pytest.mark.parametrize("timeout", [None, 10, 10.0])
def test_public_timeout_reaches_native_normalized(monkeypatch, entry, timeout):
    runtime = _behaviors.BEHAVIORS
    native_quiesce = _core.terminator_quiescence
    native_drain = _core.terminator_drain
    observed = []

    def observe_quiesce(value, resolver):
        observed.append(value)
        return native_quiesce(value, resolver)

    def observe_drain(value, resolver):
        observed.append(value)
        return native_drain(value, resolver)

    monkeypatch.setattr(_core, "terminator_quiescence", observe_quiesce)
    monkeypatch.setattr(_core, "terminator_drain", observe_drain)
    operations = {
        "quiesce": quiesce, "wait": wait,
        "runtime_quiesce": runtime.quiesce, "runtime_stop": runtime.stop,
    }
    operations[entry](timeout)
    assert observed
    if timeout == 10.0:
        assert all(type(value) is float and 0 <= value <= timeout for value in observed)
    else:
        assert all(value is None for value in observed)


@pytest.mark.parametrize("entry", ["quiesce", "wait", "runtime_quiesce", "runtime_stop"])
@pytest.mark.parametrize("base", [object, float])
def test_timeout_conversion_precedes_deadline_arithmetic(entry, base):
    class Timeout(base):
        def __init__(self):
            self.conversions = 0

        def __float__(self):
            self.conversions += 1
            return 10.0

    timeout = Timeout()
    runtime = _behaviors.BEHAVIORS
    operations = {
        "quiesce": quiesce, "wait": wait,
        "runtime_quiesce": runtime.quiesce, "runtime_stop": runtime.stop,
    }
    operations[entry](timeout)
    assert timeout.conversions == 1


@pytest.mark.parametrize("entry", ["quiesce", "wait", "runtime_quiesce", "runtime_stop"])
@pytest.mark.parametrize("timeout", [-1.0, -0.01, float("inf"), float("-inf"), float("nan")])
def test_invalid_timeout_rejected_before_draining(monkeypatch, entry, timeout):
    runtime = _behaviors.BEHAVIORS

    def unexpected_drain(timeout):
        pytest.fail("invalid timeout reached drain before validation")

    monkeypatch.setattr(runtime, "_wait_for_quiescence", unexpected_drain)
    operations = {
        "quiesce": quiesce, "wait": wait,
        "runtime_quiesce": runtime.quiesce, "runtime_stop": runtime.stop,
    }
    with pytest.raises(ValueError, match="finite and nonnegative"):
        operations[entry](timeout)
    assert _behaviors.BEHAVIORS is runtime
    assert not runtime._workers_stopped


@pytest.mark.parametrize("timeout", [-1.0, float("inf"), float("-inf"), float("nan")])
def test_scope_quiescence_rejects_invalid_timeout_without_closing(timeout):
    with Scope(7) as scope:
        with pytest.raises(ValueError, match="finite and nonnegative"):
            scope.quiesce(timeout)
        assert scope._impl.state == "active"
        assert scope.quiesce(0) is None
    assert scope.output_value == 7


@pytest.mark.parametrize("entry", ["quiesce", "wait", "runtime_quiesce", "runtime_stop"])
def test_timeout_conversion_rechecks_scope_before_join(entry):
    scope = Scope()

    class Timeout:
        def __float__(self):
            scope.__enter__()
            return 0.0

    runtime = _behaviors.BEHAVIORS
    operations = {
        "quiesce": quiesce, "wait": wait,
        "runtime_quiesce": runtime.quiesce, "runtime_stop": runtime.stop,
    }
    try:
        with pytest.raises(RuntimeError, match="inside a Scope"):
            operations[entry](Timeout())
    finally:
        scope.__exit__(None, None, None)


@pytest.mark.parametrize("batches", [1, 3, 10])
def test_global_quiescence_allows_repeated_batches(batches):
    value = Cown(0)
    for index in range(batches):
        @when(value)
        def increment(value):
            value.value += 1
            return value.value

        assert quiesce(WAIT_TIMEOUT) is None
        assert increment.unwrap() == index + 1
        assert _core.terminator_count() == 0


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("operation", ["quiesce", "wait"])
def test_global_join_rejects_counted_caller(pinned, operation):
    value = PinnedCown(None) if pinned else Cown(None)

    @when(value)
    def attempted_join(value, operation=operation):
        getattr(_behaviors, operation)(0)

    quiesce(WAIT_TIMEOUT)
    with pytest.raises(RuntimeError, match="inside.*behavior"):
        attempted_join.unwrap()
    assert _core.terminator_count() == 0


@pytest.mark.parametrize(
    "scenario", ["initial-zero", "generation", "multiple", "pinned", "spurious", "retirement", "drain"],
)
def test_native_waiter_observation(scenario):
    internal = pytest.importorskip("bocpy._internal_test")
    assert internal.terminator_scenario(scenario) is None


@pytest.fixture(autouse=True)
def quiescence_runtime():
    wait(WAIT_TIMEOUT)
    start(worker_count=4, module=(__name__, __file__))
    yield
    wait(WAIT_TIMEOUT)


@pytest.mark.parametrize("operation", ["terminator_quiescence", "terminator_drain"])
def test_native_zero_deadline_does_not_pump_ready_work(operation):
    observed = []
    pinned = PinnedCown(observed)

    @when(pinned)
    def append(value):
        value.value.append("ran")

    assert getattr(_core, operation)(0, _behaviors.BEHAVIORS.export_module) is False
    assert observed == []
    quiesce(WAIT_TIMEOUT)
    assert observed == ["ran"]
    assert append.unwrap() is None


@pytest.mark.parametrize("operation", ["terminator_quiescence", "terminator_drain"])
def test_native_deadline_bounds_continuously_ready_pinned_batches(operation):
    observed = []
    pinned = PinnedCown(observed)
    for index in range(130):
        @when(pinned)
        def append(value, item=index):
            if item == 0:
                # One callback crosses the deadline; the next pump batch must not start.
                time.sleep(0.03)
            value.value.append(item)

    assert getattr(_core, operation)(0.01, _behaviors.BEHAVIORS.export_module) is False
    assert len(observed) <= 64
    quiesce(WAIT_TIMEOUT)
    assert observed == list(range(130))


def test_native_quiescence_rejects_counted_pinned_caller():
    pinned = PinnedCown(None)

    @when(pinned)
    def check(value):
        with pytest.raises(RuntimeError, match="behavior cannot wait for root completion"):
            _core.terminator_quiescence(0)
        return "rejected"

    quiesce(WAIT_TIMEOUT)
    assert check.unwrap() == "rejected"


@pytest.mark.parametrize("batches", [1, 3, 10])
def test_scope_quiescence_allows_repeated_batches(batches):
    snapshots = []
    with Scope([]) as scope:
        assert scope.quiesce(0) is None
        for index in range(batches):
            @when(scope.output_cown)
            def append(output, item=index):
                output.value.append(item)

            assert scope.quiesce(WAIT_TIMEOUT) is None
            assert scope._impl.state == "active"
            assert scope._impl.quiescence(0) is True
            with pytest.raises(RuntimeError, match="scope output is unavailable"):
                _ = scope.output_value

            @when(scope.output_cown)
            def snapshot(output):
                return tuple(output.value)

            scope.quiesce(WAIT_TIMEOUT)
            snapshots.append(snapshot)
            with pytest.raises(RuntimeError):
                snapshot.unwrap()
    assert scope.output_value == list(range(batches))
    for index, snapshot in enumerate(snapshots):
        assert snapshot.unwrap() == tuple(range(index + 1))
    with pytest.raises(RuntimeError):
        scope.quiesce(0)


def test_scope_quiescence_requires_active_entering_host():
    scope = Scope(7)
    with pytest.raises(RuntimeError):
        scope.quiesce(0)
    with scope:
        failures = []

        def other_host():
            try:
                scope.quiesce(0)
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=other_host)
        thread.start()
        thread.join(WAIT_TIMEOUT)
        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        with Scope():
            with pytest.raises(RuntimeError):
                scope.quiesce(0)
        assert scope.quiesce(0) is None
    assert scope.output_value == 7


def test_counted_behavior_cannot_wait_on_its_scope():
    with Scope() as scope:
        @when()
        def check(destination=scope):
            try:
                destination._impl.quiescence(0)
            except RuntimeError:
                return "rejected"
            raise AssertionError("counted behavior waited for its own completion")

        scope.quiesce(WAIT_TIMEOUT)
    assert check.unwrap() == "rejected"


@pytest.mark.parametrize("ancestor", [False, True])
def test_pinned_behavior_cannot_wait_on_self_or_ancestor_scope(ancestor):
    with Scope() as outer:
        retained = PinnedCown(outer)

        def schedule_check():
            @when(retained, scope=root_scope)
            def check(value):
                scope = value.value
                for operation in (scope.quiesce, scope._impl.quiescence):
                    try:
                        operation(0)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError("counted behavior waited on its enclosing scope")
                return "rejected"
            return check

        if ancestor:
            with Scope() as inner:
                result = schedule_check()
                inner.quiesce(WAIT_TIMEOUT)
        else:
            result = schedule_check()
        outer.quiesce(WAIT_TIMEOUT)
    quiesce(WAIT_TIMEOUT)
    assert result.unwrap() == "rejected"


def test_worker_child_scope_can_quiesce_repeated_batches():
    @when()
    def parent():
        with Scope([]) as child:
            for index in range(3):
                @when(child.output_cown)
                def append(output, item=index):
                    output.value.append(item)
                assert child.quiesce(WAIT_TIMEOUT) is None
                assert child._impl.state == "active"
        return child.output_value

    quiesce(WAIT_TIMEOUT)
    assert parent.unwrap() == [0, 1, 2]


def test_pinned_self_exit_rejection_leaves_scope_active_and_usable():
    with Scope([]) as scope:
        retained = PinnedCown(scope)

        @when(retained, scope=root_scope)
        def attempted_exit(value):
            with pytest.raises(RuntimeError):
                value.value.__exit__(None, None, None)
            assert value.value._impl.state == "active"
            return "rejected"

        pump()
        assert scope._impl.state == "active"

        @when(scope.output_cown)
        def append(output):
            output.value.append("still active")

        scope.quiesce(WAIT_TIMEOUT)
        with pytest.raises(RuntimeError, match="scope output is unavailable"):
            _ = scope.output_value
    assert attempted_exit.unwrap() == "rejected"
    assert scope.output_value == ["still active"]


def test_scope_quiescence_does_not_wait_for_unrelated_root_work():
    @when(scope=root_scope)
    def background():
        send("quiesce-root-ready", None)
        assert receive("quiesce-root-release", WAIT_TIMEOUT) == ("quiesce-root-release", None)
        return "root finished"

    try:
        assert receive("quiesce-root-ready", WAIT_TIMEOUT) == ("quiesce-root-ready", None)
        with Scope([]) as scope:
            @when(scope.output_cown)
            def local(output):
                output.value.append("local")

            assert scope.quiesce(WAIT_TIMEOUT) is None
            assert scope._impl.quiescence(0) is True
            with pytest.raises(RuntimeError):
                background.unwrap()
        assert scope.output_value == ["local"]
    finally:
        send("quiesce-root-release", None)
    quiesce(WAIT_TIMEOUT)
    assert background.unwrap() == "root finished"


def test_scope_quiescence_pumps_pinned_child_of_waiting_worker():
    pinned = PinnedCown(21)
    with Scope() as outer:
        @when(outer.output_cown)
        def parent(output, main_value=pinned):
            with Scope() as inner:
                @when(main_value, scope=root_scope)
                def child(value):
                    return value.value * 2

                @when(child, inner.output_cown)
                def store_result(result, output):
                    output.value = result.value

                assert inner.quiesce(WAIT_TIMEOUT) is None
                assert inner._impl.state == "active"
            output.value = inner.output_value

        assert outer.quiesce(WAIT_TIMEOUT) is None
    assert outer.output_value == 42
    quiesce(WAIT_TIMEOUT)
    assert parent.unwrap() is None


def test_scope_quiescence_timeout_can_retry_without_closing():
    with Scope([]) as scope:
        @when(scope.output_cown)
        def blocked(output):
            send("quiesce-ready", None)
            assert receive("quiesce-release", WAIT_TIMEOUT) == ("quiesce-release", None)
            output.value.append("released")

        try:
            assert receive("quiesce-ready", WAIT_TIMEOUT) == ("quiesce-ready", None)
            for _ in range(2):
                with pytest.raises(TimeoutError):
                    scope.quiesce(0)
                assert scope._impl.state == "active"
                with pytest.raises(RuntimeError):
                    _ = scope.output_value
        finally:
            send("quiesce-release", None)
        assert scope.quiesce(WAIT_TIMEOUT) is None

        @when(scope.output_cown)
        def later(output):
            output.value.append("new batch")

        scope.quiesce(WAIT_TIMEOUT)
    assert scope.output_value == ["released", "new batch"]


def test_native_scoped_wait_rejects_nested_ancestor_host():
    with Scope() as outer:
        with Scope():
            with pytest.raises(RuntimeError):
                outer._impl.quiescence(0)
        assert outer._impl.quiescence(0) is True


def test_native_scoped_wait_rejects_other_thread():
    with Scope() as scope:
        failures = []

        def other_host():
            try:
                scope._impl.quiescence(0)
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=other_host)
        thread.start()
        thread.join(WAIT_TIMEOUT)
        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        assert scope._impl.quiescence(0) is True


def test_scope_quiescence_rejects_draining_and_closed_states():
    scope = Scope(17)
    scope._impl.enter()
    try:
        scope._impl.close()
        for operation in (scope.quiesce, scope._impl.quiescence):
            with pytest.raises(RuntimeError):
                operation()
        assert scope._impl.wait(WAIT_TIMEOUT)
    finally:
        assert scope._impl.finish(True) == 17
    with pytest.raises(RuntimeError):
        scope._impl.quiescence(0)


def test_native_scope_quiescence_observes_each_new_batch():
    with Scope([]) as scope:
        assert scope._impl.quiescence(0) is True
        for index in range(2):
            @when(scope.output_cown)
            def blocked(output, item=index):
                send("quiesce-batch-ready", item)
                assert receive("quiesce-batch-release", WAIT_TIMEOUT) == ("quiesce-batch-release", item)
                output.value.append(item)

            try:
                assert receive("quiesce-batch-ready", WAIT_TIMEOUT) == ("quiesce-batch-ready", index)
                assert scope._impl.quiescence(0) is False
                assert scope._impl.quiescence(0) is False
            finally:
                send("quiesce-batch-release", index)
            scope.quiesce(WAIT_TIMEOUT)
            assert scope._impl.quiescence(0) is True
    assert scope.output_value == [0, 1]


@pytest.mark.parametrize("destination", ["inner", "outer", "root"])
def test_root_count_includes_nested_hosts_and_counts_actual_work(destination):
    with Scope() as outer:
        assert _core.terminator_count() == 1
        with Scope() as inner:
            assert _core.terminator_count() == 2
            target = {"inner": inner, "outer": outer, "root": root_scope}[destination]

            @when(scope=target)
            def blocked():
                send("quiesce-count-ready", None)
                assert receive("quiesce-count-release", WAIT_TIMEOUT) == ("quiesce-count-release", None)

            try:
                assert receive("quiesce-count-ready", WAIT_TIMEOUT) == ("quiesce-count-ready", None)
                assert _core.terminator_count() == 3
            finally:
                send("quiesce-count-release", None)
            if destination == "inner":
                inner.quiesce(WAIT_TIMEOUT)
                assert inner._impl.quiescence(0) is True
        outer.quiesce(WAIT_TIMEOUT)
        assert outer._impl.quiescence(0) is True
        assert outer.quiesce(0) is None
    quiesce(WAIT_TIMEOUT)
    assert _core.terminator_count() == 0
    assert blocked.unwrap() is None


def test_root_quiescence_observes_zero_only_after_scope_finalization():
    with Scope() as scope:
        for _ in range(3):
            @when()
            def completed():
                return "batch"

            scope.quiesce(WAIT_TIMEOUT)
            assert _core.terminator_quiescence(0) is False
        with Scope() as inner:
            inner.quiesce(0)
            assert _core.terminator_quiescence(0) is False
        assert _core.terminator_quiescence(0) is False
    assert _core.terminator_quiescence(WAIT_TIMEOUT) is True
    assert completed.unwrap() == "batch"


def test_native_root_close_rejects_work_without_closing_admission():
    assert _core.terminator_count() == 0
    _core.terminator_inc()
    try:
        with pytest.raises(TimeoutError):
            _core.terminator_close()
        assert _core.terminator_count() == 1
        _core.terminator_inc()
        assert _core.terminator_count() == 2
        _core.terminator_dec()

        @when(scope=root_scope)
        def accepted():
            return "admission remained open"
    finally:
        _core.terminator_dec()
    quiesce(WAIT_TIMEOUT)
    assert accepted.unwrap() == "admission remained open"
    assert _core.terminator_count() == 0


def test_root_handle_routes_to_restarted_runtime():
    retained_root = root_scope
    assert _core.terminator_quiescence(0) is True
    wait(WAIT_TIMEOUT)
    start(worker_count=4, module=(__name__, __file__))
    assert root_scope is retained_root
    assert _core.terminator_count() == 0
    _core.terminator_inc()
    try:
        assert _core.terminator_quiescence(0) is False
    finally:
        _core.terminator_dec()

    @when(scope=retained_root)
    def routed():
        return "new root"

    quiesce(WAIT_TIMEOUT)
    assert routed.unwrap() == "new root"


@pytest.mark.parametrize(
    "shutdown",
    [
        "wait(0)",
        "wait(0, stats=True)",
        "wait(0, noticeboard=True)",
        "wait(0, stats=True, noticeboard=True)",
        "runtime.stop(timeout=0)",
    ],
    ids=["wait", "wait-stats", "wait-noticeboard", "wait-both", "stop"],
)
def test_timed_shutdown_preserves_root_work_and_admission(shutdown):
    from test_scope import _run_program

    program = """
        from bocpy import _core, quiesce, receive, root_scope, send, start, wait, when
        from bocpy import behaviors

        if __name__ == "__main__":
            start(worker_count=2)
            runtime = behaviors.BEHAVIORS
            retained_root = root_scope

            @when(scope=retained_root)
            def blocked():
                send("root-ready", None)
                assert receive("root-release", 10) == ("root-release", None)
                return "released"

            try:
                assert receive("root-ready", 5) == ("root-ready", None)
                assert _core.terminator_count() == 1
                try:
                    SHUTDOWN_CALL
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("shutdown accepted outstanding root work")
                assert behaviors.BEHAVIORS is runtime
                assert not runtime._workers_stopped
                assert not runtime._teardown_complete
                assert runtime.noticeboard.is_alive()
                assert _core.terminator_count() == 1
                start(worker_count=2)
                assert behaviors.BEHAVIORS is runtime

                @when(scope=retained_root)
                def admitted():
                    send("root-admitted", None)
                    return "admitted after timeout"

                assert receive("root-admitted", 5) == ("root-admitted", None)
            finally:
                send("root-release", None)
            quiesce(10)
            assert blocked.unwrap() == "released"
            assert admitted.unwrap() == "admitted after timeout"
            assert _core.terminator_count() == 0
            RETRY_CALL
            assert runtime._workers_stopped
            assert runtime._teardown_complete
            wait(10)
            start(worker_count=2)
            assert behaviors.BEHAVIORS is not runtime
            assert root_scope is retained_root

            @when(scope=retained_root)
            def restarted():
                return "restarted"

            quiesce(10)
            assert restarted.unwrap() == "restarted"
            wait(10)
            print("timed shutdown preserved root work and admission")
    """
    retry = shutdown.replace("(0", "(10").replace("timeout=0", "timeout=10")
    output = _run_program(program.replace("SHUTDOWN_CALL", shutdown).replace("RETRY_CALL", retry))
    assert "timed shutdown preserved root work and admission" in output


@pytest.mark.parametrize("timeout", [None, 0, -0.0, 0.1, 2, 1e100])
def test_timeout_normalizer_accepts_only_none_or_bounded_seconds(timeout):
    normalized = _behaviors._normalize_timeout(timeout)
    if timeout is None:
        assert normalized is None
    else:
        assert type(normalized) is float
        assert normalized == timeout


@pytest.mark.parametrize("entry", ["quiesce", "wait"])
@pytest.mark.parametrize("state", ["absent", "stopped"])
@pytest.mark.parametrize("timeout", [-1.0, float("inf"), float("-inf"), float("nan")])
def test_invalid_timeout_rejected_when_runtime_is_not_running(entry, state, timeout):
    if state == "absent":
        wait(WAIT_TIMEOUT)
    else:
        _behaviors.BEHAVIORS.stop(WAIT_TIMEOUT)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        getattr(_behaviors, entry)(timeout)
