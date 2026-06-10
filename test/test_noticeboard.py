"""Tests for the noticeboard feature."""

from functools import partial

import pytest

from bocpy import (Cown, notice_delete, notice_read,
                   notice_seed, notice_update, notice_write, noticeboard,
                   quiesce, REMOVED, start, wait, when)
import bocpy._core as _core


QUIESCE_TIMEOUT = 5


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

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("greeting") == "hello"

    def test_write_overwrite(self):
        """Overwriting a key replaces the previous value."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("counter", 10)

        @when(x, step1)
        def step2(x, _):
            notice_write("counter", 20)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("counter") == 20

    def test_snapshot_returns_mapping(self):
        """Snapshot returns a read-only mapping even with no writes."""
        x = Cown(0)

        @when(x)
        def probe(x):
            from collections.abc import Mapping
            snap = noticeboard()
            return isinstance(snap, Mapping)

        quiesce(QUIESCE_TIMEOUT)
        assert probe.unwrap() is True

    def test_multiple_keys(self):
        """Multiple keys can coexist in the noticeboard."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("a", 1)
            notice_write("b", 2)
            notice_write("c", 3)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert (snap.get("a"), snap.get("b"), snap.get("c")) == (1, 2, 3)

    def test_frozen_snapshot(self):
        """Snapshot is frozen: a write after snapshot doesn't change it."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("val", 100)

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def step2(x):
            snap1 = noticeboard()
            notice_write("val", 200)
            snap2 = noticeboard()
            return (snap1.get("val"), snap2.get("val"))

        quiesce(QUIESCE_TIMEOUT)
        val1, val2 = step2.unwrap()
        assert val1 == 100
        assert val1 == val2

    def test_snapshot_cache_cleared_between_behaviors(self):
        """Each behavior gets a fresh snapshot, not the previous one's cache."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("seq", 1)

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def step2(x):
            snap = noticeboard()
            seq = snap.get("seq")
            notice_write("seq", 2)
            return seq

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def step3(x):
            snap = noticeboard()
            return snap.get("seq")

        quiesce(QUIESCE_TIMEOUT)
        assert step2.unwrap() == 1
        assert step3.unwrap() == 2

    def test_picklable_value(self):
        """Complex (picklable) values round-trip through the noticeboard."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("data", [1, 2, 3])

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("data") == [1, 2, 3]

    def test_set_value_forces_pickle_path(self):
        """A set is not natively shareable and must take the pickle path."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("tags", {1, 2, 3})

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("tags") == {1, 2, 3}

    def test_int_value(self):
        """Integer values (native cross-interpreter) round-trip correctly."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("num", 42)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("num") == 42

    def test_none_value(self):
        """None round-trips through the noticeboard."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("empty", None)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert "empty" in snap
        assert snap["empty"] is None

    def test_notice_read_existing_key(self):
        """notice_read returns the value for an existing key."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("color", "blue")

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def step2(x):
            return notice_read("color")

        quiesce(QUIESCE_TIMEOUT)
        assert step2.unwrap() == "blue"

    def test_notice_read_missing_key_default(self):
        """notice_read returns None for a missing key by default."""
        x = Cown(0)

        @when(x)
        def probe(x):
            return notice_read("nonexistent")

        quiesce(QUIESCE_TIMEOUT)
        assert probe.unwrap() is None

    def test_notice_read_missing_key_custom_default(self):
        """notice_read returns the custom default for a missing key."""
        x = Cown(0)

        @when(x)
        def probe(x):
            return notice_read("nonexistent", 42)

        quiesce(QUIESCE_TIMEOUT)
        assert probe.unwrap() == 42

    def test_notice_read_uses_cached_snapshot(self):
        """Two notice_read calls in the same behavior use the same snapshot."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("tick", 1)

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def step2(x):
            val1 = notice_read("tick")
            notice_write("tick", 99)
            val2 = notice_read("tick")
            return (val1, val2)

        quiesce(QUIESCE_TIMEOUT)
        val1, val2 = step2.unwrap()
        assert val1 == val2


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
        long_key = "k" * 63

        @when(x)
        def step1(x):
            notice_write(long_key, "ok")

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get(long_key) == "ok"

    def test_key_length_64_bytes_rejected(self):
        """A key of 64 UTF-8 bytes is rejected with ValueError."""
        x = Cown(0)
        too_long = "k" * 64

        @when(x)
        def probe(x):
            notice_write(too_long, "fail")

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(ValueError):
            probe.unwrap()

    def test_64_entries_accepted(self):
        """The noticeboard accepts up to 64 distinct keys."""
        x = Cown(0)

        @when(x)
        def step1(x):
            for i in range(64):
                notice_write(f"slot{i}", i)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert len(snap) >= 64
        assert snap.get("slot0") == 0
        assert snap.get("slot63") == 63

    def test_65th_entry_silently_dropped(self):
        """The 65th distinct key is silently dropped by the noticeboard thread."""
        x = Cown(0)

        @when(x)
        def step1(x):
            for i in range(65):
                notice_write(f"cap{i}", i)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        cap_keys = [k for k in snap if k.startswith("cap")]
        assert len(cap_keys) == 64
        assert snap.get("cap0") == 0
        assert snap.get("cap63") == 63
        assert "cap64" not in snap

    def test_write_non_string_key_rejected(self):
        """Non-string key raises TypeError."""
        x = Cown(0)

        @when(x)
        def probe(x):
            notice_write(123, "value")

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(TypeError):
            probe.unwrap()

    def test_key_with_nul_rejected(self):
        """A key containing NUL is rejected with ValueError."""
        x = Cown(0)

        @when(x)
        def probe(x):
            notice_write("a\x00b", "value")

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(ValueError):
            probe.unwrap()


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

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        count = sum(1 for k in snap if k.startswith("cw_"))
        assert count == 8
        assert snap.get("cw_0") == 0
        assert snap.get("cw_7") == 70


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
        key_63 = "a" * 60 + "€"

        @when(x)
        def step1(x):
            notice_write(key_63, "ok")

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get(key_63) == "ok"

    def test_multibyte_key_exceeds_limit(self):
        """A 3-byte character at byte position 61 exceeds the 63-byte limit."""
        x = Cown(0)
        key_64 = "a" * 61 + "€"

        @when(x)
        def probe(x):
            notice_write(key_64, "fail")

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(ValueError):
            probe.unwrap()


