"""Unit tests for the inline ``boc_wsq_*`` helpers in ``sched.h``.

These tests exercise the work-stealing-queue cursor arithmetic and
``enqueue_spread`` distribution invariant directly via the
``bocpy._internal_test`` C shim. They stay below the dispatch /
steal layer; full-stack scheduling correctness is covered by the
existing ``test_scheduler_*`` and ``test_boc.py`` suites.
"""

import pytest

_it = pytest.importorskip(
    "bocpy._internal_test",
    reason="internal test extension not built (set BOCPY_BUILD_INTERNAL_TESTS=1 and reinstall)",
)


WSQ_N = _it.wsq_n()


# ---------------------------------------------------------------------------
# Cursor arithmetic
# ---------------------------------------------------------------------------


def test_pre_inc_uniform_over_full_cycles():
    """`boc_wsq_pre_inc` must distribute uniformly over k = N * K calls."""
    K = 1000  # noqa: N806
    counts = _it.wsq_pre_inc_histogram(WSQ_N * K)
    assert counts == [K] * WSQ_N, (
        f"non-uniform distribution: {counts}")


def test_pre_inc_first_indices():
    """First N pre-increments must visit indices 1, 2, ..., N-1, 0."""
    counts = _it.wsq_pre_inc_histogram(WSQ_N)
    # Every index hit exactly once over a full cycle (regardless of order).
    assert counts == [1] * WSQ_N


def test_pre_inc_partial_cycle_within_bounds():
    """A partial cycle hits a contiguous prefix of indices."""
    # k = N - 1: indices 1..N-1 each receive 1, index 0 receives 0.
    counts = _it.wsq_pre_inc_histogram(WSQ_N - 1)
    assert counts[0] == 0
    for i in range(1, WSQ_N):
        assert counts[i] == 1, f"index {i} received {counts[i]}"


def test_post_dec_first_returns_zero_then_wraps():
    """`boc_wsq_post_dec` returns the *pre*-decrement index."""
    seq = _it.wsq_post_dec_sequence(WSQ_N + 2)
    # First call: cursor was 0 -> returns 0, advances to N-1.
    assert seq[0] == 0
    # Then N-1, N-2, ..., 0 (wrap), N-1, N-2.
    expected = [0] + list(range(WSQ_N - 1, -1, -1)) + [WSQ_N - 1]
    assert seq == expected[: len(seq)]


# ---------------------------------------------------------------------------
# Single-node enqueue distribution
# ---------------------------------------------------------------------------


def test_enqueue_round_robin_full_cycles():
    """N*K single pushes hit every sub-queue exactly K times."""
    K = 256  # noqa: N806
    w = _it.wsq_make_worker()
    counts = _it.wsq_enqueue_drain_counts(w, WSQ_N * K)
    assert counts == [K] * WSQ_N, (
        f"enqueue did not round-robin uniformly: {counts}")


def test_enqueue_partial_cycle_distribution():
    """A non-multiple-of-N push count distributes within ±1 across sub-queues."""
    K = 7  # noqa: N806  7 pushes, N=4 -> [1, 2, 2, 2] in some rotation.
    w = _it.wsq_make_worker()
    counts = _it.wsq_enqueue_drain_counts(w, K)
    assert sum(counts) == K
    # Max-min must be <= 1: round-robin gives near-uniform.
    assert max(counts) - min(counts) <= 1


def test_enqueue_zero_pushes_leaves_all_empty():
    """Zero pushes leaves every sub-queue empty."""
    w = _it.wsq_make_worker()
    counts = _it.wsq_enqueue_drain_counts(w, 0)
    assert counts == [0] * WSQ_N


# ---------------------------------------------------------------------------
# enqueue_spread distribution invariant
# ---------------------------------------------------------------------------


def test_spread_preserves_total_count():
    """All L nodes from a stolen segment land somewhere across the WSQ."""
    for length in (1, 2, 3, WSQ_N, WSQ_N + 1, 4 * WSQ_N, 100):
        w = _it.wsq_make_worker()
        counts = _it.wsq_spread_segment_counts(w, length)
        assert sum(counts) == length, (
            f"length={length}: spread lost nodes: {counts}")


def test_spread_distributes_long_segment_uniformly():
    """A long segment fills every sub-queue (no sub-queue is starved)."""
    length = 4 * WSQ_N
    w = _it.wsq_make_worker()
    counts = _it.wsq_spread_segment_counts(w, length)
    assert sum(counts) == length
    # Every sub-queue must receive at least one node.
    assert all(c >= 1 for c in counts), (
        f"some sub-queue starved: {counts}")
    # Spread is near-uniform: max-min <= 1 for an exact multiple of N.
    assert max(counts) - min(counts) <= 1, (
        f"long-segment spread non-uniform: {counts}")


def test_spread_singleton_segment_lands_on_one_subqueue():
    """A length-1 segment results in a single sub-queue holding 1 node."""
    w = _it.wsq_make_worker()
    counts = _it.wsq_spread_segment_counts(w, 1)
    assert sum(counts) == 1
    assert counts.count(1) == 1
