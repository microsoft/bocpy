---
name: testing-message-queue
description: "Write tests for the bocpy message queue — the lock-free tag-based MPSC ring buffer. Use when: testing send/receive, FIFO ordering, selective receive, timeouts, cross-thread messaging, multi-producer scenarios, tag capacity limits, set_tags behavior, or payload round-trip fidelity."
---

# Testing the Message Queue

This skill describes how to write tests for the `bocpy` message queue — the
lock-free, tag-based MPSC ring buffer implemented in C that underlies
`send()`/`receive()`.

## Architecture Overview

| Component | Detail |
|-----------|--------|
| Queues | 16 statically allocated ring buffers (`BOC_QUEUE_COUNT = 16`), each with capacity 16 384. |
| Tags | String labels that map 1:1 to queues. Each unique tag occupies one queue slot. |
| Assignment modes | **Automatic** (default): tags claim a queue on first `send`/`receive`. **Explicit**: `set_tags()` pre-assigns tags, clears all messages, and resets queues. |
| `set_tags([])` | Drains every queue, frees all tag associations, and sets all queues to UNASSIGNED — effectively restoring auto-assign mode. |
| Thread safety | Enqueue is lock-free (atomic CAS on tail). Multiple threads can `send` to the same tag concurrently. `receive` spins on the queue with a sleep until a message is available or a timeout elapses. |

## Test File Location

Message queue tests live in `test/test_message_queue.py`.

## Fixture: Resetting Queue State

Every test must start with a clean slate. Use an **autouse fixture** that calls
`set_tags([])` before and after each test. This drains all queues and returns
them to auto-assign mode — no `set_tags` call is needed inside individual tests
unless the test is specifically exercising `set_tags` behavior.

```python
MAX_QUEUES = 16

@pytest.fixture(autouse=True)
def reset_queues():
    set_tags([])
    yield
    set_tags([])
```

### Why `set_tags([])` instead of `set_tags([...16 tags...])`?

Calling `set_tags` is **optional**. The system auto-assigns tags to queues on
first use. The purpose of the fixture is to clear stale state, not to
pre-allocate tags. Using `set_tags([])` resets all 16 queues to UNASSIGNED so
every test starts fresh, and most tests can exercise the auto-assignment path
naturally.

## Pattern 1 — Basic Send / Receive

The simplest test: send a message, receive it, assert on the result. No
`set_tags` needed — the tag auto-assigns.

```python
def test_basic():
    send("my_tag", "hello")
    tag, val = receive("my_tag", 1)
    assert tag == "my_tag"
    assert val == "hello"
```

**Always pass a timeout** to `receive` so tests fail fast rather than hanging.

## Pattern 2 — Verifying FIFO Order

Messages on the same tag are delivered in FIFO order. Send a sequence, then
receive and compare element-by-element.

```python
def test_fifo():
    for i in range(10):
        send("fifo", i)
    for i in range(10):
        _, val = receive("fifo", 1)
        assert val == i
```

## Pattern 3 — Selective Receive Across Multiple Tags

`receive` accepts a list or tuple of tags and returns the first available match.
Messages on non-matching tags remain in their queues.

```python
def test_selective():
    send("skip", "no")
    send("want", "yes")
    _, val = receive("want", 1)
    assert val == "yes"
    # "skip" message is still there
    _, val = receive("skip", 1)
    assert val == "no"

def test_multi_tag_receive():
    send("b", "found")
    tag, val = receive(["a", "b", "c"], 1)
    assert tag == "b"
    assert val == "found"
```

## Pattern 4 — Timeout Behavior

| Timeout value | Behavior |
|---------------|----------|
| `0` | Return immediately — delivers a queued message or `(TIMEOUT, None)`. |
| Positive float | Wait up to that many seconds. |
| Negative (default) | Block indefinitely until a message arrives. |

The optional `after` callback fires only on timeout and replaces the default
`(TIMEOUT, None)` return value.

```python
def test_zero_timeout():
    tag, val = receive("empty", 0)
    assert tag == TIMEOUT
    assert val is None

def test_after_callback():
    tag, val = receive("miss", 0.05, lambda: ("fallback", 99))
    assert tag == "fallback"
    assert val == 99

def test_after_not_called_on_success():
    called = []
    send("ok", "payload")
    tag, val = receive(
        "ok", 1,
        lambda: (called.append(True), ("fb", -1))[-1],
    )
    assert tag == "ok"
    assert called == []
```

## Pattern 5 — Cross-Thread Messaging

`send` and `receive` are thread-safe. Use `threading.Thread` to test
cross-thread delivery.

