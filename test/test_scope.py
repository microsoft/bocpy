"""Regression tests for locally joined behavior scopes."""

from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import threading
import uuid

import pytest

from bocpy import (
    _core, Cown, Matrix, notice_delete, PinnedCown, pump, quiesce, receive,
    root_scope, Scope, send, start, wait, when, whencall,
)
from bocpy import behaviors as _behaviors


WAIT_TIMEOUT = 10
_FINISH_CALLBACK = None


def _assign(output, value):
    output.value = value


def _restore_scope_output():
    return _FINISH_CALLBACK()


class _ReentrantScopeOutput:
    """Materialize output through a host-side reentrancy probe."""

    def __reduce__(self):
        return (_restore_scope_output, ())


class _UnpicklableAfterScope:
    """Fail after the pickler has already retained a scope identity."""

    def __reduce__(self):
        raise ValueError("scope serialization failure")


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
def scope_runtime():
    wait(WAIT_TIMEOUT)
    start(worker_count=4, module=(__name__, __file__))
    yield
    wait(WAIT_TIMEOUT)


@pytest.mark.parametrize("initial", [None, 17, [], [1, 2]])
def test_empty_scope_transfers_initial(initial):
    with Scope(initial) as scope:
        assert isinstance(scope.output_cown, Cown)
        with pytest.raises(RuntimeError):
            _ = scope.output_value
    assert scope.output_value == initial
    assert scope.output_value is scope.output_value


def test_scope_output_is_read_only():
    with Scope(23) as scope:
        assert isinstance(scope.output_cown, Cown)
        with pytest.raises(RuntimeError, match="scope output is unavailable"):
            _ = scope.output_value
    assert scope.output_value == 23
    with pytest.raises(AttributeError):
        scope.output_value = 24
    with pytest.raises(AttributeError):
        scope.output_cown = Cown(24)


@pytest.mark.parametrize("value", [None, 42, [1, 2, 3]])
def test_scope_transfers_assigned_payload(value):
    with Scope("initial") as scope:
        whencall(_assign, [scope.output_cown], [value], scope=scope)
    assert scope.output_value == value
    with pytest.raises(AttributeError):
        scope.output_value = "replacement"


def test_scope_transfers_matrix_storage():
    with Scope(Matrix(1, 2, [3.0, 4.0])) as scope:
        @when(scope.output_cown)
        def update(output):
            output.value[0, 1] = 8.0

    result = scope.output_value
    assert result[0, 0] == 3.0
    assert result[0, 1] == 8.0
    result[0, 0] = 9.0
    assert scope.output_value[0, 0] == 9.0
    with pytest.raises(RuntimeError):
        scope.output_cown.unwrap()


def test_scope_join_does_not_join_root_background():
    with Scope([]) as scope:
        @when(scope=root_scope)
        def background():
            send("scope-ready", None)
            return receive("scope-release", WAIT_TIMEOUT)

        assert receive("scope-ready", WAIT_TIMEOUT) == ("scope-ready", None)

        @when(scope.output_cown)
        def local(output):
            output.value.append("local")

    try:
        assert scope.output_value == ["local"]
        with pytest.raises(RuntimeError):
            background.unwrap()
    finally:
        send("scope-release", "released after local return")
    quiesce(WAIT_TIMEOUT)
    assert background.unwrap() == ("scope-release", "released after local return")


def test_nested_host_scopes_allow_descendant_output_access():
    with Scope([]) as outer:
        with Scope(0) as inner:
            @when(outer.output_cown, inner.output_cown)
            def child(parent_output, child_output):
                parent_output.value.append("child")
                child_output.value = 7

        assert inner.output_value == 7

        @when(outer.output_cown)
        def restored(output):
            output.value.append("parent")

    assert outer.output_value == ["child", "parent"]


def test_worker_scope_and_inherited_nested_behaviors():
    with Scope([]) as outer:
        @when(outer.output_cown)
        def parent(output):
            with Scope(0) as inner:
                @when(inner.output_cown)
                def child(value):
                    value.value = 21

            output.value.append(inner.output_value)

            @when(output)
            def descendant(value):
                value.value.append(42)

    assert outer.output_value == [21, 42]
    quiesce(WAIT_TIMEOUT)
    assert parent.unwrap() is None


def test_explicit_scope_capture_routes_from_root_worker():
    with Scope([]) as outer:
        @when(scope=root_scope)
        def route(destination=outer, output=outer.output_cown):
            @when(output, scope=destination)
            def child(value):
                value.value.append("routed")
            send("scope-ready", "scheduled")

        assert receive("scope-ready", WAIT_TIMEOUT) == ("scope-ready", "scheduled")
    assert outer.output_value == ["routed"]
    quiesce(WAIT_TIMEOUT)
    assert route.unwrap() is None


def test_whencall_scope_capture_and_inheritance():
    with Scope(0) as scope:
        @when(scope=root_scope)
        def route(destination=scope, output=scope.output_cown):
            whencall(_assign, [output], [99], scope=destination)
            send("scope-ready", "scheduled")

        assert receive("scope-ready", WAIT_TIMEOUT) == ("scope-ready", "scheduled")
    assert scope.output_value == 99
    quiesce(WAIT_TIMEOUT)
    assert route.unwrap() is None


