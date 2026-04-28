"""Integration tests for the per-worker scheduler.

The data-structure-level coverage of the queue / WSQ primitives
lives in ``test_internal_wsq.py`` and ``test_internal_mpmcq.py``
and exercises the C primitives directly via ``_internal_test``.
This file covers behaviours that can only be validated end-to-end
through the public ``@when`` surface or through the production
``_core.scheduler_*`` endpoints:

- **Runtime re-entry**: ``start()`` / ``wait()`` / ``start()`` must
  complete two independent workloads without leaks.
- **Paired-release contract**: an uncaught exception inside an
  ``@when`` body must still release the cown so a follow-on
  ``@when`` on the same cown is scheduled and runs.
- **Over-registration contract**: an extra ``scheduler_worker_register()``
  beyond ``worker_count`` must raise ``RuntimeError`` rather than
  silently corrupt state.

A prior set of timing-dependent tests (per-worker TLS coverage of
the ``pushed_local`` path, parked-peer CPU/wall ratio, parked-worker
wake latency) lived here and were removed: each asserted a property
that depends on OS scheduler behaviour rather than on bocpy code
under test, and each was repeatedly flaky on CI runners. The
underlying mechanisms (pending-eviction, parking, cross-worker
wake) are exercised end-to-end by every benchmark in ``examples/``
— a regression there would deadlock or starve the benchmark suite
long before any threshold-based test would surface a clean failure.

All tests use module-level classes/helpers (workers run in
sub-interpreters and import the test module to resolve symbols).
"""

import pytest

import bocpy
from bocpy import _core
from bocpy import Cown, drain, receive, send, TIMEOUT, wait, when


RECEIVE_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Module-level helpers (must be importable by worker sub-interpreters)
# ---------------------------------------------------------------------------


class _Counter:
    """Plain counter used as cown payload in chain workloads."""

    __slots__ = ("count",)

    def __init__(self):
        """Initialise the counter at zero."""
        self.count = 0


def _ensure_quiesced():
    """Tear down any prior runtime so the test starts from a clean state.

    ``bocpy.wait()`` is a no-op when ``BEHAVIORS`` is ``None``; if a
    previous test left the runtime up it drains and stops it.
    """
    bocpy.wait()


# ---------------------------------------------------------------------------
# Runtime re-entry
# ---------------------------------------------------------------------------


class TestRuntimeReentry:
    """``start()`` / ``wait()`` / ``start()`` runs two clean workloads."""

    @classmethod
    def teardown_class(cls):
        wait()
        drain("done")

    def test_start_wait_start_runs_two_workloads(self):
        """Two independent workloads bracketed by start/wait/start/wait.

        The worker pool, terminator, and per-worker queues all spin
        up cleanly on a second ``start()`` after a prior ``wait()``
        torn the runtime down. A workload that hangs or drops
        messages on the second run indicates state leaked across the
        cycle.
        """
        _ensure_quiesced()

        # First workload.
        bocpy.start(worker_count=2)
        try:
            c = Cown(_Counter())
            for _ in range(50):
                @when(c)
                def _(c):
                    c.value.count += 1
                    send("done", c.value.count)
            for _ in range(50):
                tag, _payload = receive("done", RECEIVE_TIMEOUT)
                assert tag != TIMEOUT, "first workload stalled"
        finally:
            drain("done")
            wait()

        assert _core.scheduler_stats() == []

        # Second workload after teardown — must come up clean.
        bocpy.start(worker_count=2)
        try:
            c = Cown(_Counter())
            for _ in range(50):
                @when(c)
                def _(c):
                    c.value.count += 1
                    send("done", c.value.count)
            for _ in range(50):
                tag, _payload = receive("done", RECEIVE_TIMEOUT)
                assert tag != TIMEOUT, "second workload stalled"
        finally:
            drain("done")
            wait()


# ---------------------------------------------------------------------------
# Paired-release on uncaught body exception
# ---------------------------------------------------------------------------


def _raising_step(c):
    """Body that raises ``RuntimeError`` after touching the cown."""
    @when(c)
    def _(c):
        c.value.count += 1
        raise RuntimeError("intentional failure")


def _follow_on(c):
    """Follow-on behaviour that must observe the cown re-acquirable."""
    @when(c)
    def _(c):
        c.value.count += 1
        send("done", c.value.count)


class TestPairedRelease:
    """An uncaught body exception must still release the cown."""

    @classmethod
    def teardown_class(cls):
        wait()
        drain("done")

    def test_cown_reacquirable_after_uncaught_exception(self):
        """A failing behaviour releases its cown so the next one runs.

        ``run_behavior`` in ``worker.py`` catches ``Exception`` and
        funnels it to ``Cown.set_exception``, then runs the
        release/release_all pair. If the release path were broken the
        follow-on ``@when(c)`` would block forever; the test would
        time out on ``receive`` instead of returning a count of 2.
        """
        _ensure_quiesced()
        bocpy.start(worker_count=2)
        try:
            c = Cown(_Counter())
            _raising_step(c)
            _follow_on(c)

            tag, payload = receive("done", RECEIVE_TIMEOUT)
            assert tag != TIMEOUT, (
                "cown was not re-acquired after an uncaught exception"
            )
            assert payload == 2, payload
        finally:
            drain("done")
            wait()


# ---------------------------------------------------------------------------
# Over-registration contract on scheduler_worker_register
# ---------------------------------------------------------------------------


def test_over_registration_raises_runtime_error():
    """An extra register() beyond worker_count must raise RuntimeError.

    With self-allocating registration, the failure mode is
    over-registration. Production callers (``worker.py``) trust that
    this raises rather than silently corrupting state.
    """
    bocpy.start()
    try:
        # Workers have already registered; one more must fail.
        with pytest.raises(RuntimeError, match="over-registration"):
            _core.scheduler_worker_register()
    finally:
        bocpy.wait()
