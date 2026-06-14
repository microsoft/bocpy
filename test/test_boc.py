"""Behavior-oriented concurrency tests."""

from dataclasses import dataclass
import functools
import random
import sys
import threading
import time
import traceback

import pytest

from bocpy import (Cown, drain, notice_write,
                   quiesce, receive, send, start, TIMEOUT, wait, when)
from bocpy._core import CownCapsule
from bocpy_test import collide_a, collide_b, dunders
from bocpy_test.chained_global import schedule_chain, schedule_direct
from bocpy_test.multiplier import GLOBAL_FACTOR, Multiplier
from bocpy_test.philosophers import Fork, Philosopher

RECEIVE_TIMEOUT = 10

QUIESCE_TIMEOUT = 5


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


class Accumulator:
    """Simple list-based accumulator for testing."""

    def __init__(self):
        """Initialize with an empty item list."""
        self.items = []

    def add(self, item):
        """Append an item to the list."""
        self.items.append(item)


class Cell:
    """Mutable two-field container used by the cown-in-cown tests.

    A ``Cell`` is stored as a cown value and gives the test a place to
    hang an inner ``Cown`` off the outer cown without reaching for a
    bare dict (which exercises a different pickle path).
    """

    def __init__(self, key):
        """Initialise the cell with a marker key and an empty child slot."""
        self.key = key
        self.child = None


@dataclass(slots=True)
class DataClassSlots:
    """``@dataclass(slots=True)`` wrapper around a single Cown field."""

    c: object


class DictOnly:
    """``__dict__``-only wrapper around a single Cown field."""

    def __init__(self, c):
        """Stash the cown on the instance ``__dict__``."""
        self.c = c