class TestNoticeboardRestart:
    """Tests for noticeboard state across runtime restart."""

    def test_noticeboard_empty_after_restart(self):
        """After wait() + new behaviors, noticeboard starts fresh."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("before_restart", 42)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("before_restart") == 42
        wait()

        y = Cown(0)

        @when(y)
        def step2(y):
            pass

        snap2 = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert "before_restart" not in snap2
        wait()


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

        @when(x, step1)
        def step2(x, _):
            notice_update("counter", _increment)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("counter") == 11

    def test_default_on_absent_key(self):
        """Update a missing key uses the default value."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_update("missing", _add_ten, default=0)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("missing") == 10

    def test_none_sentinel(self):
        """A key holding None is distinguished from an absent key."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("k", None)

        @when(x, step1)
        def step2(x, _):
            notice_update("k", _wrap_value, default="WRONG")

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("k") == (None, "seen")

    def test_concurrent_updates(self):
        """Multiple independent behaviors updating the same key."""
        n = 8
        cowns = [Cown(i) for i in range(n)]
        for i in range(n):

            @when(cowns[i])
            def writer(c):
                notice_update("counter", _increment, default=0)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("counter") == n

    def test_key_validation_type(self):
        """Non-string key raises TypeError."""
        x = Cown(0)

        @when(x)
        def probe(x):
            notice_update(123, _increment)

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(TypeError):
            probe.unwrap()

    def test_fn_not_callable(self):
        """Non-callable fn raises TypeError."""
        x = Cown(0)

        @when(x)
        def probe(x):
            notice_update("key", "not_callable")

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(TypeError):
            probe.unwrap()

    def test_fn_raises_keeps_previous_value(self):
        """If fn raises, the key retains its previous value."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("safe", 42)

        @when(x, step1)
        def step2(x, _):
            notice_update("safe", _div_by_zero)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("safe") == 42

    def test_functools_partial(self):
        """functools.partial with a builtin works as fn."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_update("best", partial(max, 42), default=0)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("best") == 42


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

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def step2(x):
            snap = noticeboard()
            raised = False
            try:
                snap["immut"] = 999
            except TypeError:
                raised = True
            return (raised, notice_read("immut"))

        quiesce(QUIESCE_TIMEOUT)
        raised, original = step2.unwrap()
        assert raised is True
        assert original == 1

    def test_snapshot_del_rejected(self):
        """Deleting a key from the snapshot raises TypeError."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("del_test", 42)

        @when(x, step1)
        def step2(x, _):
            snap = noticeboard()
            raised = False
            try:
                del snap["del_test"]
            except TypeError:
                raised = True
            return raised

        quiesce(QUIESCE_TIMEOUT)
        assert step2.unwrap() is True


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

    def test_notice_update_before_start(self):
        """notice_update raises RuntimeError before the runtime is started."""
        with pytest.raises(RuntimeError, match="cannot update the noticeboard"):
            notice_update("key", _increment)


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
            raise ValueError("intentional failure")

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("survivor") == 42
        with pytest.raises(ValueError, match="intentional failure"):
            failing.unwrap()


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
        x = Cown(0)

        @when(x)
        def writer(x):
            ring = [Cown(i * 10) for i in range(8)]
            notice_write("ring", ring)

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def first_read(x):
            ring = noticeboard()["ring"]
            return len(ring)

        @when(x, first_read)
        def second_read(x, _):
            ring = noticeboard()["ring"]
            return len(ring)

        @when(x, second_read)
        def acquire_first(x, _):
            ring = noticeboard()["ring"]
            with ring[0] as v:
                return v

        quiesce(QUIESCE_TIMEOUT)
        assert first_read.unwrap() == 8
        assert second_read.unwrap() == 8
        assert acquire_first.unwrap() == 0

    def test_overwrite_releases_old_cown_pins(self):
        """Overwriting a noticeboard entry releases the old entry's pins."""
        x = Cown(0)

        @when(x)
        def first_write(x):
            first = [Cown(i) for i in range(4)]
            notice_write("ring", first)

        @when(x, first_write)
        def second_write(x, _):
            second = [Cown(100 + i) for i in range(4)]
            notice_write("ring", second)

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def check(x):
            ring = noticeboard()["ring"]
            with ring[0] as v:
                return v

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == 100

    def test_delete_releases_cown_pins(self):
        """notice_delete drops the entry's pins; a fresh write reuses the slot."""
        x = Cown(0)

        @when(x)
        def initial_write(x):
            ring = [Cown(i) for i in range(3)]
            notice_write("ring", ring)

        @when(x, initial_write)
        def remove_entry(x, _):
            notice_delete("ring")

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def verify_gone(x):
            return "ring" in noticeboard()

        @when(x, verify_gone)
        def write_new(x, _):
            new_ring = [Cown(999)]
            notice_write("ring", new_ring)

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def check_new(x):
            ring = noticeboard()["ring"]
            with ring[0] as v:
                return v

        quiesce(QUIESCE_TIMEOUT)
        assert verify_gone.unwrap() is False
        assert check_new.unwrap() == 999

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

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def read_back(x):
            holder = noticeboard()["slot_holder"]
            with holder.cown as v:
                return (holder.label, v)

        quiesce(QUIESCE_TIMEOUT)
        label, v = read_back.unwrap()
        assert label == "first"
        assert v == 12345

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

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def read_back(x):
            holder = noticeboard()["slot_sub"]
            with holder.cown as v1, holder.extra as v2:
                return (holder.label, v1, v2)

        quiesce(QUIESCE_TIMEOUT)
        label, v1, v2 = read_back.unwrap()
        assert label == "sub"
        assert v1 == 7777
        assert v2 == 8888


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

        @when(x, setup_then_check)
        def check(x, _):
            snap = noticeboard()
            return type(snap).__name__

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == "mappingproxy"

    def test_snapshot_rejects_mutation(self):
        """Attempting to mutate the snapshot raises TypeError."""
        x = Cown(0)

        @when(x)
        def writer(x):
            notice_write("k", "v")

        @when(x, writer)
        def check(x, _):
            snap = noticeboard()
            raised = False
            try:
                snap["k"] = "new"  # type: ignore[index]
            except TypeError:
                raised = True
            return raised

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() is True


