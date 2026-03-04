"""Runtime behaviors and helpers for bocpy's cown-based scheduler."""

import inspect
import logging
import os
import shutil
import sys
import tempfile
from textwrap import dedent
import threading
from typing import Any, Generic, Mapping, Optional, TypeVar, Union

from . import _core, set_tags
from .transpiler import BehaviorInfo, export_main, export_module_from_file

try:
    import _interpreters as interpreters
except ModuleNotFoundError:
    import _xxsubinterpreters as interpreters


BEHAVIORS = None
WORKER_COUNT: int = 1
try:
    WORKER_COUNT = len(os.sched_getaffinity(0)) - 1
except AttributeError:
    from multiprocessing import cpu_count
    WORKER_COUNT = cpu_count() - 1

T = TypeVar("T")


class Cown(Generic[T]):
    """Lightweight wrapper around the underlying cown capsule."""

    def __init__(self, value: T):
        """Create a cown."""
        logging.debug(f"initialising Cown with value: {value}")
        if isinstance(value, _core.CownCapsule):
            self.impl = value
        else:
            self.impl = _core.CownCapsule(value)
            self.impl.release()

    def __enter__(self):
        """Acquire the cown for a context manager block."""
        self.acquire()
        return self.impl.get()

    def __exit__(self, exc_type, exc_value, traceback):
        """Release the cown after a context manager block."""
        self.release()

    @property
    def value(self) -> T:
        """Return the current stored value."""
        return self.impl.get()

    @value.setter
    def value(self, value: T):
        """Set a new stored value."""
        return self.impl.set(value)

    def acquire(self):
        """Acquires the cown (required for reading and writing)."""
        self.impl.acquire()

    def release(self):
        """Releases the cown."""
        self.impl.release()

    @property
    def acquired(self) -> bool:
        """Whether the cown is currently acquired."""
        return self.impl.acquired()

    def __lt__(self, other: "Cown") -> bool:
        """Order by the underying capsule for deterministic ordering."""
        if not isinstance(other, Cown):
            return NotImplemented

        return self.impl < other.impl

    def __eq__(self, other: "Cown") -> bool:
        """Equality based on the wrapped capsule."""
        if not isinstance(other, Cown):
            return NotImplemented

        return self.impl == other.impl

    def __hash__(self) -> int:
        """Hash of the underlying capsule."""
        return hash(self.impl)

    def __str__(self) -> str:
        """Readable string form."""
        return str(self.impl)

    def __repr__(self) -> str:
        """Debug representation."""
        return repr(self.impl)


class Request:
    """Wrapper for requests produced by behaviors."""

    def __init__(self, impl):
        """Store the underlying request implementation."""
        self.impl = impl

    def release(self):
        """Release the cown to the next behavior.

        This is called when the associated behavior has completed, and thus can
        allow any waiting behavior to run.

        If there is no next behavior, then the cown's `last` pointer is set to null.
        """
        _core.request_release(self.impl)

    def target(self) -> int:
        """Returns the target cown of the request."""
        return _core.request_target(self.impl)

    def start_enqueue(self, behavior: "Behavior"):
        """Start the first phase of the 2PL enqueue operation.

        This enqueues the request onto the cown.  It will only return
        once any previous behavior on this cown has finished enqueueing
        on all its required cowns.  This ensures that the 2PL is obeyed.
        """
        _core.request_start_enqueue(self.impl, behavior.impl)

    def finish_enqueue(self):
        """Finish the second phase of the 2PL enqueue operation.

        This will set the scheduled flag, so subsequent behaviors on this
        cown can continue the 2PL enqueue.
        """
        _core.request_finish_enqueue(self.impl)