def test_behavior_failure_preserves_partial_output_and_releases_cowns():
    data = Cown([])
    with Scope([]) as scope:
        @when(data, scope.output_cown)
        def failed(value, output):
            value.value.append(1)
            output.value.append("partial")
            raise ValueError("scope behavior failed")

        @when(data, scope.output_cown)
        def following(value, output):
            value.value.append(2)
            output.value.append("continued")
            return tuple(value.value)

    assert scope.output_value == ["partial", "continued"]
    quiesce(WAIT_TIMEOUT)
    with pytest.raises(ValueError, match="scope behavior failed"):
        failed.unwrap()
    assert following.unwrap() == (1, 2)

    @when(data)
    def reused(value):
        return tuple(value.value)

    quiesce(WAIT_TIMEOUT)
    assert reused.unwrap() == (1, 2)


def test_host_exception_joins_work_and_restores_outer_scope():
    error = ValueError("host failed")
    with Scope([]) as outer:
        inner = Scope([])
        with pytest.raises(ValueError) as caught:
            with inner:
                @when(outer.output_cown, inner.output_cown)
                def child(parent_output, child_output):
                    parent_output.value.append("joined")
                    child_output.value.append("not published")
                raise error
        assert caught.value is error
        with pytest.raises(ValueError, match="host failed"):
            _ = inner.output_value
        with pytest.raises(RuntimeError):
            inner.output_cown.unwrap()

        @when(outer.output_cown)
        def restored(output):
            output.value.append("restored")

    assert outer.output_value == ["joined", "restored"]


def test_scope_is_single_use_and_created_result_is_unavailable():
    scope = Scope()
    with pytest.raises(RuntimeError):
        _ = scope.output_value
    with scope:
        with pytest.raises(RuntimeError):
            scope.__enter__()
    with pytest.raises(RuntimeError):
        scope.__enter__()

    failed = Scope()
    with pytest.raises(LookupError):
        with failed:
            raise LookupError("failed body")
    with pytest.raises(RuntimeError):
        failed.__enter__()


def _assert_output_access_rejected(output):
    alias = Cown(output.impl)
    for handle in (output, alias, output.impl):
        with pytest.raises(RuntimeError):
            handle.acquire()
        with pytest.raises(RuntimeError):
            _ = handle.value
        with pytest.raises(RuntimeError):
            handle.value = "bypass"
        with pytest.raises(RuntimeError):
            handle.unwrap()
    with pytest.raises(RuntimeError):
        output.impl.get()
    with pytest.raises(RuntimeError):
        output.impl.set("bypass")
    with pytest.raises(RuntimeError):
        with alias:
            pass


def test_output_access_is_restricted_in_all_scope_states():
    scope = Scope([])
    _assert_output_access_rejected(scope.output_cown)
    with pytest.raises(RuntimeError):
        whencall(_assign, [scope.output_cown], [1])
    with scope:
        _assert_output_access_rejected(scope.output_cown)
        whencall(_assign, [scope.output_cown], [[7]])
    assert scope.output_value == [7]
    _assert_output_access_rejected(scope.output_cown)
    with pytest.raises(RuntimeError):
        whencall(_assign, [scope.output_cown], [1])


def test_captured_output_alias_cannot_bypass_scheduled_access():
    with Scope([]) as scope:
        @when(scope.output_cown)
        def check(output, alias=scope.output_cown):
            for operation in ("acquire", "unwrap", "get"):
                try:
                    getattr(alias.impl, operation)()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError(f"captured alias allowed {operation}")
            output.value.append("borrowed access works")

    assert scope.output_value == ["borrowed access works"]
    quiesce(WAIT_TIMEOUT)
    assert check.unwrap() is None


def test_output_rejects_root_destination_without_leaking_count():
    with Scope(0) as scope:
        count = _core.terminator_count()
        for args in ([scope.output_cown], [[scope.output_cown]]):
            with pytest.raises(RuntimeError):
                whencall(_assign, args, [1], scope=root_scope)
            assert _core.terminator_count() == count
        whencall(_assign, [scope.output_cown], [2])
    assert scope.output_value == 2


def test_child_output_rejects_ancestor_destination():
    with Scope() as outer:
        with Scope() as inner:
            count = _core.terminator_count()
            with pytest.raises(RuntimeError):
                whencall(_assign, [inner.output_cown], [1], scope=outer)
            assert _core.terminator_count() == count
            whencall(_assign, [inner.output_cown], [3])
        assert inner.output_value == 3


@pytest.mark.parametrize("destination", [object(), 1, "scope"])
def test_invalid_scope_rolls_back_count(destination):
    count = _core.terminator_count()
    with pytest.raises(TypeError):
        whencall(_assign, [Cown(0)], [1], scope=destination)
    assert _core.terminator_count() == count