class TestNoticeboardThreadOnly:
    """Direct mutation entry points reject calls from non-noticeboard threads."""

    @classmethod
    def setup_class(cls):
        """Start the runtime so that NB_NOTICEBOARD_TID is registered."""
        x = Cown(0)

        @when(x)
        def _noop(x):
            return 1

        quiesce(QUIESCE_TIMEOUT)
        assert _noop.unwrap() == 1

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

        @when(x, step1)
        def step2(x, _):
            notice_delete("doomed")

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert "doomed" not in snap

    def test_delete_absent_key_is_noop(self):
        """notice_delete on a missing key is a silent no-op."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("keeper", "safe")
            notice_delete("nonexistent")

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("keeper") == "safe"

    def test_update_fn_returns_removed(self):
        """When fn returns REMOVED, the entry is deleted."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("target", 42)

        @when(x, step1)
        def step2(x, _):
            notice_update("target", _return_removed)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert "target" not in snap

    def test_update_conditional_remove(self):
        """REMOVED only triggers when fn actually returns it."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("val", 50)

        @when(x, step1)
        def step2(x, _):
            notice_update("val", _conditionally_remove)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("val") == 51

    def test_update_conditional_remove_triggers(self):
        """REMOVED triggers when value exceeds threshold."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("val", 200)

        @when(x, step1)
        def step2(x, _):
            notice_update("val", _conditionally_remove)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert "val" not in snap

    def test_removed_then_update_uses_default(self):
        """After deletion, notice_update uses the default value."""
        x = Cown(0)

        @when(x)
        def step1(x):
            notice_write("counter", 10)

        @when(x, step1)
        def step2(x, _):
            notice_delete("counter")

        @when(x, step2)
        def step3(x, _):
            notice_update("counter", _increment, default=0)

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert snap.get("counter") == 1

    def test_delete_frees_capacity(self):
        """Deleting an entry frees a slot for a new entry."""
        x = Cown(0)

        @when(x)
        def fill(x):
            for i in range(64):
                notice_write(f"k{i}", i)

        @when(x, fill)
        def delete_one(x, _):
            notice_delete("k0")

        @when(x, delete_one)
        def add_new(x, _):
            notice_write("new_key", "hello")

        snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
        assert "new_key" in snap and "k0" not in snap


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