class Behavior:
    """Behavior that captures the content of a when body.

    It contains all the state required to run the body, and release the cowns
    when the body has finished.
    """

    def __init__(self, impl: _core.BehaviorCapsule):
        """Wrap the capsule and materialize request wrappers."""
        self.impl = impl
        self.bid = impl.bid()
        self.thunk = impl.thunk()
        self.requests = [Request(req_impl) for req_impl in impl.create_requests()]
        self.requests.sort(key=lambda r: r.target())

    def schedule(self):
        """Schedule the behavior using two-phase locking over requests."""
        # Complete first phase of 2PL enqueuing on all cowns.
        for r in self.requests:
            r.start_enqueue(self)

        # Complete second phase of 2PL enqueuing on all cowns.
        for r in self.requests:
            r.finish_enqueue()

        # Resolve the additional request. [See comment in the Constructor]
        # All the cowns may already be resolved, in which case, this will
        # schedule the task.
        self.impl.resolve_one()

    def start(self):
        """Send the behavior to a worker to execute."""
        _core.send("boc_worker", self.impl)

    def release(self):
        """Release all owned requests."""
        for r in self.requests:
            r.release()


WORKER_MAIN_END = "# END boc_export"


class Behaviors:
    """Coordinator that starts workers and schedules behaviors."""

    def __init__(self, num_workers: Optional[int], export_dir: Optional[str]):
        """Creates a new Behaviors scheduler.

        :param num_workers: The number of worker interpreters to start.  If
            None, defaults to the number of available cores minus one.
        :type num_workers: Optional[int]
        :param export_dir: The directory to which the target module will be
            exported for worker import.  If None, a temporary directory will
            be created and removed on shutdown.
        :type export_dir: Optional[str]
        """
        self.num_workers = WORKER_COUNT if num_workers is None else num_workers
        self.export_dir = export_dir
        self.export_tmp = export_dir is None
        self.worker_script = None
        self.classes = set()
        self.worker_threads = []
        self.behavior_lookup: Mapping[int, BehaviorInfo] = {}
        self.logger = logging.getLogger("behaviors")
        self.logger.debug("behaviors init")
        self.scheduler = None
        self.final_cowns: tuple[Cown, ...] = ()
        self.bid = 0

    def lookup_behavior(self, line_number: int) -> BehaviorInfo:
        """Resolve behavior info from a source line number."""
        if line_number in self.behavior_lookup:
            return self.behavior_lookup[line_number]

        # 3.10: Might be off by one
        if line_number - 1 in self.behavior_lookup:
            return self.behavior_lookup[line_number - 1]

        return None

    def teardown_workers(self):
        """Joins the worker threads and destroys the worker subinterpreters."""
        self.logger.debug("waiting on workers to shutdown...")
        for t in self.worker_threads:
            t.join()

        self.worker_threads.clear()

    def start_workers(self):
        """Launch worker interpreters and wait until they signal readiness."""
        def worker():
            interp = interpreters.create()
            result = interpreters.run_string(interp, dedent(self.worker_script))
            if result is not None:
                _core.send("boc_behavior", result.formatted)

            try:
                interpreters.destroy(interp)
            except RuntimeError:
                pass  # already destroyed

        for _ in range(self.num_workers):
            t = threading.Thread(target=worker)
            self.worker_threads.append(t)
            t.start()

        num_errors = 0
        self.logger.debug("waiting for workers to start")
        for _ in range(self.num_workers):
            match _core.receive("boc_behavior"):
                case ["boc_behavior", "started"]:
                    self.logger.debug("boc_behavior/started")

                case ["boc_behavior", error]:
                    print(error)
                    num_errors += 1

        if num_errors == self.num_workers:
            self.teardown_workers()
            raise RuntimeError("Error starting worker pool")

    def stop_workers(self):
        """Shut down worker interpreters and clean up any held cowns."""
        self.logger.debug("acquiring any cowns in the global context")
        frame = inspect.currentframe()
        while frame is not None:
            for name in list(frame.f_globals):
                val = frame.f_globals[name]
                if isinstance(val, Cown) or isinstance(val, _core.CownCapsule):
                    self.logger.debug(f"acquiring {name}")
                    val.acquire()

            for name in list(frame.f_locals):
                val = frame.f_locals[name]
                if isinstance(val, Cown) or isinstance(val, _core.CownCapsule):
                    self.logger.debug(f"acquiring {name}")
                    val.acquire()

            frame = frame.f_back

        for cown in self.final_cowns:
            cown.acquire()

        self.logger.debug("stopping workers")
        for _ in range(self.num_workers):
            _core.send("boc_worker", "shutdown")

        for _ in range(self.num_workers):
            _, contents = _core.receive("boc_behavior")
            assert contents == "shutdown"

        for _ in range(self.num_workers):
            _core.send("boc_cleanup", True)

        self.teardown_workers()
        self.logger.debug("workers stopped")

    def start_scheduler(self):
        """Start the scheduler loop in a dedicated thread."""
        def scheduler():
            self.logger.debug("starting the scheduler")
            behaviors: Mapping[int, Behavior] = {}
            terminator = 1
            exception = None
            self.logger.debug("all workers started, scheduling")
            while terminator > 0:
                match _core.receive("boc_behavior"):
                    case ["boc_behavior", "terminator_decrement"]:
                        terminator -= 1
                        self.logger.debug(f"boc_behavior/terminator_decrement({terminator})")

                    case ["boc_behavior", ("release", bid)]:
                        self.logger.debug(f"boc_behavior/release(bid={bid})")
                        behaviors[bid].release()
                        del behaviors[bid]
                        terminator -= 1

                    case ["boc_behavior", ("schedule", behavior_impl)]:
                        self.logger.debug("boc_behavior/schedule")
                        behavior = Behavior(behavior_impl)
                        terminator += 1
                        self.logger.debug(f"boc_behavior/schedule(thunk={behavior.thunk})")
                        # prevent runtime exiting until this has run
                        behaviors[behavior.bid] = behavior
                        behavior.schedule()
                        behavior = None
                        behavior_impl = None

                    case ["boc_behavior", ("start", bid)]:
                        self.logger.debug(f"boc_behavior/start(bid={bid})")
                        behaviors[bid].start()

            if exception:
                raise exception

        self.scheduler = threading.Thread(target=scheduler)
        self.scheduler.start()

    def start(self, module: Optional[tuple[str, str]]):
        """Export the target module and spin up workers and scheduler."""
        path = os.path.join(os.path.dirname(__file__), "worker.py")

        with open(path) as file:
            worker_script = file.read()

        worker_script = worker_script.replace("logging.NOTSET", str(logging.getLogger().level))

        if module is None:
            export = export_main()
            module_name = "__main__"
        else:
            export = export_module_from_file(module[1])
            module_name = f"{module[0]}"

        if self.export_dir is None:
            self.export_dir = tempfile.mkdtemp()
            self.export_tmp = True

        self.behavior_lookup = export.behaviors
        path = os.path.join(self.export_dir, f"{module_name}.py")
        with open(path, "w") as file:
            file.write(export.code)

        main_start = worker_script.find(WORKER_MAIN_END)

        if module_name == "__main__":
            lines = [f'load_boc_module("__bocmain__", r"{path}")', 'boc_export = sys.modules["__bocmain__"]']
            sys.modules["__bocmain__"] = sys.modules["__main__"]
            for cls in export.classes:
                lines.append(f'\n\nclass {cls}(sys.modules["__bocmain__"].{cls}):')
                lines.append("    pass")
        else:
            lines = [f'load_boc_module("{module_name}", r"{path}")', f'boc_export = sys.modules["{module_name}"]']

        lines.append("")

        self.worker_script = worker_script[:main_start] + "\n".join(lines) + worker_script[main_start:]

        set_tags(["boc_behavior", "boc_worker", "boc_cleanup"])
        self.start_workers()
        self.start_scheduler()

    def __enter__(self):
        """Enter context by starting the runtime."""
        self.start()

    def stop(self, timeout: Optional[float] = None):
        """Stop scheduler and workers, removing any temp exports."""
        _core.send("boc_behavior", "terminator_decrement")
        self.scheduler.join(timeout)
        self.stop_workers()
        if os.path.exists(self.export_dir) and self.export_tmp:
            shutil.rmtree(self.export_dir)

    def __exit__(self, exc_type, exc_value, traceback):
        """Ensure stop is called on context exit."""
        self.stop()


