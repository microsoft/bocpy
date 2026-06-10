"""End-to-end integration test for the stop/retry composition.

Scope of this file
==================

The various stop/teardown failure modes each have a dedicated
per-link regression test elsewhere in the suite (cown-acquire
unpickle rollback, finite-timeout stop with a slow noticeboard
fn, start-abort-path runtime-stop pairing, off-worker dispatch
after runtime stop, NaN/Inf timeout validation, and BaseException
discipline in worker / orphan-drain paths).

The single thing none of those tests exercises **as a unit** is
the abort/retry path: ``stop(timeout=...)`` times out on a busy
noticeboard thread, raises, and the runtime is then driven
through to a clean second ``start() / @when / wait()`` cycle.
That composition is what this file covers.

We deliberately omit a payload with raising ``__setstate__``
here because ``Behaviors.stop_workers`` walks every Python frame
on the calling thread and calls ``acquire()`` on every ``Cown``
it finds. In a pytest environment the test runner retains
references to test locals, so a payload whose ``__setstate__``
raises gets re-unpickled during teardown and crashes the second
stop unrelated to the abort/retry path. That deserialisation
rollback is fully exercised by its dedicated test.
"""

import time

import pytest

import bocpy
from bocpy import _core
from bocpy import Cown, notice_update, quiesce, wait, when


QUIESCE_TIMEOUT = 10
SLOW_FN_SECONDS = 0.6


def _slow_update_fn(_x):
    """Sleep on the noticeboard thread, then return a fresh value.

    Picklable because it is a module-level function. The argument is
    ignored -- the helper exists solely to occupy the noticeboard
    thread for ``SLOW_FN_SECONDS`` so a subsequent
    ``wait(timeout=0.1)`` reliably hits the noticeboard-join timeout.
    """
    time.sleep(SLOW_FN_SECONDS)
    return 1


class TestStopTimeoutAndRetry:
    """Stop-timeout abort followed by clean retry.

    Drives the abort/retry path that no per-link unit test covers
    as a unit:

    1. ``notice_update`` posts a slow fn to the noticeboard
       thread. ``wait(timeout=0.1)`` times out on the noticeboard
       join and raises ``RuntimeError``.
    2. The orphan-drain mitigation runs before the raise, so
       ``terminator_count`` is 0 even on the failure path.
    3. After the noticeboard fn finishes and a second ``wait()``
       drives teardown to completion, ``start()`` is called
       again. The ``scheduler_runtime_stop`` pairing on the abort
       paths means the new runtime does not inherit a leaked
       ``WORKERS`` array from the timed-out one.
    4. A ``@when`` on the new runtime succeeds. If
       ``boc_sched_dispatch`` failure were silent, this would hang
       or surface a "scheduler not running" error.
    """

    @classmethod
    def teardown_class(cls):
        """Drain the runtime to a clean state."""
        wait()

    def test_stop_timeout_then_retry(self):
        """Time out on a slow noticeboard fn, then retry start() cleanly."""
        wait()

        bocpy.start(worker_count=1)
        try:
            notice_update("retry_key", _slow_update_fn, default=0)
            time.sleep(0.05)
        except BaseException:
            try:
                wait()
            except Exception:
                pass
            raise

        with pytest.raises(RuntimeError, match="noticeboard thread did not shut down"):
            wait(timeout=0.1)

        assert _core.terminator_count() == 0, (
            "terminator_count is non-zero after wait(timeout=0.1) "
            "timed out on the noticeboard join. The orphan drain "
            "did not run before the RuntimeError."
        )

        wait()

        bocpy.start(worker_count=2)
        try:
            self._run_fresh_when()
        finally:
            wait()

    def _run_fresh_when(self):
        """Schedule a @when on the second runtime and confirm it ran.

        Wrapped in a helper so the ``fresh`` Cown leaves scope
        before the final ``wait()``.
        """
        fresh = Cown(0)

        @when(fresh)
        def _(c):
            return c.value

        quiesce(QUIESCE_TIMEOUT)
        assert fresh.unwrap() == 0
