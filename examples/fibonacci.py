"""Fibonacci computed sequentially and with cowns for comparison."""

import argparse
import functools
import logging

from bocpy import Cown, wait, when


@functools.lru_cache
def sequential(n: int) -> int:
    """Compute Fibonacci sequentially with memoization."""
    if n <= 1:
        return n

    return sequential(n-1) + sequential(n - 2)


def parallel(n: int) -> Cown:
    """Compute Fibonacci using cowns to parallelize subcalls."""
    if n <= 4:
        return Cown(sequential(n))

    @when(parallel(n - 1), parallel(n - 2))
    def do_fib(f1: Cown[int], f2: Cown[int]):
        return f1.value + f2.value

    return do_fib


def check(message: str, f: Cown, value: int):
    """Validate a computed Fibonacci value against the sequential result."""
    @when(f)
    def do_check(f: Cown[int]):
        print(f"{message}: {f.value} (expected: {value})")


def main():
    """Parse arguments, compute Fibonacci, and verify the result."""
    parser = argparse.ArgumentParser("Fibonacci")
    parser.add_argument("n", type=int, nargs="?", default=10)
    parser.add_argument("--loglevel", "-l", type=str, default=logging.WARNING)
    args = parser.parse_args()

    logging.basicConfig(level=args.loglevel)

    check(f"fib({args.n})", parallel(args.n), sequential(args.n))

    wait()


if __name__ == "__main__":
    main()
