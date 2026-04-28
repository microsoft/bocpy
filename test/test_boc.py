"""Behavior-oriented concurrency tests."""

import functools
import sys
import threading
import traceback
from typing import NamedTuple

import pytest

from bocpy import Cown, drain, receive, send, start, TIMEOUT, wait, when
from bocpy._core import CownCapsule

RECEIVE_TIMEOUT = 10

GLOBAL_FACTOR = 7


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


class Multiplier:
    """Multiplies a cown's value by a module-level global inside a method."""

    def multiply(self, x: Cown) -> Cown:
        """Schedule a behavior that captures GLOBAL_FACTOR from module scope."""
        factor = GLOBAL_FACTOR  # noqa: F841 — captured by @when below

        @when(x)
        def do_multiply(x):
            return x.value * factor  # noqa: B023

        return do_multiply

    def multiply_direct(self, x: Cown) -> Cown:
        """Schedule a behavior that captures GLOBAL_FACTOR directly."""
        @when(x)
        def do_multiply(x):
            return x.value * GLOBAL_FACTOR

        return do_multiply


def simple(x: Cown) -> Cown:
    """Double a cown's value in a behavior."""
    @when(x)
    def do_double(x: Cown):
        return x.value * 2

    return do_double


def nested(x: Cown) -> Cown:
    """Chain two behaviors that update the same cown."""
    @when(x)
    def nested_double(x: Cown):
        x.value *= 2

        @when(x)
        def nested_triple(x: Cown):
            x.value *= 3

        return nested_triple

    return nested_double


def exception(x: Cown) -> Cown:
    """Trigger a division-by-zero inside a behavior."""
    @when(x)
    def do_div0(x: Cown):
        x.value /= 0

    return do_div0


class RaiseOnUnpickle:
    """Pickles cleanly but raises ZeroDivisionError when unpickled.

    Used to drive the deserialisation-failure path inside
    ``cown_acquire``. The ``__reduce__`` protocol stores
    ``(eval, ("1/0",))``; ``eval`` is a builtin so the bytestream is
    portable across sub-interpreters, and ``eval("1/0")`` raises
    ``ZeroDivisionError`` when ``pickle.loads`` is called inside the
    worker's ``cown_acquire``.
    """

    def __reduce__(self):
        """Return a reduce tuple whose loader raises on unpickle."""
        return (eval, ("1/0",))


class Fork:
    """Simple fork that tracks usage and remaining hunger."""

    def __init__(self, hunger: int):
        """Initialize with an initial hunger counter."""
        self.hunger = hunger
        self.uses = 0

    def use(self):
        """Increment use count when acquired."""
        self.uses += 1


class Philosopher(NamedTuple("Philosopher", [("index", int), ("left", Cown),
                                             ("right", Cown), ("hunger", Cown)])):
    """Philosopher that coordinates two forks and its hunger."""

    def eat(self: "Philosopher"):
        """Attempt to eat until hunger is satisfied."""
        index = self.index

        @when(self.left, self.right, self.hunger)
        def take_bite(left, right, hunger):
            left.value.use()
            right.value.use()
            hunger.value -= 1
            if hunger.value > 0:
                Philosopher(index, left, right, hunger).eat()
            else:
                @when()
                def _():
                    send("report", ("full", index))


class Accumulator:
    """Simple list-based accumulator for testing."""

    def __init__(self):
        """Initialize with an empty item list."""
        self.items = []

    def add(self, item):
        """Append an item to the list."""
        self.items.append(item)


@functools.lru_cache
def fib_sequential(n: int) -> int:
    """Compute Fibonacci sequentially with memoization."""
    if n <= 1:
        return n

    return fib_sequential(n-1) + fib_sequential(n - 2)


def fib_parallel(n: int) -> Cown:
    """Compute Fibonacci using cowns to parallelize recursion."""
    if n <= 4:
        return Cown(fib_sequential(n))

    @when(fib_parallel(n - 1), fib_parallel(n - 2))
    def do_fib(f1, f2):
        return f1.value + f2.value

    return do_fib


def cown_grouping():
    """Group cowns to test grouping/ungrouping."""
    cowns = [Cown(i) for i in range(10)]
    expected = 45

    @when(cowns)
    def group(group: list[Cown[int]]):
        return sum([c.value for c in group])

    @when(cowns[:9], cowns[9])
    def group_single(group: list[Cown[int]], single: Cown[int]):
        return sum([c.value for c in group]) + single.value

    @when(cowns[0], cowns[1:])
    def single_group(single: Cown[int], group: list[Cown[int]]):
        return sum([c.value for c in group]) + single.value

    @when(cowns[:4], cowns[4], cowns[5:])
    def group_single_group(group0: list[Cown[int]], single: Cown[int], group1: list[Cown[int]]):
        return sum([c.value for c in group0]) + single.value + sum([c.value for c in group1])

    @when(cowns[0], cowns[1:9], cowns[9])
    def single_group_single(single0: Cown[int], group: list[Cown[int]], single1: Cown[int]):
        return single0.value + sum([c.value for c in group]) + single1.value

    return expected, [group, group_single, single_group, group_single_group, single_group_single]


