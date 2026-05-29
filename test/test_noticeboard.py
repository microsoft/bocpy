"""Tests for the noticeboard feature."""

from functools import partial

import pytest

from bocpy import (Cown, drain, notice_delete, notice_read, notice_sync,
                   notice_update, notice_write, noticeboard,
                   receive,
                   REMOVED, send, start, TIMEOUT, wait, when)
import bocpy._core as _core


RECEIVE_TIMEOUT = 10


def receive_asserts(count=1):
    """Drain all expected assertion messages, then fail on first mismatch.

    The "assert" queue is always drained before returning so that leftover
    messages from a failing test do not leak into subsequent tests in CI.
    """
    failed = None
    timed_out = False
    try:
        for _ in range(count):
            result = receive("assert", RECEIVE_TIMEOUT)
            if result[0] == TIMEOUT:
                timed_out = True
                break
            _, (actual, expected) = result
            if failed is None and actual != expected:
                failed = (actual, expected)
    finally:
        drain("assert")

    assert not timed_out, (
        "Timed out waiting for an 'assert' message from a behavior. "
        "Check that every @when arg count matches the decorated "
        "function's parameter count."
    )
    if failed is not None:
        actual, expected = failed
        assert actual == expected, f"expected {expected!r}, got {actual!r}"


class TestNoticeboard:
    """Tests for noticeboard write/read round-trip and snapshot isolation."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_write_then_read_roundtrip(self):
        """Write a value in one behavior, read it in a subsequent one."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("greeting", "hello")
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            send("assert", (snap.get("greeting"), "hello"))

        receive_asserts()

    def test_write_overwrite(self):
        """Overwriting a key replaces the previous value."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("counter", 10)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            notice_write("counter", 20)
            notice_sync()

        @when(x, step2)
        def step3(x, _):
            snap = noticeboard()
            send("assert", (snap.get("counter"), 20))

        receive_asserts()

    def test_snapshot_returns_mapping(self):
        """Snapshot returns a read-only mapping even with no writes."""
        x = Cown(0)

        @when(x)
        def _(x):
            from collections.abc import Mapping
            snap = noticeboard()
            send("assert", (isinstance(snap, Mapping), True))

        receive_asserts()

    def test_multiple_keys(self):
        """Multiple keys can coexist in the noticeboard."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("a", 1)
            notice_write("b", 2)
            notice_write("c", 3)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            send("assert", (snap.get("a"), 1))
            send("assert", (snap.get("b"), 2))
            send("assert", (snap.get("c"), 3))

        receive_asserts(3)

    def test_frozen_snapshot(self):
        """Snapshot is frozen: a write after snapshot doesn't change it."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("val", 100)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap1 = noticeboard()
            notice_write("val", 200)
            notice_sync()
            snap2 = noticeboard()
            # Both calls in the same behavior return the same cached snapshot
            send("assert", (snap1.get("val"), 100))
            send("assert", (snap1.get("val"), snap2.get("val")))

        receive_asserts(2)

    def test_snapshot_cache_cleared_between_behaviors(self):
        """Each behavior gets a fresh snapshot, not the previous one's cache."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("seq", 1)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            send("assert", (snap.get("seq"), 1))
            notice_write("seq", 2)
            notice_sync()

        @when(x, step2)
        def step3(x, _):
            snap = noticeboard()
            send("assert", (snap.get("seq"), 2))

        receive_asserts(2)

    def test_picklable_value(self):
        """Complex (picklable) values round-trip through the noticeboard."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("data", [1, 2, 3])
            notice_sync()

        @when(x)
        def step2(x):
            snap = noticeboard()
            send("assert", (snap.get("data"), [1, 2, 3]))

        receive_asserts()

    def test_set_value_forces_pickle_path(self):
        """A set is not natively shareable and must take the pickle path."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("tags", {1, 2, 3})
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            send("assert", (snap.get("tags"), {1, 2, 3}))

        receive_asserts()

    def test_int_value(self):
        """Integer values (native cross-interpreter) round-trip correctly."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("num", 42)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            send("assert", (snap.get("num"), 42))

        receive_asserts()

    def test_none_value(self):
        """None round-trips through the noticeboard."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("empty", None)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            send("assert", ("empty" in snap, True))
            send("assert", (snap["empty"], None))

        receive_asserts(2)

    def test_notice_read_existing_key(self):
        """notice_read returns the value for an existing key."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("color", "blue")
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            send("assert", (notice_read("color"), "blue"))

        receive_asserts()

    def test_notice_read_missing_key_default(self):
        """notice_read returns None for a missing key by default."""
        x = Cown(0)

        @when(x)
        def _(x):
            send("assert", (notice_read("nonexistent"), None))

        receive_asserts()

    def test_notice_read_missing_key_custom_default(self):
        """notice_read returns the custom default for a missing key."""
        x = Cown(0)

        @when(x)
        def _(x):
            send("assert", (notice_read("nonexistent", 42), 42))

        receive_asserts()

    def test_notice_read_uses_cached_snapshot(self):
        """Two notice_read calls in the same behavior use the same snapshot."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("tick", 1)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            val1 = notice_read("tick")
            notice_write("tick", 99)
            notice_sync()
            val2 = notice_read("tick")
            # Both reads see the cached snapshot, not the new write
            send("assert", (val1, val2))

        receive_asserts()


