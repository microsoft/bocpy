"""Count primes using worker threads and message passing."""

import argparse
import random
import threading

from bocpy import receive, send


def worker():
    """Process incoming batches and send back prime counts."""
    sieve = [2, 3]

    def extend_sieve(max_value: int):
        value = sieve[-1] + 2
        while sieve[-1] < max_value:
            is_prime = True
            for prime in sieve:
                if value % prime == 0:
                    is_prime = False
                    break

            if is_prime:
                sieve.append(value)

            value += 2

    def count_primes(values) -> int:
        count = 0
        for v in values:
            if v == 0 or v == 1:
                continue

            left = 0
            right = len(sieve) - 1
            found = False
            while left <= right:
                mid = left + (right - left) // 2
                if v == sieve[mid]:
                    found = True
                    break

                if sieve[mid] > v:
                    right = mid - 1
                else:
                    left = mid + 1

            if found:
                count += 1

        return count

    running = True
    while running:
        match receive("worker"):
            case ["worker", "shutdown"]:
                running = False

            case ["worker", values]:
                max_value = max(values)
                if sieve[-1] < max_value:
                    extend_sieve(max_value)

                send("result", count_primes(values))


def run(num_workers, max_value, num_values):
    """Spawn workers, distribute random values, and aggregate prime counts."""
    workers = []
    for _ in range(num_workers):
        t = threading.Thread(target=worker)
        workers.append(t)
        t.start()

    batch_size = num_values // num_workers
    if num_workers * batch_size < num_values:
        batch_size += 1

    batches = [tuple(random.randint(0, max_value) for _ in range(batch_size))
               for _ in range(num_workers)]

    for batch in batches:
        send("worker", batch)

    result = 0
    for _ in range(num_workers):
        match receive("result"):
            case ["result", value]:
                result += value

    for _ in range(num_workers):
        send("worker", "shutdown")

    for t in workers:
        t.join()

    return result


def main():
    """Parse arguments, distribute work to threads, and count primes."""
    parser = argparse.ArgumentParser("Primes threads")
    parser.add_argument("--num-workers", "-w", type=int, default=8, help="Number of worker threads")
    parser.add_argument("--max-value", "-m", type=int, default=10000, help="Maximum value for generated values")
    parser.add_argument("--num-values", "-n", type=int, default=100000, help="Number of random values to generate")
    args = parser.parse_args()

    num_primes = run(args.num_workers, args.max_value, args.num_values)
    print("Number of primes:", num_primes)


if __name__ == "__main__":
    main()
