"""Send/receive behavior tests for boc messaging primitives."""

from boc import receive, send, TIMEOUT


def test_basic():
    """Send then receive a tagged message without timeout."""
    send("a", "test")
    tag, value = receive("a", 1)
    assert tag != TIMEOUT
    assert value == "test"


def test_timeout():
    """Receive with timeout yields TIMEOUT and None payload."""
    tag, value = receive("a", 0.1)
    assert tag == TIMEOUT
    assert value is None


def test_timeout_after():
    """Execute after-callback when timeout elapses."""
    tag, value = receive("a", 0.1, lambda: ("after", 5))
    assert tag == "after"
    assert value == 5


def test_guard():
    """Guard predicate filters messages by payload."""
    send("a", 1)
    send("a", 10)
    tag, value = receive("a", 1, guard=lambda _, v: v > 5)
    assert tag == "a"
    assert value == 10
    _, value = receive("a", 1)
    assert tag == "a"
    assert value == 1


def test_tagset():
    """Receive across a set of tags and match each payload."""
    send("a", "alfa")
    send("b", "bravo")
    send("c", "charlie")
    tags = ["a", "b", "c"]
    for _ in range(3):
        match receive(tags, 1):
            case ["a", value]:
                assert value == "alfa"
            case ["b", value]:
                assert value == "bravo"
            case ["c", value]:
                assert value == "charlie"
            case ["timeout", _]:
                raise AssertionError("Unexpected timeout")


def test_threads():
    """Sum batches across multiple worker threads."""
    import threading

    num_workers = 10
    max_value = 1000
    batch_size = 20

    def worker():
        tags = ["worker", "shutdown"]
        running = True
        send("started", True)
        while running:
            match receive(tags):
                case ["worker", values]:
                    send("result", sum(values))
                case ["shutdown", _]:
                    running = False

    workers = []
    for _ in range(num_workers):
        t = threading.Thread(target=worker)
        workers.append(t)
        t.start()

    for _ in range(num_workers):
        receive("started")

    num_batches = max_value // batch_size
    for i in range(0, max_value, batch_size):
        values = tuple(range(i, i + batch_size))
        send("worker", values)

    result = 0
    for _ in range(num_batches):
        match receive("result"):
            case ["result", value]:
                result += value

    for _ in range(num_workers):
        send("shutdown", True)

    for t in workers:
        t.join()

    assert result == (max_value * (max_value - 1)) // 2