class TestNoticeboardBoundary:
    """Boundary tests for noticeboard key length and entry capacity."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def setup_method(self):
        """Clear the noticeboard before each boundary test."""
        _core.noticeboard_clear()

    def test_max_key_length_63_bytes(self):
        """A key of exactly 63 UTF-8 bytes is accepted."""
        x = Cown(0)
        long_key = "k" * 63  # exactly 63 bytes

        @when(x)
        def step1(x):
            notice_write(long_key, "ok")
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            val = notice_read(long_key)
            send("assert", (val, "ok"))

        receive_asserts()

    def test_key_length_64_bytes_rejected(self):
        """A key of 64 UTF-8 bytes is rejected with ValueError."""
        x = Cown(0)
        too_long = "k" * 64  # 64 bytes, exceeds 63-byte limit

        @when(x)
        def _(x):
            try:
                notice_write(too_long, "fail")
                notice_sync()
                send("assert", (False, True))  # should not reach here
            except ValueError:
                send("assert", (True, True))

        receive_asserts()

    def test_64_entries_accepted(self):
        """The noticeboard accepts up to 64 distinct keys."""
        x = Cown(0)

        @when(x)
        def step1(x):
            for i in range(64):
                notice_write(f"slot{i}", i)
                notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            send("assert", (len(snap) >= 64, True))
            send("assert", (snap.get("slot0"), 0))
            send("assert", (snap.get("slot63"), 63))

        receive_asserts(3)

    def test_65th_entry_silently_dropped(self):
        """The 65th distinct key is silently dropped by the noticeboard thread."""
        x = Cown(0)

        @when(x)
        def step1(x):
            for i in range(65):
                notice_write(f"cap{i}", i)
                notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            # Only 64 entries should be present; the 65th is dropped
            cap_keys = [k for k in snap if k.startswith("cap")]
            send("assert", (len(cap_keys), 64))
            # The first 64 keys (cap0..cap63) should be present
            send("assert", (snap.get("cap0"), 0))
            send("assert", (snap.get("cap63"), 63))
            # The 65th key (cap64) should be missing
            send("assert", ("cap64" not in snap, True))

        receive_asserts(4)

    def test_write_non_string_key_rejected(self):
        """Non-string key raises TypeError."""
        x = Cown(0)

        @when(x)
        def _(x):
            try:
                notice_write(123, "value")
                notice_sync()
                send("assert", (False, True))
            except TypeError:
                send("assert", (True, True))

        receive_asserts()

    def test_key_with_nul_rejected(self):
        """A key containing NUL is rejected with ValueError."""
        x = Cown(0)

        @when(x)
        def _(x):
            try:
                notice_write("a\x00b", "value")
                notice_sync()
                send("assert", (False, True))
            except ValueError:
                send("assert", (True, True))

        receive_asserts()


class TestNoticeboardConcurrency:
    """Stress tests for concurrent noticeboard writes from independent behaviors."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def setup_method(self):
        """Clear the noticeboard before each test."""
        _core.noticeboard_clear()

    def test_concurrent_writes_from_independent_behaviors(self):
        """Independent behaviors on separate cowns write unique keys concurrently."""
        cowns = [Cown(i) for i in range(8)]
        for i in range(8):

            @when(cowns[i])
            def writer(c):
                notice_write(f"cw_{c.value}", c.value * 10)
                # Block this behavior until the write commits, so the
                # reader (which acquires every cown below) is guaranteed
                # to observe it.
                notice_sync()

        # The reader requires every writer cown, so it cannot run until
        # every writer behavior has returned — and notice_sync() above
        # ensures each writer's mutation is committed before it returns.
        @when(cowns)
        def reader(cowns):
            snap = noticeboard()
            count = sum(1 for k in snap if k.startswith("cw_"))
            send("assert", (count, 8))
            send("assert", (snap.get("cw_0"), 0))
            send("assert", (snap.get("cw_7"), 70))

        receive_asserts(3)


class TestNoticeboardUTF8:
    """Tests for multi-byte UTF-8 key handling."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def setup_method(self):
        """Clear the noticeboard before each test."""
        _core.noticeboard_clear()

    def test_multibyte_key_within_limit(self):
        """A 3-byte character at byte position 60 fits within 63-byte limit."""
        x = Cown(0)
        # "€" is 3 UTF-8 bytes; 60 ASCII + 3 = 63 bytes total
        key_63 = "a" * 60 + "€"

        @when(x)
        def step1(x):
            notice_write(key_63, "ok")
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            val = notice_read(key_63)
            send("assert", (val, "ok"))

        receive_asserts()

    def test_multibyte_key_exceeds_limit(self):
        """A 3-byte character at byte position 61 exceeds the 63-byte limit."""
        x = Cown(0)
        # 61 ASCII + 3 = 64 bytes total, exceeds limit
        key_64 = "a" * 61 + "€"

        @when(x)
        def _(x):
            try:
                notice_write(key_64, "fail")
                notice_sync()
                send("assert", (False, True))
            except ValueError:
                send("assert", (True, True))

        receive_asserts()


class TestNoticeboardRestart:
    """Tests for noticeboard state across runtime restart."""

    def test_noticeboard_empty_after_restart(self):
        """After wait() + new behaviors, noticeboard starts fresh."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("before_restart", 42)
            notice_sync()

        @when(x)
        def step2(x):
            snap = noticeboard()
            send("assert", (snap.get("before_restart"), 42))

        receive_asserts()
        wait()

        # Start fresh — noticeboard should be cleared by stop()
        y = Cown(0)

        @when(y)
        def check(y):
            snap = noticeboard()
            send("assert", ("before_restart" not in snap, True))

        receive_asserts()
        wait()