class TestBOC:
    """Integration-style tests for bocpy behaviors."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_simple_dispatch(self):
        """Verify single when schedules and returns doubled value."""
        x = Cown(1)
        y = simple(x)
        assert isinstance(y, Cown)

        @when(y)
        def _(y):
            send("assert", (y.value, 2))

        receive_asserts()

    def test_nested_dispatch(self):
        """Ensure nested behaviors see updated state."""
        x = Cown(1)
        y = nested(x)

        # Only assert the final state. The intermediate value of x is racy:
        # the inner nested_triple is scheduled on x from inside nested_double
        # and may run before or after a behavior the main thread enqueues on
        # x, depending on worker timing.
        @when(x, y)
        def check_double(x, y):
            @when(x, y.value)
            def check_triple(x, _inner):
                send("assert", (x.value, 6))

        receive_asserts()

    def test_exception(self):
        """Exceptions propagate as values in behaviors."""
        x = Cown(1)
        y = exception(x)

        @when(y)
        def _(y):
            send("assert", (isinstance(y.value, ZeroDivisionError), True))
            y.value = None

        receive_asserts()

    def test_two_cown_coordination(self):
        """Move value between two cowns with coordinated when."""
        x = Cown(100)
        y = Cown(0)

        def check(c: Cown, value: int):
            @when(c)
            def do_check(c):
                send("assert", (c.value, value))

        check(x, 100)
        check(y, 0)

        @when(x, y)
        def _(x, y):
            y.value += 50
            x.value -= 50

        check(x, 50)
        check(y, 50)

        receive_asserts(4)

    def test_classes(self, num_philosophers=5, hunger=4):
        """Simulate dining philosophers and verify fork usage."""
        forks = [Cown(Fork(hunger)) for _ in range(num_philosophers)]
        for idx in range(num_philosophers):
            Philosopher.eat(Philosopher(idx, forks[idx-1], forks[idx], Cown(hunger)))

        num_eating = num_philosophers
        while num_eating > 0:
            match receive("report"):
                case ["report", ("full", _)]:
                    num_eating -= 1

        for _, f in enumerate(forks):
            @when(f)
            def _(f):
                send("assert", (f.value.uses, 2*f.value.hunger))

        receive_asserts(num_philosophers)

    @pytest.mark.parametrize("n", [1, 10, 15])
    def test_variable_termination(self, n: int):
        """Compare parallel Fibonacci against sequential baseline."""
        result = fib_parallel(n)
        expected = fib_sequential(n)

        @when(result)
        def check(result):
            send("assert", (result.value, expected))

        receive_asserts()

    def test_cown_grouping(self):
        """Verify cown grouping returns correct sums."""
        expected, results = cown_grouping()

        @when(results)
        def check(results: list[Cown]):
            for r in results:
                send("assert", (r.value, expected))

        receive_asserts(len(results))

    def test_grouped_cown_mutation(self):
        """Write to cowns within a group and verify mutations stick."""
        cowns = [Cown(i) for i in range(5)]

        @when(cowns)
        def double_all(group: list[Cown[int]]):
            for c in group:
                c.value *= 2

        @when(cowns)
        def verify(group: list[Cown[int]]):
            for i, c in enumerate(group):
                send("assert", (c.value, i * 2))

        receive_asserts(5)

    def test_group_and_single_mutation(self):
        """Mutate a group and a single cown in the same behavior."""
        items = [Cown(1), Cown(2), Cown(3)]
        total = Cown(0)

        @when(items, total)
        def accumulate(group: list[Cown[int]], t: Cown[int]):
            for c in group:
                t.value += c.value
                c.value = 0

        @when(total)
        def check_total(t):
            send("assert", (t.value, 6))

        @when(items)
        def check_zeroed(group: list[Cown[int]]):
            for c in group:
                send("assert", (c.value, 0))

        receive_asserts(4)

    def test_behavior_chain(self):
        """Chain three behaviors where each result feeds the next."""
        x = Cown(2)

        @when(x)
        def step1(x):
            return x.value + 3          # 5

        @when(step1)
        def step2(s1):
            return s1.value * 4         # 20

        @when(step2)
        def step3(s2):
            return s2.value - 7         # 13

        @when(step3)
        def check(s3):
            send("assert", (s3.value, 13))

        receive_asserts()

    def test_contention(self):
        """Many behaviors on the same cown serialize correctly."""
        counter = Cown(0)
        n = 50

        for _ in range(n):
            @when(counter)
            def _(c):
                c.value += 1

        @when(counter)
        def check(c):
            send("assert", (c.value, n))

        receive_asserts()

    def test_exception_type_error(self):
        """Verify TypeError inside a behavior is captured in the result cown."""
        x = Cown("hello")

        @when(x)
        def bad(x):
            return x.value + 1          # str + int -> TypeError

        @when(bad)
        def check(b):
            send("assert", (isinstance(b.value, TypeError), True))
            b.value = None

        receive_asserts()

    def test_exception_key_error(self):
        """Verify KeyError inside a behavior is captured in the result cown."""
        x = Cown({})

        @when(x)
        def bad(x):
            return x.value["missing"]   # KeyError

        @when(bad)
        def check(b):
            send("assert", (isinstance(b.value, KeyError), True))
            b.value = None

        receive_asserts()

    def test_complex_object_repeated_mutation(self):
        """Multiple sequential behaviors mutate the same object in a cown."""
        acc = Cown(Accumulator())

        for i in range(10):
            val_to_add = i

            @when(acc)
            def _(a):
                a.value.add(val_to_add)  # noqa: B023

        @when(acc)
        def check(a):
            send("assert", (sorted(a.value.items), list(range(10))))

        receive_asserts()

    def test_duplicate_cown_same_twice(self):
        """Same cown passed twice to @when completes without deadlock."""
        c = Cown(5)

        @when(c, c)
        def add(a, b):
            return a.value + b.value

        @when(add)
        def check(r):
            send("assert", (r.value, 10))

        receive_asserts()

    def test_duplicate_cown_same_thrice(self):
        """Same cown passed three times to @when completes without deadlock."""
        c = Cown(3)

        @when(c, c, c)
        def triple(a, b, d):
            return a.value + b.value + d.value

        @when(triple)
        def check(r):
            send("assert", (r.value, 9))

        receive_asserts()

    def test_duplicate_cown_non_adjacent(self):
        """Non-adjacent duplicate cowns in @when complete correctly."""
        a = Cown(10)
        b = Cown(20)

        @when(a, b, a)
        def mixed(x, y, z):
            return x.value + y.value + z.value

        @when(mixed)
        def check(r):
            send("assert", (r.value, 40))

        receive_asserts()

    def test_duplicate_cown_in_group(self):
        """Duplicate cowns within a group complete without deadlock."""
        c = Cown(7)

        @when([c, c])
        def group_sum(group):
            return sum(g.value for g in group)

        @when(group_sum)
        def check(r):
            send("assert", (r.value, 14))

        receive_asserts()

    def test_duplicate_cown_mutation(self):
        """Mutating a cown passed twice reflects same underlying value."""
        c = Cown(1)

        @when(c, c)
        def mutate(a, b):
            a.value = 42
            return b.value

        @when(mutate)
        def check(r):
            send("assert", (r.value, 42))

        receive_asserts()

    def test_cown_of_cown_direct(self):
        """CownCapsule as direct child of a Cown survives release/acquire."""
        inner = Cown(42)
        outer = Cown(inner)

        @when(outer)
        def read_outer(o):
            send("assert", (type(o.value).__name__, "Cown"))

        receive_asserts()

    def test_cown_of_cown_access_inner(self):
        """Inner cown's value is accessible after outer round-trip."""
        inner = Cown(99)
        outer = Cown(inner)

        @when(outer, inner)
        def check_both(o, i):
            send("assert", (i.value, 99))

        receive_asserts()

    def test_cown_of_cown_in_container(self):
        """CownCapsule nested in a dict survives pickle round-trip."""
        inner = Cown(7)
        outer = Cown({"key": inner})

        @when(outer)
        def check_container(o):
            send("assert", (type(o.value["key"]).__name__, "Cown"))

        receive_asserts()

    def test_cown_of_cown_schedule_inner(self):
        """Extract inner cown from outer and schedule a behavior on it."""
        inner = Cown(10)
        outer = Cown(inner)

        @when(outer)
        def extract(o):
            return o.value

        @when(extract)
        def schedule_on_inner(r):
            inner_cown = Cown(r.value)

            @when(inner_cown)
            def read_inner(i):
                send("assert", (i.value, 10))

        receive_asserts()