def whencall(thunk: str, args: list[Union[Cown, list[Cown]]], captures: list[Any]) -> Cown:
    """Invoke a behavior by name with cown args and captured values."""
    result = Cown(None)

    cowns = []
    group_id = 1
    for item in args:
        if isinstance(item, (Cown, _core.CownCapsule)):
            cowns.append((group_id, item.impl))
            group_id += 1
            continue

        if not isinstance(item, (list, tuple)):
            raise TypeError("can only schedule over cowns or sequences of cowns")

        for c in item:
            if not isinstance(c, (Cown, _core.CownCapsule)):
                raise TypeError("can only schedule over cowns or sequences of cowns")

            cowns.append((-group_id, c.impl))

        group_id += 1

    behavior = _core.BehaviorCapsule(thunk, result.impl, cowns, captures)
    logging.debug(f"whencall:behavior=Behavior(thunk={thunk}, result={result}, args={args}, captures={captures})")
    _core.send("boc_behavior", ("schedule", behavior))
    return result


def get_caller_module():
    """Get the caller's module name and file path."""
    frame = inspect.currentframe().f_back.f_back
    name = frame.f_globals["__name__"]
    file = frame.f_globals["__file__"]
    return (name, file)


def start(worker_count: Optional[int] = None,
          export_dir: Optional[str] = None,
          module: Optional[tuple[str, str]] = None):
    """Start the behavior scheduler and worker pool.

    :param worker_count: The number of worker interpreters to start.  If
        None, defaults to the number of available cores minus one.
    :type worker_count: Optional[int]
    :param export_dir: The directory to which the target module will be
        exported for worker import.  If None, a temporary directory will
        be created and removed on shutdown.
    :type export_dir: Optional[str]
    :param module: A tuple of the target module name and file path to export
        for worker import.  If None, the caller's module will be used.
    :type module: Optional[tuple[str, str]]
    """
    global BEHAVIORS
    if BEHAVIORS is not None:
        raise RuntimeError("Behavior scheduler already started")

    if worker_count is None:
        worker_count = WORKER_COUNT

    if not _core.is_primary():
        raise RuntimeError("start() can only be called from the main interpreter")

    if module is None:
        module = get_caller_module()
    BEHAVIORS = Behaviors(worker_count, export_dir)
    BEHAVIORS.start(module)