# Module-level helpers for notice_update tests (must be picklable).


def _increment(x):
    """Return x + 1."""
    return x + 1


def _add_ten(x):
    """Return x + 10."""
    return x + 10


def _wrap_value(x):
    """Return (x, 'seen') to verify what fn received."""
    return (x, "seen")


def _div_by_zero(x):
    """Raise ZeroDivisionError."""
    return x / 0


def _return_removed(x):
    """Return the REMOVED sentinel."""
    return REMOVED


def _conditionally_remove(x):
    """Return REMOVED if x > 100, else x + 1."""
    if x > 100:
        return REMOVED
    return x + 1


class TestNoticeUpdate:
    """Tests for notice_update atomic read-modify-write."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def setup_method(self):
        """Clear the noticeboard before each test."""
        _core.noticeboard_clear()

    def test_basic_increment(self):
        """Update an existing key with a module-level function."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("counter", 10)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            notice_update("counter", _increment)
            notice_sync()

        @when(x, step2)
        def step3(x, _):
            send("assert", (notice_read("counter"), 11))

        receive_asserts()

    def test_default_on_absent_key(self):
        """Update a missing key uses the default value."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_update("missing", _add_ten, default=0)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            send("assert", (notice_read("missing"), 10))

        receive_asserts()

    def test_none_sentinel(self):
        """A key holding None is distinguished from an absent key."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("k", None)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            notice_update("k", _wrap_value, default="WRONG")
            notice_sync()

        @when(x, step2)
        def step3(x, _):
            val = notice_read("k")
            # fn should have received None (the stored value), not "WRONG"
            send("assert", (val, (None, "seen")))

        receive_asserts()

    def test_concurrent_updates(self):
        """Multiple independent behaviors updating the same key."""
        n = 8
        cowns = [Cown(i) for i in range(n)]
        for i in range(n):

            @when(cowns[i])
            def writer(c):
                notice_update("counter", _increment, default=0)
                notice_sync()

        # Reader requires every writer cown -> runs only after every
        # writer behavior returns -> after every notice_sync() commits.
        @when(cowns)
        def reader(_):
            send("assert", (notice_read("counter"), n))

        receive_asserts()

    def test_key_validation_type(self):
        """Non-string key raises TypeError."""
        x = Cown(0)

        @when(x)
        def _(x):
            try:
                notice_update(123, _increment)
                notice_sync()
                send("assert", (False, True))
            except TypeError:
                send("assert", (True, True))

        receive_asserts()

    def test_fn_not_callable(self):
        """Non-callable fn raises TypeError."""
        x = Cown(0)

        @when(x)
        def _(x):
            try:
                notice_update("key", "not_callable")
                notice_sync()
                send("assert", (False, True))
            except TypeError:
                send("assert", (True, True))

        receive_asserts()

    def test_fn_raises_keeps_previous_value(self):
        """If fn raises, the key retains its previous value."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("safe", 42)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            notice_update("safe", _div_by_zero)
            notice_sync()

        @when(x, step2)
        def step3(x, _):
            send("assert", (notice_read("safe"), 42))

        receive_asserts()

    def test_functools_partial(self):
        """functools.partial with a builtin works as fn."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_update("best", partial(max, 42), default=0)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            send("assert", (notice_read("best"), 42))

        receive_asserts()


class TestNoticeboardReadOnly:
    """Tests that the snapshot is read-only (MappingProxyType)."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_snapshot_mutation_rejected(self):
        """Direct mutation of the snapshot raises TypeError."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("immut", 1)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            try:
                snap["immut"] = 999
                send("assert", (False, True))  # should not reach here
            except TypeError:
                send("assert", (True, True))
            # Original value is unaffected
            send("assert", (notice_read("immut"), 1))

        receive_asserts(2)

    def test_snapshot_del_rejected(self):
        """Deleting a key from the snapshot raises TypeError."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("del_test", 42)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            try:
                del snap["del_test"]
                send("assert", (False, True))
            except TypeError:
                send("assert", (True, True))

        receive_asserts()


class TestNoticeboardPreRuntime:
    """Tests for noticeboard calls before the runtime is started."""

    @classmethod
    def setup_class(cls):
        """Ensure runtime is stopped before this class runs.

        These tests never start the runtime, so we do not need per-test
        wait() calls; one shutdown at class entry is enough and avoids
        hammering the worker lifecycle (which can intermittently trip
        CPython 3.13 sub-interpreter teardown bugs).
        """
        wait()

    def test_notice_write_before_start(self):
        """notice_write raises RuntimeError before the runtime is started."""
        with pytest.raises(RuntimeError, match="cannot write to the noticeboard"):
            notice_write("key", "value")
            notice_sync()

    def test_notice_update_before_start(self):
        """notice_update raises RuntimeError before the runtime is started."""
        with pytest.raises(RuntimeError, match="cannot update the noticeboard"):
            notice_update("key", _increment)
            notice_sync()


class TestNoticeboardFireAndForget:
    """Tests for fire-and-forget write semantics."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def setup_method(self):
        """Clear the noticeboard before each test."""
        _core.noticeboard_clear()

    def test_write_persists_after_behavior_failure(self):
        """A notice_write sent before a behavior raises is still applied."""
        x = Cown(0)

        @when(x)
        def failing(x):
            notice_write("survivor", 42)
            notice_sync()
            raise ValueError("intentional failure")

        @when(x, failing)
        def check(x, _):
            send("assert", (notice_read("survivor"), 42))

        receive_asserts()


