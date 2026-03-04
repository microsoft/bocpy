"""Concurrent calculator using message-passing channels."""

import argparse
import random
from threading import Thread
import time

from bocpy import receive, send


def client(num_operations: int):
    """Send random arithmetic operations to the calculator channel."""
    actions = ["+", "-", "/", "*"]

    for _ in range(num_operations):
        time.sleep(random.random() * 0.1)
        action = random.choice(actions)
        value = random.random() * 10 - 5
        send("calculator", (action, value))


def server(timeout):
    """Receive and process arithmetic operations until stopped."""
    value = 0
    num_operations = 0
    running = True

    def after():
        return "calculator", ("print", True)

    while running:
        match receive("calculator", timeout, after):
            case [_, ("+", x)]:
                num_operations += 1
                value += x

            case [_, ("-", x)]:
                num_operations += 1
                value -= x

            case [_, ("*", x)]:
                num_operations += 1
                value *= x

            case [_, ("/", x)]:
                num_operations += 1
                value /= x

            case [_, ("print", timeout)]:
                if timeout:
                    print("Timed out")

                print("Total operations:", num_operations)
                print("Final value:", value)
                running = False


def main():
    """Parse arguments and run the calculator server and clients."""
    parser = argparse.ArgumentParser("Calculator")
    parser.add_argument("--num-clients", "-n", type=int, default=8)
    parser.add_argument("--num-operations", "-a", type=int, default=10)
    parser.add_argument("--timeout", "-t", type=int, default=-1)
    args = parser.parse_args()

    print("# client:", args.num_clients)
    print("# ops/client:", args.num_operations)
    server_thread = Thread(target=server, args=(args.timeout,))
    server_thread.start()
    client_threads = []
    for _ in range(args.num_clients):
        client_thread = Thread(target=client, args=(args.num_operations,))
        client_thread.start()
        client_threads.append(client_thread)

    for c in client_threads:
        c.join()

    if args.timeout < 0:
        send("calculator", ("print", False))

    server_thread.join()


if __name__ == "__main__":
    main()