class TestGlobalCapture:
    """Tests for capturing module-level globals inside class methods."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_method_captures_global_via_local(self):
        """A method assigns a global to a local; @when captures the local."""
        m = Multiplier()
        x = Cown(5)
        result = m.multiply(x)

        @when(result)
        def _(r):
            send("assert", (r.value, 5 * GLOBAL_FACTOR))

        receive_asserts()

    def test_method_captures_global_directly(self):
        """A method's @when captures a module-level global by name."""
        m = Multiplier()
        x = Cown(3)
        result = m.multiply_direct(x)

        @when(result)
        def _(r):
            send("assert", (r.value, 3 * GLOBAL_FACTOR))

        receive_asserts()

    @pytest.mark.parametrize("value", [1, 10, 100])
    def test_method_captures_global_parametrized(self, value):
        """Parametrized: global capture from a method works across inputs."""
        m = Multiplier()
        x = Cown(value)
        result = m.multiply_direct(x)

        @when(result)
        def _(r):
            send("assert", (r.value, value * GLOBAL_FACTOR))  # noqa: B023

        receive_asserts()


class TestCownAcquireDeserialiseFailure:
    """``cown_acquire`` rolls back owner on unpickle failure.

    When ``xidata_to_object`` (which calls ``_PyPickle_Loads``) raises,
    ``cown_acquire`` previously returned -1 with the cown left in a
    half-acquired ``(owner=worker, value=NULL, xidata!=NULL)`` state.
    The worker-side recovery arm in ``run_behavior`` then called
    ``behavior.release()``, whose ``cown_release`` aborts on
    ``assert(cown->value != NULL)`` (debug build) or NULL-derefs in
    ``object_to_xidata`` (release build).

    The fix stores ``NO_OWNER`` back into ``cown->owner`` before
    returning -1, so the recovery arm's ``cown_release`` short-circuits
    cleanly via the ``owner == NO_OWNER`` branch and the result Cown
    surfaces the exception to downstream behaviors.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_acquire_rollback_surfaces_exception(self):
        """Acquire failure produces a result Cown with .exception True.

        The first behavior ``use_bad`` is scheduled against a Cown wrapping
        an instance of :class:`RaiseOnUnpickle`. When the worker dequeues
        the behavior and calls ``cown_acquire``, ``_PyPickle_Loads`` raises
        ``ZeroDivisionError``. The worker's recovery arm marks ``use_bad``'s
        result Cown with the exception. The downstream behavior ``check``
        observes ``b.exception is True``.

        Without this rollback, ``cown_release`` aborts before
        ``check`` is ever scheduled, the assert messages never
        arrive, and the test either segfaults or times out.
        """
        bad = Cown(RaiseOnUnpickle())

        @when(bad)
        def use_bad(b):
            # This body never runs — acquire fails first.
            send("assert", (b.value, "unreachable"))

        @when(use_bad)
        def check(b):
            send("assert", (b.exception, True))
            send("assert", (isinstance(b.value, ZeroDivisionError), True))

        receive_asserts(2)


class TestBehaviorCapsuleArgsSize:
    """``BehaviorCapsule`` ``group_ids`` allocation corner cases.

    ``BehaviorCapsule_init`` allocates ``behavior->group_ids`` via
    ``PyMem_RawCalloc(args_size, sizeof(int))``. Two corner cases must
    work:

    * ``args_size == 0`` -- ``PyMem_RawCalloc`` may legally return NULL
      for a zero-element request, so the NULL check must be guarded
      ``args_size > 0``.
    * ``args_size > 0`` with a successful allocation -- the standard
      path; verifies the gating logic does not regress normal use.

    OOM injection for the failure path requires allocator hooks that
    do not exist in the test infrastructure today.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_zero_args_behavior_capsule(self):
        """BehaviorCapsule with empty args list must construct cleanly."""
        from bocpy import start as _start_runtime
        from bocpy._core import BehaviorCapsule
        try:
            _start_runtime()
        except RuntimeError:
            pass  # Runtime already started by a prior test.

        result = Cown(None)
        # Empty args list — args_size == 0. The
        # ``args_size > 0 && group_ids == NULL`` guard avoids a
        # spurious failure if PyMem_RawCalloc(0, ...) returns NULL.
        capsule = BehaviorCapsule(
            "__behavior_zero_args__",
            result.impl,
            [],
            [],
        )
        assert capsule is not None

    def test_large_args_behavior_capsule(self):
        """BehaviorCapsule with many args constructs and group_ids works."""
        from bocpy import start as _start_runtime
        from bocpy._core import BehaviorCapsule
        try:
            _start_runtime()
        except RuntimeError:
            pass  # Runtime already started by a prior test.

        result = Cown(None)
        # 32 distinct cowns with distinct group_ids. Exercises the
        # group_ids[i] = group_id loop that NULL-derefs without
        # the alloc check on OOM.
        cowns = [Cown(i) for i in range(32)]
        args = [(i, c.impl) for i, c in enumerate(cowns)]

        capsule = BehaviorCapsule(
            "__behavior_large_args__",
            result.impl,
            args,
            [],
        )
        assert capsule is not None