# Module-level helpers for notice_delete / REMOVED tests.


def _read_ring_first_value(_ignored):
    """Return the value of ``ring[0]`` from the noticeboard.

    Module-level so the transpiler can serialize it for the worker.
    """
    ring = noticeboard()["ring"]
    return ring[0].value


def _read_ring_size(_ignored):
    """Return the length of the noticeboard's ``ring`` entry."""
    return len(noticeboard()["ring"])


class SlotHolder:
    """Slot-only container used by the `__slots__` MRO regression test.

    Module-level so the transpiler can serialize it for the workers.
    Has no ``__dict__``; every attribute lives in a slot.
    """

    __slots__ = ("cown", "label")

    def __init__(self, cown, label):
        """Store *cown* and *label* as the instance's only state."""
        self.cown = cown
        self.label = label


class SlotSubclass(SlotHolder):
    """Slot-only subclass: slots declared at a different MRO level."""

    __slots__ = ("extra",)

    def __init__(self, cown, label, extra):
        """Initialise the base fields plus a subclass-only slot."""
        super().__init__(cown, label)
        self.extra = extra


class TestNoticeboardCownPinning:
    """Regression tests: cowns stored on the noticeboard outlive the writer.

    These cover the bug where a ``Cown`` placed on the noticeboard was
    only kept alive by the original wrapper's COWN_INCREF; once the
    wrapper went out of scope, every worker that had unpickled a copy
    would issue a matching DECREF on dealloc, sending the underlying
    BOCCown's refcount negative. The fix takes an independent strong
    reference inside the noticeboard entry.
    """

    @classmethod
    def setup_class(cls):
        """Start the runtime so the noticeboard thread is registered."""
        start()

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def setup_method(self):
        """Clear the noticeboard before each test."""
        _core.noticeboard_clear()

    def test_ring_of_cowns_survives_writer_dropping_reference(self):
        """A list of cowns on the noticeboard is usable after writer drops it."""
        # Build a small ring of cowns in a behavior, publish it to the
        # noticeboard, then drop every local reference to the ring on the
        # writer side. The noticeboard becomes the only thing keeping the
        # cowns alive across worker reads.
        x = Cown(0)

        @when(x)
        def writer(x):
            ring = [Cown(i * 10) for i in range(8)]
            notice_write("ring", ring)
            notice_sync()
            # Local goes out of scope at function return — only the
            # noticeboard's pin is left.

        @when(x, writer)
        def first_read(x, _):
            ring = noticeboard()["ring"]
            send("assert", (len(ring), 8))

        @when(x, first_read)
        def second_read(x, _):
            ring = noticeboard()["ring"]
            send("assert", (len(ring), 8))

        @when(x, second_read)
        def acquire_first(x, _):
            ring = noticeboard()["ring"]
            # Acquire the first cown for read; this dereferences the
            # underlying BOCCown and would assert if it had been freed.
            with ring[0] as v:
                send("assert", (v, 0))

        receive_asserts(count=3)

    def test_overwrite_releases_old_cown_pins(self):
        """Overwriting a noticeboard entry releases the old entry's pins."""
        x = Cown(0)

        @when(x)
        def first_write(x):
            first = [Cown(i) for i in range(4)]
            notice_write("ring", first)
            notice_sync()

        @when(x, first_write)
        def second_write(x, _):
            second = [Cown(100 + i) for i in range(4)]
            notice_write("ring", second)
            notice_sync()

        @when(x, second_write)
        def check(x, _):
            ring = noticeboard()["ring"]
            with ring[0] as v:
                send("assert", (v, 100))

        receive_asserts()

    def test_delete_releases_cown_pins(self):
        """notice_delete drops the entry's pins; a fresh write reuses the slot."""
        x = Cown(0)

        @when(x)
        def initial_write(x):
            ring = [Cown(i) for i in range(3)]
            notice_write("ring", ring)
            notice_sync()

        @when(x, initial_write)
        def remove_entry(x, _):
            notice_delete("ring")
            notice_sync()

        # The delete is non-blocking; verify in a subsequent behavior so
        # the noticeboard thread has had a chance to process the message and the
        # per-behavior snapshot cache is rebuilt.
        @when(x, remove_entry)
        def verify_gone(x, _):
            send("assert", ("ring" in noticeboard(), False))

        # After delete + new write the noticeboard reads the new entry.
        @when(x, verify_gone)
        def write_new(x, _):
            new_ring = [Cown(999)]
            notice_write("ring", new_ring)
            notice_sync()

        @when(x, write_new)
        def check_new(x, _):
            ring = noticeboard()["ring"]
            with ring[0] as v:
                send("assert", (v, 999))

        receive_asserts(count=2)

    def test_slot_only_holder_cown_survives_writer(self):
        """Cowns reachable through ``__slots__`` are pinned by the noticeboard.

        Regression: ``_collect_cown_capsules`` used to only descend
        into ``obj.__dict__``. A slot-only class has no ``__dict__``,
        so any cown stored in a slot attribute was silently dropped
        from the pin list -- the BOCCown would be freed with pickled
        bytes still referring to it, and the next reader would crash
        on the dangling pointer.
        """
        x = Cown(0)

        @when(x)
        def writer(x):
            holder = SlotHolder(Cown(12345), "first")
            notice_write("slot_holder", holder)
            notice_sync()
            # Local goes out of scope at function return -- only the
            # noticeboard's pin should keep the inner Cown alive.

        @when(x, writer)
        def read_back(x, _):
            holder = noticeboard()["slot_holder"]
            send("assert", (holder.label, "first"))
            with holder.cown as v:
                send("assert", (v, 12345))

        receive_asserts(count=2)

    def test_slot_subclass_cown_survives_writer(self):
        """Cowns reachable through an MRO chain of ``__slots__`` are pinned.

        Extends the previous test to classes that declare slots at
        different levels of the MRO, exercising the MRO walk rather
        than only the leaf type's ``__slots__``.
        """
        x = Cown(0)

        @when(x)
        def writer(x):
            holder = SlotSubclass(Cown(7777), "sub", Cown(8888))
            notice_write("slot_sub", holder)
            notice_sync()

        @when(x, writer)
        def read_back(x, _):
            holder = noticeboard()["slot_sub"]
            send("assert", (holder.label, "sub"))
            with holder.cown as v:
                send("assert", (v, 7777))
            with holder.extra as v:
                send("assert", (v, 8888))

        receive_asserts(count=3)


