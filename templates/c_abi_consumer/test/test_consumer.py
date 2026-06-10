"""End-to-end exercise of the bocpy public C ABI via the runtime itself.

Building ``_bocpy_probe`` against ``bocpy.get_include()`` and
``bocpy.get_sources()`` proves the ABI compiles. Importing it proves
the headers and atomic shim link correctly. The headline test then
shows that downstream extensions can ride the cross-interpreter
machinery — ``Cown`` and ``@when`` — for free:

  * A ``Counter`` (a downstream type registered via
    ``XIDATA_REGISTERCLASS``) is wrapped in a ``Cown``.
  * A tail-recursive ``@when`` chain re-schedules itself on a worker
    sub-interpreter until the impl's atomic ``count`` reaches a
    target. Each behavior dispatch round-trips the impl through the
    registered producer + consumer callbacks, which is what bumps the
    count.
  * The terminal behavior reads ``c.value.address`` and
    ``c.value.count`` *inside* the ``@when`` (where it owns the cown
    under the proto-Region discipline) and writes the observed
    ``(address, count)`` pair into a dedicated result ``Cown`` it also
    holds. After ``quiesce()`` the main thread reads that pair with
    ``Cown.unwrap`` and fails if the address drifted or the count
    never reached the target — proving the impl pointer survived every
    XIData hop and the consumer callback fired on every dispatch.

Together this exercises the real BOC scheduler and the real worker
handoff — not just an in-process round-trip of a single XIData
callback.
"""

import _bocpy_probe
import pytest

from bocpy import Cown, quiesce, wait, when


def test_counter_construction():
    """Default-constructed Counter exposes a non-NULL impl with count=0."""
    c = _bocpy_probe.Counter()
    assert c.count == 0
    assert c.refcount == 1
    assert isinstance(c.address, int)
    assert c.address != 0


def test_counter_uninitialised_raises():
    """Getters must refuse to dereference a NULL impl rather than segfault.

    ``Counter.__new__(Counter)`` skips ``__init__``, so the wrapper has
    ``impl == NULL``. Each getter must raise ``RuntimeError`` instead of
    crashing.
    """
    c = _bocpy_probe.Counter.__new__(_bocpy_probe.Counter)
    with pytest.raises(RuntimeError, match="not initialised"):
        c.count
    with pytest.raises(RuntimeError, match="not initialised"):
        c.address
    with pytest.raises(RuntimeError, match="not initialised"):
        c.refcount


TARGET = 5

QUIESCE_TIMEOUT = 10


def _step(c, result):
    """Schedule one round of the tail loop.

    Defined at module level so the transpiler can resolve it from the
    worker interpreter when the recursive call inside the behavior is
    executed. The terminal behavior records the observed
    ``(address, count)`` pair into ``result`` so the main thread can
    read it with ``Cown.unwrap`` after ``quiesce()``.
    """
    @when(c, result)
    def _(c, result):
        addr = c.value.address
        count = c.value.count
        if count < TARGET:
            _step(c, result)
        else:
            result.value = (addr, count)


class TestBOCRoundtrip:
    """BOC-driven round-trip of a ``Counter`` cown via ``@when``."""

    @classmethod
    def teardown_class(cls):
        """Drain pending behaviors so the runtime can shut cleanly."""
        wait()

    def test_tail_loop_roundtrips_counter_through_when(self):
        """Ship a Counter cown through a tail-recursive @when chain."""
        counter = _bocpy_probe.Counter()
        expected_addr = counter.address
        c = Cown(counter)
        result = Cown(None)

        _step(c, result)

        quiesce(QUIESCE_TIMEOUT)
        observed_addr, observed_count = result.unwrap()

        assert observed_addr == expected_addr, (
            f"impl pointer drifted: expected {expected_addr!r}, "
            f"got {observed_addr!r}")
        assert observed_count >= TARGET, (
            f"count only reached {observed_count}, expected >= {TARGET}; "
            "the XIData round-trip did not increment the counter")