class SlotsOnly:
    """``__slots__``-only wrapper around a single Cown field."""

    __slots__ = ("c",)

    def __init__(self, c):
        """Stash the cown on the single declared slot."""
        self.c = c


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

        quiesce(QUIESCE_TIMEOUT)
        assert y.unwrap() == 2

    def test_nested_dispatch(self):
        """Ensure nested behaviors see updated state."""
        x = Cown(1)
        nested(x)

        quiesce(QUIESCE_TIMEOUT)
        assert x.unwrap() == 6

    def test_exception(self):
        """Exceptions propagate as values in behaviors."""
        x = Cown(1)
        y = exception(x)

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(ZeroDivisionError):
            y.unwrap()

    def test_unwrap_consumes_value(self):
        """unwrap() consumes the cown: a second unwrap returns None."""
        x = Cown(1)
        y = simple(x)

        quiesce(QUIESCE_TIMEOUT)
        assert y.unwrap() == 2
        assert y.unwrap() is None

    def test_two_cown_coordination(self):
        """Move value between two cowns with coordinated when."""
        x = Cown(100)
        y = Cown(0)

        def read(c: Cown):
            @when(c)
            def do_read(c):
                return c.value

            return do_read

        x_before = read(x)
        y_before = read(y)

        @when(x, y)
        def _(x, y):
            y.value += 50
            x.value -= 50

        x_after = read(x)
        y_after = read(y)

        quiesce(QUIESCE_TIMEOUT)
        assert x_before.unwrap() == 100
        assert y_before.unwrap() == 0
        assert x_after.unwrap() == 50
        assert y_after.unwrap() == 50

    def test_classes(self, num_philosophers=5, hunger=4):
        """Dining philosophers from an installed package resolve on workers.

        ``Fork`` and ``Philosopher`` are defined in the ``bocpy_test``
        package, not in this test module, so a worker resolves their
        ``@when`` bodies through the imported-module branch of
        ``Resolver._target_dict`` (it imports ``bocpy_test.philosophers``
        rather than binding to the bindings module). Each fork is shared
        by two philosophers, so after every philosopher empties its
        hunger cown the fork's use count is ``2 * hunger``.
        """
        fork_cowns = [Cown(Fork(i)) for i in range(num_philosophers)]
        hunger_cowns = [Cown(hunger) for _ in range(num_philosophers)]
        for i, hunger_cown in enumerate(hunger_cowns):
            Philosopher.eat(
                Philosopher(i, fork_cowns[i-1], fork_cowns[i], hunger_cown))

        quiesce(QUIESCE_TIMEOUT)
        for f_cown, h_cown in zip(fork_cowns, hunger_cowns):
            assert f_cown.unwrap().uses == 2 * hunger
            assert h_cown.unwrap() == 0

    @pytest.mark.parametrize("n", [1, 10, 15])
    def test_variable_termination(self, n: int):
        """Compare parallel Fibonacci against sequential baseline."""
        result = fib_parallel(n)
        expected = fib_sequential(n)

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == expected

    def test_cown_grouping(self):
        """Verify cown grouping returns correct sums."""
        expected, results = cown_grouping()

        quiesce(QUIESCE_TIMEOUT)
        for r in results:
            assert r.unwrap() == expected

    def test_grouped_cown_mutation(self):
        """Write to cowns within a group and verify mutations stick."""
        cowns = [Cown(i) for i in range(5)]

        @when(cowns)
        def double_all(group: list[Cown[int]]):
            for c in group:
                c.value *= 2

        @when(cowns)
        def verify(group: list[Cown[int]]):
            return [c.value for c in group]

        quiesce(QUIESCE_TIMEOUT)
        assert verify.unwrap() == [i * 2 for i in range(5)]

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
        def read_total(t):
            return t.value

        @when(items)
        def read_items(group: list[Cown[int]]):
            return [c.value for c in group]

        quiesce(QUIESCE_TIMEOUT)
        assert read_total.unwrap() == 6
        assert read_items.unwrap() == [0, 0, 0]

    def test_behavior_chain(self):
        """Chain three behaviors where each result feeds the next."""
        x = Cown(2)

        @when(x)
        def step1(x):
            return x.value + 3

        @when(step1)
        def step2(s1):
            return s1.value * 4

        @when(step2)
        def step3(s2):
            return s2.value - 7

        quiesce(QUIESCE_TIMEOUT)
        assert step3.unwrap() == 13

    def test_contention(self):
        """Many behaviors on the same cown serialize correctly."""
        counter = Cown(0)
        n = 50

        for _ in range(n):
            @when(counter)
            def _(c):
                c.value += 1

        @when(counter)
        def read(c):
            return c.value

        quiesce(QUIESCE_TIMEOUT)
        assert read.unwrap() == n

    def test_exception_type_error(self):
        """Verify TypeError inside a behavior is captured in the result cown."""
        x = Cown("hello")

        @when(x)
        def bad(x):
            return x.value + 1

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(TypeError):
            bad.unwrap()

    def test_exception_key_error(self):
        """Verify KeyError inside a behavior is captured in the result cown."""
        x = Cown({})

        @when(x)
        def bad(x):
            return x.value["missing"]

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(KeyError):
            bad.unwrap()

    def test_complex_object_repeated_mutation(self):
        """Multiple sequential behaviors mutate the same object in a cown."""
        acc = Cown(Accumulator())

        for i in range(10):
            val_to_add = i

            @when(acc)
            def _(a, val_to_add=val_to_add):
                a.value.add(val_to_add)

        @when(acc)
        def read(a):
            return sorted(a.value.items)

        quiesce(QUIESCE_TIMEOUT)
        assert read.unwrap() == list(range(10))

    def test_duplicate_cown_same_twice(self):
        """Same cown passed twice to @when completes without deadlock."""
        c = Cown(5)

        @when(c, c)
        def add(a, b):
            return a.value + b.value

        quiesce(QUIESCE_TIMEOUT)
        assert add.unwrap() == 10

    def test_duplicate_cown_same_thrice(self):
        """Same cown passed three times to @when completes without deadlock."""
        c = Cown(3)

        @when(c, c, c)
        def triple(a, b, d):
            return a.value + b.value + d.value

        quiesce(QUIESCE_TIMEOUT)
        assert triple.unwrap() == 9

    def test_duplicate_cown_non_adjacent(self):
        """Non-adjacent duplicate cowns in @when complete correctly."""
        a = Cown(10)
        b = Cown(20)

        @when(a, b, a)
        def mixed(x, y, z):
            return x.value + y.value + z.value

        quiesce(QUIESCE_TIMEOUT)
        assert mixed.unwrap() == 40

    def test_duplicate_cown_in_group(self):
        """Duplicate cowns within a group complete without deadlock."""
        c = Cown(7)

        @when([c, c])
        def group_sum(group):
            return sum(g.value for g in group)

        quiesce(QUIESCE_TIMEOUT)
        assert group_sum.unwrap() == 14

    def test_duplicate_cown_mutation(self):
        """Mutating a cown passed twice reflects same underlying value."""
        c = Cown(1)

        @when(c, c)
        def mutate(a, b):
            a.value = 42
            return b.value

        quiesce(QUIESCE_TIMEOUT)
        assert mutate.unwrap() == 42

    def test_cown_of_cown_direct(self):
        """CownCapsule as direct child of a Cown survives release/acquire."""
        inner = Cown(42)
        outer = Cown(inner)

        @when(outer)
        def read_outer(o):
            return type(o.value).__name__

        quiesce(QUIESCE_TIMEOUT)
        assert read_outer.unwrap() == "Cown"

    def test_cown_of_cown_access_inner(self):
        """Inner cown's value is accessible after outer round-trip."""
        inner = Cown(99)
        outer = Cown(inner)

        @when(outer, inner)
        def check_both(o, i):
            return i.value

        quiesce(QUIESCE_TIMEOUT)
        assert check_both.unwrap() == 99

    def test_cown_of_cown_in_container(self):
        """CownCapsule nested in a dict survives pickle round-trip."""
        inner = Cown(7)
        outer = Cown({"key": inner})

        @when(outer)
        def check_container(o):
            return type(o.value["key"]).__name__

        quiesce(QUIESCE_TIMEOUT)
        assert check_container.unwrap() == "Cown"

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
                return i.value

            return read_inner

        quiesce(QUIESCE_TIMEOUT)
        assert schedule_on_inner.unwrap().unwrap() == 10


