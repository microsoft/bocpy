"""Regression tests for root zero-transition quiescence and safe shutdown."""

from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time
import uuid

import pytest

from bocpy import _core, Cown, PinnedCown, quiesce, start, wait, when
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


def test_quiescence_has_no_python_waiter_type():
    assert not hasattr(_core, "_QuiescenceWaiter")
    assert _core.terminator_quiescence(0) is True


@pytest.mark.parametrize(
    "scenario", ["initial-zero", "generation", "multiple", "pinned", "spurious", "retirement", "drain"],
)
def test_native_waiter_observation(scenario):
    internal = pytest.importorskip("bocpy._internal_test")
    assert internal.terminator_scenario(scenario) is None


def _run_program(program, module=False):
    directory = Path(__file__).resolve().parents[1] / ".copilot" / f"scope-test-{uuid.uuid4().hex}"
    directory.mkdir(parents=True)
    try:
        script = directory / "scope_probe.py"
        script.write_text(textwrap.dedent(program))
        args = ["-m", "scope_probe"] if module else [str(script)]
        result = subprocess.run(
            [sys.executable, *args], cwd=directory,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        return result.stdout
    finally:
        shutil.rmtree(directory)


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

        @when()
        def accepted():
            return "admission remained open"
    finally:
        _core.terminator_dec()
    quiesce(WAIT_TIMEOUT)
    assert accepted.unwrap() == "admission remained open"
    assert _core.terminator_count() == 0


def test_native_quiescence_observes_restarted_runtime():
    assert _core.terminator_quiescence(0) is True
    wait(WAIT_TIMEOUT)
    start(worker_count=4, module=(__name__, __file__))
    assert _core.terminator_count() == 0
    _core.terminator_inc()
    try:
        assert _core.terminator_quiescence(0) is False
    finally:
        _core.terminator_dec()

    @when()
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
    program = """
        from bocpy import _core, quiesce, receive, send, start, wait, when
        from bocpy import behaviors

        if __name__ == "__main__":
            start(worker_count=2)
            runtime = behaviors.BEHAVIORS

            @when()
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

                @when()
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

            @when()
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