def test_unentered_and_completed_scope_reject_scheduling():
    scope = Scope()
    count = _core.terminator_count()
    with pytest.raises(RuntimeError):
        whencall(_assign, [Cown(0)], [1], scope=scope)
    assert _core.terminator_count() == count
    with scope:
        pass
    with pytest.raises(RuntimeError):
        whencall(_assign, [Cown(0)], [1], scope=scope)
    assert _core.terminator_count() == count


@pytest.mark.parametrize("operation", ["wait", "quiesce", "stop"])
def test_global_lifecycle_rejected_inside_host_scope(operation):
    with Scope(0) as scope:
        function = _behaviors.BEHAVIORS.stop if operation == "stop" else getattr(_behaviors, operation)
        with pytest.raises(RuntimeError):
            function(timeout=0.01)
        whencall(_assign, [scope.output_cown], [7])
    assert scope.output_value == 7


@pytest.mark.parametrize("operation", ["wait", "quiesce"])
def test_global_lifecycle_rejected_inside_worker_behavior(operation):
    @when()
    def check(name=operation):
        getattr(_behaviors, name)(timeout=0.01)

    quiesce(WAIT_TIMEOUT)
    with pytest.raises(RuntimeError):
        check.unwrap()


def test_pinned_behavior_uses_root_and_can_schedule_scoped_descendants():
    pinned = PinnedCown([])
    root_value = Cown(0)
    with Scope([]) as scope:
        @when(pinned, scope=root_scope)
        def on_main(value, scope=scope, root_value=root_value):
            value.value.append("main")

            @when(root_value)
            def inherited(value):
                value.value += 1

            @when(scope.output_cown, scope=scope)
            def explicit(output):
                output.value.append("local descendant")

            @when(root_value, scope=root_scope)
            def root_owned(value):
                value.value += 41
                return value.value

            return inherited, explicit, root_owned

        @when(on_main, scope.output_cown, scope=scope)
        def observe_pinned(output_cowns, output):
            output.value.append("pinned completed")

    assert scope.output_value == ["pinned completed", "local descendant"]
    quiesce(WAIT_TIMEOUT)
    assert pinned.unwrap() == ["main"]
    inherited, explicit, root_owned = on_main.unwrap()
    assert inherited.unwrap() is None
    assert explicit.unwrap() is None
    assert root_owned.unwrap() == 42


def test_pinned_behavior_rejects_inherited_and_explicit_local_scope():
    pinned = PinnedCown(None)
    with Scope() as scope:
        count = _core.terminator_count()
        with pytest.raises(RuntimeError, match="must be scheduled with root_scope"):
            @when(pinned)
            def inherited(_value):
                raise AssertionError("inherited local pinned work ran")

        with pytest.raises(RuntimeError, match="must be scheduled with root_scope"):
            @when(pinned, scope=scope)
            def explicit(_value):
                raise AssertionError("explicit local pinned work ran")

        assert _core.terminator_count() == count

        @when(pinned, scope=root_scope)
        def allowed(_value):
            return "root"

    quiesce(WAIT_TIMEOUT)
    assert allowed.unwrap() == "root"


def test_pinned_behavior_cannot_enter_fresh_scope_when_outer_wait_pumps():
    scope = Scope([])
    pinned = PinnedCown(None)
    count = _core.terminator_count()

    @when(pinned, scope=root_scope)
    def attempt_entry(_value, destination=scope):
        before = _core.terminator_count()
        local = Scope([])
        body_ran = False
        messages = []
        try:
            destination._impl.enter()
        except RuntimeError as error:
            messages.append(str(error))
        else:
            raise AssertionError("captured native scope entry was accepted")
        try:
            with local:
                body_ran = True
        except RuntimeError as error:
            messages.append(str(error))
        else:
            raise AssertionError("pinned behavior scope body ran")
        return (
            destination._impl.state, local._impl.state, _core.terminator_count() == before,
            body_ran, messages,
        )

    quiesce(WAIT_TIMEOUT)
    assert scope._impl.state == "created"
    assert _core.terminator_count() == count
    destination_state, local_state, reservation_unchanged, body_ran, messages = attempt_entry.unwrap()
    assert destination_state == local_state == "created"
    assert reservation_unchanged
    assert not body_ran
    assert len(messages) == 2
    assert all("pinned behaviors cannot enter" in message for message in messages)

    with scope:
        @when(scope.output_cown)
        def finish(output):
            output.value.append("legitimate host")

    assert scope.output_value == ["legitimate host"]


def test_direct_native_scope_entry_and_joins_reject_pinned_execution():
    fresh = Scope()
    with Scope([]) as active:
        handles = PinnedCown((fresh, active))

        @when(handles, scope=root_scope)
        def attempt_native_operations(value):
            fresh, active = value.value
            state = active._impl.state
            count = _core.terminator_count()
            operations = (
                (fresh._impl.enter, ()),
                (active._impl.quiescence, (0,)),
                (active._impl.close, ()),
                (active._impl.wait, (0,)),
                (active._impl.finish, (True,)),
                (active._impl.exit, (True, None)),
            )
            for operation, args in operations:
                try:
                    operation(*args)
                except RuntimeError as error:
                    if "pinned behaviors cannot enter or synchronously join" not in str(error):
                        raise AssertionError(str(error))
                else:
                    raise AssertionError(f"{operation} bypassed pinned execution guard")
            if (fresh._impl.state != "created" or
                    active._impl.state != state or state != "active" or
                    _core.terminator_count() != count):
                raise AssertionError("rejected operation changed scope state or accounting")
            return "rejected"

        pump()

    assert attempt_native_operations.unwrap() == "rejected"
    with fresh:
        pass
    assert fresh.output_value is None


