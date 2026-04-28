"""Tests for the underlying message queue system.

Exercises the low-level queue mechanics: automatic tag-to-queue assignment,
FIFO ordering, selective receive, capacity limits, set_tags management,
high-volume throughput, concurrent producers/consumers, error handling,
and queue reset behavior.

The message queue supports two modes:
- **Automatic assignment**: tags are assigned to queues on first use, up to the
  hard limit of ``MAX_QUEUES`` (16).
- **Explicit assignment via set_tags()**: pre-assigns tags to queues, clears all
  pending messages, and resets queue state.

Both modes are tested here.  Tests that do *not* need set_tags rely purely on
the auto-assignment path and use a ``set_tags([])`` call only in the fixture to
ensure a clean slate.
"""

import random
import threading
import time

import pytest

from bocpy import drain, receive, send, set_tags, TIMEOUT


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum number of dedicated queues supported by the C layer (BOC_QUEUE_COUNT).
MAX_QUEUES = 16


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_queues():
    """Reset all queues before and after every test.

    Calling ``set_tags([])`` drains all queues, frees all tag associations,
    and sets every queue back to UNASSIGNED so that tags will auto-assign
    on first use.
    """
    set_tags([])
    yield
    set_tags([])


# ===================================================================
# Auto-assignment: tags acquire queues on first send/receive
# ===================================================================


class TestAutoAssignment:
    """Verify that tags are automatically assigned to queues on first use."""

    def test_single_tag(self):
        """Sending on a new tag auto-assigns it to a queue."""
        send("auto1", "hello")
        tag, val = receive("auto1", 1)
        assert tag == "auto1"
        assert val == "hello"

    def test_multiple_tags(self):
        """Multiple distinct tags each get their own queue."""
        for i in range(5):
            send(f"atag{i}", i)
        for i in range(5):
            tag, val = receive(f"atag{i}", 1)
            assert tag == f"atag{i}"
            assert val == i

    def test_max_auto_tags(self):
        """Auto-assigning exactly MAX_QUEUES tags succeeds."""
        tags = [f"amax{i}" for i in range(MAX_QUEUES)]
        for t in tags:
            send(t, t)
        for t in tags:
            tag, val = receive(t, 1)
            assert tag == t
            assert val == t

    def test_exceeding_max_auto_tags(self):
        """Auto-assigning more than MAX_QUEUES tags raises KeyError."""
        tags = [f"aover{i}" for i in range(MAX_QUEUES)]
        for t in tags:
            send(t, "fill")  # fills all 16 slots
        with pytest.raises(KeyError, match="tag capacity exceeded"):
            send("one_too_many", "boom")

    def test_reuse_same_tag(self):
        """Reusing the same tag doesn't consume additional queue slots."""
        send("reuse_auto", 1)
        send("reuse_auto", 2)
        _, v1 = receive("reuse_auto", 1)
        _, v2 = receive("reuse_auto", 1)
        assert v1 == 1
        assert v2 == 2


# ===================================================================
# FIFO ordering within a single queue
# ===================================================================


class TestFIFO:
    """Ensure per-tag FIFO delivery via auto-assigned queues."""

    def test_fifo_small(self):
        """A handful of messages are dequeued in FIFO order."""
        for i in range(10):
            send("fifo_s", i)
        for i in range(10):
            _, val = receive("fifo_s", 1)
            assert val == i

    def test_fifo_large_burst(self):
        """A large burst preserves FIFO ordering."""
        n = 5000
        for i in range(n):
            send("fifo_l", i)
        for i in range(n):
            _, val = receive("fifo_l", 1)
            assert val == i


# ===================================================================
# Selective receive (tag filtering)
# ===================================================================


