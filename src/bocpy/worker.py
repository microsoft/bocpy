"""Worker process that runs exported behaviors in subinterpreters."""

import logging
import sys

from bocpy import _core, receive, send


logging.basicConfig(level=logging.NOTSET)
index = _core.index()
logger = logging.getLogger(f"worker{index}")

_CLEANUP_RECEIVE_TIMEOUT = 120.0


boc_export = None


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
                logger.exception(ex)
                try:
                    behavior.set_exception(ex)
                except Exception as inner:
                    logger.exception(inner)

            if acquired:
                try:
                    behavior.execute(boc_export)
                except Exception as ex:
                    logger.exception(ex)
                    behavior.set_exception(ex)
        finally:
            try:
                behavior.release()
            except Exception as ex:
                logger.exception(ex)
            try:
                behavior.release_all()
            except Exception as ex:
                logger.exception(ex)
    finally:
        try:
            _core.terminator_dec()
        except Exception as ex:
            logger.exception(ex)


def do_work():
    """Main worker loop receiving behaviors or shutdown messages."""
    try:
        logger.debug("worker starting")
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
                behavior = _core.scheduler_worker_pop()
                if behavior is None:
                    logger.debug("scheduler stop signal received")
                    break
                run_behavior(behavior)
                behavior = None
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as ex:
                logger.exception(ex)

        logger.debug("worker stopped")
    except Exception as ex:
        logger.exception(ex)
    finally:
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
        tag, _ = receive("boc_cleanup", _CLEANUP_RECEIVE_TIMEOUT)
        if tag == _core.TIMEOUT:
            logger.warning(
                "cleanup: boc_cleanup signal not received within %.1fs; "
                "proceeding with cown recycle so the sub-interpreter "
                "can be torn down.",
                _CLEANUP_RECEIVE_TIMEOUT,
            )

        orphan_cowns = _core.cowns()
        if len(orphan_cowns) != 0:
            logger.debug("acquiring orphan cowns")
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
    # cleanup() must run on any BaseException: leftover live cross-interpreter
    # data makes interpreters.destroy() fail and hangs pool teardown.
    try:
        cleanup()
    finally:
        logger = None
        # <=3.12: leaving these in sys.modules wedges sub-interpreter destruction.
        for _modname in ("logging", "threading"):
            sys.modules.pop(_modname, None)