class TestNoticeDeleteValidation:
    """Tests for notice_delete input validation (runtime must be running)."""

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_notice_delete_non_string_key(self):
        """notice_delete raises TypeError for non-string key."""
        x = Cown(0)

        @when(x)
        def _(x):
            pass

        with pytest.raises(TypeError, match="noticeboard key must be a str"):
            notice_delete(123)


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

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def step2(x):
            before = notice_read("self")
            notice_write("self", "after")
            after_same_behavior = notice_read("self")
            return (before, after_same_behavior)

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def step3(x):
            return notice_read("self")

        quiesce(QUIESCE_TIMEOUT)
        before, after_same_behavior = step2.unwrap()
        assert before == "before"
        assert after_same_behavior == "before"
        assert step3.unwrap() == "after"

    def test_cross_behavior_visibility_preserved(self):
        """Sanity: a write in A is visible to a later behavior after a barrier."""
        x = Cown(0)

        @when(x)
        def writer(x):
            notice_write("xv", "from_A")

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def reader(x):
            return notice_read("xv")

        quiesce(QUIESCE_TIMEOUT)
        assert reader.unwrap() == "from_A"


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
        _HIDDEN_CACHE.clear()

    def test_hidden_cown_rejected_and_entry_not_installed(self, caplog):
        """Walker-invisible cown -> warning logged, entry never appears."""
        x = Cown(0)

        @when(x)
        def writer(x):
            hidden_cown = Cown(42)
            token = _HiddenCownToken(7, hidden_cown)
            notice_write("hidden", token)

        with caplog.at_level("WARNING", logger="behaviors"):
            snap = quiesce(QUIESCE_TIMEOUT, noticeboard=True)
            assert snap.get("hidden") is None

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

        quiesce(QUIESCE_TIMEOUT, noticeboard=True)

        @when(x)
        def reader(x):
            pair = notice_read("pair")
            return (
                pair is not None,
                isinstance(pair, _VisibleCownPair),
                isinstance(pair.a, Cown),
                isinstance(pair.b, Cown),
            )

        quiesce(QUIESCE_TIMEOUT)
        not_none, is_pair, a_cown, b_cown = reader.unwrap()
        assert not_none is True
        assert is_pair is True
        assert a_cown is True
        assert b_cown is True