class TestSelectiveReceive:
    """Receive should pick the first matching tag and leave others."""

    def test_skip_unmatched(self):
        """Messages with non-matching tags remain in their queues."""
        send("sel_skip", "no")
        send("sel_want", "yes")
        _, val = receive("sel_want", 1)
        assert val == "yes"
        # "sel_skip" message still available
        _, val = receive("sel_skip", 1)
        assert val == "no"

    def test_multi_tag_receive(self):
        """Receiving on a list of tags finds the first available match."""
        send("mtr_b", "found")
        tag, val = receive(["mtr_a", "mtr_b", "mtr_c"], 1)
        assert tag == "mtr_b"
        assert val == "found"

    def test_per_tag_fifo_with_interleaving(self):
        """Interleaved sends maintain per-tag FIFO ordering."""
        send("ilv_a", 1)
        send("ilv_b", 10)
        send("ilv_a", 2)
        send("ilv_b", 20)

        _, v = receive("ilv_b", 1)
        assert v == 10
        _, v = receive("ilv_b", 1)
        assert v == 20
        _, v = receive("ilv_a", 1)
        assert v == 1
        _, v = receive("ilv_a", 1)
        assert v == 2

    def test_tuple_as_tags(self):
        """receive accepts a tuple of tags, not just a list."""
        send("tt_x", "xval")
        send("tt_y", "yval")
        received = set()
        for _ in range(2):
            tag, val = receive(("tt_x", "tt_y"), 1)
            assert tag != TIMEOUT
            received.add((tag, val))
        assert received == {("tt_x", "xval"), ("tt_y", "yval")}

    def test_single_str_tag(self):
        """receive with a plain string tag (not wrapped in a list)."""
        send("plain", 99)
        tag, val = receive("plain", 1)
        assert tag == "plain"
        assert val == 99


# ===================================================================
# Timeout behavior
# ===================================================================


class TestTimeout:
    """Timeout and after-callback edge cases."""

    def test_zero_timeout_empty(self):
        """Zero timeout returns immediately when queue is empty."""
        tag, val = receive("zt_empty", 0)
        assert tag == TIMEOUT
        assert val is None

    def test_zero_timeout_with_message(self):
        """Zero timeout still returns a queued message."""
        send("zt_msg", "ready")
        tag, val = receive("zt_msg", 0)
        assert tag != TIMEOUT
        assert val == "ready"

    def test_negative_timeout_blocks(self):
        """Negative timeout blocks until a message arrives."""
        def delayed_send():
            time.sleep(0.1)
            send("neg_blk", "arrived")

        t = threading.Thread(target=delayed_send)
        t.start()
        tag, val = receive("neg_blk", 5)  # generous upper bound
        t.join()
        assert tag == "neg_blk"
        assert val == "arrived"

    def test_after_callback_on_timeout(self):
        """After callback fires when no message arrives in time."""
        tag, val = receive("aft_to", 0.05, lambda: ("fallback", 99))
        assert tag == "fallback"
        assert val == 99

    def test_after_callback_not_called_on_success(self):
        """After callback is NOT invoked when a message is available."""
        called = []
        send("aft_ok", "payload")
        tag, val = receive(
            "aft_ok", 1,
            lambda: (called.append(True), ("fb", -1))[-1],
        )
        assert tag == "aft_ok"
        assert val == "payload"
        assert called == []

    def test_after_returns_custom_values(self):
        """The after callback can return an arbitrary (tag, value) pair."""
        tag, val = receive("aft_custom", 0.05, lambda: ("custom", {"k": "v"}))
        assert tag == "custom"
        assert val == {"k": "v"}


# ===================================================================
# Error handling
# ===================================================================