class TestExceptionFlag:
    """Tests for the Cown.exception flag distinguishing thrown vs returned."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_exception_flag_on_throw(self):
        """Thrown exception sets .exception to True."""
        x = Cown(1)

        @when(x)
        def bad(x):
            x.value /= 0

        @when(bad)
        def check(b):
            send("assert", (b.exception, True))
            send("assert", (isinstance(b.value, ZeroDivisionError), True))
            b.value = None

        receive_asserts(2)

    def test_exception_flag_on_return(self):
        """Returned Exception object has .exception False."""
        x = Cown(1)

        @when(x)
        def returns_exc(x):
            return ValueError("not an error")

        @when(returns_exc)
        def check(r):
            send("assert", (r.exception, False))
            send("assert", (isinstance(r.value, ValueError), True))

        receive_asserts(2)

    def test_exception_flag_cleared_on_value_write(self):
        """Writing .value clears the exception flag."""
        x = Cown(1)

        @when(x)
        def bad(x):
            x.value /= 0

        @when(bad)
        def check(b):
            send("assert", (b.exception, True))
            b.value = "fixed"
            send("assert", (b.exception, False))

        receive_asserts(2)

    def test_exception_flag_manual_set_clear(self):
        """Manual .exception set and clear works."""
        x = Cown(42)

        @when(x)
        def check(x):
            send("assert", (x.exception, False))
            x.exception = True
            send("assert", (x.exception, True))
            x.exception = False
            send("assert", (x.exception, False))

        receive_asserts(3)

    def test_returned_exception_no_unhandled_report(self, capsys):
        """Returned Exception doesn't trigger unhandled exception report."""
        x = Cown(1)

        @when(x)
        def returns_exc(x):
            return ValueError("just a value")

        @when(returns_exc)
        def check(r):
            send("assert", (r.exception, False))
            send("assert", (isinstance(r.value, ValueError), True))

        receive_asserts(2)
        wait()
        captured = capsys.readouterr()
        assert "unhandled exception" not in captured.err.lower()


