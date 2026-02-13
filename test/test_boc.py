"""Behavior-oriented concurrency tests."""

import functools
from typing import NamedTuple

from boc import Cown, receive, send, wait, when
import pytest


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
                send("report", ("full", index))


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


class TestBOC:
    """Integration-style tests for boc behaviors."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def receive_asserts(self, count=1):
        """Drain assertion messages and compare actual vs expected."""
        failed = None
        for _ in range(count):
            _, (actual, expected) = receive("assert")
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
