"""Smoke tests for `_core.scheduler_stats()` and `_core.queue_stats()`.

These tests verify:
- shape of the two snapshots (no crash on empty),
- that ``scheduler_stats()`` is empty when the runtime is down,
- that ``wait(stats=True)`` returns the post-session snapshot,
- that ``queue_stats()`` reflects ``set_tags`` and increments under
  ``send`` / ``receive``,
- monotonicity across two consecutive snapshots,
- that calling either accessor has no observable side effects on the
  next snapshot's counters.
"""

import bocpy
from bocpy import _core, Cown, drain, receive, send, set_tags, wait, when


SCHEDULER_FIELDS = {
    "worker_index",
    "pushed_local",
    "dispatched_to_pending",
    "pushed_remote",
    "popped_local",
    "popped_via_steal",
    "enqueue_cas_retries",
    "dequeue_cas_retries",
    "batch_resets",
    "steal_attempts",
    "steal_failures",
    "parked",
    "last_steal_attempt_ns",
    "fairness_arm_fires",
}

QUEUE_FIELDS = {
    "queue_index",
    "tag",
    "enqueue_cas_retries",
    "dequeue_cas_retries",
    "pushed_total",
    "popped_total",
}


def test_scheduler_stats_empty_when_runtime_down():
    """With the runtime down, the snapshot must be an empty list."""
    wait()  # ensure runtime is down
    stats = _core.scheduler_stats()
    assert isinstance(stats, list)
    assert stats == []


def test_wait_returns_final_snapshot():
    """`wait(stats=True)` returns the post-session snapshot.

    `_core.scheduler_stats()` after `wait()` is empty because the
    per-worker array has been freed; `wait(stats=True)` is the
    correct way to read the counters for the session that just
    ended.
    """
    wait()  # baseline
    W = 2  # noqa: N806
    bocpy.start(worker_count=W)
    c = Cown(0)

    @when(c)
    def _(c):
        send("swt_done", 1)

    tag, _payload = receive("swt_done", 5.0)
    assert tag == "swt_done"

    snapshot = wait(stats=True)
    assert isinstance(snapshot, list)
    assert len(snapshot) == W, snapshot
    for s in snapshot:
        assert SCHEDULER_FIELDS == set(s.keys()), s
    # At least one push happened across the pool.
    assert sum(s["pushed_local"] + s["dispatched_to_pending"]
               + s["pushed_remote"] for s in snapshot) >= 1
    # And the per-worker array is gone now.
    assert _core.scheduler_stats() == []


def test_wait_stats_default_returns_none():
    """`wait()` without `stats=True` returns ``None`` (back-compat)."""
    wait()
    assert wait() is None
    # Even with a real session, default still returns None.
    bocpy.start(worker_count=2)
    c = Cown(0)

    @when(c)
    def _(c):
        send("swt_default_done", 1)

    receive("swt_default_done", 5.0)
    assert wait() is None


def test_wait_stats_true_when_runtime_never_started():
    """`wait(stats=True)` returns ``[]`` when no runtime exists."""
    wait()
    assert wait(stats=True) == []


def test_off_worker_dispatch_bumps_pushed_remote_not_pending():
    """Main-thread `@when` dispatches use the off-worker (remote) arm.

    `boc_sched_dispatch`'s off-worker arm (`current_worker == NULL`)
    bumps `pushed_remote` on the round-robin target. It never
    touches the producer-local `pending` slot, so the resulting
    snapshot must show `sum(pushed_remote) >= N` and
    `sum(dispatched_to_pending) == 0`.
    """
    wait()
    W = 4  # noqa: N806
    N = 16  # noqa: N806
    bocpy.start(worker_count=W)
    cowns = [Cown(0) for _ in range(N)]
    for c in cowns:
        @when(c)
        def _(c):
            send("opp_done", 1)  # noqa: B023

    for _ in range(N):
        tag, _payload = receive("opp_done", 5.0)
        assert tag == "opp_done"

    snap = wait(stats=True)
    total_remote = sum(s["pushed_remote"] for s in snap)
    total_pending = sum(s["dispatched_to_pending"] for s in snap)
    assert total_remote >= N, snap
    # No producer-local arm was ever taken, so pending stays at 0.
    assert total_pending == 0, snap