class TestUnicodeSource:
    """Source containing non-ASCII characters must round-trip through export.

    Regression: the exported behavior module was previously written without
    an explicit ``encoding`` argument, so on platforms whose locale encoding
    is not UTF-8 (notably Windows / cp1252) any non-ASCII literal in the
    source was written as a non-UTF-8 byte. Worker sub-interpreters then
    failed to import the module with a SyntaxError on the offending byte,
    causing the worker pool to fail to start.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_non_ascii_literal_in_behavior(self):
        """A behavior containing a non-ASCII string literal runs correctly."""
        x = Cown(0)

        @when(x)
        def _(x):
            # "€" (U+20AC) is 3 bytes in UTF-8 and a single byte 0x80 in
            # cp1252; if the export file is not written as UTF-8 the
            # worker fails to import this module.
            send("assert", ("€", "€"))

        receive_asserts()


class TestModuleDunderCapture:
    """Module-level dunders inside a behavior must resolve to the user module.

    Regression: ``__name__``, ``__doc__``, ``__package__``, ``__spec__``,
    and ``__loader__`` are exposed via ``__builtins__``. They were being
    silently filtered out of the capture set, so inside a behavior they
    resolved against the worker's exported module (e.g. ``__name__`` was
    ``"__bocmain__"`` instead of the original module name). They must now
    flow through the capture mechanism so the call-site value is used.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_name_resolves_to_user_module(self):
        """__name__ inside a behavior is the user module, not the worker's."""
        x = Cown(0)
        expected = __name__

        @when(x)
        def _(x):
            send("assert", (__name__, expected))  # noqa: B023

        receive_asserts()

    def test_package_resolves_to_user_module(self):
        """__package__ inside a behavior matches the user module's value."""
        x = Cown(0)
        expected = __package__

        @when(x)
        def _(x):
            send("assert", (__package__, expected))  # noqa: B023

        receive_asserts()


# ---------------------------------------------------------------------------
# Cross-worker scheduling and cown-identity round-trip invariants.
#
# These two properties of the BOC runtime are not asserted directly by
# any of the @when / Cown / capture tests above:
#
#   1. With workers >= 2, behaviors really run on more than one worker
#      thread. Without this, every "parallel" workload degenerates to
#      single-threaded throughput.
#   2. A Cown round-tripped through XIData into a worker arrives back
#      as a CownCapsule. This exercises the XIData round-trip path
#      that the 2PL dedup machinery relies on.
# ---------------------------------------------------------------------------