def test_worker_nested_scope_progresses_when_main_pumps():
    pinned = PinnedCown(5)
    with Scope() as outer:
        @when(outer.output_cown)
        def on_worker(output, main_value=pinned):
            with Scope() as inner:
                @when(main_value, scope=root_scope)
                def on_main(value):
                    return value.value * 2

                @when(on_main, inner.output_cown)
                def store_result(result, output):
                    output.value = result.value
            output.value = inner.output_value

    assert outer.output_value == 10
    quiesce(WAIT_TIMEOUT)
    assert on_worker.unwrap() is None


def test_host_threads_have_independent_scope_contexts():
    outcomes = []

    def other_host():
        try:
            with Scope([]) as other:
                send("scope-ready", other.output_cown)
                assert receive("scope-release", WAIT_TIMEOUT) == ("scope-release", None)
                whencall(_assign, [other.output_cown], [["other"]])
            outcomes.append(other.output_value)
        except Exception as error:
            outcomes.append(error)

    thread = threading.Thread(target=other_host)
    thread.start()
    try:
        tag, other_output = receive("scope-ready", WAIT_TIMEOUT)
        assert tag == "scope-ready"
        with Scope([]) as local:
            with pytest.raises(RuntimeError):
                whencall(_assign, [other_output], [1])
            whencall(_assign, [local.output_cown], [["local"]])
        assert local.output_value == ["local"]
    finally:
        send("scope-release", None)
        thread.join(WAIT_TIMEOUT)
    assert not thread.is_alive()
    assert outcomes == [["other"]]


def test_global_quiescence_includes_host_scope_without_behaviors():
    outcomes = []

    def other_host():
        try:
            with Scope():
                send("scope-ready", None)
                outcomes.append(receive("scope-release", WAIT_TIMEOUT))
        except Exception as error:
            outcomes.append(error)

    thread = threading.Thread(target=other_host)
    thread.start()
    try:
        assert receive("scope-ready", WAIT_TIMEOUT) == ("scope-ready", None)
        assert _core.terminator_count() == 1
        with pytest.raises(TimeoutError):
            quiesce(0)
        assert thread.is_alive()
    finally:
        send("scope-release", None)
        thread.join(WAIT_TIMEOUT)
    assert not thread.is_alive()
    assert outcomes == [("scope-release", None)]
    quiesce(WAIT_TIMEOUT)


def test_retained_handles_do_not_block_runtime_restart():
    retained_root = root_scope
    with Scope([1]) as completed:
        pass
    wait(WAIT_TIMEOUT)
    start(worker_count=4, module=(__name__, __file__))
    assert retained_root is root_scope
    assert not hasattr(root_scope, "output_cown")
    with Scope() as fresh:
        whencall(_assign, [fresh.output_cown], [2])
        root_result = whencall(_assign, [Cown(0)], [3], scope=retained_root)
    assert fresh.output_value == 2
    assert completed.output_value == [1]
    quiesce(WAIT_TIMEOUT)
    assert root_result.unwrap() is None
    with pytest.raises(RuntimeError):
        whencall(_assign, [Cown(0)], [4], scope=completed)