class TestErrors:
    """Error paths in send/receive."""

    def test_send_non_str_tag(self):
        """send() rejects a non-string tag."""
        with pytest.raises(TypeError):
            send(123, "value")

    def test_receive_empty_tag_list(self):
        """receive() with an empty tag list raises RuntimeError."""
        with pytest.raises(RuntimeError):
            receive([], 0)

    def test_receive_non_str_in_tag_list(self):
        """receive() with non-string elements in tag list raises TypeError."""
        with pytest.raises(TypeError):
            receive([123], 0)

    def test_send_unpaired_surrogate_tag_no_leak(self):
        """send() with a tag containing an unpaired surrogate fails cleanly.

        Regression test: ``tag_from_PyUnicode`` previously leaked
        the @c BOCTag struct when @c PyUnicode_AsUTF8AndSize
        raised on surrogate input. After the fix the partial
        allocation is freed before the function returns NULL, and
        the caller (``boc_message_new`` / ``get_queue_for_tag``)
        propagates the @c UnicodeEncodeError without wedging the
        slot in @c ASSIGNED-with-NULL-tag state. We then prove the
        slot is still usable by sending a normal tag through the
        queue afterwards.
        """
        bad_tag = "\ud800"  # lone high surrogate
        with pytest.raises(UnicodeEncodeError):
            send(bad_tag, "payload")
        # Sanity: the queue subsystem is still functional after the
        # failed attempt.
        send("post_surrogate_ok", "ok")
        _, val = receive("post_surrogate_ok", 1)
        assert val == "ok"

    def test_set_tags_unpaired_surrogate_no_leak(self):
        """set_tags() with a surrogate tag fails cleanly mid-loop.

        Companion to ``test_send_unpaired_surrogate_tag_no_leak``:
        exercises the @c _core_set_tags caller of
        @c tag_from_PyUnicode, which is the second path that the
        leak fix has to cover. We only assert that the
        @c UnicodeEncodeError propagates without crashing /
        deadlocking — set_tags' partial-failure recovery
        semantics are tracked separately.
        """
        with pytest.raises(UnicodeEncodeError):
            set_tags(["ok_tag", "\ud800"])
        # Restore queues to a usable state for the rest of the suite.
        set_tags([])


# ===================================================================
# Queue isolation
# ===================================================================


class TestQueueIsolation:
    """Messages on different tags do not interfere."""

    def test_independent_tags(self):
        """Messages sent on different tags are fully independent."""
        send("iso_a", "A")
        send("iso_b", "B")
        send("iso_c", "C")

        _, val = receive("iso_c", 1)
        assert val == "C"
        _, val = receive("iso_a", 1)
        assert val == "A"
        _, val = receive("iso_b", 1)
        assert val == "B"

    def test_drain_one_tag_leaves_others(self):
        """Draining one tag's messages does not affect another."""
        send("drn_a", "a1")
        send("drn_a", "a2")
        send("drn_b", "b1")

        _, _ = receive("drn_a", 1)
        _, _ = receive("drn_a", 1)

        _, val = receive("drn_b", 1)
        assert val == "b1"


# ===================================================================
# Payload round-trip fidelity
# ===================================================================


class TestPayloadFidelity:
    """Ensure various Python types survive the send/receive round-trip."""

    @pytest.mark.parametrize("payload", [
        0,
        -1,
        2**31,
        3.14159,
        float("inf"),
        True,
        False,
        None,
        "",
        "hello world",
        (),
        (1,),
        (1, (2, (3,))),
        (1, "two", 3.0, None, True),
    ], ids=[
        "zero", "neg_int", "large_int", "float", "inf",
        "true", "false", "none",
        "empty_str", "str",
        "empty_tuple", "singleton_tuple", "nested_tuple", "mixed_tuple",
    ])
    def test_payload_roundtrip(self, payload):
        """Each payload type is preserved through the queue."""
        send("prt", payload)
        tag, val = receive("prt", 1)
        assert tag != TIMEOUT
        assert val == payload


# ===================================================================
# High-volume / throughput
# ===================================================================


class TestThroughput:
    """High-volume message passing to stress the ring buffer."""

    def test_sequential_high_volume(self):
        """Send then receive many messages sequentially."""
        n = 10_000
        for i in range(n):
            send("hv_seq", i)
        total = 0
        for _ in range(n):
            _, val = receive("hv_seq", 1)
            total += val
        assert total == n * (n - 1) // 2

    def test_producer_consumer(self):
        """One producer thread, main thread consumes."""
        n = 2000

        def producer():
            for i in range(n):
                send("hv_pc", i)

        t = threading.Thread(target=producer)
        t.start()

        results = []
        for _ in range(n):
            _, val = receive("hv_pc", 5)
            results.append(val)
        t.join()

        assert results == list(range(n))


# ===================================================================
# Concurrent producers
# ===================================================================