class TestNoticeboardSnapshotImmutable:
    """The cached snapshot is read-only; user code cannot corrupt it."""

    @classmethod
    def setup_class(cls):
        start()

    @classmethod
    def teardown_class(cls):
        wait()

    def setup_method(self):
        _core.noticeboard_clear()

    def test_snapshot_is_mappingproxy(self):
        """noticeboard() returns a read-only mapping proxy."""
        x = Cown(0)

        @when(x)
        def setup_then_check(x):
            notice_write("k", "v")
            notice_sync()

        @when(x, setup_then_check)
        def check(x, _):
            snap = noticeboard()
            # Avoid importing MappingProxyType inside the behavior — the
            # transpiler would capture the symbol and pickling the
            # ``mappingproxy`` builtin class fails. Compare by type name
            # instead.
            send("assert", (type(snap).__name__, "mappingproxy"))

        receive_asserts()

    def test_snapshot_rejects_mutation(self):
        """Attempting to mutate the snapshot raises TypeError."""
        x = Cown(0)

        @when(x)
        def writer(x):
            notice_write("k", "v")
            notice_sync()

        @when(x, writer)
        def check(x, _):
            snap = noticeboard()
            try:
                snap["k"] = "new"  # type: ignore[index]
                send("assert", ("no-error", "TypeError"))
            except TypeError:
                send("assert", ("TypeError", "TypeError"))

        receive_asserts()


class TestNoticeboardThreadOnly:
    """Direct mutation entry points reject calls from non-noticeboard threads."""

    @classmethod
    def setup_class(cls):
        """Start the runtime so that NB_NOTICEBOARD_TID is registered."""
        # A trivial behavior is enough to spin up the runtime. After
        # this point any direct C-level write/delete from the main
        # thread must be rejected.
        x = Cown(0)

        @when(x)
        def _noop(x):
            send("assert", (1, 1))

        receive_asserts()

    @classmethod
    def teardown_class(cls):
        wait()

    def test_main_thread_write_direct_rejected(self):
        """noticeboard_write_direct raises if called from the main thread."""
        with pytest.raises(RuntimeError, match="noticeboard thread"):
            _core.noticeboard_write_direct("k", "v", [])

    def test_main_thread_delete_rejected(self):
        """noticeboard_delete raises if called from the main thread."""
        with pytest.raises(RuntimeError, match="noticeboard thread"):
            _core.noticeboard_delete("k")