class TestCrossWorker:
    """Verify cross-worker scheduling and cown round-trip through XIData."""

    @classmethod
    def teardown_class(cls):
        """Drain leftover tagged messages so subsequent tests start clean."""
        for tag in ("probe_tid", "probe_id"):
            try:
                drain(tag)
            except Exception:
                pass

    def test_two_workers_observe_distinct_thread_ids(self):
        """At workers=2, >=2 distinct worker thread ids must appear."""
        if sys.version_info < (3, 12):
            pytest.skip(
                "per-interpreter GIL only available on Python 3.12+; on "
                "shared-GIL interpreters a single worker can drain the "
                "queue before the other wakes up, so this property does "
                "not hold")
        tid_samples = 16
        cells = [Cown(0) for _ in range(tid_samples)]

        start(worker_count=2)
        try:
            for c in cells:
                @when(c)
                def _tid(_c):
                    send("probe_tid", threading.get_ident())
        finally:
            del cells
            wait()

        thread_ids = set()
        for _ in range(tid_samples):
            msg = receive(["probe_tid"], RECEIVE_TIMEOUT)
            assert msg is not None and msg[0] != TIMEOUT, (
                "thread-id probe timed out")
            thread_ids.add(msg[1])

        assert len(thread_ids) >= 2, (
            f"only {len(thread_ids)} distinct worker thread id observed "
            f"across {tid_samples} samples on workers=2; cross-worker "
            "scheduling appears broken")

    def test_cown_round_trips_through_xidata(self):
        """A Cown sent from a worker arrives back as a CownCapsule.

        Cross-interpreter ``send`` does not preserve raw ``CownCapsule``
        pointer equality on the receive side — XIData may resurrect a
        fresh wrapper. The 2PL identity invariant the runtime relies on
        lives in the runtime's ``xidata_to_cowns`` dedup machinery at
        acquire time, not in ``__eq__`` after a queue round-trip. This
        test therefore asserts only that every slot's probe came back
        with a ``CownCapsule`` payload (i.e. the cown survived XIData
        in both directions); it does not assert wrapper identity.
        """
        ring_size = 4
        ring = [Cown(0) for _ in range(ring_size)]
        seen = {}

        start(worker_count=2)
        try:
            for idx, cell in enumerate(ring):
                # The transpiler auto-captures `idx` and `cell` as free
                # variables; do NOT use the `idx=idx` default-arg trick
                # — it confuses the worker module export.
                @when(cell)
                def _probe(c):
                    send("probe_id", (idx, c))  # noqa: B023
            for _ in range(ring_size):
                msg = receive(["probe_id"], RECEIVE_TIMEOUT)
                assert msg is not None and msg[0] != TIMEOUT, (
                    "identity probe timed out")
                _, (probe_idx, probe_cown) = msg
                seen[probe_idx] = probe_cown
        finally:
            del ring
            wait()

        for idx in range(ring_size):
            observed = seen.get(idx)
            assert observed is not None, (
                f"identity probe missing for slot {idx}")
            assert isinstance(observed, CownCapsule), (
                f"slot {idx} returned {type(observed).__name__}, "
                "expected CownCapsule")