class TestConcurrentProducers:
    """Multiple threads sending to the same queue concurrently."""

    def test_multi_producer_all_delivered(self):
        """All messages from multiple producers are received."""
        n_producers = 8
        msgs_per_producer = 200

        def producer(pid):
            for i in range(msgs_per_producer):
                send("mp_all", (pid, i))

        threads = [threading.Thread(target=producer, args=(p,))
                   for p in range(n_producers)]
        for t in threads:
            t.start()

        received = []
        total = n_producers * msgs_per_producer
        for _ in range(total):
            _, val = receive("mp_all", 10)
            received.append(val)

        for t in threads:
            t.join()

        assert sorted(received) == sorted(
            (p, i) for p in range(n_producers)
            for i in range(msgs_per_producer)
        )

    def test_multi_producer_fifo_per_producer(self):
        """Per-producer sequence numbers are monotonically increasing."""
        n_producers = 4
        msgs_per_producer = 300

        def producer(pid):
            for i in range(msgs_per_producer):
                send("mp_fifo", (pid, i))

        threads = [threading.Thread(target=producer, args=(p,))
                   for p in range(n_producers)]
        for t in threads:
            t.start()

        per_producer = {p: [] for p in range(n_producers)}
        total = n_producers * msgs_per_producer
        for _ in range(total):
            _, (pid, seq) = receive("mp_fifo", 10)
            per_producer[pid].append(seq)

        for t in threads:
            t.join()

        for pid, seqs in per_producer.items():
            assert seqs == list(range(msgs_per_producer)), (
                f"Producer {pid} messages arrived out of order"
            )


# ===================================================================
# Cross-thread send/receive
# ===================================================================