def test_native_scope_simultaneous_enter_has_one_host():
    for _ in range(12):
        scope = Scope(7)
        entering = threading.Barrier(3, timeout=WAIT_TIMEOUT)
        entered = threading.Barrier(3, timeout=WAIT_TIMEOUT)
        outcomes = []
        failures = []
        count = _core.terminator_count()

        def contender(scope, entering, entered, outcomes, failures):
            won = False
            try:
                entering.wait()
                try:
                    scope._impl.enter()
                except RuntimeError:
                    outcomes.append("rejected")
                else:
                    won = True
                    outcomes.append("entered")
                    assert scope._impl.state == "active"
                entered.wait()
            except Exception as error:
                failures.append(error)
            finally:
                if won:
                    try:
                        scope._impl.close()
                        assert scope._impl.wait(WAIT_TIMEOUT)
                        assert scope._impl.finish(True) == 7
                    except Exception as error:
                        failures.append(error)

        threads = [
            threading.Thread(target=contender, args=(scope, entering, entered, outcomes, failures))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        try:
            entering.wait()
            entered.wait()
        finally:
            for thread in threads:
                thread.join(WAIT_TIMEOUT)
        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert sorted(outcomes) == ["entered", "rejected"]
        assert _core.terminator_count() == count


def test_scope_lifecycle_drops_host_hold_once():
    scope = Scope(7)
    impl = scope._impl
    count = _core.terminator_count()
    assert impl.state == "created"
    with pytest.raises(AttributeError):
        impl.state = "active"
    for operation, args in (("close", ()), ("wait", (0.0,)), ("finish", (True,))):
        with pytest.raises(RuntimeError):
            getattr(impl, operation)(*args)

    impl.enter()
    assert impl.state == "active"
    assert _core.terminator_count() == count + 1
    try:
        with pytest.raises(RuntimeError):
            impl.enter()
        with pytest.raises(RuntimeError, match="begin exit"):
            impl.wait(0)
        with pytest.raises(RuntimeError, match="outstanding work"):
            impl.finish(True)
        impl.close()
        assert impl.state == "draining"
        impl.close()
        assert impl.wait(WAIT_TIMEOUT)
        assert _core.terminator_count() == count + 1
        with pytest.raises(RuntimeError, match="completed"):
            whencall(_assign, [scope.output_cown], [19], scope=scope)
    finally:
        assert impl.finish(True) == 7
    assert impl.state == "closed"
    assert _core.terminator_count() == count
    for operation, args in (("enter", ()), ("close", ()), ("wait", (0.0,)), ("finish", (True,))):
        with pytest.raises(RuntimeError):
            getattr(impl, operation)(*args)


def test_draining_scope_keeps_output_available_to_behaviors():
    scope = Scope([])
    impl = scope._impl
    impl.enter()
    finished = False
    try:
        @when(scope.output_cown)
        def parent(output):
            assert receive("state-draining", WAIT_TIMEOUT) == ("state-draining", None)
            output.value.append("parent")

            @when(output)
            def child(value):
                value.value.append("child")

        impl.close()
        impl.close()
        assert impl.state == "draining"
        assert not impl.wait(0)
        with pytest.raises(RuntimeError, match="outstanding work"):
            impl.finish(True)
        assert impl.state == "draining"
        send("state-draining", None)
        assert impl.wait(WAIT_TIMEOUT)
        assert impl.finish(True) == ["parent", "child"]
        finished = True
    finally:
        if not finished:
            send("state-draining", None)
            impl.close()
            assert impl.wait(WAIT_TIMEOUT)
            impl.finish(False)
    assert impl.state == "closed"


def test_empty_scope_close_allows_work_until_draining_wait_fences_zero():
    scope = Scope(7)
    impl = scope._impl
    impl.enter()
    try:
        impl.close()
        assert impl.state == "draining"
        whencall(_assign, [scope.output_cown], [19], scope=scope)
        assert impl.wait(WAIT_TIMEOUT)
        with pytest.raises(RuntimeError, match="completed"):
            whencall(_assign, [scope.output_cown], [23], scope=scope)
    finally:
        assert impl.wait(WAIT_TIMEOUT)
        assert impl.finish(True) == 19


def test_scope_failed_entry_can_retry_without_consuming_lifecycle():
    scope = Scope(11)
    wait(WAIT_TIMEOUT)
    with pytest.raises(RuntimeError, match="shutting down"):
        scope._impl.enter()
    assert scope._impl.state == "created"
    assert _core.terminator_count() == 0
    start(worker_count=4, module=(__name__, __file__))
    with scope:
        assert scope._impl.state == "active"
    assert scope._impl.state == "closed"
    assert scope.output_value == 11


@pytest.mark.parametrize("operation", ["close", "wait", "finish"])
def test_native_scope_rejects_wrong_thread_host_operations(operation):
    scope = Scope(13)
    scope._impl.enter()
    scope._impl.close()
    assert scope._impl.wait(WAIT_TIMEOUT)
    count = _core.terminator_count()
    outcomes = []

    def other_host():
        args = {"close": (), "wait": (0.0,), "finish": (True,)}
        try:
            getattr(scope._impl, operation)(*args[operation])
        except Exception as error:
            outcomes.append(error)
        else:
            outcomes.append("incorrectly accepted")

    thread = threading.Thread(target=other_host)
    try:
        thread.start()
        thread.join(WAIT_TIMEOUT)
        assert not thread.is_alive()
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], RuntimeError)
        assert "entering thread" in str(outcomes[0])
        assert _core.terminator_count() == count
    finally:
        assert scope._impl.finish(True) == 13


def test_native_scope_finish_rejects_deserializer_reentry(monkeypatch):
    scope = Scope(_ReentrantScopeOutput())
    rejections = []
    count = _core.terminator_count()

    def reconstruct():
        assert scope._impl.state == "extracting"
        assert _core.terminator_count() == count + 1
        monkeypatch.setattr(sys.modules[__name__], "_FINISH_CALLBACK", lambda: 29)
        for operation, args in (("close", ()), ("wait", (0.0,)), ("finish", (True,))):
            with pytest.raises(RuntimeError, match="entering thread"):
                getattr(scope._impl, operation)(*args)
            rejections.append(operation)
        return 29

    monkeypatch.setattr(sys.modules[__name__], "_FINISH_CALLBACK", reconstruct)
    scope._impl.enter()
    scope._impl.close()
    assert scope._impl.wait(WAIT_TIMEOUT)
    assert scope._impl.finish(True) == 29
    assert scope._impl.state == "closed"
    assert rejections == ["close", "wait", "finish"]
    assert _core.terminator_count() == count