class TestUnwrap:
    """Cown.unwrap() under the quiesce() result-reading scheme."""

    @classmethod
    def teardown_class(cls):
        wait()

    def test_unwrap_returns_value(self):
        """unwrap() returns a worker-produced value after quiesce()."""
        x = Cown(3)
        y = simple(x)

        quiesce(QUIESCE_TIMEOUT)
        assert y.unwrap() == 6

    def test_unwrap_reraises_behavior_exception(self):
        """unwrap() re-raises a captured exception verbatim on the caller."""
        x = Cown(1)

        @when(x)
        def boom(x):
            # A bare ``assert`` would be pytest-rewritten to reference the
            # module-global ``@pytest_ar`` helper, which the marshalled
            # code object carries but the worker namespace lacks; raise
            # explicitly so the body is self-contained across interpreters.
            if x.value != 999:
                raise AssertionError(f"expected 999, got {x.value}")

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(AssertionError, match="expected 999, got 1"):
            boom.unwrap()

    def test_unwrap_clears_exception_after_consuming(self):
        """A consumed exception is cleared: a second unwrap() returns None."""
        x = Cown(1)

        @when(x)
        def boom(x):
            raise ValueError("once")

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(ValueError, match="once"):
            boom.unwrap()
        assert boom.unwrap() is None

    def test_unwrap_returned_exception_is_a_value(self):
        """An Exception *returned* (not raised) is a value, so unwrap() returns it."""
        x = Cown(1)

        @when(x)
        def returns_exc(x):
            return ValueError("just a value")

        quiesce(QUIESCE_TIMEOUT)
        result = returns_exc.unwrap()
        assert isinstance(result, ValueError)
        assert str(result) == "just a value"

    def test_unwrap_in_flight_raises(self):
        """unwrap() before quiesce(), while work is in flight, raises RuntimeError."""
        x = Cown(0)

        @when(x)
        def slow(x):
            time.sleep(0.2)
            return x.value + 1

        with pytest.raises(RuntimeError, match="still in flight"):
            slow.unwrap()

        quiesce(QUIESCE_TIMEOUT)
        assert slow.unwrap() == 1

    def test_unwrap_rejects_last_behavior_in_seed_dropped_window(self):
        """unwrap() rejects an in-flight behavior even while the seed is dropped.

        During quiesce()/wait() the Pyrona seed is dropped, so a single
        in-flight behavior leaves ``terminator_count == 1`` -- the same
        value as a fully quiesced, seed-armed runtime. The guard keys
        off ``count - seeded`` rather than ``count > 1`` so it still
        rejects this case. Simulated by poking the terminator into the
        seed-dropped + one-hold state on the primary interpreter.
        """
        from bocpy import _core

        x = Cown(0)
        result = simple(x)
        quiesce(QUIESCE_TIMEOUT)

        seed_dropped = _core.terminator_seed_dec()
        held = _core.terminator_inc() >= 0
        try:
            assert _core.terminator_count() == 1
            assert _core.terminator_seeded() == 0
            with pytest.raises(RuntimeError, match="still in flight"):
                result.unwrap()
        finally:
            if held:
                _core.terminator_dec()
            if seed_dropped:
                _core.terminator_seed_inc()

        assert result.unwrap() == 0


class TestGlobalCapture:
    """Capturing a module-level global from an imported package's method.

    ``Multiplier`` and ``GLOBAL_FACTOR`` live in ``bocpy_test.multiplier``,
    so a worker resolves these behaviors through the imported-module
    branch of ``Resolver._target_dict`` and binds their globals to that
    module. ``multiply_direct`` reads ``GLOBAL_FACTOR`` as a free global,
    proving the value resolves against the behavior's defining module
    across the interpreter boundary -- not against the runtime's bindings
    module.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_method_captures_global_via_local(self):
        """A method assigns a global to a local; @when captures the local."""
        m = Multiplier()
        x = Cown(5)
        result = m.multiply(x)

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == 5 * GLOBAL_FACTOR

    def test_method_captures_global_directly(self):
        """A method's @when captures a module-level global by name."""
        m = Multiplier()
        x = Cown(3)
        result = m.multiply_direct(x)

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == 3 * GLOBAL_FACTOR

    @pytest.mark.parametrize("value", [1, 10, 100])
    def test_method_captures_global_parametrized(self, value):
        """Parametrized: global capture from a method works across inputs."""
        m = Multiplier()
        x = Cown(value)
        result = m.multiply_direct(x)

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == value * GLOBAL_FACTOR


class TestNonConstantModuleGlobal:
    """A non-constant module global resolves for behaviors that read it.

    Regression: the worker bindings reducer keeps imports, classes,
    functions, and UPPERCASE constants but drops lowercase,
    expression-valued module globals (``module_cache = dict(...)`` in
    ``bocpy_test.chained_global``, mirroring the physics
    ``shell_cache = geometry.ShellCache()``). A behavior defined in an
    importable module must bind its globals to that module's *real*
    namespace -- never to a reduced bindings copy -- so the global is
    present. ``schedule_chain`` also proves a *chained* behavior (one
    scheduled from inside another) resolves the same way, which is the
    exact shape that surfaced as ``NameError: name 'shell_cache' is not
    defined``.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_direct_behavior_reads_non_constant_global(self):
        """A single behavior reads its lowercase, expression-valued global."""
        x = Cown(1)
        result = schedule_direct(x)

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == "module-level-value"

    def test_chained_behavior_reads_non_constant_global(self):
        """Both links of an in-module behavior chain read the dropped global."""
        c1 = Cown(1)
        c2 = Cown(2)
        first = schedule_chain(c1, c2)

        quiesce(QUIESCE_TIMEOUT)
        # first holds the second behavior's result cown; unwrap twice.
        assert first.unwrap().unwrap() == (
            "module-level-value", "module-level-value")


class TestCrossModuleIdenticalBody:
    """Byte-identical bodies in two packages resolve to their own module.

    ``collide_a`` and ``collide_b`` schedule behaviors whose bodies are
    byte-identical and differ only in each module's ``OFFSET`` global.
    The canonical behavior key excludes ``co_filename``, so the two
    bodies share the code-identity half of their keys; only the folded-in
    defining module separates them. If that fold were missing, the
    process-global append-only registry would resolve the second body's
    ``OFFSET`` against the first body's module, and both would return the
    same value. Running both on workers and asserting divergent results
    is the end-to-end regression guard for that collision.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_identical_bodies_bind_to_own_module_globals(self):
        """Two identical bodies read their own module's OFFSET on a worker."""
        a = collide_a.schedule_probe(Cown(5))
        b = collide_b.schedule_probe(Cown(5))

        quiesce(QUIESCE_TIMEOUT)
        assert a.unwrap() == 5 + collide_a.OFFSET
        assert b.unwrap() == 5 + collide_b.OFFSET
        assert collide_a.OFFSET != collide_b.OFFSET


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
            return b.value

        @when(use_bad)
        def check(b):
            return (b.exception, isinstance(b.value, ZeroDivisionError))

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == (True, True)