class TestWaitNoticeboardCapture:
    """Tests for ``wait(noticeboard=True)`` final-state capture.

    Mirrors the existing ``wait(stats=True)`` pattern. The snapshot is
    taken on the main thread after the noticeboard mutator has exited
    but before the entries are freed by ``noticeboard_clear()``.

    ``wait()`` itself is the quiescence barrier *and* drains the
    ``boc_noticeboard`` queue (the shutdown sentinel is FIFO behind
    every prior mutation), so the test bodies do not need an extra
    ``send``/``receive`` handshake before calling
    ``wait(noticeboard=True)``.
    """

    @classmethod
    def teardown_class(cls):
        """Drain the runtime after the suite."""
        wait()

    def test_wait_noticeboard_true_returns_final_state(self):
        """The captured dict contains everything a behavior wrote."""
        wait()
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
        snap["local_only"] = True
        assert snap["local_only"] is True
        assert snap.get("k") == "v"

    def test_wait_noticeboard_true_runtime_never_started(self):
        """Empty dict when the runtime was never up."""
        wait()
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

        inst = B.BEHAVIORS
        assert inst is not None
        inst.stop()
        assert inst._final_noticeboard == {"k": "v"}, (
            inst._final_noticeboard
        )

        snap = wait(noticeboard=True)
        assert snap == {"k": "v"}, snap


class TestNoticeSeed:
    """Tests for the synchronous main-thread notice_seed write."""

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

    def test_seed_visible_to_subsequent_behavior(self):
        """A behavior scheduled after notice_seed observes the value."""
        notice_seed("cfg", {"threshold": 7})
        x = Cown(0)

        @when(x)
        def reader(x):
            return notice_read("cfg", {}).get("threshold", -1)

        quiesce(QUIESCE_TIMEOUT)
        assert reader.unwrap() == 7

    def test_seed_commits_before_return(self):
        """notice_seed commits synchronously: a fresh snapshot sees it at once."""
        notice_seed("now", 123)
        assert _core.noticeboard_snapshot().get("now") == 123

    def test_seed_visible_after_warm_main_cache(self):
        """A seed is visible to the seeding thread even after its cache is warm.

        The main thread has no behavior boundary to re-arm its snapshot
        cache, so a read taken before the seed must not mask the seeded
        value on the next read.
        """
        notice_seed("warm", 0)
        assert notice_read("warm") == 0
        notice_seed("warm", 1)
        assert notice_read("warm") == 1

    def test_seed_overwrite_last_wins(self):
        """Seeding the same key twice keeps the last value."""
        notice_seed("counter", 1)
        notice_seed("counter", 2)
        x = Cown(0)

        @when(x)
        def reader(x):
            return notice_read("counter")

        quiesce(QUIESCE_TIMEOUT)
        assert reader.unwrap() == 2

    def test_seed_rejected_in_worker(self):
        """notice_seed raises RuntimeError when called from a behavior body."""
        x = Cown(0)

        @when(x)
        def offender(x):
            notice_seed("k", "v")

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(RuntimeError, match="primary interpreter"):
            offender.unwrap()

    def test_seed_invalid_key_raises_on_main(self):
        """An over-long key fails fast on the calling thread."""
        with pytest.raises(ValueError):
            notice_seed("k" * 64, "v")

    def test_seed_embedded_cown_survives(self):
        """A cown seeded from main outlives the writer's reference."""
        notice_seed("ring", [Cown(i * 10) for i in range(4)])
        x = Cown(0)

        @when(x)
        def reader(x):
            ring = noticeboard()["ring"]
            with ring[2] as v:
                return (len(ring), v)

        quiesce(QUIESCE_TIMEOUT)
        size, value = reader.unwrap()
        assert size == 4
        assert value == 20


class TestNoticeSeedAutoStart:
    """notice_seed starts the runtime when it is the first bocpy call."""

    @classmethod
    def setup_class(cls):
        """Guarantee a stopped runtime so the seed must auto-start it."""
        wait()

    @classmethod
    def teardown_class(cls):
        """Ensure runtime is drained after suite."""
        wait()

    def test_seed_auto_starts_runtime(self):
        """Seeding with no prior start() brings the runtime up and commits."""
        import bocpy.behaviors as B

        assert B.BEHAVIORS is None
        notice_seed("boot", "ready")
        assert B.BEHAVIORS is not None

        x = Cown(0)

        @when(x)
        def reader(x):
            return notice_read("boot")

        quiesce(QUIESCE_TIMEOUT)
        assert reader.unwrap() == "ready"