def test_scope_extraction_failure_closes_lifecycle(monkeypatch):
    scope = Scope(_ReentrantScopeOutput())
    error = ValueError("output reconstruction failed")

    def reconstruct():
        assert scope._impl.state == "extracting"
        assert _core.terminator_count() == count + 1
        with pytest.raises(RuntimeError):
            scope.__exit__(None, None, None)
        assert _core.terminator_count() == count + 1
        raise error

    monkeypatch.setattr(sys.modules[__name__], "_FINISH_CALLBACK", reconstruct)
    count = _core.terminator_count()
    with pytest.raises(ValueError, match="output reconstruction failed"):
        with scope:
            pass
    assert scope._impl.state == "closed"
    assert not _core.scope_active()
    assert _core.terminator_count() == count
    with pytest.raises(ValueError):
        _ = scope.output_value
    with pytest.raises(RuntimeError):
        scope.__enter__()


@pytest.mark.parametrize("converted", [0.0, "not a float"])
def test_native_scope_wait_rechecks_host_after_timeout_finishes_scope(converted):
    scope = Scope(31)
    count = _core.terminator_count()
    scope._impl.enter()
    scope._impl.close()
    results = []

    class FinishingTimeout:
        """Finish the waiting scope while converting its timeout."""

        def __float__(self):
            results.append(scope._impl.finish(True))
            return converted

    expected = RuntimeError if isinstance(converted, float) else TypeError
    with pytest.raises(expected):
        scope._impl.wait(FinishingTimeout())
    assert results == [31]
    assert _core.terminator_count() == count
    with pytest.raises(RuntimeError):
        scope._impl.finish(True)


def test_native_scope_wait_rechecks_nesting_after_timeout_conversion():
    outer = Scope(37)
    inner = Scope(41)
    count = _core.terminator_count()
    outer._impl.enter()
    outer._impl.close()

    class NestingTimeout:
        """Change the current scope while converting a timeout."""

        def __float__(self):
            inner._impl.enter()
            return 0.0

    try:
        with pytest.raises(RuntimeError, match="nesting order"):
            outer._impl.wait(NestingTimeout())
    finally:
        inner._impl.close()
        assert inner._impl.wait(WAIT_TIMEOUT)
        assert inner._impl.finish(True) == 41
        assert outer._impl.wait(WAIT_TIMEOUT)
        assert outer._impl.finish(True) == 37
    assert _core.terminator_count() == count


@pytest.mark.parametrize("idempotent_start", [False, True])
def test_nested_host_scope_can_enter_during_global_wait(idempotent_start):
    program = """
        from threading import Event, Thread

        from bocpy import _core, Scope, start, wait

        def host(entered, joining, outcomes):
            with Scope():
                entered.set()
                assert joining.wait(5)
                START_EXISTING_RUNTIME
                with Scope(42) as child:
                    pass
                outcomes.append(child.output_value)

        if __name__ == "__main__":
            start(worker_count=2)
            entered = Event()
            joining = Event()
            outcomes = []
            thread = Thread(target=host, args=(entered, joining, outcomes))
            thread.start()
            assert entered.wait(5)
            quiescence_wait = _core.terminator_quiescence

            def begin_join(*args, **kwargs):
                joining.set()
                return quiescence_wait(*args, **kwargs)

            _core.terminator_quiescence = begin_join
            try:
                wait()
            finally:
                _core.terminator_quiescence = quiescence_wait
            thread.join(5)
            assert not thread.is_alive()
            assert outcomes == [42]
            print("nested host scope completed during global join")
    """
    output = _run_program(program.replace("START_EXISTING_RUNTIME", "start()" if idempotent_start else "pass"))
    assert "nested host scope completed during global join" in output


def test_repeated_pinned_baseexception_restores_scope_host():
    output = _run_program("""
        from bocpy import _core, Scope, PinnedCown, pump, root_scope, start, wait, when

        if __name__ == "__main__":
            start(worker_count=2)
            pinned = PinnedCown([])
            errors = []
            with Scope():
                for label in ("first", "second"):
                    @when(pinned, scope=root_scope)
                    def fail(value, label=label):
                        value.value.append(label)
                        raise BaseException(label)
                for _ in range(2):
                    try:
                        pump(max_behaviors=1)
                    except BaseException as error:
                        errors.append(error)
            assert [error.args for error in errors] == [("first",), ("second",)]
            assert not _core.scope_active()
            assert _core.terminator_count() == 0
            assert pinned.unwrap() == ["first", "second"]
            wait(10)
            assert _core.terminator_count() == 0
            print("pinned failures drained and host restored")
    """)
    assert "pinned failures drained and host restored" in output