class TestInMemoryExport:
    """Regression tests for the in-memory transpiler export path.

    Prior to the in-memory export path the transpiled module was
    written to a temporary directory under ``tempfile.mkdtemp()``
    and re-read by every worker via
    ``importlib.util.spec_from_file_location``. That path had three problems: a world-traversable on-disk artifact, a
    small TOCTOU window between write and per-worker read, and an
    f-string interpolation of ``module_name`` into ``r"..."`` that
    re-opened a code-injection vector if a hostile name reached
    ``start()``.

    The replacement embeds the transpiled source as a Python string
    literal (via ``repr()``) inside the per-worker bootstrap, exec's
    it into a fresh ``types.ModuleType``, and registers a
    ``linecache`` entry under a synthetic filename
    ``<bocpy:NAME>`` so tracebacks still point at the transpiled
    source line. These tests exercise the surfaces that change.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure the runtime is drained between this and the next class."""
        wait()

    def test_traceback_resolves_via_linecache(self):
        """A raising body's traceback shows the transpiled source line.

        The ``linecache`` registration in the worker bootstrap is the
        only thing keeping tracebacks debuggable now that the source
        is no longer on disk. We capture a worker-side traceback
        string via ``traceback.format_exc()`` and assert it references
        the synthetic ``<bocpy:NAME>`` filename — proof the bootstrap
        registered the cache entry under that name.
        """
        c = Cown(0)
        start(worker_count=2)
        try:
            @when(c)
            def _b(c):  # noqa: B023
                try:
                    raise RuntimeError("synthetic-from-test-traceback")
                except RuntimeError:
                    send("tb_done", traceback.format_exc())
            tag, tb_str = receive(["tb_done"], RECEIVE_TIMEOUT)
            assert tag != TIMEOUT, "traceback probe timed out"
        finally:
            drain("tb_done")
            wait()

        # The traceback must reference the synthetic bootstrap
        # filename ``<bocpy:__bocmain__>`` (the test module is the
        # worker's __main__ alias).
        assert "<bocpy:" in tb_str, (
            f"traceback did not reference synthetic filename; got:\n{tb_str}"
        )

    def test_tricky_source_round_trips(self):
        """Tricky literals (Unicode, backslashes, triple-quotes) survive.

        ``repr()`` is the source-of-truth for embedding the transpiled
        text into the worker bootstrap. This test puts every embedding
        hazard we can think of into a single behavior body and
        confirms it executes correctly.
        """
        c = Cown(0)
        start(worker_count=2)
        try:
            @when(c)
            def _(c):  # noqa: B023
                # 1. Non-ASCII identifier-class literal
                # 2. Embedded quotes of every flavour
                # 3. Triple-quoted string literal
                # 4. Backslash and raw-string-style content
                # 5. Surrogate-free Unicode (U+1F600 grinning face)
                # 6. NUL byte in a literal — repr() must escape it
                payload = (
                    "héllo",
                    'mix "single" and \'double\' quotes',
                    """triple-quoted with embedded "quote" and 'apostrophe'""",
                    r"raw \n not a newline",
                    'back\\slash and "escaped quote"',
                    "emoji \U0001F600 in literal",
                    "with\x00nul",
                )
                send("tricky_done", payload)
            tag, payload = receive(["tricky_done"], RECEIVE_TIMEOUT)
            assert tag != TIMEOUT, "tricky-source probe timed out"
            assert payload == (
                "héllo",
                'mix "single" and \'double\' quotes',
                """triple-quoted with embedded "quote" and 'apostrophe'""",
                r"raw \n not a newline",
                'back\\slash and "escaped quote"',
                "emoji \U0001F600 in literal",
                "with\x00nul",
            ), f"payload round-trip mismatch: {payload!r}"
        finally:
            drain("tricky_done")
            wait()

    def test_module_name_with_quote_rejected(self):
        """``module_name`` containing a double-quote is rejected at start().

        Defence in depth: even though every interpolation now uses
        ``repr()``, ``Behaviors.start`` validates ``module_name`` is
        a dotted Python module path before building the bootstrap
        snippet. A name with a quote would ``repr()`` cleanly but
        is still nonsensical and the boundary check refuses it with
        a ``ValueError``.
        """
        # Reach Behaviors.start directly so we can pass an arbitrary
        # module name. We cannot use the public ``bocpy.start()``
        # entry point because it overrides ``module`` from the
        # caller's frame.
        from bocpy import behaviors as _behaviors

        wait()  # ensure no live runtime
        b = _behaviors.Behaviors(2)
        # Provide a path that exists so export_module_from_file does not
        # raise on FileNotFoundError before reaching the validation.
        # The transpiler will parse this test file itself; the body
        # never runs because the validation fires first.
        with pytest.raises(ValueError, match="dotted Python module path"):
            b.start(module=('a"b', __file__))


# ---------------------------------------------------------------------------
# NaN/Inf timeout helper
# ---------------------------------------------------------------------------