class TestNoticeDelete:
    """Tests for notice_delete and the REMOVED sentinel."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def setup_method(self):
        """Clear the noticeboard before each test."""
        _core.noticeboard_clear()

    def test_delete_existing_key(self):
        """notice_delete removes an existing key from the noticeboard."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("doomed", 99)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            notice_delete("doomed")
            notice_sync()

        @when(x, step2)
        def check(x, _):
            snap = noticeboard()
            send("assert", ("doomed" not in snap, True))

        receive_asserts()

    def test_delete_absent_key_is_noop(self):
        """notice_delete on a missing key is a silent no-op."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("keeper", "safe")
            notice_delete("nonexistent")
            notice_sync()

        @when(x, step1)
        def check(x, _):
            send("assert", (notice_read("keeper"), "safe"))

        receive_asserts()

    def test_update_fn_returns_removed(self):
        """When fn returns REMOVED, the entry is deleted."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("target", 42)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            notice_update("target", _return_removed)
            notice_sync()

        @when(x, step2)
        def check(x, _):
            snap = noticeboard()
            send("assert", ("target" not in snap, True))

        receive_asserts()

    def test_update_conditional_remove(self):
        """REMOVED only triggers when fn actually returns it."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("val", 50)
            notice_sync()

        # 50 <= 100, so fn returns 51
        @when(x, step1)
        def step2(x, _):
            notice_update("val", _conditionally_remove)
            notice_sync()

        @when(x, step2)
        def check1(x, _):
            send("assert", (notice_read("val"), 51))

        receive_asserts()

    def test_update_conditional_remove_triggers(self):
        """REMOVED triggers when value exceeds threshold."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("val", 200)
            notice_sync()

        # 200 > 100, so fn returns REMOVED
        @when(x, step1)
        def step2(x, _):
            notice_update("val", _conditionally_remove)
            notice_sync()

        @when(x, step2)
        def check(x, _):
            snap = noticeboard()
            send("assert", ("val" not in snap, True))

        receive_asserts()

    def test_removed_then_update_uses_default(self):
        """After deletion, notice_update uses the default value."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("counter", 10)
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            notice_delete("counter")
            notice_sync()

        @when(x, step2)
        def step3(x, _):
            notice_update("counter", _increment, default=0)
            notice_sync()

        @when(x, step3)
        def check(x, _):
            send("assert", (notice_read("counter"), 1))

        receive_asserts()

    def test_delete_frees_capacity(self):
        """Deleting an entry frees a slot for a new entry."""
        x = Cown(0)

        @when(x)
        def fill(x):
            for i in range(64):
                notice_write(f"k{i}", i)
            notice_sync()

        @when(x, fill)
        def delete_one(x, _):
            notice_delete("k0")
            notice_sync()

        @when(x, delete_one)
        def add_new(x, _):
            notice_write("new_key", "hello")
            notice_sync()

        @when(x, add_new)
        def check(x, _):
            snap = noticeboard()
            present = "new_key" in snap and "k0" not in snap
            send("assert", (present, True))

        receive_asserts()


class TestNoticeDeletePreRuntime:
    """Tests that notice_delete validates before runtime start."""

    @classmethod
    def setup_class(cls):
        """Ensure runtime is stopped before this class runs.

        See TestNoticeboardPreRuntime for rationale.
        """
        wait()

    def test_notice_delete_before_start(self):
        """notice_delete raises RuntimeError before the runtime is started."""
        with pytest.raises(RuntimeError, match="cannot delete from the noticeboard"):
            notice_delete("key")
            notice_sync()


class TestNoticeDeleteValidation:
    """Tests for notice_delete input validation (runtime must be running)."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_notice_delete_non_string_key(self):
        """notice_delete raises TypeError for non-string key."""
        x = Cown(0)  # triggers runtime start

        @when(x)
        def _(x):
            pass

        with pytest.raises(TypeError, match="noticeboard key must be a str"):
            notice_delete(123)
            notice_sync()


class TestRemovedSentinel:
    """Tests for the REMOVED sentinel object."""

    def test_removed_is_not_none(self):
        """REMOVED is distinct from None."""
        assert REMOVED is not None

    def test_removed_repr(self):
        """REMOVED has a clear repr."""
        assert repr(REMOVED) == "REMOVED"

    def test_removed_identity(self):
        """REMOVED is a singleton."""
        from bocpy import REMOVED as REMOVED2
        assert REMOVED is REMOVED2

    def test_removed_is_picklable(self):
        """REMOVED survives pickle round-trip as identity."""
        import pickle
        restored = pickle.loads(pickle.dumps(REMOVED))
        assert restored is REMOVED


class TestNoticeboardVersioning:
    """Tests for the version-counter-based snapshot cache.

    These tests confirm that the version counter eliminates redundant
    snapshot rebuilds without breaking the no-polling invariant
    (see ``test_frozen_snapshot`` and friends in ``TestNoticeboard``).
    """

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_self_write_invisible_within_behavior(self):
        """A behavior that writes the noticeboard does NOT see its own write.

        This is the no-polling invariant on the writer side: even after
        the version bump, the writer's thread keeps returning the cached
        dict for the rest of this behavior.
        """
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("self", "before")
            notice_sync()

        @when(x, step1)
        def step2(x, _):
            before = notice_read("self")
            notice_write("self", "after")
            notice_sync()
            after_same_behavior = notice_read("self")
            send("assert", (before, "before"))
            send("assert", (after_same_behavior, "before"))

        @when(x, step2)
        def step3(x, _):
            # New behavior — must see the committed write.
            send("assert", (notice_read("self"), "after"))

        receive_asserts(3)

    def test_cross_behavior_visibility_preserved(self):
        """Sanity: write in A is visible in B (no regression vs baseline)."""
        x = Cown(0)

        @when(x)
        def writer(x):
            notice_write("xv", "from_A")
            notice_sync()

        @when(x, writer)
        def reader(x, _):
            send("assert", (notice_read("xv"), "from_A"))

        receive_asserts()


class TestNoticeSyncReturnType:
    """Pin the documented return type of ``notice_sync()`` (None)."""

    @classmethod
    def teardown_class(cls):
        """Drain the runtime after the suite."""
        wait()

    def test_returns_none_inside_behavior(self):
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("rk", 1)
            result = notice_sync()
            send("assert", (result, None))

        receive_asserts()


# ---------------------------------------------------------------------------
# Pin-walker audit: cowns hidden from the walker must not survive a write.
# ---------------------------------------------------------------------------
#
# The pin walker (``_gather_pins`` / ``_collect_cown_capsules``) traverses
# ``__dict__``, ``__slots__`` (up the MRO), and the standard container
# protocols (dict/list/tuple/set/frozenset). A value whose pickler reaches
# a cown by *any other route* — module-level cache lookup, closure capture,
# ``copyreg.dispatch_table``, custom ``__reduce__`` / ``__getstate__`` —
# would, without the audit, produce a borrowing token whose underlying
# ``BOCCown`` is not held alive by the noticeboard entry's pin set. The
# first reader to resurrect that pointer after the writer's local wrapper
# drops would touch freed memory (CWE-416).
#
# The audit checks every ``CownCapsule_reduce`` against the caller's pin
# set during the borrowing pickle and fails the whole write closed if any
# cown is unaccounted for. These tests pin that contract.