def test_dispatched_to_pending_increments_from_worker_dispatch():
    """A worker-side `@when` against a fresh cown bumps `dispatched_to_pending`.

    Inside a behavior body `current_worker != NULL`, so dispatch
    enters the producer-local arm. With nothing already in the
    worker's `pending` slot the dispatch falls through the
    "install into empty pending" branch and bumps
    `dispatched_to_pending` (not `pushed_local`). With one chained
    dispatch per outer behavior across N outers, the snapshot
    must show `sum(dispatched_to_pending) >= N`.
    """
    wait()
    W = 2  # noqa: N806
    N = 32  # noqa: N806
    bocpy.start(worker_count=W)
    outers = [Cown(0) for _ in range(N)]
    inners = [Cown(0) for _ in range(N)]
    for o, i in zip(outers, inners):
        @when(o)
        def _(o):
            @when(i)  # noqa: B023
            def _inner(i):
                send("ppi_done", 1)

    for _ in range(N):
        tag, _payload = receive("ppi_done", 5.0)
        assert tag == "ppi_done"

    snap = wait(stats=True)
    total_pending = sum(s["dispatched_to_pending"] for s in snap)
    assert total_pending >= N, snap


def test_queue_stats_reflects_set_tags_and_traffic():
    """`queue_stats` should expose tagged queues with monotonic counters."""
    set_tags(["t_one", "t_two"])
    # Drain in case a previous test sent on these tags.
    drain(["t_one", "t_two"])

    before = _core.queue_stats()
    by_tag_before = {q["tag"]: q for q in before}
    assert "t_one" in by_tag_before
    assert "t_two" in by_tag_before
    for q in before:
        assert QUEUE_FIELDS == set(q.keys())
        assert isinstance(q["queue_index"], int)
        assert isinstance(q["pushed_total"], int)
        assert isinstance(q["popped_total"], int)
        assert q["pushed_total"] >= 0
        assert q["popped_total"] >= 0

    pushed_before = by_tag_before["t_one"]["pushed_total"]
    popped_before = by_tag_before["t_one"]["popped_total"]

    send("t_one", "alpha")
    send("t_one", "beta")
    msg = receive("t_one")
    assert msg == ("t_one", "alpha")

    after = _core.queue_stats()
    by_tag_after = {q["tag"]: q for q in after}
    assert by_tag_after["t_one"]["pushed_total"] == pushed_before + 2
    assert by_tag_after["t_one"]["popped_total"] == popped_before + 1
    # Other tag must not move.
    assert (by_tag_after["t_two"]["pushed_total"]
            == by_tag_before["t_two"]["pushed_total"])
    assert (by_tag_after["t_two"]["popped_total"]
            == by_tag_before["t_two"]["popped_total"])


def test_queue_stats_monotonic_and_no_side_effect():
    """Calling the snapshots must not perturb the counters."""
    set_tags(["t_idle"])
    drain(["t_idle"])

    snap1 = _core.queue_stats()
    snap2 = _core.queue_stats()
    snap3 = _core.queue_stats()

    by_tag = lambda snap: {q["tag"]: q for q in snap}  # noqa: E731
    s1 = by_tag(snap1)
    s2 = by_tag(snap2)
    s3 = by_tag(snap3)

    # No traffic between snapshots → counters are stable.
    for tag in s1:
        assert s2[tag]["pushed_total"] == s1[tag]["pushed_total"]
        assert s2[tag]["popped_total"] == s1[tag]["popped_total"]
        assert s3[tag]["pushed_total"] == s1[tag]["pushed_total"]
        assert s3[tag]["popped_total"] == s1[tag]["popped_total"]

    # And calling scheduler_stats does not perturb queue_stats either.
    _ = _core.scheduler_stats()
    snap4 = _core.queue_stats()
    s4 = by_tag(snap4)
    for tag in s1:
        assert s4[tag]["pushed_total"] == s1[tag]["pushed_total"]
        assert s4[tag]["popped_total"] == s1[tag]["popped_total"]


def test_drain_does_not_decrement_pushed_or_popped_total():
    """`drain` must clear messages without decrementing the counters.

    The counters track *cumulative* traffic for the lifetime of the
    process; drain is an administrative operation, not a dequeue.
    """
    set_tags(["t_drain"])
    drain(["t_drain"])

    send("t_drain", "x")
    send("t_drain", "y")

    before = next(q for q in _core.queue_stats() if q["tag"] == "t_drain")
    drain(["t_drain"])
    after = next(q for q in _core.queue_stats() if q["tag"] == "t_drain")

    # Drain pulls the messages out via boc_dequeue, so popped_total
    # advances. pushed_total must not retreat.
    assert after["pushed_total"] == before["pushed_total"]
    assert after["popped_total"] >= before["popped_total"]