class TestTimeoutValidation:
    """Boundary validation for wait/notice_sync_wait timeouts.

    The C-level ``boc_validate_finite_timeout`` helper rejects NaN with
    ``ValueError``, treats ``+Inf`` as "wait forever", and clamps
    negatives to 0 (return immediately). Without it NaN would compute a
    nonsensical ``ms`` argument to the OS timed-wait primitive (UB on
    Windows, wedge-forever on POSIX).
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_terminator_wait_nan_timeout_raises_value_error(self):
        """NaN timeout to ``_core.terminator_wait`` raises ``ValueError``."""
        from bocpy import _core
        with pytest.raises(ValueError, match="NaN"):
            _core.terminator_wait(float("nan"))

    def test_notice_sync_wait_nan_timeout_raises_value_error(self):
        """NaN timeout to ``_core.notice_sync_wait`` raises ``ValueError``."""
        from bocpy import _core
        with pytest.raises(ValueError, match="NaN"):
            _core.notice_sync_wait(0, float("nan"))

    def test_wait_inf_timeout_blocks_until_done(self):
        """``+Inf`` timeout treats wait as "wait forever" and returns once done.

        With no live behaviors the terminator count is already 0, so
        ``terminator_wait(+Inf)`` returns ``True`` immediately rather
        than blocking. The point is that it does *not* raise.
        """
        from bocpy import _core
        # No runtime has incremented the terminator, so this returns at
        # once. The test exists to assert +Inf is accepted (not ValueError).
        assert _core.terminator_wait(float("inf")) is True

    def test_terminator_wait_negative_timeout_returns_immediately(self):
        """Negative timeout to ``_core.terminator_wait`` is mapped to wait_forever.

        bocpy's existing convention treats negatives as "wait forever"
        (matching the historical Python-side semantics). The new
        validator preserves that behaviour for negatives — only NaN is
        upgraded to a hard error. With no live runtime the terminator
        is already at 0, so this returns immediately either way.
        """
        from bocpy import _core
        # Returns True immediately because count is already 0.
        assert _core.terminator_wait(-1.0) is True


# ---------------------------------------------------------------------------
# BaseException discipline
# ---------------------------------------------------------------------------


class TestBaseExceptionDiscipline:
    """KeyboardInterrupt in a @when body releases the cown.

    Without ``finally``-based cleanup, ``except Exception`` arms in
    ``worker.py`` and the orphan-drain loop in ``behaviors.py``
    silently let ``KeyboardInterrupt`` / ``SystemExit`` escape past
    the per-iteration cleanup. The MCS chain would stay linked, the
    cown would stay owned, and every successor on it would strand.
    """

    @classmethod
    def teardown_class(cls):
        wait()
        drain("ki_done")

    def test_keyboard_interrupt_during_worker_releases_cown(self):
        """A ``KeyboardInterrupt`` from a @when body releases the cown.

        Schedules a behavior that raises ``KeyboardInterrupt``, then
        a follow-on behavior on the same cown. If the
        ``finally``-based release / release_all chain is wired
        correctly, the follow-on runs and the test sees its message.
        Otherwise the cown is stranded and ``receive`` times out.
        """
        wait()
        start(worker_count=2)
        try:
            c = Cown(0)

            @when(c)
            def _raise(c):
                raise KeyboardInterrupt("intentional KI")

            @when(c)
            def _follow(c):
                send("ki_done", "ok")

            tag, payload = receive("ki_done", RECEIVE_TIMEOUT)
            assert tag != TIMEOUT, (
                "follow-on never ran -- cown was not released after KI"
            )
            assert payload == "ok"
        finally:
            drain("ki_done")
            wait()

    def test_keyboard_interrupt_during_orphan_drain_completes_drain(self):
        """KI mid-drain still drains the remaining orphans.

        Patches ``BehaviorCapsule.set_drop_exception`` so the first
        orphan raises ``KeyboardInterrupt`` (mimicking a Ctrl-C landing
        inside the drain loop). The drain must finish the remaining
        orphans before the deferred KI is re-raised, so no MCS chain or
        terminator hold leaks.
        """
        from unittest import mock

        from bocpy import behaviors as _behaviors

        wait()
        # Build a Behaviors directly so we can drive _drain_orphan_behaviors
        # against synthetic capsules without standing up the full runtime.
        b = _behaviors.Behaviors(2)

        # Synthetic capsule that records its release_all call. We do
        # NOT actually inject these into the C scheduler queue; instead
        # we monkey-patch `_core.scheduler_drain_all_queues` to return
        # them, and patch `_core.terminator_dec` to be a no-op so the
        # test does not touch global C state.
        class FakeCapsule:
            def __init__(self):
                self.set_drop_called = False
                self.released = False

            def set_drop_exception(self, exc):
                self.set_drop_called = True

            def release_all(self):
                self.released = True

        cap_ki = FakeCapsule()
        cap_ok = FakeCapsule()

        # First call returns both capsules; second call returns [] so
        # the drain loop terminates cleanly.
        drain_returns = [[cap_ki, cap_ok], []]

        def fake_drain():
            return drain_returns.pop(0) if drain_returns else []

        # Make set_drop_exception on cap_ki raise KI; cap_ok works normally.
        original_set_drop = FakeCapsule.set_drop_exception

        def patched_set_drop(self, exc):
            if self is cap_ki:
                raise KeyboardInterrupt("orphan-drain KI")
            return original_set_drop(self, exc)

        with mock.patch.object(FakeCapsule, "set_drop_exception",
                               patched_set_drop), \
             mock.patch("bocpy._core.scheduler_drain_all_queues",
                        side_effect=fake_drain), \
             mock.patch("bocpy._core.terminator_dec", return_value=0):
            with pytest.raises(KeyboardInterrupt, match="orphan-drain KI"):
                b._drain_orphan_behaviors()

        # cap_ok must still have had its release_all called -- the KI on
        # cap_ki did not abort the drain partway.
        assert cap_ok.released, (
            "second orphan was not drained -- KI aborted the loop"
        )
        # cap_ki's release_all was attempted too (the KI was raised
        # from set_drop_exception, which runs *before* release_all).
        assert cap_ki.released
