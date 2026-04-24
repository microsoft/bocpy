"""Runtime lifecycle and Python-side glue for bocpy's behavior runtime.

This module owns the runtime singleton, the worker-pool launcher, the
noticeboard thread, and the Python `Cown` / `Behavior` / `@when`
facades. It does **not** contain a central scheduler thread: scheduling (2PL,
request linking, dispatch) runs in the caller's thread via
`_core.BehaviorCapsule.schedule`, and release runs in the worker thread that
just executed the behavior. The only centralized helper that survives
is the noticeboard thread, which serializes mutator messages so the
C-level read-modify-write stays consistent without forcing behaviors
to block on a mutex.
"""

import inspect
import logging
import os
import shutil
import sys
import tempfile
from textwrap import dedent
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Generic, Mapping, Optional, TypeVar, Union

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

# Sentinel distinguishing "key absent" from "key is None" in noticeboard updates.
_ABSENT = object()


class _RemovedType:
    """Sentinel returned by notice_update fn to delete the entry."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "REMOVED"

    def __reduce__(self):
        return (_RemovedType, ())


REMOVED = _RemovedType()


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
    def exception(self) -> bool:
        """Whether the held value is the result of an unhandled exception."""
        return self.impl.exception

    @exception.setter
    def exception(self, value: bool):
        """Set or clear the exception flag."""
        self.impl.exception = value

    @property
    def acquired(self) -> bool:
        """Whether the cown is currently acquired."""
        return self.impl.acquired()

    def __lt__(self, other: "Cown") -> bool:
        """Order by the underlying capsule for deterministic ordering."""
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


WORKER_MAIN_END = "# END boc_export"


class Behaviors:
    """Coordinator that starts workers and schedules behaviors."""

    def __init__(self, num_workers: Optional[int], export_dir: Optional[str]):
        """Creates a new Behaviors runtime.

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
        # The runtime has no central scheduler thread. Caller threads do 2PL
        # inline (whencall -> behavior_schedule), workers release inline,
        # and the C-level terminator is the only pending counter.
        self.noticeboard = None
        self._noticeboard_start_error: Optional[BaseException] = None
        # Set to True by stop() once worker shutdown, noticeboard
        # tear-down, and tempdir cleanup have all completed. The
        # warned-stop / drain-error raise from stop() happens *after*
        # this flips, so wait()/__exit__ can use the flag to
        # distinguish "stop() raised but the runtime is dead -- clear
        # the global handle" from "stop() raised mid-teardown and the
        # runtime is still alive -- retain the handle so the caller
        # can retry stop()".
        self._teardown_complete = False
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

    def start_noticeboard(self):
        """Start the dedicated noticeboard mutator thread.

        The noticeboard intentionally remains message-driven: writers
        (``notice_write``/``notice_update``/``notice_delete``) are
        fire-and-forget from the calling behavior, so behaviors never
        block on the noticeboard mutex. This thread owns the C-level
        single-writer slot and serves the ``boc_noticeboard`` queue.

        Startup is synchronous: the thread signals readiness only after
        ``set_noticeboard_thread()`` has successfully claimed the C-level
        single-writer slot. If the claim fails (e.g. a prior ``stop()``
        left the slot pinned), the exception is captured and re-raised
        on the calling thread so the runtime never enters a half-started
        state where mutations would queue forever with no consumer.
        """
        ready = threading.Event()
        self._noticeboard_start_error = None

        def noticeboard():
            self.logger.debug("starting the noticeboard thread")
            # Pin this thread as the only legitimate noticeboard mutator.
            # The C layer rejects write_direct/delete from any other
            # thread, eliminating the TOCTOU window in the Python-level
            # read-modify-write performed by noticeboard_update.
            try:
                _core.set_noticeboard_thread()
            except BaseException as ex:  # noqa: B036
                # Captured here and re-raised on the starter thread by
                # start_noticeboard so the runtime fails loudly instead
                # of silently stranding the noticeboard mutator.
                self._noticeboard_start_error = ex
                ready.set()
                return
            ready.set()
            while True:
                match _core.receive("boc_noticeboard"):
                    case ["boc_noticeboard", "shutdown"]:
                        self.logger.debug("boc_noticeboard/shutdown")
                        return

                    case ["boc_noticeboard", ("noticeboard_write", key, value, cowns)]:
                        try:
                            _core.noticeboard_write_direct(key, value, cowns)
                        except Exception as ex:
                            self.logger.warning(f"noticeboard_write({key!r}) failed: {ex}")

                    case ["boc_noticeboard", ("noticeboard_update", key, fn, default)]:
                        try:
                            # Force a fresh snapshot for this read-modify-write:
                            # this thread is not a behavior, so the
                            # default no-polling semantics do not apply here and
                            # we want to see the latest committed state.
                            _core.noticeboard_cache_clear()
                            snap = _core.noticeboard_snapshot()
                            current = snap.get(key, _ABSENT)
                            if current is _ABSENT:
                                current = default
                            new_value = fn(current)
                            if new_value is REMOVED:
                                _core.noticeboard_delete(key)
                            else:
                                # write_direct bumps NB_VERSION; other readers'
                                # caches will revalidate at their next behavior
                                # boundary. Re-pin any cowns reachable from
                                # the new value (the previous entry's pins are
                                # released by write_direct). We are on the
                                # noticeboard thread here so cown_pin_pointers
                                # is safe — its INCREFs will be transferred
                                # into the entry by write_direct.
                                pin_ptrs = _core.cown_pin_pointers(
                                    _gather_pins(new_value))
                                _core.noticeboard_write_direct(
                                    key, new_value, pin_ptrs)
                        except Exception as ex:
                            self.logger.warning(f"noticeboard_update({key!r}) failed: {ex}")
                        finally:
                            # Re-arm the version check for any subsequent
                            # snapshot call from this thread.
                            _core.noticeboard_cache_clear()

                    case ["boc_noticeboard", ("noticeboard_delete", key)]:
                        try:
                            _core.noticeboard_delete(key)
                        except Exception as ex:
                            self.logger.warning(f"noticeboard_delete({key!r}) failed: {ex}")

                    case ["boc_noticeboard", ("sync", seq)]:
                        # Barrier sentinel posted by notice_sync(). Marking
                        # this sequence complete wakes any caller blocked
                        # in notice_sync_wait. Because the boc_noticeboard
                        # tag is FIFO per producer, every write/update/delete
                        # the caller posted before this sentinel has already
                        # been processed above by the time we get here.
                        _core.notice_sync_complete(seq)

        self.noticeboard = threading.Thread(target=noticeboard)
        self.noticeboard.start()
        # Block until the thread has either claimed the noticeboard slot
        # or captured an error. Without this handshake a failed claim
        # would be invisible: notice_write/update/delete would enqueue
        # to boc_noticeboard with no consumer, notice_sync() would block
        # forever, and stop() would observe a non-alive thread and
        # discard the entire backlog.
        ready.wait()
        if self._noticeboard_start_error is not None:
            err = self._noticeboard_start_error
            self._noticeboard_start_error = None
            self.noticeboard.join()
            raise RuntimeError(
                "noticeboard thread failed to claim the C-level "
                "single-writer slot"
            ) from err

    def start(self, module: Optional[tuple[str, str]] = None):
        """Export the target module and spin up workers and the noticeboard thread.

        :param module: Optional ``(module_name, source_path)`` tuple
            identifying the user module to transpile and export.
            ``None`` (the default) exports ``__main__`` instead, which
            is the case auto-triggered by the first ``@when`` call in a
            script.
        :type module: Optional[tuple[str, str]]
        """
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
        with open(path, "w", encoding="utf-8") as file:
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

        set_tags(["boc_behavior", "boc_worker", "boc_cleanup", "boc_noticeboard"])
        # Bring up workers and the noticeboard thread first. We seed
        # the C-level terminator only after both succeed so a failure
        # in start_noticeboard (or anywhere between here and the
        # terminator_reset below) leaves the terminator in its
        # post-stop() quiescent state (count=0, seeded=0) and the
        # next start() can proceed cleanly without a drift diagnostic
        # firing. On a partial-startup failure we also tear the
        # workers back down so the subsequent start() is not blocked
        # by stale shutdown handshakes or dangling sub-interpreters.
        self.start_workers()
        try:
            self.start_noticeboard()
        except BaseException:
            # Close the terminator first so any sibling thread that
            # somehow races a whencall during the abort window is
            # refused at terminator_inc rather than slipping a real
            # behavior into boc_worker between our shutdown sentinels.
            # TERMINATOR_CLOSED is 0 on the very first start() of the
            # process and 1 after any prior stop()/abort; either way,
            # set it to 1 explicitly. terminator_close() is idempotent.
            _core.terminator_close()
            self._abort_workers()
            raise

        # Arm the C-level terminator (count=1 seed, closed=0, seeded=1).
        # reset() returns the prior (count, seeded) so we can detect a
        # previous run that died without reaching its reconciliation
        # point (KeyboardInterrupt, stop() that raised, etc.). We refuse
        # to start on drift rather than silently clobbering whatever
        # state was left behind -- the previous run is still leaking
        # behaviors or cowns and starting fresh would mask the bug.
        prior_count, prior_seeded = _core.terminator_reset()
        if prior_count != 0 or prior_seeded != 0:
            # We just armed the terminator (count=1, seeded=1, closed=0).
            # Close it FIRST so any sibling thread that races a
            # whencall during the abort window is refused before
            # touching the half-shut-down pool. Then drop our own
            # seed via terminator_seed_dec so the next start() sees
            # (count=0, seeded=0) instead of re-firing the same
            # drift diagnostic forever. Finally tear down workers
            # and the noticeboard so the next start() can re-spawn
            # without colliding with the orphans.
            _core.terminator_close()
            _core.terminator_seed_dec()
            self._abort_noticeboard()
            self._abort_workers()
            raise RuntimeError(
                "terminator drift carried over from a previous run "
                f"(prior_count={prior_count}, prior_seeded={prior_seeded}). "
                "This indicates a leaked whencall, a stop() that raised "
                "before reconciliation, or an interrupted teardown. "
                "Resolve the earlier failure before starting again."
            )

    def _abort_workers(self):
        """Tear down the worker pool after a partial-startup failure.

        Sends the same ``("boc_worker", "shutdown")`` / cleanup
        handshake as :py:meth:`stop_workers` but without the cown
        round-up, which is unsafe before the runtime is fully alive.
        Used only on the error path of :py:meth:`start`; on the normal
        path :py:meth:`stop_workers` performs the equivalent work.
        """
        self.logger.debug("aborting workers after failed startup")
        for _ in range(self.num_workers):
            _core.send("boc_worker", "shutdown")
        for _ in range(self.num_workers):
            try:
                _, contents = _core.receive("boc_behavior")
                assert contents == "shutdown"
            except Exception as ex:
                self.logger.exception(ex)
        for _ in range(self.num_workers):
            _core.send("boc_cleanup", True)
        self.teardown_workers()

    def _abort_noticeboard(self):
        """Tear down the noticeboard thread after a startup failure.

        Idempotent: if the thread never started or already exited (the
        common case when ``start_noticeboard`` raised), this is a
        no-op aside from clearing the C-level slot.
        """
        if self.noticeboard is not None and self.noticeboard.is_alive():
            try:
                _core.send("boc_noticeboard", "shutdown")
            except Exception as ex:
                self.logger.exception(ex)
            self.noticeboard.join()
        try:
            _core.clear_noticeboard_thread()
        except Exception as ex:
            self.logger.exception(ex)

    def __enter__(self):
        """Enter context by starting the runtime."""
        self.start()
        return self

    def stop(self, timeout: Optional[float] = None):
        """Quiesce all behaviors and tear the runtime down.

        :param timeout: Upper bound on the **quiescence** and
            **noticeboard-drain** phases (steps 1, 2, and 4 below). The
            worker shutdown handshake (step 5), orphan-behavior drain,
            and tempdir cleanup that follow run to completion regardless;
            ``timeout`` does not bound total ``stop()`` runtime. ``None``
            means wait forever for quiescence.
        :type timeout: Optional[float]

        With no central scheduler thread, ``stop()`` drives
        the C terminator directly. The sequence is:

        1. Drop the seed (idempotent) so quiescence is reachable.
        2. Block on ``terminator_wait`` until every in-flight
           behavior has decremented (worker side) and no caller is
           racing to schedule more.
        3. Close the terminator. Any later ``whencall`` raises
           ``RuntimeError("runtime is shutting down")`` from the
           ``terminator_inc()`` check rather than racing teardown.
        4. Tear down the noticeboard thread (it must have drained any
           in-flight messages from the last behaviors before the
           single-writer slot is released).
        5. Stop workers and clean up the export tempdir.

        After ``terminator_wait`` returns we assert ``terminator_count
        == 0 and terminator_seeded == 0``; any non-zero value indicates
        a bookkeeping bug (a missed decrement, or a scheduling-after-
        wait that slipped past ``terminator_close``).
        """
        # Take down the seed and wait for quiescence. Both
        # are idempotent so a second stop() / wait() is a no-op.
        # Compute one deadline up front so each stage gets the *remaining*
        # budget rather than the original timeout. Without this, a
        # caller-supplied timeout=T would let terminator_wait, the
        # noticeboard drain, and stop_workers each consume up to T,
        # turning the visible upper bound into 3*T.
        if timeout is None:
            deadline = None
        else:
            deadline = time.monotonic() + timeout

        def _remaining():
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        _core.terminator_seed_dec()
        _core.terminator_wait(_remaining())

        # Post-wait reconciliation. If wait() timed out the count is
        # still > 0 -- skip the assertion in that case so a partial
        # teardown does not mask the underlying timeout.
        c_count = _core.terminator_count()
        c_seeded = _core.terminator_seeded()
        quiesced = (c_count == 0 and c_seeded == 0)
        # Close the terminator unconditionally before any further drain
        # work. On the clean path this is the documented refusal point;
        # on the warned path it MUST happen before _drain_orphan_behaviors
        # so a late whencall caller cannot slip a fresh BehaviorCapsule
        # into boc_worker between the drain's last receive() and the
        # cleanup that follows. terminator_close() is idempotent.
        _core.terminator_close()
        if not quiesced:
            self.logger.warning(
                "stop(): terminator did not reach quiescence "
                f"(count={c_count}, seeded={c_seeded}). "
                "This typically means stop() was invoked with a timeout "
                "that elapsed while behaviors were still in flight."
            )

        # Drain the noticeboard thread.
        _core.send("boc_noticeboard", "shutdown")
        self.noticeboard.join(_remaining())
        if self.noticeboard.is_alive():
            # join() timed out. Do not proceed to stop_workers / cleanup:
            # the noticeboard thread still owns the single-writer slot
            # and may be holding NB_MUTEX while processing an in-flight
            # mutation. Tearing workers down under it would be racy.
            raise RuntimeError(
                "stop(): noticeboard thread did not shut down within "
                f"timeout={timeout!r}. The runtime is left running so "
                "the leak can be diagnosed; a later stop() call may "
                "succeed once the in-flight mutation completes."
            )
        # Shut workers down and reset noticeboard ownership.
        self.stop_workers()
        # Defensive drain: if stop() entered the "terminator did not
        # quiesce" branch above (or any late whencall slipped in
        # between terminator_close and the worker shutdown messages),
        # behaviors may still sit in boc_worker with their MCS links
        # pinned. Release them inline so we do not leak cowns on a
        # warned-only stop, and drop the terminator holds the whencall
        # callers took. With a clean stop this is a no-op.
        drain_errors = self._drain_orphan_behaviors()
        _core.clear_noticeboard_thread()
        _core.noticeboard_clear()
        # Teardown is complete: workers are joined, the noticeboard
        # thread has exited, and the C-level slot is released. The
        # tempdir cleanup that follows is bookkeeping; if it raises
        # the runtime is still gone and wait()/__exit__ should null
        # the global BEHAVIORS handle so the next @when starts fresh
        # rather than retrying stop() on a dead instance.
        self._teardown_complete = True
        if os.path.exists(self.export_dir) and self.export_tmp:
            try:
                shutil.rmtree(self.export_dir)
            except Exception as ex:
                # An orphan tempdir is annoying but not fatal: log and
                # continue so the caller observes a normal stop().
                self.logger.exception(ex)
        if drain_errors:
            # Surface the first failure so the caller sees the leak at
            # the failure site rather than later as a mysterious
            # deadlock on the affected cowns. The remaining errors
            # were logged inside _drain_orphan_behaviors.
            raise RuntimeError(
                "stop(): release_all failed for "
                f"{len(drain_errors)} orphan behavior(s) during drain; "
                "cowns may be leaked"
            ) from drain_errors[0]

    def _drain_orphan_behaviors(self):
        """Release any BehaviorCapsules left on ``boc_worker`` post-shutdown.

        Called after :py:meth:`stop_workers`. Each orphan has had its
        cowns scheduled (MCS links established) but never acquired by
        a worker. ``release_all`` walks the MCS queues, hands off to any
        waiting successors, and frees the request array; ``terminator_dec``
        drops the hold the ``whencall`` caller took before
        ``behavior_schedule``. The result Cown of each dropped behavior
        is *not* mutated here: it has already been released (owner
        ``NO_OWNER``, ``value`` is ``NULL``, ``xidata`` is set), and
        writing into ``value`` would put it in a state ``cown_acquire``
        cannot recover from on a subsequent runtime restart.

        :returns: A list of exceptions captured from
            ``release_all`` failures, or ``[]`` on a clean
            drain. ``stop()`` re-raises if non-empty so a release-side
            leak is visible at the failure site rather than later as a
            mysterious deadlock on the affected cowns.
        """
        errors = []
        while True:
            msg = _core.receive("boc_worker", timeout=0)
            if msg[0] == _core.TIMEOUT:
                return errors
            payload = msg[1]
            if isinstance(payload, _core.BehaviorCapsule):
                self.logger.warning(
                    "behavior dropped during stop(); the runtime was "
                    "torn down before this behavior could acquire its cowns"
                )
                try:
                    payload.release_all()
                except Exception as ex:
                    self.logger.exception(ex)
                    errors.append(ex)
                try:
                    _core.terminator_dec()
                except Exception as ex:
                    self.logger.exception(ex)
            # Non-capsule payloads (e.g. a stray "shutdown") are silently
            # ignored. Worker shutdowns balance 1:1 with workers, so a
            # stray sentinel here would already indicate a bug elsewhere;
            # the loop body just falls through to the next receive().

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
    # Caller threads run the entire 2PL inline. Register with the
    # C terminator first so a concurrent stop()/terminator_close() will
    # refuse the schedule rather than racing teardown. Once the
    # terminator hold is taken, behavior_schedule is infallible past
    # prepare; any failure during the prepare phase rolls the hold back.
    # The matching decrement happens on the worker thread once the
    # behavior body runs.
    if _core.terminator_inc() < 0:
        raise RuntimeError("runtime is shutting down")
    try:
        behavior.schedule()
    except BaseException:
        _core.terminator_dec()
        raise
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
    """Start the behavior runtime: worker pool plus noticeboard thread.

    The runtime distributes scheduling (2PL link/release) across caller
    and worker threads; there is no central scheduler thread.

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
        raise RuntimeError("Behavior runtime already started")

    if worker_count is None:
        worker_count = WORKER_COUNT

    if not _core.is_primary():
        raise RuntimeError("start() can only be called from the main interpreter")

    if module is None:
        module = get_caller_module()
    BEHAVIORS = Behaviors(worker_count, export_dir)
    try:
        BEHAVIORS.start(module)
    except BaseException:
        # Failed startup must not leave a half-initialised Behaviors
        # instance bound globally: the next @when would skip start()
        # entirely and run against a runtime whose noticeboard thread
        # never claimed the C-level slot (or whose workers never
        # spawned). Reset the slot so the caller can retry once the
        # underlying cause is cleared.
        BEHAVIORS = None
        raise


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

                if name in frame.f_globals:
                    val = frame.f_globals[name]
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
        # Clear BEHAVIORS only if stop() drove the runtime all the
        # way through teardown (workers joined, noticeboard exited,
        # tempdir removed). On stop()'s noticeboard-join-timeout path
        # the runtime is intentionally left running so the caller can
        # diagnose the leak and retry; nulling the global handle
        # there would strand the live workers / noticeboard thread
        # with no Python-side reference.
        try:
            BEHAVIORS.stop(timeout)
        except BaseException:
            if BEHAVIORS._teardown_complete:
                BEHAVIORS = None
            raise
        BEHAVIORS = None


def _validate_noticeboard_key(key: str) -> None:
    """Validate a noticeboard key, raising on invalid input.

    The C layer (noticeboard_write_direct) has its own checks, but we
    validate here to fail fast on the caller's interpreter.
    """
    if not isinstance(key, str):
        raise TypeError("noticeboard key must be a str")
    if "\x00" in key:
        raise ValueError("noticeboard key must not contain NUL characters")
    if len(key.encode("utf-8")) > 63:
        raise ValueError("noticeboard key too long (max 63 UTF-8 bytes)")


def _require_noticeboard_ready(key: str, operation: str) -> None:
    """Check that the runtime is running and the key is valid."""
    if _core.is_primary() and BEHAVIORS is None:
        raise RuntimeError(f"cannot {operation} the noticeboard before the runtime is started")
    _validate_noticeboard_key(key)


# Container types we recurse into when scanning a noticeboard value for
# CownCapsules to pin. Custom user objects are also descended via __dict__.
_NB_CONTAINER_TYPES = (list, tuple, set, frozenset)


def _collect_cown_capsules(obj: Any, out: list, seen: set) -> None:
    """Recursively collect every CownCapsule reachable from *obj*.

    The result is appended to *out* (a list of CownCapsule instances).
    The noticeboard uses this list to take an independent strong
    reference on every BOCCown referenced by the serialized bytes, so
    that the cowns outlive every reader's pickled view regardless of
    whether the original Cown wrapper is dropped.

    *seen* is a set of object ids used to break reference cycles.

    Recurses into Cown wrappers (extracting ``impl``), built-in
    containers (list/tuple/set/frozenset/dict), and any other object
    that exposes a ``__dict__``. Strings and bytes are not descended
    even though they are sequences.
    """
    obj_id = id(obj)
    if obj_id in seen:
        return
    if isinstance(obj, _core.CownCapsule):
        out.append(obj)
        seen.add(obj_id)
        return
    if isinstance(obj, Cown):
        out.append(obj.impl)
        seen.add(obj_id)
        return
    if isinstance(obj, (str, bytes, bytearray, int, float, bool, type(None))):
        # Common leaf types: skip cheaply without recording in `seen`.
        return
    seen.add(obj_id)
    if isinstance(obj, dict):
        for k, v in obj.items():
            _collect_cown_capsules(k, out, seen)
            _collect_cown_capsules(v, out, seen)
        return
    if isinstance(obj, _NB_CONTAINER_TYPES):
        for item in obj:
            _collect_cown_capsules(item, out, seen)
        return
    # Fall back to inspecting attributes for ordinary user classes. Built-in
    # opaque objects (e.g. compiled regex patterns) have no __dict__ and are
    # left alone.
    d = getattr(obj, "__dict__", None)
    if d is not None:
        _collect_cown_capsules(d, out, seen)
    # Walk __slots__ up the MRO: slot-only classes (e.g. @dataclass(slots=True))
    # have no __dict__ at all, so cowns stored in slot attributes would
    # otherwise be silently missed and recycled out from under the
    # noticeboard entry.
    cls = type(obj)
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__")
        if not slots:
            continue
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            # __dict__ and __weakref__ are reserved slot names that
            # expose the mapping / weakref itself; skip them.
            if name in ("__dict__", "__weakref__"):
                continue
            try:
                attr = getattr(obj, name)
            except AttributeError:
                continue
            _collect_cown_capsules(attr, out, seen)


def _gather_pins(value: Any) -> list:
    """Return the list of CownCapsules to pin for *value*."""
    pins: list = []
    _collect_cown_capsules(value, pins, set())
    return pins


def notice_write(key: str, value: Any) -> None:
    """Write a value to the noticeboard.

    The write is fire-and-forget: the value is serialized immediately and
    handed to a dedicated noticeboard thread, which applies it under
    mutex. ``notice_write`` returns as soon as the message is enqueued.

    **No ordering guarantee.** A subsequent behavior — even one that
    chains directly off the writer through a shared cown — is *not*
    guaranteed to observe this write. The noticeboard mutator runs on
    its own thread and may not have processed the message by the time
    the next behavior reads. Treat the noticeboard as eventually
    consistent shared state, never as a synchronization channel between
    behaviors. Use cowns or ``send``/``receive`` for that.

    The noticeboard supports up to 64 distinct keys.  Writes beyond the
    limit are not applied; the noticeboard thread catches the resulting
    error and logs a warning.  No exception propagates to the caller.

    :param key: The noticeboard key (max 63 UTF-8 bytes).
    :type key: str
    :param value: The value to store.
    :type value: Any
    """
    _require_noticeboard_ready(key, "write to")
    # Gather every CownCapsule reachable from `value` so the noticeboard
    # can take an independent strong reference on each. We pre-pin them
    # here on the writer thread (cown_pin_pointers does COWN_INCREF on
    # each and returns the raw pointers as ints). The pointers ride
    # along in the message; the noticeboard thread transfers ownership
    # into the noticeboard entry without an extra INCREF. This closes
    # the window where the writer behavior could return and drop its
    # pin list before the noticeboard thread dequeues the message —
    # without pre-pinning the BOCCowns get freed to the recycle pool
    # and the unpickle of the value's CownCapsules touches dangling
    # pointers.
    pin_ptrs = _core.cown_pin_pointers(_gather_pins(value))
    _core.send("boc_noticeboard", ("noticeboard_write", key, value, pin_ptrs))


def notice_update(key: str, fn: Callable[[Any], Any], default: Any = None) -> None:
    """Atomically update a noticeboard entry.

    Reads the current value for *key* (or *default* if absent), applies
    *fn* to it, and writes the result back.  The read-modify-write is
    atomic because the single-threaded noticeboard mutator performs all
    three steps without interleaving.

    Like :func:`notice_write`, the call is fire-and-forget and carries
    **no ordering guarantee** with respect to other behaviors. The
    update is processed on the noticeboard thread; subsequent behaviors
    may or may not observe the result.

    Both *fn* and *default* must be picklable — they are serialized and
    sent to the noticeboard thread via the message queue.  Lambdas and
    closures are **not** picklable; use ``functools.partial`` with a
    module-level function or an ``operator`` function instead::

        import operator
        from functools import partial
        notice_update("total", partial(operator.add, 5), default=0)
        notice_update("best", partial(max, 42), default=float("-inf"))

    If *fn* raises, the key retains its previous value and a warning is
    logged by the noticeboard thread.

    **Important:** *fn* runs synchronously on the single-threaded
    noticeboard mutator.  It must be fast, pure (no side effects), and
    must not call any bocpy API (``notice_write``, ``send``, ``when``,
    etc.). A blocking or expensive *fn* will stall every other
    noticeboard mutation.

    If *fn* returns the ``REMOVED`` sentinel, the entry is deleted from
    the noticeboard instead of being updated.

    .. warning::

       *fn* and *default* are pickled and sent to the noticeboard thread
       for execution. **Anyone with permission to call this function can
       therefore cause arbitrary Python code to run on the noticeboard
       thread**, which holds the privileged noticeboard-mutator role.
       In the current threat model bocpy treats all code running in the
       runtime (primary and sub-interpreters) as equally trusted, so
       this is no worse than any other cross-interpreter message. If you
       need to run untrusted behavior code, restrict what can reach
       ``boc_noticeboard`` and audit callers of :func:`notice_update`.

    :param key: The noticeboard key (max 63 UTF-8 bytes).
    :type key: str
    :param fn: A picklable callable taking the current value, returning the new.
    :type fn: Callable[[Any], Any]
    :param default: Value used when *key* does not yet exist.
    :type default: Any
    """
    _require_noticeboard_ready(key, "update")
    if not callable(fn):
        raise TypeError("notice_update fn must be callable")
    _core.send("boc_noticeboard", ("noticeboard_update", key, fn, default))


def notice_delete(key: str) -> None:
    """Delete a single noticeboard entry.

    The deletion is fire-and-forget: the request is sent to the
    noticeboard thread, which removes the entry under mutex.  If the
    key does not exist, the operation is a no-op. Like
    :func:`notice_write`, this carries **no ordering guarantee** with
    respect to other behaviors.

    Alternatively, use ``notice_update`` with a function that returns
    ``REMOVED`` to conditionally delete an entry based on its current
    value.

    :param key: The noticeboard key to delete (max 63 UTF-8 bytes).
    :type key: str
    """
    _require_noticeboard_ready(key, "delete from")
    _core.send("boc_noticeboard", ("noticeboard_delete", key))


def noticeboard() -> Mapping[str, Any]:
    """Return a cached snapshot of the noticeboard.

    Must be called from within a ``@when`` behavior. The first call within a
    behavior captures all entries under mutex and caches the data.
    Subsequent calls in the same behavior return a view of the same
    cached data.

    The returned mapping is read-only.

    Calling from outside a behavior (e.g. the main thread) will return a
    snapshot that is never refreshed for that thread.

    :return: A read-only mapping of keys to their stored values.
    :rtype: Mapping[str, Any]
    """
    return MappingProxyType(_core.noticeboard_snapshot())


def notice_read(key: str, default: Any = None) -> Any:
    """Read a single key from the noticeboard.

    Must be called from within a ``@when`` behavior. Convenience wrapper
    that takes a snapshot and returns one value.

    Calling from outside a behavior (e.g. the main thread) will return a
    snapshot that is never refreshed for that thread.

    :param key: The noticeboard key to read.
    :type key: str
    :param default: Value returned when key is absent.
    :type default: Any
    :return: The stored value, or *default* if the key does not exist.
    :rtype: Any
    """
    _validate_noticeboard_key(key)
    return _core.noticeboard_snapshot().get(key, default)


def noticeboard_version() -> int:
    """Return the current noticeboard version counter.

    The counter is incremented every time the noticeboard is
    successfully written, updated, or cleared. Two reads returning the
    same value mean no commit happened between them; a strictly larger
    value means at least one commit happened.

    The counter is global (across all threads and interpreters) and
    monotonic. Useful as a *hint* for detecting noticeboard changes
    without taking a full snapshot — for example, polling for any
    change before deciding whether to refresh a derived view.

    .. note::

       This is *not* a synchronization primitive. Because
       :func:`notice_write`, :func:`notice_update`, and
       :func:`notice_delete` are fire-and-forget, the version may not
       have advanced yet when a behavior that depends on a write
       observes the noticeboard. For strict read-your-writes ordering,
       use :func:`notice_sync`.

    :return: The current noticeboard version.
    :rtype: int
    """
    return _core.noticeboard_version()


def notice_sync(timeout: Optional[float] = 30.0) -> int:
    """Block until the caller's prior noticeboard mutations are committed.

    Because :func:`notice_write`, :func:`notice_update`, and
    :func:`notice_delete` are fire-and-forget, a behavior that wants
    read-your-writes ordering against a *subsequent* behavior must call
    ``notice_sync()`` after its writes. The call posts a sentinel onto
    the ``boc_noticeboard`` tag (which is FIFO per producer) and blocks
    until the noticeboard thread has drained that sentinel. By the time
    this returns, every write/update/delete posted from the calling
    thread before the sentinel has been applied to the noticeboard.

    The barrier carries **no ordering guarantee** with respect to
    writes posted from other threads or behaviors interleaved with the
    caller's; it only flushes the caller's own queued mutations.

    :param timeout: Maximum seconds to wait. ``None`` waits forever.
        Defaults to 30 seconds.
    :type timeout: float or None
    :raises TimeoutError: If the noticeboard thread does not drain the
        caller's sentinel within *timeout* seconds.
    :raises RuntimeError: If the runtime is not started.
    :return: The :func:`noticeboard_version` after the flush.
    :rtype: int
    """
    if _core.is_primary() and BEHAVIORS is None:
        raise RuntimeError("cannot notice_sync before the runtime is started")
    seq = _core.notice_sync_request()
    _core.send("boc_noticeboard", ("sync", seq))
    if not _core.notice_sync_wait(seq, timeout):
        raise TimeoutError(f"notice_sync({timeout}s) timed out waiting for seq={seq}")
    return _core.noticeboard_version()
