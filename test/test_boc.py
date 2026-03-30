"""Behavior-oriented concurrency tests."""

import functools
from typing import NamedTuple

from bocpy import Cown, receive, send, TIMEOUT, wait, when
import pytest


RECEIVE_TIMEOUT = 10


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

    def receive_asserts(self, count=1):
        """Drain assertion messages and compare actual vs expected.

        Uses a timeout so that if a behavior never fires (e.g. due to a
        parameter-count mismatch in @when) the test fails quickly instead
        of hanging forever.
        """
        failed = None
        for _ in range(count):
            result = receive("assert", RECEIVE_TIMEOUT)
            assert result[0] != TIMEOUT, (
                "Timed out waiting for an 'assert' message from a behavior. "
                "Check that every @when arg count matches the decorated "
                "function's parameter count."
            )
            _, (actual, expected) = result
            if actual != expected:
                failed = (actual, expected)

        if failed is not None:
            assert failed[0] != failed[1]

    def test_simple_dispatch(self):
        """Verify single when schedules and returns doubled value."""
        x = Cown(1)
        y = simple(x)
        assert isinstance(y, Cown)

        @when(y)
        def _(y):
            send("assert", (y.value, 2))

        self.receive_asserts()

    def test_nested_dispatch(self):
        """Ensure nested behaviors see updated state."""
        x = Cown(1)
        y = nested(x)

        @when(x, y)
        def check_double(x, y):
            send("assert", (x.value, 2))

            @when(x, y.value)
            def check_triple(x, y):
                send("assert", (x.value, 6))

        self.receive_asserts(2)

    def test_exception(self):
        """Exceptions propagate as values in behaviors."""
        x = Cown(1)
        y = exception(x)

        @when(y)
        def _(y):
            send("assert", (isinstance(y.value, ZeroDivisionError), True))
            y.value = None

        self.receive_asserts()

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

        self.receive_asserts(4)

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

        self.receive_asserts(num_philosophers)

    @pytest.mark.parametrize("n", [1, 10, 15])
    def test_variable_termination(self, n: int):
        """Compare parallel Fibonacci against sequential baseline."""
        result = fib_parallel(n)
        expected = fib_sequential(n)

        @when(result)
        def check(result):
            send("assert", (result.value, expected))

        self.receive_asserts()

    def test_cown_grouping(self):
        """Verify cown grouping returns correct sums."""
        expected, results = cown_grouping()

        @when(results)
        def check(results: list[Cown]):
            for r in results:
                send("assert", (r.value, expected))

        self.receive_asserts(len(results))

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

        self.receive_asserts(5)

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

        self.receive_asserts(4)

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

        self.receive_asserts()

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

        self.receive_asserts()

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

        self.receive_asserts()

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

        self.receive_asserts()

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

        self.receive_asserts()

    def test_duplicate_cown_same_twice(self):
        """Same cown passed twice to @when completes without deadlock."""
        c = Cown(5)

        @when(c, c)
        def add(a, b):
            return a.value + b.value

        @when(add)
        def check(r):
            send("assert", (r.value, 10))

        self.receive_asserts()

    def test_duplicate_cown_same_thrice(self):
        """Same cown passed three times to @when completes without deadlock."""
        c = Cown(3)

        @when(c, c, c)
        def triple(a, b, d):
            return a.value + b.value + d.value

        @when(triple)
        def check(r):
            send("assert", (r.value, 9))

        self.receive_asserts()

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

        self.receive_asserts()

    def test_duplicate_cown_in_group(self):
        """Duplicate cowns within a group complete without deadlock."""
        c = Cown(7)

        @when([c, c])
        def group_sum(group):
            return sum(g.value for g in group)

        @when(group_sum)
        def check(r):
            send("assert", (r.value, 14))

        self.receive_asserts()

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

        self.receive_asserts()
