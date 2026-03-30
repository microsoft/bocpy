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
    """Execute a single behavior and notify the scheduler."""
    bid = behavior.bid()
    behavior.acquire()
    try:
        behavior.execute(boc_export)
    except Exception as ex:
        logger.exception(ex)
        behavior.set_result(ex)

    behavior.release()
    send("boc_behavior", ("release", bid))


def do_work():
    """Main worker loop receiving behaviors or shutdown messages."""
    try:
        running = True
        logger.debug("worker starting")
        send("boc_behavior", "started")
        while running:
            match receive("boc_worker"):
                case ["boc_worker", "shutdown"]:
                    logger.debug("boc_worker/shutdown")
                    running = False

                case ["boc_worker", behavior]:
                    run_behavior(behavior)
                    behavior = None

        logger.debug("worker stopped")
        send("boc_behavior", "shutdown")
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