def when(*cowns):
    """Decorator to schedule a function as a behavior using given cowns.

    This decorator takes a list of zero or more cown objects, which will be passed in the order
    in which they were provided to the decorated function. The function itself is extracted and
    run as a behavior once all the cowns are available (i.e., not acquired by other behaviors).
    Behaviors are scheduled such that deadlock will not occur.

    The function itself will be replaced by a Cown which will hold the
    result of executing the behavior. This Cown can be used for further
    coordination.
    """

    def when_factory(func):
        when_frame = inspect.currentframe().f_back

        if BEHAVIORS is None and _core.is_primary():
            start(module=get_caller_module())

        logging.debug("when:start")
        binfo = BEHAVIORS.lookup_behavior(when_frame.f_lineno)
        if binfo is None:
            print("Behavior not found at line", when_frame.f_lineno)
            print(BEHAVIORS.behavior_lookup)
            return None

        logging.debug(f"when:behavior={binfo}")
        captures = []
        for name in binfo.captures:
            frame = when_frame
            found = False
            while frame is not None:
                if name in frame.f_locals:
                    val = frame.f_locals[name]
                    captures.append(val)
                    found = True
                    break

                frame = frame.f_back

            if not found:
                raise RuntimeError(f"Cannot resolve capture: {name}")

        result = whencall(binfo.name, cowns, captures)

        logging.debug("when:end")

        return result

    return when_factory


def wait(timeout: Optional[float] = None):
    """Block until all behaviors complete, with optional timeout."""
    global BEHAVIORS
    if BEHAVIORS:
        BEHAVIORS.stop(timeout)
        BEHAVIORS = None
