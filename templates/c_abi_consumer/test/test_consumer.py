"""End-to-end exercise of the bocpy public C ABI via the runtime itself.

Building ``_bocpy_probe`` against ``bocpy.get_include()`` and
``bocpy.get_sources()`` proves the ABI compiles. Importing it proves
the headers and atomic shim link correctly. The headline test then
shows that downstream extensions can ride the cross-interpreter
machinery — ``Cown``, ``@when`` and ``send``/``receive`` — for free:

  * A ``Counter`` (a downstream type registered via
    ``XIDATA_REGISTERCLASS``) is wrapped in a ``Cown``.
  * A tail-recursive ``@when`` chain re-schedules itself on a worker
    sub-interpreter until the impl's atomic ``count`` reaches a
    target. Each behavior dispatch round-trips the impl through the
    registered producer + consumer callbacks, which is what bumps the
    count.
  * The terminal behavior reads ``c.value.address`` and
    ``c.value.count`` *inside* the ``@when`` (where it owns the cown
    under the proto-Region discipline) and ``send``s the assertion
    pairs ``(addr, expected_addr)`` and ``(count >= TARGET, True)``
    back to the main thread. The test ``receive``s them and fails if
    either pair disagrees, proving the impl pointer survived every
    XIData hop and the consumer callback fired on every dispatch.

Together this exercises the real BOC scheduler, the real worker
handoff, and the real MPSC message queue — not just an in-process
round-trip of a single XIData callback.
"""

# Top-level, unconditional import. The transpiler propagates module-
# level ``import`` statements into worker sub-interpreters, where the
# extension's per-interpreter exec slot must run before the consumer
# callback can dereference its ``LOCAL_STATE``. ``pytest.importorskip``
# is a runtime call the transpiler does not see, so it would leave the
# worker without the probe and segfault on the first reconstruction.
import _bocpy_probe
import pytest

from bocpy import Cown, drain, receive, send, TIMEOUT, wait, when


# --- construction smoke checks -------------------------------------------
#
# These do not need BOC. They just confirm the extension built and that
# the per-interpreter exec slot ran on the main interpreter.


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


# --- BOC-driven XIData round-trip ----------------------------------------

TARGET = 5
RECEIVE_TIMEOUT = 10


def _step(c, expected_addr):
    """Schedule one round of the tail loop.

    Defined at module level so the transpiler can resolve it from the
    worker interpreter when the recursive call inside the behavior is
    executed. ``expected_addr`` is closed over by value at schedule
    time (the transpiler snapshots captures into a tuple) so the
    terminal behavior can compare it against the impl pointer it
    observes from inside the worker.
    """
    @when(c)
    def _(c):
        # Counter follows proto-Region semantics: only the interpreter
        # currently owning the cown may inspect ``c.value``. Do all
        # checks here, inside the @when, where ownership is held.
        addr = c.value.address
        count = c.value.count
        if count < TARGET:
            _step(c, expected_addr)
        else:
            # Identity check: the impl pointer must survive every
            # @when handoff in the tail loop. If XIData ever lost it,
            # ``addr`` would not match the cown's original address.
            send("assert", (addr, expected_addr))
            # Progress check: the consumer callback bumps ``count`` on
            # every reconstruction, so by the terminal behavior we
            # must have round-tripped at least TARGET times.
            send("assert", (count >= TARGET, True))


class TestBOCRoundtrip:
    """BOC-driven round-trip of a ``Counter`` cown via ``@when`` + send."""

    @classmethod
    def teardown_class(cls):
        """Drain pending behaviors so the runtime can shut cleanly."""
        wait()

    def receive_asserts(self, count):
        """Collect ``count`` assertion messages and fail on mismatch.

        Mirrors the helper from .github/skills/testing-with-boc — uses
        a timeout so a stalled behavior fails the test loudly instead
        of hanging, and drains the queue on the way out.
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
            "tail-recursive @when chain never reached its terminal "
            "send('assert', ...). Either XIData round-trip is not "
            "incrementing the counter or the behavior chain stalled.")
        if failed is not None:
            actual, expected = failed
            assert actual == expected, f"expected {expected!r}, got {actual!r}"

    def test_tail_loop_roundtrips_counter_through_when_and_send(self):
        """Ship a Counter cown through a tail-recursive @when chain."""
        counter = _bocpy_probe.Counter()
        expected_addr = counter.address
        c = Cown(counter)

        _step(c, expected_addr)

        # Two asserts from the terminal behavior: address identity
        # and count progress. ``receive_asserts`` blocks until both
        # arrive (or times out), so no extra sentinel is needed.
        self.receive_asserts(2)
