"""Worker process that runs exported behaviors in subinterpreters."""

import importlib.util
import logging
import sys

from bocpy import _core, receive, send


logging.basicConfig(level=logging.NOTSET)
index = _core.index()
logger = logging.getLogger(f"worker{index}")


def load_boc_module(module_name, file_path):
    """Load a bocpy-exported module into this interpreter."""
    logger.debug(f"Loading bocpy export {module_name} from {file_path}")
    # Create a module specification from the file location
    spec = importlib.util.spec_from_file_location(module_name, file_path)

    # Create a new module based on the spec
    module = importlib.util.module_from_spec(spec)

    # Register the module in sys.modules
    sys.modules[module_name] = module

    # Execute the module
    spec.loader.exec_module(module)


boc_export = None

# The boc_export module and any of its classes which are needed for unpickling
# are loaded and aliased within these tags when the worker script is generated.

# BEGIN boc_export
# END boc_export


def run_behavior(behavior):
    """Execute a single behavior and release its requests inline."""
    try:
        try:
            _core.noticeboard_cache_clear()
            behavior.acquire()
        except Exception as ex:
            # acquire() / cache_clear() failed before the body ran. The
            # MCS chain for this behavior is still linked (behavior_schedule
            # established the links on the caller thread), so we must
            # unwind it here or every successor blocks forever. Mark
            # the result Cown with the exception so any caller awaiting
            # it sees a diagnostic instead of a permanent None.
            logger.exception(ex)
            try:
                behavior.set_exception(ex)
            except Exception as inner:
                logger.exception(inner)
            # acquire() is sequential (result -> args -> captures) and
            # bails on first failure, so on a partial-success raise some
            # cowns are owned by this worker and some are not. release()
            # is similarly tolerant (it short-circuits NO_OWNER cowns),
            # so calling it here releases the ones we did acquire before
            # release_all hands the request to a successor. Without this
            # the successor's cown_acquire fails with "already acquired
            # by <this interp>" and every behavior on that cown strands.
            try:
                behavior.release()
            except Exception as inner:
                logger.exception(inner)
            try:
                behavior.release_all()
            except Exception as inner:
                logger.exception(inner)
            return

        try:
            behavior.execute(boc_export)
        except Exception as ex:
            logger.exception(ex)
            behavior.set_exception(ex)

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
        # raised, failing to decrement here would leave wait() hung
        # forever. Log and swallow so a single misbehaving worker step
        # cannot strand the runtime.
        try:
            _core.terminator_dec()
        except Exception as ex:
            logger.exception(ex)


def do_work():
    """Main worker loop receiving behaviors or shutdown messages."""
    try:
        running = True
        logger.debug("worker starting")
        send("boc_behavior", "started")
        while running:
            try:
                match receive("boc_worker"):
                    case ["boc_worker", "shutdown"]:
                        logger.debug("boc_worker/shutdown")
                        running = False

                    case ["boc_worker", behavior]:
                        run_behavior(behavior)
                        behavior = None
            except Exception as ex:
                # A failure inside run_behavior or receive must not
                # break the loop -- if it did, this worker would exit
                # without sending its "shutdown" reply and stop_workers
                # would block forever waiting for it.
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


do_work()
cleanup()

logger = None

# in Python 3.12 and prior, the threading module can cause issues with
# subinterpreter destruction
del sys.modules["logging"]
del sys.modules["threading"]