@pytest.mark.skipif(not sysconfig.get_config_var("Py_GIL_DISABLED"), reason="requires a free-threaded build")
def test_scope_exit_waits_for_concurrent_pumper_and_restores_host():
    output = _run_program("""
        from threading import Event, Thread

        from bocpy import _core, PinnedCown, pump, root_scope, Scope, start, wait, when

        def pump_once(failures):
            try:
                pump(max_behaviors=1)
            except BaseException as error:
                failures.append(error)

        def host(ready, finished, outcomes, failures):
            try:
                with Scope([]) as scope:
                    pinned = PinnedCown(None)
                    @when(pinned, scope=root_scope)
                    def child(value):
                        return 42

                    @when(child, scope.output_cown)
                    def store_child(value, output):
                        output.value.append(value.value)
                    ready.set()
                assert not _core.scope_active()
                outcomes.append(scope.output_value)
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()

        if __name__ == "__main__":
            start(worker_count=2)
            entered = Event()
            release = Event()
            ready = Event()
            finished = Event()
            failures = []
            outcomes = []
            blocker = PinnedCown((entered, release))

            @when(blocker, scope=root_scope)
            def hold_pump(value):
                entered, release = value.value
                entered.set()
                assert release.wait(10)

            pumper = Thread(target=pump_once, args=(failures,))
            joining_host = Thread(target=host, args=(ready, finished, outcomes, failures))
            pumper.start()
            try:
                assert entered.wait(5)
                try:
                    pump()
                except RuntimeError as error:
                    assert "in use by another thread" in str(error)
                else:
                    raise AssertionError("explicit concurrent pump was accepted")
                joining_host.start()
                assert ready.wait(5)
                assert not finished.wait(0.2), failures
            finally:
                release.set()
                pumper.join(5)
                if joining_host.ident is not None:
                    joining_host.join(5)
            assert not pumper.is_alive()
            assert not joining_host.is_alive()
            assert failures == [], failures
            assert outcomes == [[42]]
            assert _core.terminator_count() == 0
            wait(10)
            print("concurrent pump drained and scope host restored")
    """)
    assert "concurrent pump drained and scope host restored" in output


def test_retained_scope_output_does_not_break_shutdown_cleanup():
    output = _run_program("""
        from bocpy import Scope, start, wait, when

        if __name__ == "__main__":
            start(worker_count=2)
            with Scope([]) as scope:
                retained = scope.output_cown
                @when(retained)
                def update(output):
                    output.value.append(42)
            assert scope.output_value == [42]
            for before_shutdown in (True, False):
                if not before_shutdown:
                    wait(10)
                try:
                    retained.acquire()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("scope output became directly accessible")
            assert scope.output_value == [42]
            print("retained output cleanup succeeded")
    """)
    assert "retained output cleanup succeeded" in output


@pytest.mark.parametrize("shutdown", ["wait", "stop"])
def test_timed_shutdown_preserves_active_host_output_and_runtime(shutdown):
    program = """
        from threading import Event, Thread
        from bocpy import _core, Scope, receive, send, start, wait, when
        from bocpy import behaviors

        def host(release, outcomes):
            with Scope([]) as scope:
                with Scope():
                    @when(scope.output_cown)
                    def write(output):
                        output.value.append(42)
                send("scope-written", 42)
                assert release.wait(10)
            outcomes.append(scope.output_value)

        if __name__ == "__main__":
            start(worker_count=2)
            release = Event()
            outcomes = []
            runtime = behaviors.BEHAVIORS
            thread = Thread(target=host, args=(release, outcomes))
            thread.start()
            try:
                assert receive("scope-written", 5) == ("scope-written", 42)
                count = _core.terminator_count()
                assert count == 1
                try:
                    SHUTDOWN_CALL
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("shutdown accepted an active scope host")
                assert behaviors.BEHAVIORS is runtime
                assert not runtime._workers_stopped
                assert not runtime._teardown_complete
                assert runtime.noticeboard.is_alive()
                assert _core.terminator_count() == count
                for attempt in range(2):
                    start(worker_count=2)
                    assert behaviors.BEHAVIORS is runtime
                    assert _core.terminator_count() == count
            finally:
                release.set()
                thread.join(5)
            assert not thread.is_alive()
            assert outcomes == [[42]]
            assert _core.terminator_count() == 0
            wait(10)
            assert _core.terminator_count() == 0
            start(worker_count=2)
            assert _core.terminator_count() == 0
            wait(10)
            assert _core.terminator_count() == 0
            print("timed shutdown preserved active host output and runtime")
    """
    call = "wait(0.01)" if shutdown == "wait" else "runtime.stop(timeout=0.01)"
    output = _run_program(program.replace("SHUTDOWN_CALL", call))
    assert "timed shutdown preserved active host output and runtime" in output


def test_noticeboard_scope_handle_survives_repeated_pinned_snapshots():
    output = _run_program("""
        import gc
        from bocpy import Scope, PinnedCown, notice_read, notice_seed, pump, root_scope, start, wait, when

        if __name__ == "__main__":
            start(worker_count=2)
            pinned = PinnedCown(None)
            with Scope(0) as scope:
                notice_seed("destination", scope)
                for index in range(50):
                    notice_seed("unrelated", index)
                    @when(pinned, scope=root_scope)
                    def read(value):
                        destination = notice_read("destination")
                        @when(destination.output_cown, scope=destination)
                        def update(output):
                            output.value += 1
                    pump()
            assert scope.output_value == 50
            del scope
            gc.collect()
            wait(10)
            print("scope destination survived 50 snapshots")
    """)
    assert "scope destination survived 50 snapshots" in output