# Module-level state for the hidden-cown reducer. The class can be pickled
# by sub-interpreters (it's importable), but the inner cown is fetched via
# the module cache rather than stored as an attribute — so the walker
# cannot see it.
_HIDDEN_CACHE: dict = {}


class _HiddenCownToken:
    """Pickles to a cown the walker cannot find.

    The constructor stashes the cown in a module-level dict keyed by an
    integer. The instance itself carries only the key, so ``__dict__``
    holds no cowns and the walker pins nothing. ``__reduce__`` then pulls
    the cown back out of the cache so the unpickle emits a CownCapsule —
    exactly the shape a user might write to "optimise" a class with
    ``__reduce__`` without realising the cown has become invisible to
    the noticeboard's pin machinery.
    """

    def __init__(self, key, cown=None):
        self.key = key
        if cown is not None:
            _HIDDEN_CACHE[key] = cown

    def __reduce__(self):
        return (_rebuild_hidden, (self.key, _HIDDEN_CACHE[self.key]))


def _rebuild_hidden(key, cown):
    return _HiddenCownToken(key, cown)


class _VisibleCownPair:
    """Custom ``__reduce__`` whose cowns are also visible to the walker.

    This is the documented optimisation pattern: a class defines
    ``__reduce__`` to control its pickle shape but keeps the cown
    references in plain attributes so the walker pins them. The
    pin-walker audit must keep this pattern working — rejecting it
    would punish every user who follows the documented pickle
    optimisation guide.
    """

    def __init__(self, a, b):
        self.a = a
        self.b = b

    def __reduce__(self):
        return (_VisibleCownPair, (self.a, self.b))


