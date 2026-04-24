"""Dining philosophers demo using cowns and message passing."""

import argparse
import logging
from typing import NamedTuple

from bocpy import Cown, receive, send, wait, when


class Fork:
    """A fork that tracks how many times it is used."""

    def __init__(self, hunger: int):
        """Create a fork with an associated philosopher hunger counter."""
        self.hunger = hunger
        self.uses = 0

    def use(self):
        """Increment the usage counter when a philosopher eats."""
        self.uses += 1


class Philosopher(NamedTuple("Philosopher", [("index", int), ("left", Cown),
                                             ("right", Cown), ("hunger", Cown)])):
    """Philosopher that coordinates access to two forks."""

    def eat(self: "Philosopher"):
        """Attempt to eat; reschedule until hunger reaches zero."""
        index = self.index

        @when(self.left, self.right, self.hunger)
        def take_bite(left: Cown[Fork], right: Cown[Fork], hunger: Cown[int]):
            left.value.use()
            right.value.use()
            send("report", ("bite", index))
            hunger.value -= 1
            if hunger.value > 0:
                Philosopher(index, left, right, hunger).eat()
            else:
                # send the report after the forks have been released
                @when()
                def _():
                    send("report", ("full", index))


def main():
    """Run the dining philosophers example."""
    parser = argparse.ArgumentParser("Dining Philosophers")
    parser.add_argument("--hunger", "-g", type=int, default=4)
    parser.add_argument("--philosophers", "-p", type=int, default=5)
    parser.add_argument("--loglevel", "-l", type=str, default=logging.WARNING)
    args = parser.parse_args()

    logging.basicConfig(level=args.loglevel)
    forks = [Cown(Fork(args.hunger)) for _ in range(args.philosophers)]
    for i in range(args.philosophers):
        Philosopher.eat(Philosopher(i, forks[i-1], forks[i], Cown(args.hunger)))

    num_eating = args.philosophers
    while num_eating > 0:
        match receive("report"):
            case ["report", ("bite", index)]:
                print(f"Philosopher {index} has taken a bite")

            case ["report", ("full", index)]:
                print(f"Philosopher {index} is full")
                num_eating -= 1

    for i, f in enumerate(forks):
        with f as fork:
            print(f"Fork {i}: uses={fork.uses}, hunger={fork.hunger}")

    wait()


if __name__ == "__main__":
    main()