def test_scope_handle_rejects_standalone_pickle_without_corrupting_process():
    output = _run_program("""
        import gc
        import pickle

        from bocpy import Cown, Scope

        if __name__ == "__main__":
            scope = Scope()
            class ReentrantSerializer:
                def __reduce__(self):
                    pickle.dumps(scope)
                    return (str, ("not reached",))

            for value in (scope, scope._impl):
                try:
                    pickle.dumps(value)
                except TypeError as error:
                    assert "bocpy-managed transfers" in str(error)
                else:
                    raise AssertionError("standalone scope pickle was accepted")
            try:
                Cown(ReentrantSerializer())
            except TypeError as error:
                assert "bocpy-managed transfers" in str(error)
            else:
                raise AssertionError("reentrant standalone scope pickle was accepted")
            del scope
            gc.collect()
            print("standalone scope pickle rejected")
    """)
    assert "standalone scope pickle rejected" in output


def test_scope_handle_round_trips_through_managed_message_transfer():
    with Scope(0) as scope:
        send("scope-transfer", scope)
        tag, transferred = receive("scope-transfer", WAIT_TIMEOUT)
        assert tag == "scope-transfer"

        @when(transferred.output_cown, scope=transferred)
        def update(output):
            output.value = 42

    assert scope.output_value == 42
    quiesce(WAIT_TIMEOUT)
    assert update.unwrap() is None


def test_private_scope_capsule_survives_nested_managed_transfer():
    with Scope() as scope:
        send("scope-capsule-direct", scope._impl)
        tag, capsule = receive("scope-capsule-direct", WAIT_TIMEOUT)
        assert tag == "scope-capsule-direct"

        send("scope-capsule-nested", [capsule])
        tag, nested = receive("scope-capsule-nested", WAIT_TIMEOUT)
        assert tag == "scope-capsule-nested"
        assert nested[0].state == "active"


def test_failed_managed_scope_transfer_rolls_back_output_cown_ref():
    with Scope() as scope:
        before = _core.cown_refcount(scope.output_cown)
        for _ in range(3):
            with pytest.raises(ValueError, match="scope serialization failure"):
                Cown([scope, _UnpicklableAfterScope()])
        assert _core.cown_refcount(scope.output_cown) == before


@pytest.mark.parametrize("release", ["replace", "delete", "clear"])
def test_noticeboard_owner_only_references_survive_entry_release(release):
    with Scope(None) as scope:
        _core.noticeboard_seed(
            "owner-only", {"first": scope._impl, "again": scope._impl}, None,
        )
    del scope

    readers = []
    for revision in range(3):
        _core.noticeboard_seed("revision", revision, None)
        _core.noticeboard_cache_clear()
        readers.append(_core.noticeboard_snapshot()["owner-only"]["first"])
        assert readers[-1].state == "closed"
    assert readers[0] is not readers[-1]

    if release == "replace":
        _core.noticeboard_seed("owner-only", None, None)
    elif release == "delete":
        notice_delete("owner-only")
        quiesce(WAIT_TIMEOUT, noticeboard=True)
    else:
        _core.noticeboard_clear()
    _core.noticeboard_cache_clear()
    _core.noticeboard_snapshot()
    for reader in readers:
        assert reader.state == "closed"
        with pytest.raises(RuntimeError, match="single-use"):
            reader.enter()


def test_noticeboard_owner_reference_pickle_failure_preserves_previous_entry():
    with Scope(None) as scope:
        _core.noticeboard_seed("owner-only", {"handle": scope._impl}, None)
        for _ in range(3):
            with pytest.raises(ValueError, match="scope serialization failure"):
                _core.noticeboard_seed(
                    "owner-only", {"handle": scope._impl, "bad": _UnpicklableAfterScope()}, None,
                )
        _core.noticeboard_cache_clear()
        assert _core.noticeboard_snapshot()["owner-only"]["handle"].state == "active"
    del scope
    _core.noticeboard_seed("revision", 1, None)
    _core.noticeboard_cache_clear()
    assert _core.noticeboard_snapshot()["owner-only"]["handle"].state == "closed"
    _core.noticeboard_clear()


@pytest.mark.parametrize("module", [False, True])
@pytest.mark.parametrize("explicit_start", [False, True])
def test_main_bindings_startup_and_class_result_transfer(module, explicit_start):
    program = """
        from bocpy import Scope, start, wait, when
        from bocpy import behaviors

        class Payload:
            def __init__(self, value):
                self.value = value

        def library():
            with Scope(Payload(21)) as scope:
                @when(scope.output_cown)
                def double(output):
                    output.value.value *= 2
            return scope.output_value

        if __name__ == "__main__":
            behaviors.WORKER_COUNT = 2
            START_RUNTIME
            result = library()
            assert isinstance(result, Payload)
            assert result.value == 42
            wait(10)
            print("scope class roundtrip ok")
    """
    program = program.replace("START_RUNTIME", "start(worker_count=2)" if explicit_start else "pass")
    output = _run_program(program, module=module)
    assert "scope class roundtrip ok" in output