class TestNoticeboardHiddenCownRejection:
    """A cown reached only via a custom reducer must abort the write."""

    @classmethod
    def teardown_class(cls):
        """Drain the runtime after the suite."""
        wait()

    def setup_method(self):
        _core.noticeboard_clear()
        _HIDDEN_CACHE.clear()

    def teardown_method(self):
        # Symmetric clear so a strong reference to the hidden cown
        # (the dict value) does not linger past the test that created
        # it. Without this, the cown survives until the next test's
        # setup_method runs — long enough to alias with subsequent
        # noticeboard activity if the suite is reordered or a new
        # test imports _HiddenCownToken from another module.
        _HIDDEN_CACHE.clear()

    def test_hidden_cown_rejected_and_entry_not_installed(self, caplog):
        """Walker-invisible cown -> warning logged, entry never appears."""
        x = Cown(0)

        @when(x)
        def writer(x):
            hidden_cown = Cown(42)
            # _HIDDEN_CACHE entry created as a side effect; only the key
            # is reachable through __dict__, so _gather_pins returns [].
            token = _HiddenCownToken(7, hidden_cown)
            notice_write("hidden", token)
            notice_sync()

        @when(x, writer)
        def reader(x, _):
            # The audit fires on the noticeboard thread; the exception
            # is caught and logged at WARNING. The behavioural assertion
            # is that the entry never landed.
            send("assert", (notice_read("hidden"), None))

        with caplog.at_level("WARNING", logger="behaviors"):
            receive_asserts()

        matching = [
            r for r in caplog.records
            if r.name == "behaviors"
            and "noticeboard_write('hidden') failed" in r.getMessage()
            and "pin walker did not see" in r.getMessage()
        ]
        assert matching, (
            "expected a WARNING from the noticeboard thread naming the "
            f"rejected key and the pin-walker error text, got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    def test_custom_reduce_with_visible_cowns_still_works(self):
        """The documented ``__reduce__`` optimisation pattern is preserved.

        ``_VisibleCownPair`` defines ``__reduce__`` but keeps its cowns
        in ``__dict__`` — the walker pins them. The write must succeed
        and the value must round-trip; this regression-guards against an
        over-eager pin-walker audit that rejects every custom reducer.
        """
        x = Cown(0)

        @when(x)
        def writer(x):
            a = Cown("a")
            b = Cown("b")
            notice_write("pair", _VisibleCownPair(a, b))
            notice_sync()

        @when(x, writer)
        def reader(x, _):
            pair = notice_read("pair")
            # Snapshot returned a value, the value is the right type,
            # and both embedded cowns survived as live CownCapsules.
            send("assert", (pair is not None, True))
            send("assert", (isinstance(pair, _VisibleCownPair), True))
            send("assert", (isinstance(pair.a, Cown), True))
            send("assert", (isinstance(pair.b, Cown), True))

        receive_asserts(4)


class TestWaitNoticeboardCapture:
    """Tests for ``wait(noticeboard=True)`` final-state capture.

    Mirrors the existing ``wait(stats=True)`` pattern. The snapshot is
    taken on the main thread after the noticeboard mutator has exited
    but before the entries are freed by ``noticeboard_clear()``.

    ``wait()`` itself is the quiescence barrier *and* drains the
    ``boc_noticeboard`` queue (the shutdown sentinel is FIFO behind
    every prior mutation), so the test bodies do not need an extra
    ``send``/``receive`` handshake or a ``notice_sync()`` before
    calling ``wait(noticeboard=True)``.
    """

    @classmethod
    def teardown_class(cls):
        """Drain the runtime after the suite."""
        wait()

    def test_wait_noticeboard_true_returns_final_state(self):
        """The captured dict contains everything a behavior wrote."""
        wait()  # baseline: runtime down
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("answer", 42)
            notice_write("label", "done")

        snap = wait(noticeboard=True)
        assert isinstance(snap, dict), type(snap)
        assert snap.get("answer") == 42, snap
        assert snap.get("label") == "done", snap

    def test_wait_noticeboard_true_returns_plain_mutable_dict(self):
        """Returned snapshot is a plain dict (not a MappingProxyType)."""
        wait()
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("k", "v")

        snap = wait(noticeboard=True)
        # Plain dict means we can mutate locally without disturbing
        # the now-cleared runtime.
        snap["local_only"] = True
        assert snap["local_only"] is True
        assert snap.get("k") == "v"

    def test_wait_noticeboard_true_runtime_never_started(self):
        """Empty dict when the runtime was never up."""
        wait()  # ensure runtime is down
        assert wait(noticeboard=True) == {}

    def test_wait_noticeboard_true_empty_noticeboard(self):
        """Empty dict when no behavior wrote anything."""
        wait()
        x = Cown(0)

        @when(x)
        def _(x):
            pass

        snap = wait(noticeboard=True)
        assert snap == {}, snap

    def test_wait_default_returns_none_even_with_noticeboard_data(self):
        """Default ``wait()`` is still ``None``."""
        wait()
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("anything", 1)

        assert wait() is None

    def test_wait_stats_only_returns_list_unchanged(self):
        """Single ``stats=True`` still returns a list."""
        wait()
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("k", 1)

        result = wait(stats=True)
        assert isinstance(result, list), type(result)
        assert len(result) >= 1

    def test_wait_both_flags_returns_named_tuple(self):
        """Both flags -> ``WaitResult`` with both fields populated."""
        from bocpy import WaitResult
        wait()
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("k", "v")

        result = wait(stats=True, noticeboard=True)
        assert isinstance(result, WaitResult), type(result)
        # Tuple-shape access still works (NamedTuple).
        stats_snap, nb_snap = result
        assert isinstance(stats_snap, list), type(stats_snap)
        assert isinstance(nb_snap, dict), type(nb_snap)
        assert nb_snap.get("k") == "v"
        assert len(stats_snap) >= 1
        assert result.stats is stats_snap
        assert result.noticeboard is nb_snap

    def test_wait_both_flags_runtime_never_started(self):
        """Both flags with no runtime -> WaitResult([], {})."""
        from bocpy import WaitResult
        wait()
        result = wait(stats=True, noticeboard=True)
        assert isinstance(result, WaitResult), type(result)
        assert result.stats == []
        assert result.noticeboard == {}

    def test_wait_noticeboard_captures_complex_value(self):
        """Pickle-roundtrip values (dict/list) are preserved verbatim."""
        wait()
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("nested", {"counter": 7, "items": [1, 2, 3]})

        snap = wait(noticeboard=True)
        assert snap.get("nested") == {"counter": 7, "items": [1, 2, 3]}

    def test_wait_noticeboard_reflects_later_writes(self):
        """The final dict reflects the last write, not the first."""
        wait()
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("k", "first")
            notice_write("k", "second")
            notice_write("k", "third")

        snap = wait(noticeboard=True)
        assert snap.get("k") == "third", snap

    def test_wait_noticeboard_reflects_deletes(self):
        """Keys deleted via ``notice_delete`` are absent from snapshot."""
        wait()
        x = Cown(0)

        @when(x)
        def writer(x):
            notice_write("keep", 1)
            notice_write("drop", 2)

        # A second behavior runs the delete -- and because it is
        # ordered after ``writer`` via the cown chain, the delete
        # is guaranteed to land after the writes (FIFO ordering on
        # the noticeboard queue alone is not enough across separate
        # behaviors, which the cown chain provides).
        @when(x, writer)
        def deleter(x, _):
            notice_delete("drop")

        snap = wait(noticeboard=True)
        assert "keep" in snap
        assert "drop" not in snap, snap

    def test_wait_noticeboard_across_restart(self):
        """A fresh session starts with an empty noticeboard snapshot."""
        wait()
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("session1", True)

        snap1 = wait(noticeboard=True)
        assert snap1.get("session1") is True

        # New session; the previous session's data must be gone.
        y = Cown(0)

        @when(y)
        def _(y):
            pass

        snap2 = wait(noticeboard=True)
        assert "session1" not in snap2, snap2

    def test_wait_noticeboard_survives_explicit_stop(self):
        """Explicit ``stop()`` before ``wait()`` preserves the snapshot.

        ``stop()`` is documented as idempotent; the snapshot capture
        is a single-shot teardown step and a second invocation (from
        ``wait()``-triggered re-entry) must NOT re-snapshot the
        now-empty noticeboard.
        """
        import bocpy.behaviors as B

        wait()
        x = Cown(0)

        @when(x)
        def _(x):
            notice_write("k", "v")

        # Explicit stop drives quiescence and captures the snapshot.
        inst = B.BEHAVIORS
        assert inst is not None
        inst.stop()
        assert inst._final_noticeboard == {"k": "v"}, (
            inst._final_noticeboard
        )

        # Now ``wait(noticeboard=True)`` re-enters ``stop()``; the
        # captured snapshot must survive the second pass.
        snap = wait(noticeboard=True)
        assert snap == {"k": "v"}, snap
