"""Parallel prime factorisation with early termination via the noticeboard."""

import argparse
from functools import partial
import logging
import math
import random

from bocpy import (Cown, notice_read, notice_seed, notice_update,
                   notice_write, quiesce, wait, when)


def _merge_sieve(existing, new_primes):
    """Extend *existing* with the tail of *new_primes* beyond its end.

    Because the sieve is built by extending upward, *new_primes* is a
    sorted run of consecutive primes that is either:
    1. entirely contained in *existing* (another lane already found them),
    2. overlapping the end (the prefix is known, the suffix is new), or
    3. strictly continuing *existing* (all new).
    In every case we just need to append the primes past *existing*[-1].
    """
    if not new_primes:
        return existing
    if not existing:
        return new_primes

    cutoff = existing[-1]
    lo, hi = 0, len(new_primes)
    while lo < hi:
        mid = (lo + hi) // 2
        if new_primes[mid] <= cutoff:
            lo = mid + 1
        else:
            hi = mid

    if lo >= len(new_primes):
        return existing
    return existing + new_primes[lo:]


class SieveLane:
    """Progress state for one sieve lane."""

    def __init__(self, lane_id: int, remaining: int, batch: int, lo: int, hi: int):
        """Initialise a sieve lane.

        :param lane_id: Numeric identifier for this lane.
        :param remaining: How many candidates this lane should still test.
        :param batch: How many candidates to generate and test per behavior.
        :param lo: Lower bound for random candidates (inclusive).
        :param hi: Upper bound for random candidates (inclusive).
        """
        self.lane_id = lane_id
        self.remaining = remaining
        self.batch = batch
        self.lo = lo
        self.hi = hi
        self.found = []


def sieve_check(lane: Cown[SieveLane]):
    """Check whether this sieve lane has more work to do."""
    @when(lane)
    def _(lane):
        if lane.value.remaining <= 0:
            return

        sieve_work(lane)


def sieve_work(lane: Cown[SieveLane]):
    """Generate a batch of candidates and test for primality."""
    @when(lane)
    def _(lane):
        info = lane.value
        sieve = list(notice_read("sieve"))
        new_sieve_primes = []
        count = min(info.batch, info.remaining)

        for _ in range(count):
            c = random.randrange(info.lo, info.hi) | 1
            limit = int(math.isqrt(c)) + 1

            n = sieve[-1] + 2
            while sieve[-1] < limit:
                if all(n % p != 0 for p in sieve if p * p <= n):
                    sieve.append(n)
                    new_sieve_primes.append(n)
                n += 2

            is_prime = True
            for p in sieve:
                if p * p > c:
                    break
                if c % p == 0:
                    is_prime = False
                    break

            if is_prime:
                info.found.append(c)

        info.remaining -= count
        if new_sieve_primes:
            notice_update("sieve",
                          partial(_merge_sieve, new_primes=new_sieve_primes),
                          default=[2, 3])

        sieve_check(lane)


class RhoLane:
    """State for one Pollard's rho random walk."""

    def __init__(self, lane_id: int, n: int, batch: int):
        """Initialise a rho lane with a random starting point and constant.

        :param lane_id: Numeric identifier for this lane.
        :param n: The number being factored.
        :param batch: Iterations per work behavior.
        """
        self.lane_id = lane_id
        self.c = random.randrange(1, n)
        self.x = random.randrange(2, n)
        self.y = self.x
        self.batch = batch


def rho_check(lane: Cown[RhoLane], n: int):
    """Check the noticeboard for a result before continuing the walk."""
    @when(lane)
    def _(lane, n=n):
        if notice_read("factor") is not None:
            return

        rho_work(lane, n)


def rho_work(lane: Cown[RhoLane], n: int):
    """Run a batch of Pollard's rho iterations using Floyd's cycle detection."""
    @when(lane)
    def _(lane, n=n):
        info = lane.value
        x, y, c = info.x, info.y, info.c

        for _ in range(info.batch):
            x = (x * x + c) % n
            y = (y * y + c) % n
            y = (y * y + c) % n
            d = math.gcd(abs(x - y), n)
            if d != 1 and d != n:
                notice_write("factor", d)
                print(f"  lane {info.lane_id} found factor {d}")
                return
            if d == n:
                info.c = random.randrange(1, n)
                info.x = random.randrange(2, n)
                info.y = info.x
                rho_check(lane, n)
                return

        info.x = x
        info.y = y
        rho_check(lane, n)


def main():
    """Sieve for primes, build a semiprime, then factor it in parallel."""
    parser = argparse.ArgumentParser("Prime Factor")
    parser.add_argument("--lanes", "-n", type=int, default=4,
                        help="number of parallel search lanes")
    parser.add_argument("--candidates", "-c", type=int, default=2000,
                        help="number of random candidates to sieve")
    parser.add_argument("--batch", "-b", type=int, default=100,
                        help="candidates tested per work behavior")
    parser.add_argument("--bits", type=int, default=16,
                        help="bit-size of candidate numbers")
    parser.add_argument("--loglevel", "-l", type=str, default=logging.WARNING)
    args = parser.parse_args()

    logging.basicConfig(level=args.loglevel)

    lo = 1 << (args.bits - 1)
    hi = (1 << args.bits) - 1
    per_lane = args.candidates // args.lanes
    print(f"sieving {args.candidates} candidates ({args.bits}-bit) "
          f"across {args.lanes} lanes ...")

    # Seed the shared sieve synchronously so every lane reads a populated
    # "sieve" entry from its first behavior; later growth is merged by the
    # worker-side notice_update below.
    notice_seed("sieve", [2, 3, 5, 7, 11, 13, 17, 19])

    sieve_lane_cowns = []
    for i in range(args.lanes):
        lane_cown = Cown(SieveLane(i, per_lane, args.batch, lo, hi))
        sieve_check(lane_cown)
        sieve_lane_cowns.append(lane_cown)

    quiesce()

    primes = []
    for lane_cown in sieve_lane_cowns:
        with lane_cown as lane:
            primes.extend(lane.found)

    print(f"found {len(primes)} primes")

    p, q = random.sample(primes, 2)
    n = p * q

    print(f"factoring {n} (= {p} x {q})")
    print(f"Pollard's rho with {args.lanes} parallel walks, batch={args.batch}")

    for i in range(args.lanes):
        rho_check(Cown(RhoLane(i, n, args.batch)), n)

    snap = wait(noticeboard=True)
    factor = snap.get("factor")
    if factor is None:
        print(f"no factor found for {n}; rho lanes quiesced without a hit")
        return
    other = n // factor
    print(f"result: {n} = {factor} x {other}")


if __name__ == "__main__":
    main()
