"""Worker process that runs exported behaviors in subinterpreters."""

import logging
import sys

from bocpy import _core, receive, send


logging.basicConfig(level=logging.NOTSET)
index = _core.index()
logger = logging.getLogger(f"worker{index}")


boc_export = None

# The boc_export module and any of its classes which are needed for
# unpickling are loaded and aliased within these tags when the worker
# script is generated. The transpiled source is embedded as a Python
# string literal (via ``repr()``) and exec'd into a fresh
# ``types.ModuleType``; a ``linecache`` entry under a synthetic
# filename ``<bocpy:NAME>`` keeps tracebacks pointing at the
# transpiled source line. No on-disk artifact is created.

# BEGIN boc_export
# END boc_export


def run_behavior(behavior):
    """Execute a single behavior and release its requests inline.

    Layered ``try/finally`` blocks guarantee the MCS unlink and the
    terminator decrement run even when the body raises a non-``Exception``
    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``,
    ``PythonFinalizationError`` since 3.13). Such an exception is **not**
    caught here — it propagates upward through the finallies, which is
    exactly what we want: every cleanup step still runs, and the outer
    worker loop in :func:`do_work` re-raises so the worker exits cleanly.
    Only ``Exception`` (user-code errors, transient C failures) is
    explicitly handled and logged.
    """
    try:
        acquired = False
        try:
            try:
                _core.noticeboard_cache_clear()
                behavior.acquire()
                acquired = True
            except Exception as ex:
                # acquire() / cache_clear() failed before the body ran.
                # The MCS chain is still linked (behavior_schedule
                # established the links on the caller thread), so the
                # outer finally below MUST run release/release_all to
                # unwind it -- otherwise every successor blocks forever.
                # Mark the result Cown so a caller awaiting it sees a
                # diagnostic instead of a permanent None.
                logger.exception(ex)
                try:
                    behavior.set_exception(ex)
                except Exception as inner:
                    logger.exception(inner)
                # Fall through: `acquired` is False, so we skip execute()
                # but still run the release pair in the outer finally.

            if acquired:
                try:
                    behavior.execute(boc_export)
                except Exception as ex:
                    logger.exception(ex)
                    behavior.set_exception(ex)
        finally:
            # Runs on every path: clean acquire, failed acquire, normal
            # body return, body Exception, OR body KI/SystemExit (which
            # propagates after this finally completes).
            #
            # acquire() is sequential (result -> args -> captures) and
            # bails on first failure, so on a partial-success raise some
            # cowns are owned by this worker and some are not. release()
            # is tolerant (it short-circuits NO_OWNER cowns), so calling
            # it here releases the ones we did acquire before
            # release_all hands the request to a successor.
            try:
                behavior.release()
            except Exception as ex:
                logger.exception(ex)
            # Release the request array on the worker thread instead of
            # round-tripping ("release", capsule) through the (now-gone)
            # central scheduler thread.
            try:
                behavior.release_all()
            except Exception as ex:
                logger.exception(ex)
    finally:
        # Drop the terminator hold unconditionally. If anything above
        # raised (Exception or BaseException), failing to decrement
        # here would leave wait() hung forever. Log and swallow
        # Exception so a single misbehaving step cannot strand the
        # runtime; KI/SystemExit from terminator_dec itself is
        # extraordinarily unlikely (pure C atomic) and would propagate.
        try:
            _core.terminator_dec()
        except Exception as ex:
            logger.exception(ex)


def do_work():
    """Main worker loop receiving behaviors or shutdown messages."""
    try:
        logger.debug("worker starting")
        # Claim a scheduler slot and stamp the per-thread TLS handle
        # before announcing readiness. Subsequent dispatch / pop paths
        # rely on this slot being installed. If registration fails
        # (over-spawn vs. scheduler_runtime_start), surface the error
        # so start_workers stops waiting.
        try:
            slot = _core.scheduler_worker_register()
            logger.debug("registered scheduler slot %d", slot)
        except Exception as ex:
            logger.exception(ex)
            send("boc_behavior", f"register failed: {ex}")
            return
        send("boc_behavior", "started")
        while True:
            try:
                # scheduler_worker_pop blocks on the worker's own
                # condvar (with the GIL released). It returns None
                # only when scheduler_request_stop_all has been
                # called by stop_workers.
                behavior = _core.scheduler_worker_pop()
                if behavior is None:
                    logger.debug("scheduler stop signal received")
                    break
                run_behavior(behavior)
                behavior = None
            except (KeyboardInterrupt, SystemExit):
                # Propagate so the worker can wind down: the outer
                # try/finally still sends "shutdown" before the
                # interpreter exits, so stop_workers does not hang.
                raise
            except Exception as ex:
                # A regular Exception inside run_behavior or
                # scheduler_worker_pop must not break the loop -- if
                # it did, this worker would exit without sending its
                # "shutdown" reply and stop_workers would block forever
                # waiting for it.
                logger.exception(ex)

        logger.debug("worker stopped")
    except Exception as ex:
        logger.exception(ex)
    finally:
        # Always tell stop_workers we are leaving the loop, even on an
        # unexpected exception, so it never hangs in receive("boc_behavior").
        try:
            send("boc_behavior", "shutdown")
        except Exception as ex:
            logger.exception(ex)
        try:
            _core.noticeboard_cache_clear()
        except Exception as ex:
            logger.exception(ex)


def cleanup():
    """Recycle remaining cowns and wait for cleanup signal."""
    try:
        receive("boc_cleanup")

        orphan_cowns = _core.cowns()
        if len(orphan_cowns) != 0:
            logger.debug("acquiring orphan cowns")
            # at this stage all behaviors have exited, but it may be the case
            # that some cowns are released but associated with this interpreter.
            # by acquiring them, we ensure that the XIData objects have been
            # freed _before_ this interpreter is destroyed.
            for cown in orphan_cowns:
                if cown is not None:
                    cown.acquire()
                    cown.disown()

        orphan_cowns = None
        _core.recycle()
    except Exception as ex:
        logger.exception(ex)


try:
    do_work()
finally:
    # Always run cleanup, even if do_work() bubbled out a
    # KeyboardInterrupt / SystemExit / PythonFinalizationError.
    # Skipping cleanup leaves XIData objects live inside this
    # sub-interpreter; subsequent destruction then fails with
    # "interpreter has live cross-interpreter data" and the
    # worker pool teardown blocks.
    #
    # The post-cleanup `sys.modules` clears below are also
    # destruction-critical on Python 3.12 and prior, so they live in
    # an inner `finally` that runs even if `cleanup()` itself raises
    # a BaseException (e.g. KeyboardInterrupt parking inside
    # `receive("boc_cleanup")`, or PythonFinalizationError out of
    # `_core.recycle()`). Skipping them re-introduces the
    # subinterpreter-destruction wedge in mirror image.
    try:
        cleanup()
    finally:
        logger = None
        # in Python 3.12 and prior, the threading module can cause
        # issues with subinterpreter destruction. `pop(..., None)`
        # is used instead of `del` so a module already removed by
        # an earlier failure path does not raise KeyError here.
        for _modname in ("logging", "threading"):
            sys.modules.pop(_modname, None)