class TestCownInCown:
    """Regression coverage for the cown-in-cown use-after-free.

    A ``CownCapsule`` embedded inside another cown's value, a message
    queue payload, or a noticeboard snapshot does not inherently keep
    its inner ``BOCCown`` alive: pickle bytes are dead data. Without
    either an inheriting ``COWN_INCREF`` in ``CownCapsule_reduce`` or
    a borrowing reconstructor backed by a noticeboard pin, the inner
    ``BOCCown`` is freed when the writer's Python wrapper drops, and
    the next consumer (downstream acquire, queue receiver, or
    noticeboard reader) dereferences a dangling pointer and segfaults.
    A regression that breaks the contract will fail the tests below;
    in the worst case it will crash a worker and take pytest down
    with it — louder than a silent miscompare.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_cown_created_in_behavior_survives_release(self):
        """Inner Cown created inside a behavior must outlive its writer.

        Behavior ``a`` allocates ``Cown(Cell(20))`` and stashes it on
        ``c1.value.child``. Behavior ``b`` observes ``c1.value.child``
        and schedules ``c`` against it. Without the inheriting
        ``COWN_INCREF``, the inner cown is freed when ``a`` returns
        and ``b``'s acquire dereferences a dangling pointer.
        """
        c1 = Cown(Cell(10))

        @when(c1)
        def a(c1):
            c1.value.child = Cown(Cell(20))

        @when(c1)
        def b(c1):
            c2 = c1.value.child

            @when(c1, c2)
            def c(c1, c2):
                return c2.value.key

            return c

        quiesce(QUIESCE_TIMEOUT)
        assert b.unwrap().unwrap() == 20

    def test_cown_chain_through_message_queue(self):
        """Cown sent via message queue must survive sender's release.

        The sender behavior allocates a ``Cown(42)``, hands it to
        ``send`` and returns. Its local ``Cown`` wrapper is dropped.
        The receiver behavior pops the message, schedules a
        ``@when`` against the received cown and reads its value.
        Without the inheriting ``COWN_INCREF``, the receiver decodes
        a dangling ``BOCCown*`` from the queued xidata and UAFs on
        acquire.

        Uses a dedicated ``"cown_chain"`` tag so the payload does
        not collide with the project-wide ``"assert"`` queue.
        """
        anchor = Cown(0)

        @when(anchor)
        def sender(a):
            inner = Cown(42)
            send("cown_chain", inner)

        @when(anchor)
        def receiver(a):
            tag, payload = receive("cown_chain", RECEIVE_TIMEOUT)
            assert tag != TIMEOUT, "receive timed out"

            @when(payload)
            def use(c):
                return c.value

            return use

        quiesce(QUIESCE_TIMEOUT)
        assert receiver.unwrap().unwrap() == 42
        drain("cown_chain")

    def test_cown_of_cown_fuzz_container_shapes(self):
        """Fuzz over container shapes that embed a Cown.

        Runs 50 trials with ``random.Random(0xC0C0)`` so the
        sequence is deterministic but mixes shapes. Each trial
        constructs an inner ``Cown(expected)`` wrapped inside one
        of seven container shapes:

        * ``list[Cown]``
        * ``tuple[Cown]``
        * ``dict[str, Cown]`` (cown-as-value)
        * ``@dataclass(slots=True)`` wrapper
        * ``__dict__``-only instance
        * ``__slots__``-only instance
        * 2-level ``Cown[Cown[T]]`` chain

        Every trial returns its leaf value through a result-cown chain;
        after :func:`quiesce` each leaf is read with :meth:`Cown.unwrap`.
        The 2-level ``Cown[Cown[T]]`` shape adds one extra result-cown
        layer, so its leaf is one ``unwrap()`` deeper.
        """
        n_trials = 50
        n_shapes = 7
        rng = random.Random(0xC0C0)
        # The last shape index is the Cown(inner) deep case (the else branch
        # below); keep them in sync if shapes are reordered.
        deep_shape = n_shapes - 1

        results = []
        for trial in range(n_trials):
            shape = rng.randrange(n_shapes)
            expected = trial * 1000 + 17
            outer = Cown(None)

            @when(outer)
            def make(o, expected=expected, shape=shape):
                inner = Cown(expected)
                if shape == 0:
                    o.value = [inner]
                elif shape == 1:
                    o.value = (inner,)
                elif shape == 2:
                    o.value = {"k": inner}
                elif shape == 3:
                    o.value = DataClassSlots(inner)
                elif shape == 4:
                    o.value = DictOnly(inner)
                elif shape == 5:
                    o.value = SlotsOnly(inner)
                else:
                    o.value = Cown(inner)

            if shape == deep_shape:
                @when(outer)
                def verify_deep(o):
                    wrapping = o.value

                    @when(wrapping)
                    def peel(wc):
                        leaf = wc.value

                        @when(leaf)
                        def check_nested(c):
                            return c.value

                        return check_nested

                    return peel

                results.append((shape, expected, verify_deep))
                continue

            @when(outer)
            def verify(o):
                container = o.value
                if isinstance(container, (list, tuple)):
                    inner_c = container[0]
                elif isinstance(container, dict):
                    inner_c = container["k"]
                else:
                    inner_c = container.c

                @when(inner_c)
                def check(c):
                    return c.value

                return check

            results.append((shape, expected, verify))

        quiesce(QUIESCE_TIMEOUT)
        for shape, expected, verify in results:
            leaf = verify.unwrap()
            if shape == deep_shape:
                leaf = leaf.unwrap()
            assert leaf.unwrap() == expected

    def test_cached_snapshot_survives_entry_overwrite(self):
        """Borrowing reconstructor: snapshot's CownCapsule owns its own ref.

        On the noticeboard write path, ``CownCapsule_reduce`` embeds
        the borrowing reconstructor; the reader's reconstructor takes
        its own fresh ``COWN_INCREF`` on unpickle. Each reader's
        cached snapshot therefore owns an independent strong reference
        to the inner ``BOCCown``, and the snapshot stays valid even
        after the noticeboard entry (and its ``nb_pin_cowns`` +1) is
        overwritten.
        """
        anchor = Cown(0)
        inner = Cown(20)
        stash = {}

        @when(anchor)
        def write_initial(a, inner=inner):
            notice_write("k", inner)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        stash["k"] = snap["k"]
        del snap

        del inner

        @when(anchor)
        def overwrite(a):
            notice_write("k", "unrelated_value")

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        stashed = stash["k"]

        @when(stashed)
        def check(s):
            return s.value

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == 20


class TestAcquireFailureTerminal:
    """Repeat acquire of a permanently-undeserialisable cown.

    The first behavior that fails to deserialise a cown's value sees
    the original exception (preserved through
    ``PyErr_Fetch`` / ``PyErr_Restore``). Subsequent waiters on the
    same cown receive an identical ``RuntimeError("...permanently
    unavailable...")`` because the cown can never satisfy any
    behavior again. None of the behavior bodies run; none of the
    acquires segfaults.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_three_waiters_after_decode_failure(self):
        """Three waiters: #1 sees original exception; #2 and #3 match.

        Three waiters (not two) so the asymmetry contract is pinned:
        only the first waiter sees the original exception; every
        later waiter sees an identical terminal-state
        ``RuntimeError``. Substring match keeps the test resilient
        to wording tweaks while still catching a future caching
        regression that produced different messages for repeat
        waiters.
        """
        bad = Cown(RaiseOnUnpickle())

        @when(bad)
        def use_bad_1(b):
            return b.value

        @when(bad)
        def use_bad_2(b):
            return b.value

        @when(bad)
        def use_bad_3(b):
            return b.value

        @when(use_bad_1)
        def check_1(b):
            return (b.exception, isinstance(b.value, ZeroDivisionError))

        @when(use_bad_2)
        def check_2(b):
            return (
                b.exception,
                isinstance(b.value, RuntimeError),
                "permanently unavailable" in str(b.value),
                str(b.value),
            )

        @when(use_bad_3)
        def check_3(b):
            return (
                b.exception,
                isinstance(b.value, RuntimeError),
                "permanently unavailable" in str(b.value),
                str(b.value),
            )

        quiesce(QUIESCE_TIMEOUT)

        assert check_1.unwrap() == (True, True)

        c2 = check_2.unwrap()
        c3 = check_3.unwrap()
        assert c2[:3] == (True, True, True)
        assert c3[:3] == (True, True, True)

        assert c2[3] == c3[3], (
            f"terminal-state messages diverged: {c2[3]!r} != {c3[3]!r}"
        )
        assert "permanently unavailable" in c2[3], c2[3]


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
        _start_runtime()

        result = Cown(None)
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
        _start_runtime()

        result = Cown(None)
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
            flags = (b.exception, isinstance(b.value, ZeroDivisionError))
            b.value = None
            return flags

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == (True, True)

    def test_exception_flag_on_return(self):
        """Returned Exception object has .exception False."""
        x = Cown(1)

        @when(x)
        def returns_exc(x):
            return ValueError("not an error")

        @when(returns_exc)
        def check(r):
            return (r.exception, isinstance(r.value, ValueError))

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == (False, True)

    def test_exception_flag_cleared_on_value_write(self):
        """Writing .value clears the exception flag."""
        x = Cown(1)

        @when(x)
        def bad(x):
            x.value /= 0

        @when(bad)
        def check(b):
            before = b.exception
            b.value = "fixed"
            after = b.exception
            return (before, after)

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == (True, False)

    def test_exception_flag_manual_set_clear(self):
        """Manual .exception set and clear works."""
        x = Cown(42)

        @when(x)
        def check(x):
            s0 = x.exception
            x.exception = True
            s1 = x.exception
            x.exception = False
            s2 = x.exception
            return (s0, s1, s2)

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == (False, True, False)

    def test_returned_exception_no_unhandled_report(self, capsys):
        """Returned Exception doesn't trigger unhandled exception report."""
        x = Cown(1)

        @when(x)
        def returns_exc(x):
            return ValueError("just a value")

        @when(returns_exc)
        def check(r):
            return (r.exception, isinstance(r.value, ValueError))

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == (False, True)
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
        def euro(x):
            return "€"

        quiesce(QUIESCE_TIMEOUT)
        assert euro.unwrap() == "€"


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
        def read_name(x):
            return __name__

        quiesce(QUIESCE_TIMEOUT)
        assert read_name.unwrap() == expected

    def test_package_resolves_to_user_module(self):
        """__package__ inside a behavior matches the user module's value."""
        x = Cown(0)
        expected = __package__

        @when(x)
        def read_package(x):
            return __package__

        quiesce(QUIESCE_TIMEOUT)
        assert read_package.unwrap() == expected

    def test_imported_package_behavior_reads_its_own_dunders(self):
        """An imported-package behavior sees its defining module's dunders.

        The sibling tests cover the bindings-module case (a behavior
        defined in this test module reads ``test_boc``'s dunders). Here
        the behavior is defined in ``bocpy_test.dunders`` and resolves on
        a worker through the imported-module branch, so its globals bind
        to that package module: ``__name__`` is the dotted module path
        and ``__package__`` is the containing package, not the bindings
        module's.
        """
        result = dunders.schedule_read_dunders(Cown(0))

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == ("bocpy_test.dunders", "bocpy_test")


class TestCrossWorker:
    """Verify cross-worker scheduling and cown round-trip through XIData."""

    @classmethod
    def teardown_class(cls):
        """Drain the runtime after the cross-worker probes."""
        wait()

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
        readers = []
        for c in cells:
            @when(c)
            def _tid(_c):
                deadline = time.perf_counter() + 0.01
                while time.perf_counter() < deadline:
                    pass
                return threading.get_ident()

            readers.append(_tid)

        quiesce(QUIESCE_TIMEOUT)
        thread_ids = {r.unwrap() for r in readers}

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
        probes = []
        for idx, cell in enumerate(ring):
            @when(cell)
            def _probe(c, idx=idx):
                return (idx, c)

            probes.append(_probe)

        quiesce(QUIESCE_TIMEOUT)
        for p in probes:
            probe_idx, probe_cown = p.unwrap()
            seen[probe_idx] = probe_cown

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
        """A raising behavior's traceback points at its real source line.

        Under the marshalled-code registry an importable behavior keeps
        its original ``co_filename``, so a worker-side traceback resolves
        against the real module file on disk via ``linecache`` — pointing
        the user at the actual source line rather than a synthetic name.
        (Interactive behaviors, which have no source file, are relabelled
        to ``<behavior:KEY>`` instead; that path is covered separately.)
        """
        c = Cown(0)
        start(worker_count=2)

        @when(c)
        def _b(c):
            try:
                raise RuntimeError("synthetic-from-test-traceback")
            except RuntimeError:
                return traceback.format_exc()

        quiesce(QUIESCE_TIMEOUT)
        tb_str = _b.unwrap()

        assert __file__ in tb_str, (
            f"traceback did not reference the real source file; got:\n{tb_str}"
        )
        assert 'raise RuntimeError("synthetic-from-test-traceback")' in tb_str, (
            f"traceback did not show the real source line; got:\n{tb_str}"
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

        @when(c)
        def _tricky(c):
            return (
                "héllo",
                'mix "single" and \'double\' quotes',
                """triple-quoted with embedded "quote" and 'apostrophe'""",
                r"raw \n not a newline",
                'back\\slash and "escaped quote"',
                "emoji \U0001F600 in literal",
                "with\x00nul",
            )

        quiesce(QUIESCE_TIMEOUT)
        assert _tricky.unwrap() == (
            "héllo",
            'mix "single" and \'double\' quotes',
            """triple-quoted with embedded "quote" and 'apostrophe'""",
            r"raw \n not a newline",
            'back\\slash and "escaped quote"',
            "emoji \U0001F600 in literal",
            "with\x00nul",
        ), "payload round-trip mismatch"

    def test_module_name_with_quote_rejected(self):
        """``module_name`` containing a double-quote is rejected at start().

        Defence in depth: even though every interpolation now uses
        ``repr()``, ``Behaviors.start`` validates ``module_name`` is
        a dotted Python module path before building the bootstrap
        snippet. A name with a quote would ``repr()`` cleanly but
        is still nonsensical and the boundary check refuses it with
        a ``ValueError``.
        """
        from bocpy import behaviors as _behaviors

        wait()
        b = _behaviors.Behaviors(2)
        with pytest.raises(ValueError, match="dotted Python module path"):
            b.start(module=('a"b', __file__))


class TestTimeoutValidation:
    """Boundary validation for wait timeouts.

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

    def test_wait_inf_timeout_blocks_until_done(self):
        """``+Inf`` timeout treats wait as "wait forever" and returns once done.

        With no live behaviors the terminator count is already 0, so
        ``terminator_wait(+Inf)`` returns ``True`` immediately rather
        than blocking. The point is that it does *not* raise.
        """
        from bocpy import _core
        assert _core.terminator_wait(float("inf")) is True

    def test_terminator_wait_negative_timeout_returns_immediately(self):
        """Negative timeout to ``_core.terminator_wait`` is mapped to wait_forever.

        bocpy treats negatives as "wait forever"; the timeout validator
        preserves that for negatives and upgrades only NaN to a hard
        error. With no live runtime the terminator
        is already at 0, so this returns immediately either way.
        """
        from bocpy import _core
        assert _core.terminator_wait(-1.0) is True


class TestBaseExceptionDiscipline:
    """A ``BaseException`` escaping a @when body still releases the cown.

    The worker's per-behavior cleanup and the orphan-drain loop in
    ``behaviors.py`` must release cowns in a ``finally``, not only
    under ``except Exception``. ``KeyboardInterrupt`` and
    ``SystemExit`` derive from ``BaseException`` (not ``Exception``),
    so an ``except Exception`` arm lets them escape past the
    per-iteration cleanup: the MCS chain stays linked, the cown stays
    owned, and every successor on it strands.

    Note: these tests *explicitly* ``raise KeyboardInterrupt`` inside
    the body — they do not simulate a Ctrl-C / SIGINT. A signal-driven
    ``KeyboardInterrupt`` can only ever surface on the main thread of
    the main interpreter, never inside a worker sub-interpreter;
    ``KeyboardInterrupt`` is used here purely as the canonical
    non-``Exception`` ``BaseException`` to drive the cleanup path.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_base_exception_from_worker_body_releases_cown(self):
        """An explicitly-raised ``BaseException`` releases the cown.

        Schedules a behavior that does ``raise KeyboardInterrupt`` (a
        ``BaseException``, not an ``Exception``), then a follow-on
        behavior on the same cown. If the worker's ``finally``-based
        release / release_all chain is wired correctly, the cown is
        released even though the escaping exception is not an
        ``Exception``, so the follow-on runs and ``_follow.unwrap()``
        returns its value. If cleanup were gated on ``except
        Exception`` the cown would stay owned and ``quiesce`` would
        time out.

        The worker captures the escaped ``BaseException`` onto
        ``_raise``'s result cown, so ``_raise.unwrap()`` re-raises it;
        consuming it here also keeps it from being reported as
        unhandled at teardown.
        """
        wait()
        start(worker_count=2)

        c = Cown(0)

        @when(c)
        def _raise(c):
            raise KeyboardInterrupt("intentional KI")

        @when(c)
        def _follow(c):
            return "ok"

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(KeyboardInterrupt, match="intentional KI"):
            _raise.unwrap()
        assert _follow.unwrap() == "ok", (
            "follow-on never ran -- cown was not released after the "
            "BaseException escaped the body"
        )

    def test_keyboard_interrupt_during_orphan_drain_completes_drain(self):
        """KI mid-drain still drains the remaining orphans.

        Patches ``BehaviorCapsule.set_drop_exception`` so the first
        orphan raises ``KeyboardInterrupt`` (mimicking a Ctrl-C landing
        inside the drain loop). The drain must finish the remaining
        orphans before the deferred KI is re-raised, so no MCS chain or
        terminator hold leaks.
        """
        from mockreplacement import patch_attr

        from bocpy import behaviors as _behaviors

        wait()
        b = _behaviors.Behaviors(2)

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

        drain_returns = [[cap_ki, cap_ok], []]

        def fake_drain():
            return drain_returns.pop(0) if drain_returns else []

        original_set_drop = FakeCapsule.set_drop_exception

        def patched_set_drop(self, exc):
            if self is cap_ki:
                raise KeyboardInterrupt("orphan-drain KI")
            return original_set_drop(self, exc)

        import bocpy._core as _core_mod

        def _fake_terminator_dec(*args, **kwargs):
            return 0

        with patch_attr(FakeCapsule, "set_drop_exception",
                        patched_set_drop), \
             patch_attr(_core_mod, "scheduler_drain_all_queues",
                        fake_drain), \
             patch_attr(_core_mod, "terminator_dec",
                        _fake_terminator_dec):
            with pytest.raises(KeyboardInterrupt, match="orphan-drain KI"):
                b._drain_orphan_behaviors()

        assert cap_ok.released, (
            "second orphan was not drained -- KI aborted the loop"
        )
        assert cap_ki.released


def add_one(fn):
    """Module-level decorator that adds 1 to the return value."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs) + 1
    return wrapper


def times_two(fn):
    """Module-level decorator that multiplies the return value by 2."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs) * 2
    return wrapper


class TestDecoratorComposition:
    """Decorators below @when should compose with the behavior body.

    All three tests are ``xfail`` under the marshalled-code registry:
    a below-`@when` decorator returns a wrapper that closes over the
    undecorated function (``co_freevars`` non-empty) and typically takes
    ``*args``/``**kwargs``, neither of which survives marshalling across
    the sub-interpreter boundary. Re-introducing composition needs a
    dedicated design: marshal the undecorated body plus an importable
    decorator chain to re-apply on the worker. Until that lands, stacking
    decorators below ``@when`` is unsupported.
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    @pytest.mark.xfail(
        reason="below-@when decorator composition not supported under the "
        "marshalled-code registry (the wrapper closes over the undecorated "
        "body, which cannot be marshalled across interpreters)",
        strict=True,
    )
    def test_decorator_modifies_return_value(self):
        x = Cown(10)

        @when(x)
        @add_one
        def doubled_plus_one(x):
            return x.value * 2

        quiesce(QUIESCE_TIMEOUT)
        assert doubled_plus_one.unwrap() == 21

    @pytest.mark.xfail(
        reason="below-@when decorator composition not supported under the "
        "marshalled-code registry (the wrapper closes over the undecorated "
        "body, which cannot be marshalled across interpreters)",
        strict=True,
    )
    def test_stacked_below_decorators_apply_in_order(self):
        """Stacked below-decorators compose innermost-first on the worker.

        ``@times_two @add_one def f(x): return x.value`` should compute
        ``(x + 1) * 2`` because ``add_one`` wraps the body first, then
        ``times_two`` wraps the result.
        """
        x = Cown(10)

        @when(x)
        @times_two
        @add_one
        def composed(x):
            return x.value

        quiesce(QUIESCE_TIMEOUT)
        assert composed.unwrap() == 22

    @pytest.mark.xfail(
        reason="below-@when decorator composition not supported under the "
        "marshalled-code registry (the wrapper closes over the undecorated "
        "body, which cannot be marshalled across interpreters)",
        strict=True,
    )
    def test_below_decorator_inside_nested_when(self):
        """A nested ``@when`` body may itself carry a below-decorator."""
        x = Cown(10)
        y = Cown(7)

        @when(x)
        def outer(x, y=y):
            @when(y)
            @add_one
            def inner(y):
                return y.value

            return inner

        quiesce(QUIESCE_TIMEOUT)
        assert outer.unwrap().unwrap() == 8


class TestLoopDefaultCapture:
    """``def b(c, i=i)`` — canonical Python loop-snapshot idiom for @when."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_loop_default_captures_per_iteration_value(self):
        """``i=i`` captures the loop value at schedule time, not at execution."""
        c = Cown(0)
        readers = []
        for i in range(4):
            @when(c)
            def read(c, i=i):
                return i

            readers.append(read)

        quiesce(QUIESCE_TIMEOUT)
        for idx, r in enumerate(readers):
            assert r.unwrap() == idx

    def test_rename_default_binds_into_param(self):
        """``def b(c, x=y)`` — capture ``y`` from caller, bind into ``x``."""
        c = Cown(0)
        y = 99

        @when(c)
        def read(c, x=y):
            return x

        quiesce(QUIESCE_TIMEOUT)
        assert read.unwrap() == 99


class TestGetCallerModule:
    """``get_caller_module`` degrades gracefully on exotic frames.

    The two-hop frame walk reads the scheduling caller's ``__name__``
    and ``__file__``. A frame that lacks either (``exec`` against bare
    globals, a stdin/``-c`` ``__main__``) must not raise; ``__name__``
    falls back to ``"__main__"`` and a non-file ``__file__`` to ``None``
    so the runtime takes the sourceless path.
    """

    @staticmethod
    def _call_from(module_globals):
        # get_caller_module walks ``f_back.f_back``; build two nested
        # frames so ``outer``'s globals (``module_globals``) are what it
        # reads.
        from bocpy.behaviors import get_caller_module
        src = ("def inner():\n"
               "    return get_caller_module()\n"
               "def outer():\n"
               "    return inner()\n")
        g = dict(module_globals)
        g["get_caller_module"] = get_caller_module
        exec(compile(src, "<exotic>", "exec"), g)
        return g["outer"]()

    def test_missing_name_falls_back_to_main(self):
        name, file = self._call_from({})
        assert name == "__main__"
        assert file is None

    def test_non_file_path_normalises_to_none(self):
        name, file = self._call_from(
            {"__name__": "weird", "__file__": "<stdin>"})
        assert name == "weird"
        assert file is None


class TestSourcelessMainBehaviors:
    """End-to-end behaviors defined in a ``__main__`` with no source file.

    Covers the REPL / ``python -c`` / piped-stdin case where ``__main__``
    has no readable file on disk, so the runtime reduces the live
    namespace to its imports instead of parsing a file. A behavior
    referencing an imported module must still resolve that module on a
    worker.
    """

    def _run(self, args, stdin=None):
        import subprocess
        result = subprocess.run(
            [sys.executable, *args],
            input=stdin,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"subprocess failed:\nSTDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )
        return result.stdout

    def test_dash_c_behavior_using_imported_module(self):
        """``python -c`` (no ``__file__``) runs a behavior using ``math``."""
        program = (
            "import math\n"
            "from bocpy import Cown, when, wait\n"
            "c = Cown(9)\n"
            "@when(c)\n"
            "def root(c):\n"
            "    return math.sqrt(c.value)\n"
            "wait()\n"
            "print('RESULT', root.unwrap())\n"
        )
        out = self._run(["-c", program])
        assert "RESULT 3.0" in out

    def test_piped_stdin_behavior_using_imported_module(self):
        """Piped-stdin REPL (``__file__ == '<stdin>'``) runs a behavior."""
        program = (
            "import math\n"
            "from bocpy import Cown, when, wait\n"
            "c = Cown(21)\n"
            "@when(c)\n"
            "def dbl(c): return math.floor(c.value * 2.5)\n"
            "\n"
            "wait()\n"
            "print('RESULT', dbl.unwrap())\n"
        )
        out = self._run([], stdin=program)
        assert "RESULT 52" in out

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="readline is not part of the Windows stdlib",
    )
    def test_namespace_with_denied_repl_module(self):
        """A denied REPL noise module is dropped, not propagated to a worker.

        An interactive session injects modules the user never imported
        (``readline``), and on pre-3.12 shared-GIL sub-interpreters
        importing ``readline`` succeeds and blocks on the controlling
        terminal. The synthesized bindings deny such modules outright, so
        a behavior still resolves its real dependency (``math``).
        """
        program = (
            "import readline\n"
            "import math\n"
            "from bocpy import Cown, when, wait\n"
            "c = Cown(36)\n"
            "@when(c)\n"
            "def root(c):\n"
            "    return math.sqrt(c.value)\n"
            "wait()\n"
            "print('RESULT', root.unwrap())\n"
        )
        out = self._run(["-c", program])
        assert "RESULT 6.0" in out