```python
def test_cross_thread():
    def sender():
        send("ct", "from_thread")

    t = threading.Thread(target=sender)
    t.start()
    tag, val = receive("ct", 5)
    t.join()
    assert tag == "ct"
    assert val == "from_thread"

def test_bidirectional():
    def echo():
        _, val = receive("ping", 5)
        send("pong", val * 2)

    t = threading.Thread(target=echo)
    t.start()
    send("ping", 21)
    _, val = receive("pong", 5)
    t.join()
    assert val == 42
```

## Pattern 6 — Multiple Concurrent Producers

Multiple threads sending to the same tag exercises the lock-free MPSC enqueue.
Collect all messages and verify completeness. Per-producer FIFO ordering is
guaranteed (messages from a single thread arrive in send order).

```python
def test_multi_producer():
    n_producers = 8
    msgs_per = 200

    def producer(pid):
        for i in range(msgs_per):
            send("mp", (pid, i))

    threads = [threading.Thread(target=producer, args=(p,))
               for p in range(n_producers)]
    for t in threads:
        t.start()

    received = []
    for _ in range(n_producers * msgs_per):
        _, val = receive("mp", 10)
        received.append(val)

    for t in threads:
        t.join()

    # Every (pid, seq) pair delivered exactly once
    assert sorted(received) == sorted(
        (p, i) for p in range(n_producers) for i in range(msgs_per)
    )
```

## Pattern 7 — Tag Capacity Limits

There are exactly 16 queue slots. Exceeding them raises `KeyError`.

```python
def test_capacity_exceeded():
    for i in range(MAX_QUEUES):
        send(f"tag{i}", "fill")
    with pytest.raises(KeyError, match="tag capacity exceeded"):
        send("one_too_many", "boom")
```

## Pattern 8 — Testing `set_tags` Explicitly

Use `set_tags` tests to verify pre-assignment, clearing, overflow, and reset
behavior. These tests call `set_tags` directly inside the test body.

```python
def test_set_tags_clears():
    send("old", "stale")
    set_tags(["new"])
    # Old messages are gone; "new" starts empty.
    tag, val = receive("new", 0)
    assert tag == TIMEOUT

def test_set_tags_resets_to_auto():
    set_tags(["pre"])
    send("pre", "msg")
    set_tags([])  # back to auto-assign
    send("fresh", "works")
    tag, val = receive("fresh", 1)
    assert val == "works"

def test_set_tags_overflow():
    set_tags([f"t{i}" for i in range(MAX_QUEUES)])
    with pytest.raises(KeyError, match="tag capacity exceeded"):
        send("extra", "x")

def test_set_tags_rejects_too_many():
    with pytest.raises(IndexError):
        set_tags([f"t{i}" for i in range(MAX_QUEUES + 1)])
```

## Pattern 9 — Payload Round-Trip Fidelity

Messages cross interpreter boundaries via cross-interpreter data (XIData) or
pickle. Use `@pytest.mark.parametrize` to sweep payload types.

```python
@pytest.mark.parametrize("payload", [
    0, -1, 2**31, 3.14, float("inf"),
    True, False, None,
    "", "hello",
    (), (1,), (1, (2, (3,))),
])
def test_roundtrip(payload):
    send("rt", payload)
    _, val = receive("rt", 1)
    assert val == payload
```

## Error Handling Tests

| Error | How to trigger | Expected exception |
|-------|---------------|--------------------|
| Non-string tag in `send` | `send(123, "x")` | `TypeError` |
| Empty tag list in `receive` | `receive([], 0)` | `RuntimeError` |
| Non-string in tag list | `receive([123], 0)` | `TypeError` |
| Tag capacity exceeded | `send` on 17th unique tag | `KeyError` |
| Too many tags in `set_tags` | `set_tags([...17 items...])` | `IndexError` |
| Non-str in `set_tags` | `set_tags([123])` | `TypeError` |
| Non-sequence in `set_tags` | `set_tags(42)` | `TypeError` |

## Common Pitfalls

| Pitfall | Fix |
|---------|-----|
| Calling `receive` without a timeout | Tests hang forever if no message arrives. Always pass a timeout. |
| Using too many unique tags across tests | Each unique tag consumes a queue slot. The fixture must reset queues between tests via `set_tags([])`. |
| Forgetting the fixture resets queues | Without the autouse fixture, stale messages or tag assignments leak between tests, causing flaky failures. |
| Assuming `set_tags` is required | It isn't. Tags auto-assign on first use. Only call `set_tags` when you need to pre-assign or clear queues. |
| Not joining threads before asserting | A thread may still hold the GIL or be mid-`send`. Always `t.join()` before final assertions. |
| Sending from more threads than queue capacity | If threads use distinct tags, you'll hit the 16-queue limit. Have concurrent threads share a single tag. |
