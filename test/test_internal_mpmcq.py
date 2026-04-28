"""Tests for the Verona MPMC behaviour queue (`boc_bq_*`).

These exercise the C-level queue exposed via ``bocpy._internal_test``
(prefix ``bq_*``). The queue is a port of Verona's ``MPMCQ<T>`` from
``mpmcq.h``; we test it in isolation, decoupled from any production
caller.
"""

from __future__ import annotations

import threading

import pytest

bq = pytest.importorskip(
    "bocpy._internal_test",
    reason="internal test extension not built (set BOCPY_BUILD_INTERNAL_TESTS=1 and reinstall)",
)


# ---------------------------------------------------------------------------
# Single-threaded sanity
# ---------------------------------------------------------------------------


def test_empty_on_construction_and_after_drain():
    """A fresh queue is empty, and remains empty after a drain cycle."""
    q = bq.bq_make_queue()
    assert bq.bq_is_empty(q)

    nodes = [bq.bq_make_node(i) for i in range(8)]
    for n in nodes:
        bq.bq_enqueue(q, n)
    assert not bq.bq_is_empty(q)

    seen = []
    while True:
        got = bq.bq_dequeue(q)
        if got is None:
            break
        seen.append(got)
    assert seen == list(range(8))
    assert bq.bq_is_empty(q)


def test_fifo_single_thread():
    """Single-thread enqueue / dequeue preserves FIFO order."""
    q = bq.bq_make_queue()
    nodes = [bq.bq_make_node(i) for i in range(100)]
    for n in nodes:
        bq.bq_enqueue(q, n)
    out = [bq.bq_dequeue(q) for _ in range(100)]
    assert out == list(range(100))
    assert bq.bq_dequeue(q) is None


def test_dequeue_on_empty_returns_none():
    q = bq.bq_make_queue()
    assert bq.bq_dequeue(q) is None
    assert bq.bq_dequeue_all(q) == []


def test_enqueue_front_on_empty_then_dequeue():
    """enqueue_front on an empty queue routes to the back path."""
    q = bq.bq_make_queue()
    n = bq.bq_make_node(42)
    bq.bq_enqueue_front(q, n)
    assert not bq.bq_is_empty(q)
    assert bq.bq_dequeue(q) == 42
    assert bq.bq_is_empty(q)


def test_enqueue_front_orders_before_existing():
    """A node pushed via enqueue_front comes out before existing items."""
    q = bq.bq_make_queue()
    keep = [bq.bq_make_node(i) for i in range(3)]
    for n in keep:
        bq.bq_enqueue(q, n)
    head = bq.bq_make_node(99)
    bq.bq_enqueue_front(q, head)
    out = []
    while True:
        v = bq.bq_dequeue(q)
        if v is None:
            break
        out.append(v)
    assert out == [99, 0, 1, 2]


def test_dequeue_all_returns_fifo_segment():
    """dequeue_all returns every currently-enqueued node in FIFO order."""
    q = bq.bq_make_queue()
    nodes = [bq.bq_make_node(i) for i in range(50)]
    for n in nodes:
        bq.bq_enqueue(q, n)
    seg = bq.bq_dequeue_all(q)
    assert seg == list(range(50))
    assert bq.bq_is_empty(q)


# ---------------------------------------------------------------------------
# Multi-producer stress
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("producers,per_producer", [(8, 20_000)])
def test_mpmc_stress_no_loss_no_dup(producers, per_producer):
    """Many producers, two consumers (one dequeue + one dequeue_all loop).

    With ``producers * per_producer`` enqueues split across encoded
    producer IDs, every value must appear exactly once on the consumer
    side. ``producers=8, per_producer=2000`` already exceeds 10^4 ops;
    raise ``per_producer`` to push past 10^6 when stress-bumping
    locally.
    """
    total = producers * per_producer
    q = bq.bq_make_queue()

    # Pre-allocate every node up front (alloc under GIL is not what we
    # want to stress). Encode (producer_id, sequence) in a single int
    # so the consumer side can verify per-producer FIFO ordering.
    nodes = [
        [bq.bq_make_node(p * per_producer + i) for i in range(per_producer)]
        for p in range(producers)
    ]

    seen: list[int] = []
    seen_lock = threading.Lock()
    stop = threading.Event()
    expected_total = total

    def producer(pid: int) -> None:
        for n in nodes[pid]:
            bq.bq_enqueue(q, n)

    def dequeue_consumer() -> None:
        while not stop.is_set():
            v = bq.bq_dequeue(q)
            if v is None:
                continue
            with seen_lock:
                seen.append(v)

    def dequeue_all_consumer() -> None:
        while not stop.is_set():
            chunk = bq.bq_dequeue_all(q)
            if chunk:
                with seen_lock:
                    seen.extend(chunk)

    prods = [threading.Thread(target=producer, args=(p,))
             for p in range(producers)]
    cons1 = threading.Thread(target=dequeue_consumer)
    cons2 = threading.Thread(target=dequeue_all_consumer)
    cons1.start()
    cons2.start()
    for t in prods:
        t.start()
    for t in prods:
        t.join()

    # Drain remainder under stop signal.
    # Spin until consumers report all values seen, then stop them.
    import time
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        with seen_lock:
            if len(seen) >= expected_total:
                break
        time.sleep(0.005)
    stop.set()
    cons1.join()
    cons2.join()

    # Final mop-up in case the consumer threads exited mid-segment.
    while True:
        v = bq.bq_dequeue(q)
        if v is None:
            break
        seen.append(v)

    assert len(seen) == expected_total, (
        f"lost or duplicated values: got {len(seen)}, expected {expected_total}"
    )
    assert sorted(seen) == list(range(expected_total)), (
        "values do not form 0..N-1 — duplication or corruption"
    )

    # Note: we deliberately do NOT assert per-producer FIFO on `seen`.
    # Even though MPMCQ preserves enqueue order at the dequeue point,
    # `seen` is appended under a lock by two concurrent consumers, so
    # its order reflects lock-acquisition order, not dequeue order.
    # The invariant under test is that every value appears exactly
    # once — no losses, no duplicates.

    assert bq.bq_is_empty(q)
