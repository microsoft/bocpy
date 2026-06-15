"""Dining-philosophers behavior fixtures for the bocpy test suite.

``Fork`` and ``Philosopher`` mirror ``examples/dining_philosophers.py``.
They live in the installed ``bocpy_test`` package (not under ``test/``)
so their ``@when`` bodies are importable on every worker
sub-interpreter, exercising the imported-module branch of
``Resolver._target_dict``. A ``test/`` helper would only be on pytest's
main-interpreter ``sys.path`` and would fail to resolve on a worker.
"""

from typing import NamedTuple

from bocpy import Cown, when


class Fork:
    """A fork that tracks how many times it is used."""

    def __init__(self, index: int):
        """Create a fork with an associated philosopher use counter."""
        self.index = index
        self.uses = 0

    def use(self):
        """Increment the usage counter when a philosopher eats."""
        self.uses += 1


class Philosopher(NamedTuple("Philosopher", [("index", int), ("left", Cown[Fork]),
                                             ("right", Cown[Fork]), ("hunger", Cown[int])])):
    """Philosopher that coordinates access to two forks."""

    def eat(self: "Philosopher"):
        """Attempt to eat; reschedule until hunger reaches zero."""
        index = self.index

        # BOC acquires both forks atomically in cown-id order, so the classic deadlock cannot occur.
        @when(self.left, self.right, self.hunger)
        def take_bite(left: Cown[Fork], right: Cown[Fork], hunger: Cown[int],
                      index=index):
            left.value.use()
            right.value.use()
            print(f"Philosopher {index} has taken a bite")
            hunger.value -= 1
            if hunger.value > 0:
                Philosopher(index, left, right, hunger).eat()
            else:
                print(f"Philosopher {index} is full")
