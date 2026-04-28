"""Tests for the typed `boc_atomic_*_explicit` API in `compat.h`.

These tests drive the C extension `bocpy._internal_test` (atomics
domain, `atomics_*` methods) from real Python threads. On
free-threaded CPython (.env313t / .env315t) the threads run truly
in parallel; on regular CPython the C functions release the GIL
across their hot loops via `Py_BEGIN_ALLOW_THREADS`, so the
producer/consumer handshake and CAS contention loops still race.

On x86/x64 these tests are smoke tests (every Interlocked* on those
architectures is a full barrier). On ARM64 they are the canonical
weak-memory correctness tests for the `__ldar*`/`__stlr*` and
`Interlocked*_{nf,acq,rel}` dispatch in `compat.h`.
"""

import threading
from types import SimpleNamespace

import pytest

_it = pytest.importorskip(
    "bocpy._internal_test",
    reason="internal test extension not built (set BOCPY_BUILD_INTERNAL_TESTS=1 and reinstall)",
)

# Bind the atomics-domain methods under the historical `ca.*` name so
# the body of this file stays readable and untouched.
ca = SimpleNamespace(
    make_state=_it.atomics_make_state,
    reset=_it.atomics_reset,
    load_counter64=_it.atomics_load_counter64,
    load_counter32=_it.atomics_load_counter32,
    load_bool=_it.atomics_load_bool,
    load_ptr=_it.atomics_load_ptr,
    producer=_it.atomics_producer,
    consumer=_it.atomics_consumer,
    fetch_add_loop_u64=_it.atomics_fetch_add_loop_u64,
    fetch_add_loop_u32=_it.atomics_fetch_add_loop_u32,
    cas_increment_loop_u64=_it.atomics_cas_increment_loop_u64,
    round_trip=_it.atomics_round_trip,
)


def test_round_trip_single_thread():
    """Every (op, type, order) returns the right value at least once."""
    ca.round_trip()


def test_state_starts_zeroed():
    h = ca.make_state()
    assert ca.load_counter64(h) == 0
    assert ca.load_counter32(h) == 0
    assert ca.load_bool(h) is False
    assert ca.load_ptr(h) == 0


def test_reset_zeros_all_slots():
    h = ca.make_state()
    ca.fetch_add_loop_u64(h, 5)
    ca.fetch_add_loop_u32(h, 3)
    assert ca.load_counter64(h) == 5
    assert ca.load_counter32(h) == 3
    ca.reset(h)
    assert ca.load_counter64(h) == 0
    assert ca.load_counter32(h) == 0


# ---------------------------------------------------------------------------
# Acquire / release handshake
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    1,
    0xDEADBEEF,
    0xCAFEBABEDEADBEEF,
    0xFFFFFFFFFFFFFFFF,
])
def test_handshake_single(payload):
    """Single producer/consumer round; consumer must see the payload."""
    h = ca.make_state()
    result = []

    def consume():
        result.append(ca.consumer(h))

    t = threading.Thread(target=consume)
    t.start()
    ca.producer(h, payload)
    t.join(timeout=5.0)
    assert not t.is_alive(), "consumer thread did not observe release-store"
    assert result == [payload]


def test_handshake_repeated():
    """Repeat the handshake many times; every iteration reads its payload.

    This is the canonical message-passing weak-memory test: a relaxed
    load on `flag` would let the consumer observe `flag==1` while the
    prior `payload` write is still in the producer's store buffer.
    """
    iters = 2000
    for i in range(iters):
        h = ca.make_state()
        payload = 0xA5A5_0000_0000_0000 | i
        result = []

        def consume(h=h, result=result):
            result.append(ca.consumer(h))

        t = threading.Thread(target=consume)
        t.start()
        ca.producer(h, payload)
        t.join(timeout=5.0)
        assert not t.is_alive(), f"iteration {i}: consumer hung"
        assert result == [payload], f"iteration {i}: expected {payload:#x}, got {result[0]:#x}"


# ---------------------------------------------------------------------------
# Multi-thread fetch_add contention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("threads,per_thread", [
    (2, 50_000),
    (4, 50_000),
    (8, 25_000),
])
def test_fetch_add_u64_contention(threads, per_thread):
    """Sum `threads * per_thread` fetch-adds; assert no lost updates.

    N threads each `fetch_add(+1)` `per_thread` times; final
    counter must equal `threads * per_thread`. A non-atomic
    increment would lose updates under contention.
    """
    h = ca.make_state()
    workers = [
        threading.Thread(target=ca.fetch_add_loop_u64, args=(h, per_thread))
        for _ in range(threads)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30.0)
        assert not w.is_alive()
    assert ca.load_counter64(h) == threads * per_thread


@pytest.mark.parametrize("threads,per_thread", [
    (2, 50_000),
    (4, 50_000),
])
def test_fetch_add_u32_contention(threads, per_thread):
    h = ca.make_state()
    workers = [
        threading.Thread(target=ca.fetch_add_loop_u32, args=(h, per_thread))
        for _ in range(threads)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30.0)
        assert not w.is_alive()
    assert ca.load_counter32(h) == threads * per_thread


# ---------------------------------------------------------------------------
# Multi-thread CAS contention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("threads,per_thread", [
    (2, 25_000),
    (4, 25_000),
    (8, 10_000),
])
def test_cas_increment_contention(threads, per_thread):
    """Sum `threads * per_thread` CAS-increments; assert none lost.

    N threads each CAS-increment counter `per_thread` times;
    success path uses `BOC_MO_ACQ_REL`. Final value must equal
    `threads * per_thread` — any lost CAS would short the count.
    """
    h = ca.make_state()
    workers = [
        threading.Thread(target=ca.cas_increment_loop_u64,
                         args=(h, per_thread))
        for _ in range(threads)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60.0)
        assert not w.is_alive()
    assert ca.load_counter64(h) == threads * per_thread