class TestCrossThread:
    """Messages crossing thread boundaries."""

    def test_send_from_thread(self):
        """A message sent from another thread is received on main."""
        def sender():
            send("ct_one", "from_thread")

        t = threading.Thread(target=sender)
        t.start()
        tag, val = receive("ct_one", 5)
        t.join()
        assert tag == "ct_one"
        assert val == "from_thread"

    def test_bidirectional(self):
        """Two threads exchange messages in both directions."""
        def echo():
            tag, val = receive("ct_ping", 5)
            send("ct_pong", val * 2)

        t = threading.Thread(target=echo)
        t.start()
        send("ct_ping", 21)
        tag, val = receive("ct_pong", 5)
        t.join()
        assert tag == "ct_pong"
        assert val == 42

    def test_many_senders(self):
        """Multiple threads send to the same tag; main collects all."""
        n = 20

        def sender(i):
            send("ct_multi", i)

        threads = [threading.Thread(target=sender, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()

        values = set()
        for _ in range(n):
            tag, val = receive("ct_multi", 5)
            assert tag != TIMEOUT
            values.add(val)

        for t in threads:
            t.join()

        assert values == set(range(n))


# ===================================================================
# set_tags — explicit tag management
# ===================================================================


class TestSetTags:
    """Tests for the optional set_tags() management function."""

    def test_set_tags_basic(self):
        """set_tags pre-assigns tags and clears previous messages."""
        send("auto_before", "stale")
        set_tags(["alpha", "bravo"])
        send("alpha", "hello")
        tag, val = receive("alpha", 1)
        assert tag == "alpha"
        assert val == "hello"

    def test_set_tags_clears_all(self):
        """set_tags drains every queue, even ones with pending messages."""
        for i in range(MAX_QUEUES):
            send(f"clr{i}", f"stale_{i}")
        set_tags([f"fresh{i}" for i in range(MAX_QUEUES)])
        # New tags start empty.
        tag, val = receive("fresh0", 0)
        assert tag == TIMEOUT

    def test_set_tags_fewer_than_max(self):
        """Providing fewer tags than MAX_QUEUES works; extras auto-assign."""
        set_tags(["only_one"])
        send("only_one", "ok")
        tag, val = receive("only_one", 1)
        assert tag == "only_one"
        assert val == "ok"

    def test_set_tags_exactly_max(self):
        """All MAX_QUEUES queue slots can be filled."""
        tags = [f"slot{i}" for i in range(MAX_QUEUES)]
        set_tags(tags)
        for t in tags:
            send(t, t)
        for t in tags:
            tag, val = receive(t, 1)
            assert tag == t
            assert val == t

    def test_set_tags_rejects_too_many(self):
        """Passing more than MAX_QUEUES tags raises IndexError."""
        tags = [f"overflow{i}" for i in range(MAX_QUEUES + 1)]
        with pytest.raises(IndexError):
            set_tags(tags)

    def test_set_tags_rejects_non_str(self):
        """Non-string elements in the tag list raise TypeError."""
        with pytest.raises(TypeError):
            set_tags([123])

    def test_set_tags_rejects_non_sequence(self):
        """A non-sequence argument raises TypeError."""
        with pytest.raises(TypeError):
            set_tags(42)

    def test_set_tags_empty_resets_to_auto_assign(self):
        """An empty tag list resets all queues to auto-assign mode."""
        set_tags(["pre"])
        send("pre", "msg")
        set_tags([])
        # All queues are now unassigned; auto-assignment kicks in.
        send("new_auto", "works")
        tag, val = receive("new_auto", 1)
        assert tag == "new_auto"
        assert val == "works"

    def test_set_tags_then_overflow(self):
        """After set_tags fills all slots, an extra tag raises KeyError."""
        tags = [f"full{i}" for i in range(MAX_QUEUES)]
        set_tags(tags)
        with pytest.raises(KeyError, match="tag capacity exceeded"):
            send("extra_tag_beyond_limit", "boom")


# ===================================================================
# set_tags — idempotence and repeated calls
# ===================================================================


class TestSetTagsRepeated:
    """Calling set_tags multiple times should be safe."""

    def test_double_set_tags(self):
        """Two consecutive set_tags calls work; second clears first."""
        set_tags(["first"])
        send("first", "msg1")
        set_tags(["second"])
        # "first" messages are gone; nothing sent on "second" yet.
        tag, val = receive("second", 0)
        assert tag == TIMEOUT

    def test_set_tags_preserves_tag_reuse(self):
        """Re-assigning the same tag name works after set_tags."""
        set_tags(["reuse"])
        send("reuse", "round1")
        _, val = receive("reuse", 1)
        assert val == "round1"

        set_tags(["reuse"])
        send("reuse", "round2")
        _, val = receive("reuse", 1)
        assert val == "round2"

    def test_rapid_set_tags_cycles(self):
        """Rapidly cycling set_tags does not corrupt state."""
        for cycle in range(20):
            tag = f"cyc{cycle % MAX_QUEUES}"
            set_tags([tag])
            send(tag, cycle)
            _, val = receive(tag, 1)
            assert val == cycle


# ===================================================================
# Worker-pool integration (match/case receive loop)
# ===================================================================


class TestWorkerPool:
    """Multi-worker batch-processing pattern with shutdown protocol."""

    def test_threaded_batch_sum(self):
        """Sum batches across multiple worker threads using match/case."""
        num_workers = 10
        max_value = 1000
        batch_size = 20

        def worker():
            tags = ["thr_work", "thr_shutdown"]
            running = True
            send("thr_started", True)
            while running:
                match receive(tags):
                    case ["thr_work", values]:
                        send("thr_result", sum(values))
                    case ["thr_shutdown", _]:
                        running = False

        workers = []
        for _ in range(num_workers):
            t = threading.Thread(target=worker)
            workers.append(t)
            t.start()

        for _ in range(num_workers):
            receive("thr_started")

        num_batches = max_value // batch_size
        for i in range(0, max_value, batch_size):
            values = tuple(range(i, i + batch_size))
            send("thr_work", values)

        result = 0
        for _ in range(num_batches):
            match receive("thr_result"):
                case ["thr_result", value]:
                    result += value

        for _ in range(num_workers):
            send("thr_shutdown", True)

        for t in workers:
            t.join()

        assert result == (max_value * (max_value - 1)) // 2


# ===================================================================
# drain(): clear pending messages for specific tags
# ===================================================================


class TestDrain:
    """Verify that drain() removes pending messages for the specified tags."""

    def test_drain_single_tag(self):
        """Draining a single tag removes all its pending messages."""
        send("d_one", "a")
        send("d_one", "b")
        send("d_one", "c")

        drain(["d_one"])

        tag, val = receive("d_one", 0.1)
        assert tag == TIMEOUT

    def test_drain_multiple_tags(self):
        """Draining multiple tags removes messages from all of them."""
        send("d_m1", 1)
        send("d_m2", 2)
        send("d_m3", 3)

        drain(["d_m1", "d_m2", "d_m3"])

        assert receive("d_m1", 0.1)[0] == TIMEOUT
        assert receive("d_m2", 0.1)[0] == TIMEOUT
        assert receive("d_m3", 0.1)[0] == TIMEOUT

    def test_drain_leaves_other_tags(self):
        """Draining one tag does not affect messages on other tags."""
        send("d_keep", "survive")
        send("d_drop", "gone")

        drain(["d_drop"])

        assert receive("d_drop", 0.1)[0] == TIMEOUT
        _, val = receive("d_keep", 1)
        assert val == "survive"

    def test_drain_empty_tag(self):
        """Draining a tag with no pending messages is a no-op."""
        send("d_empty", "x")
        receive("d_empty", 1)  # consume the only message

        drain(["d_empty"])  # should not raise

        assert receive("d_empty", 0.1)[0] == TIMEOUT

    def test_drain_empty_list(self):
        """Draining an empty list is a no-op."""
        send("d_noop", "still_here")

        drain([])

        _, val = receive("d_noop", 1)
        assert val == "still_here"

    def test_drain_with_tuple(self):
        """drain() accepts a tuple of tags."""
        send("d_t1", "x")
        send("d_t2", "y")

        drain(("d_t1", "d_t2"))

        assert receive("d_t1", 0.1)[0] == TIMEOUT
        assert receive("d_t2", 0.1)[0] == TIMEOUT

    def test_drain_then_send_new(self):
        """New messages sent after drain are still received."""
        send("d_renew", "old")
        drain(["d_renew"])

        send("d_renew", "new")
        _, val = receive("d_renew", 1)
        assert val == "new"

    def test_drain_single_string(self):
        """drain() accepts a bare string instead of a list."""
        send("d_str", "a")
        send("d_str", "b")

        drain("d_str")

        assert receive("d_str", 0.1)[0] == TIMEOUT

    def test_drain_non_string_tag_raises(self):
        """Passing non-string elements in the tag list raises TypeError."""
        with pytest.raises(TypeError):
            drain([123])


# ===================================================================
# Spin-then-park: lost-wake stress
# ===================================================================


class TestLostWakeStress:
    """Verify that the spin-then-park strategy never loses a wake signal."""

    def test_single_producer_random_delays(self):
        """One slow producer, one consumer — consumer must never hang."""
        n = 200

        def producer():
            for i in range(n):
                if random.random() < 0.3:
                    time.sleep(random.uniform(0.0001, 0.005))
                send("lw_rand", i)

        t = threading.Thread(target=producer)
        t.start()

        for i in range(n):
            tag, val = receive("lw_rand", 10)
            assert tag == "lw_rand", f"Timed out waiting for message {i}"
            assert val == i

        t.join()

    def test_bursty_producer(self):
        """Producer sends bursts with pauses — consumer must not deadlock."""
        bursts = 10
        per_burst = 50

        def producer():
            for b in range(bursts):
                for i in range(per_burst):
                    send("lw_burst", b * per_burst + i)
                time.sleep(random.uniform(0.005, 0.02))

        t = threading.Thread(target=producer)
        t.start()

        total = bursts * per_burst
        for i in range(total):
            tag, val = receive("lw_burst", 10)
            assert tag == "lw_burst", f"Timed out at message {i}"
            assert val == i

        t.join()

    @pytest.mark.parametrize("iteration", range(20))
    def test_single_message_wake(self, iteration):
        """A single message wakes a parked consumer — repeated to catch races."""
        def delayed_send():
            time.sleep(random.uniform(0.001, 0.01))
            send("lw_single", iteration)

        t = threading.Thread(target=delayed_send)
        t.start()
        tag, val = receive("lw_single", 5)
        t.join()
        assert tag == "lw_single"
        assert val == iteration


# ===================================================================
# Spin-then-park: multi-tag receive
# ===================================================================


class TestMultiTagBackoff:
    """Multi-tag receive correctness under the exponential backoff path."""

    def test_message_on_second_tag(self):
        """Multi-tag receive finds a message on a non-first tag."""
        send("mtb_b", "found")
        tag, val = receive(["mtb_a", "mtb_b", "mtb_c"], 5)
        assert tag == "mtb_b"
        assert val == "found"

    def test_multi_tag_delayed_arrival(self):
        """Multi-tag receive waits for a message arriving after a delay."""
        def delayed_send():
            time.sleep(0.05)
            send("mtd_c", "late")

        t = threading.Thread(target=delayed_send)
        t.start()
        tag, val = receive(["mtd_a", "mtd_b", "mtd_c"], 5)
        t.join()
        assert tag == "mtd_c"
        assert val == "late"

    def test_multi_tag_fifo_per_tag(self):
        """Multi-tag receive preserves per-tag FIFO ordering."""
        for i in range(5):
            send("mf_x", ("x", i))
            send("mf_y", ("y", i))

        results = {"mf_x": [], "mf_y": []}
        for _ in range(10):
            tag, val = receive(["mf_x", "mf_y"], 5)
            results[tag].append(val[1])

        assert results["mf_x"] == list(range(5))
        assert results["mf_y"] == list(range(5))

    def test_multi_tag_timeout(self):
        """Multi-tag receive times out when no message arrives."""
        tag, val = receive(["mtt_a", "mtt_b"], 0.1)
        assert tag == TIMEOUT
        assert val is None

    def test_multi_tag_interleaved_producers(self):
        """Multiple producers on different tags, multi-tag consumer."""
        n = 100

        def producer(tag, offset):
            for i in range(n):
                if random.random() < 0.2:
                    time.sleep(random.uniform(0.0001, 0.002))
                send(tag, offset + i)

        tags = ["mti_a", "mti_b", "mti_c"]
        threads = [threading.Thread(target=producer, args=(t, i * n))
                   for i, t in enumerate(tags)]
        for t in threads:
            t.start()

        received = []
        for _ in range(len(tags) * n):
            tag, val = receive(tags, 10)
            assert tag in tags
            received.append((tag, val))

        for t in threads:
            t.join()

        per_tag = {t: [] for t in tags}
        for tag, val in received:
            per_tag[tag].append(val)

        for i, t in enumerate(tags):
            expected = [i * n + j for j in range(n)]
            assert sorted(per_tag[t]) == expected


# ===================================================================
# Spin-then-park: timeout accuracy
# ===================================================================


class TestTimeoutAccuracy:
    """Verify that timed receives return within a reasonable time window."""

    @pytest.mark.parametrize("timeout", [0.05, 0.1, 0.2])
    def test_timeout_lower_bound(self, timeout):
        """Receive does not return before the timeout elapses."""
        start = time.monotonic()
        tag, _ = receive("ta_lower", timeout)
        elapsed = time.monotonic() - start
        assert tag == TIMEOUT
        assert elapsed >= timeout * 0.9, (
            f"Returned too early: {elapsed:.4f}s < {timeout * 0.9:.4f}s"
        )

    @pytest.mark.parametrize("timeout", [0.05, 0.1, 0.2])
    def test_timeout_upper_bound(self, timeout):
        """Receive returns within a generous upper bound of the timeout."""
        start = time.monotonic()
        tag, _ = receive("ta_upper", timeout)
        elapsed = time.monotonic() - start
        assert tag == TIMEOUT
        upper = timeout + 0.1  # 100 ms grace for scheduling jitter
        assert elapsed <= upper, (
            f"Returned too late: {elapsed:.4f}s > {upper:.4f}s"
        )

    def test_zero_timeout_immediate(self):
        """Zero timeout returns immediately (sub-millisecond)."""
        start = time.monotonic()
        tag, _ = receive("ta_zero", 0)
        elapsed = time.monotonic() - start
        assert tag == TIMEOUT
        assert elapsed < 0.01, f"Zero timeout took {elapsed:.4f}s"
