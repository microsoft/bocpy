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
import linecache
import logging
import os
import sys
from textwrap import dedent
import threading
import time
import types
from types import MappingProxyType
from typing import Any, Callable, Generic, Mapping, NamedTuple, Optional, TypeVar, Union

from . import _core, set_tags
from .transpiler import BehaviorInfo, export_main, export_module_from_file

try:
    import _interpreters as interpreters
except ModuleNotFoundError:
    import _xxsubinterpreters as interpreters


BEHAVIORS = None


def _default_worker_count() -> int:
    """Pick a sensible default worker count for this process.

    Resolution order:

    1. ``BOCPY_WORKERS`` environment variable (must parse as a positive
       integer; ignored otherwise).
    2. ``_core.physical_cpu_count() - 1`` -- one worker per physical
       core, leaving one for the main interpreter. Avoids HT
       oversubscription, which on CPU-bound Python workloads commonly
       *reduces* throughput because hyperthread siblings on the same
       physical core fight for the same execution units.
    3. ``len(os.sched_getaffinity(0)) - 1`` (logical cores minus the
       main interpreter) when physical detection is unavailable
       (returns 0).
    4. ``multiprocessing.cpu_count() - 1`` as a final portable fallback.

    Always returns at least 1 so a single-core / 2-logical-core
    machine still produces a usable runtime.
    """
    env = os.environ.get("BOCPY_WORKERS")
    if env is not None:
        try:
            value = int(env)
        except ValueError:
            value = 0
        if value >= 1:
            return value

    physical = _core.physical_cpu_count()
    if physical >= 1:
        return max(1, physical - 1)

    try:
        return max(1, len(os.sched_getaffinity(0)) - 1)
    except AttributeError:
        from multiprocessing import cpu_count
        return max(1, cpu_count() - 1)


WORKER_COUNT: int = _default_worker_count()

# Per-handshake receive deadline (s): a wedged sub-interpreter becomes a loud failure, not a silent hang.
_LIFECYCLE_RECEIVE_TIMEOUT = 120.0

# Cap on stop()'s drain loop so a producer re-feeding the pinned queue cannot wedge teardown forever.
_MAX_STOP_DRAIN_ROUNDS = 64

# Largest ms pump arg that survives the C side's ms*1_000_000 ns scaling without overflowing int64.
_MAX_PUMP_MS = (1 << 63) // 1_000_000 - 1

T = TypeVar("T")

# Distinguishes "key absent" from "key is None" in noticeboard updates.
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


class WaitResult(NamedTuple):
    """Bundle of optional artifacts returned by :func:`wait`.

    Only produced when both ``stats=True`` and ``noticeboard=True``
    are passed (e.g. ``wait(stats=True, noticeboard=True)``).

    :ivar stats: Per-worker scheduler-stats snapshot.
    :ivar noticeboard: Final noticeboard contents as a plain ``dict``.
    """

    stats: list[dict]
    noticeboard: dict[str, Any]


class Cown(Generic[T]):
    """Lightweight wrapper around the underlying cown capsule."""

    def __init__(self, value: T):
        """Create a cown.

        .. note::
           Calling :func:`pickle.dumps` on a cown produces bytes that
           carry one strong reference per embedded cown. If those
           bytes are never unpickled in the producing process — for
           example, if they are saved to disk or sent to an external
           store — each embedded cown leaks one strong reference per
           orphan byte string. The bocpy runtime never produces orphan
           bytes; the leak surface only applies to third-party code
           that calls ``pickle.dumps(cown)`` directly.
        """
        logging.debug("initialising Cown with value: %r", value)
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

    def unwrap(self) -> T:
        """Consume and return the stored value, or re-raise a captured behavior exception on the caller's thread.

        Mirrors Rust's ``Result::unwrap``: on success the value is
        returned; if the cown carries an unhandled behavior exception
        the exception is cleared and re-raised here. ``unwrap``
        **consumes** the cown -- it hands the stored payload to the
        caller and empties the cown to ``None`` -- so the returned value
        is owned by the caller and a second :meth:`unwrap` returns
        ``None``. Consuming is what makes move-type values (e.g.
        :class:`Matrix`) usable after the call: the cown no longer
        aliases the value's single backing store, so the value keeps its
        ownership on the caller's interpreter instead of being released
        back into the cown. The emptied cown stays schedulable, so you
        may store a fresh value into it again. Acquires the cown for the
        read, so call it from the caller's thread once the runtime is
        globally quiescent -- after :func:`quiesce` or :func:`wait`, not
        merely after this cown's own producer.

        Delegates to the C-level :meth:`CownCapsule.unwrap` so a
        behavior that returns a :class:`Cown` (which surfaces
        downstream as a bare ``CownCapsule``) can be unwrapped the same
        way, without rewrapping it in a Python :class:`Cown` first.

        :returns: The stored value when no exception is held.
        :raises BaseException: The captured exception, re-raised verbatim.
        :raises RuntimeError: If the runtime is not quiescent (behaviors
            are still in flight); call :func:`quiesce` or :func:`wait` first.
        """
        return self.impl.unwrap()

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


class PinnedCown(Cown[T]):
    """A cown whose value never leaves the main interpreter.

    Behaviors whose request set contains *any* PinnedCown run on the
    main interpreter, scheduled onto a pump queue that the runtime
    drains under :func:`wait` and that hosts may drive explicitly via
    :func:`bocpy.pump`.

    A regular :class:`Cown` stores its value as cross-interpreter
    data: every time a worker acquires the cown the value is
    unpickled into the worker's interpreter, mutated, and re-pickled
    on release. That round-trip is the reason a cown can be acquired
    by any worker -- but it also means the value must be picklable
    and that **the same Python object is never observed twice** in
    a worker.

    Many useful values cannot survive that round-trip: pyglet shapes,
    Tk widgets, open file handles, ctypes pointers into a library
    loaded by ``__main__``, an asyncio event loop, a GPU context.
    Their ``__reduce__`` either raises or silently reconstructs a
    broken object on the other side.

    A :class:`PinnedCown` holds its value as a plain
    :c:type:`PyObject` reference in the main interpreter. The value
    never goes through ``XIData``; the same Python object is
    observed on every acquire. The trade-off: every behavior whose
    request set contains a pinned cown runs **on the main thread**,
    drained by :func:`pump` (called from your event loop) or
    implicitly by :func:`wait`.

    Pattern: coarse-grained pinned dispatch
        The pinned arm is single-consumer (the main thread). If you
        schedule a pinned behavior per item, those behaviors
        serialise on the main thread and you lose worker
        parallelism. Schedule pinned behaviors coarsely -- one per
        logical frame or batch, not per item. Do per-item
        computation on workers against per-item :class:`Cown`
        slices, then dispatch **one** pinned ``@when`` per frame
        that captures all of them together with the main-thread
        canvas / handle and performs the batched write-back.

    Thread affinity
        Pinned cowns may only be constructed from the **main
        interpreter**. Constructing one from a worker raises
        :class:`RuntimeError`; the value would have no home
        interpreter to live in. :func:`pump` likewise requires the
        main interpreter -- any thread within it on classic CPython;
        on free-threaded builds (``Py_GIL_DISABLED``) a single
        thread at a time, enforced by a CAS on pump entry that
        raises :class:`RuntimeError` if a second thread tries to
        pump concurrently. The CAS is cleared on **every** exit
        path, including ``BaseException`` propagation from a
        pinned body.

    Mixed request sets
        A behavior may freely combine pinned and unpinned cowns;
        the 2PL acquisition order is unchanged. As soon as the
        request set contains any pinned cown, the body runs on the
        main thread. Unpinned cowns in the set still travel through
        XIData into the main interpreter for the body's duration.

    Exception model
        Body exceptions follow the same rules as worker behaviors:
        captured on the result :class:`Cown` and surfaced through
        ``cown.exception``. The default :func:`pump` does **not**
        re-raise; pass ``raise_on_error=True`` to opt into
        fail-fast propagation.

    Nested pumping
        Calling :func:`pump` from inside a pinned-behavior body
        raises :class:`RuntimeError` (v1).

    Handle vs. value
        A :class:`PinnedCown` *handle* (the Python wrapper object
        and its C capsule) is a normal cross-interpreter shareable.
        It travels via the same XIData mechanism as a regular
        :class:`Cown` and may be:

        - shipped as a captured variable to a worker behavior,
        - embedded in any value graph stored in a regular
          :class:`Cown` (``Cown(PinnedCown(x))`` is supported),
        - placed in a noticeboard entry via :func:`notice_write`
          or :func:`notice_update`.

        What never crosses interpreter boundaries is the *value*
        ``x``. A worker that ends up holding a pinned-cown handle
        can do exactly one useful thing with it: schedule pinned
        behaviors against it (which the runtime auto-routes to
        the main pump queue). Any attempt to acquire the value
        from a worker is rejected by the C-level owner CAS -- the
        value's owner is permanently the main interpreter.

    Restrictions
        - Constructible only on the main interpreter (see
          *Thread affinity* above).
        - The pinning interpreter is the main interpreter, by
          design. There is one pinned queue per process and one
          consumer of that queue (the main pumper); pinned cowns do
          not split across interpreters.
    """

    def __init__(self, value: T):
        """Create a pinned cown wrapping *value*.

        :param value: The initial value to wrap. Stored as a plain
            :c:type:`PyObject` reference in the main interpreter --
            no pickling, no XIData round-trip.
        :raises RuntimeError: If called from a non-main interpreter.
        """
        # Skip super().__init__: the value must stay a plain PyObject ref, never an XIData round-trip.
        self.impl = _core.PinnedCownCapsule(value)


class PumpResult(NamedTuple):
    """Result of a :func:`pump` call.

    :ivar executed: Pinned behaviors whose lifecycle ran to
        completion this call. Counts the iteration even if the body
        raised or the acquire failed (the MCS chain still drained).
    :ivar deadline_reached: ``True`` iff the loop exited because
        ``deadline_ms`` tripped before the queue drained and before
        ``max_behaviors`` capped. ``False`` on drain, on
        ``max_behaviors`` cap, or when ``deadline_ms`` is ``None``.
    :ivar raised: Pinned behaviors whose body raised an
        :class:`Exception` captured to the result cown's
        ``.exception``. Cleanup-path failures (acquire, release,
        noticeboard cache-clear) do **not** count: they are logged
        via ``PyErr_WriteUnraisable`` and the iteration is still
        counted in ``executed``. On :class:`BaseException`
        propagation, :func:`pump` raises and no
        :class:`PumpResult` is returned.
    """

    executed: int
    deadline_reached: bool
    raised: int


def _validate_pump_bound(name: str, value: Optional[int], *,
                         ms: bool = False) -> Optional[int]:
    """Validate a `pump()` bound argument.

    ``None`` is accepted as "unbounded". Otherwise the value must be
    a positive :class:`int` and must not be a :class:`bool` (the
    bool-as-int trap silently turns ``True`` into ``1`` and ``False``
    into ``0``, masking caller bugs). ``0`` is rejected: an explicit
    zero bound carries no information the caller cannot express with
    a one-line ``if budget:`` guard at the call site, and admitting
    it forces a short-circuit branch that bypasses other entry-side
    checks. ``ms=True`` additionally caps the value at
    ``_MAX_PUMP_MS`` so the C side's ``value * 1_000_000`` ns
    conversion cannot wrap past int64. The cap is keyed off the
    explicit kwarg rather than a name-string heuristic so a future
    caller that passes a non-``_ms`` name does not silently lose the
    overflow protection.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{name} must be None or a positive int, "
            f"got {type(value).__name__}"
        )
    if value <= 0:
        raise TypeError(
            f"{name} must be None or a positive int, got {value}"
        )
    if ms and value > _MAX_PUMP_MS:
        raise OverflowError(
            f"{name}={value} exceeds the maximum supported "
            f"millisecond value ({_MAX_PUMP_MS}); the C side would "
            f"overflow when scaling to nanoseconds"
        )
    return value


def pump(deadline_ms: Optional[int] = None,
         max_behaviors: Optional[int] = None,
         raise_on_error: bool = False) -> PumpResult:
    """Run pinned behaviors that are ready, then return.

    Drains the main-thread queue of behaviors whose request sets
    contain at least one :class:`PinnedCown`. Each behavior runs to
    completion before the next starts. The pump is non-preemptive:
    ``deadline_ms`` gates *starting* the next behavior, not
    interrupting one already running.

    Call :func:`pump` from your event loop's idle / on-tick hook.
    Script-mode programs need not call it explicitly -- :func:`wait`
    pumps internally when any :class:`PinnedCown` exists in the
    process.

    Bounding
        - ``deadline_ms``: wall-clock budget. ``None`` drains to
          empty; otherwise a positive :class:`int`.
        - ``max_behaviors``: hard count. ``None`` drains to empty;
          otherwise a positive :class:`int`.

        ``0`` is rejected for both bounds (use ``if budget:`` at
        the call site instead of relying on the pump to no-op).

    Exception model
        By default body exceptions land on the result cown; pump
        continues. With ``raise_on_error=True``, the first body
        exception re-raises on the pump thread after the queue
        finishes draining. :class:`BaseException`
        (``KeyboardInterrupt``, ``SystemExit``, ``GeneratorExit``)
        propagates immediately after the offending behavior's
        per-iteration cleanup completes; any behaviors still queued
        are left in place for the next :func:`pump` call.

    Thread affinity
        :func:`pump` must run on the **main interpreter**. Calling
        from a worker interpreter raises :class:`RuntimeError`
        immediately. On free-threaded builds (``Py_GIL_DISABLED``)
        only one thread may pump at a time: a concurrent call from
        a different thread raises :class:`RuntimeError`. Calling
        :func:`pump` when no :class:`PinnedCown` exists is a no-op
        returning ``PumpResult(0, False, 0)``.

    Reentrance
        Not reentrant. Calling from inside a pinned-behavior body
        raises :class:`RuntimeError` (v1).

    :param deadline_ms: Wall-clock budget in milliseconds.
        ``None`` for unbounded; otherwise a positive :class:`int`.
        Must not be :class:`bool`.
    :type deadline_ms: Optional[int]
    :param max_behaviors: Maximum behaviors to start this call.
        ``None`` for unbounded; otherwise a positive :class:`int`.
        Must not be :class:`bool`.
    :type max_behaviors: Optional[int]
    :param raise_on_error: Re-raise the first body exception after
        drain.
    :type raise_on_error: bool
    :return: :class:`PumpResult` (``executed``,
        ``deadline_reached``, ``raised``). On
        :class:`BaseException` propagation, :func:`pump` raises and
        no :class:`PumpResult` is returned.
    :rtype: PumpResult
    :raises TypeError: if ``deadline_ms`` or ``max_behaviors`` is
        not ``None``, a positive :class:`int`, or is :class:`bool`.
    :raises RuntimeError: wrong interpreter, concurrent pump on
        free-threaded, nested pump, no live runtime
        (:func:`start` has not been called), or watchdog raise
        threshold tripped.
    """
    deadline_ms = _validate_pump_bound("deadline_ms", deadline_ms, ms=True)
    max_behaviors = _validate_pump_bound("max_behaviors", max_behaviors)

    boc_export = None
    if BEHAVIORS is not None:
        boc_export = getattr(BEHAVIORS, "export_module", None)
    if boc_export is None:
        raise RuntimeError(
            "pump() requires a live bocpy runtime: call bocpy.start() "
            "(or schedule a @when, which auto-starts) before pump(). "
            "If the runtime was already stopped, restart it before "
            "draining pinned work."
        )
    return PumpResult(*_core.main_pump_bounded(
        deadline_ms, max_behaviors, raise_on_error, boc_export,
    ))


def set_pump_watchdog(warn_ms: Optional[int] = 1000,
                      on_starve: Optional[
                          Callable[[int, str], None]] = None) -> None:
    """Configure the pinned-queue starvation watchdog.

    **The watchdog is disabled until this function is called.** No
    call means no warnings, regardless of how long the pinned queue
    has been non-empty. ``warn_ms=1000`` is the kwarg default that
    applies *if and when* you opt in, not the runtime default.

    Warn-side sampling fires from :func:`pump` on entry (so
    :func:`wait`'s auto-pump loop counts). The threshold gates on
    **queue-non-empty time**: a program that runs only unpinned work
    indefinitely never trips it.

    - ``warn_ms`` (kwarg default 1000): logs a warning carrying the
      queue's non-empty duration (ms) and current depth. Pass
      ``None`` to disable. Must be a positive int when set.
    - ``on_starve``: optional callable ``(severity, message)`` to
      replace the default logger. Use this to escalate (for
      example ``on_starve=lambda s, m: pytest.fail(m)`` in tests, or
      a counter / alert hook in production).

    :param warn_ms: Warn-after threshold in milliseconds, or
        ``None`` to disable warnings.
    :type warn_ms: Optional[int]
    :param on_starve: Optional ``(severity, message)`` callback that
        replaces the default logger sink.
    :type on_starve: Optional[Callable[[int, str], None]]
    :raises TypeError: if ``warn_ms`` is not ``None`` or a positive
        :class:`int`, or ``on_starve`` is not callable.
    :raises OverflowError: if ``warn_ms`` exceeds the maximum
        representable nanosecond value.
    """
    if warn_ms is not None:
        if (not isinstance(warn_ms, int) or isinstance(warn_ms, bool)
                or warn_ms <= 0):
            raise TypeError(
                f"warn_ms must be a positive int or None to disable, "
                f"got {warn_ms!r}")
        if warn_ms > _MAX_PUMP_MS:
            raise OverflowError(
                f"warn_ms={warn_ms} exceeds the maximum supported "
                f"millisecond value ({_MAX_PUMP_MS}); the C side "
                f"would overflow when scaling to nanoseconds")
    if on_starve is not None and not callable(on_starve):
        raise TypeError(
            f"on_starve must be a callable or None, got {on_starve!r}")
    _core.set_pump_watchdog(warn_ms=warn_ms, on_starve=on_starve)


def set_wait_pump_poll(ms: int = 50) -> None:
    """Set the poll cadence for :func:`wait`'s auto-pump loop.

    Default cadence is **50 ms** — the upper bound on how long the
    auto-pump loop will park between checks when no broadcast wakes
    it. The setting is process-global and may be changed at any
    time; the active :func:`wait` loop picks up the new value on
    its next iteration.

    :param ms: Poll cadence in milliseconds. Must be positive.
    :type ms: int
    """
    if not isinstance(ms, int) or isinstance(ms, bool) or ms <= 0:
        raise TypeError(f"ms must be a positive int, got {ms!r}")
    global _WAIT_PUMP_POLL_MS
    _WAIT_PUMP_POLL_MS = ms


# Re-read each wait() auto-pump iteration so a mid-wait set_wait_pump_poll change takes effect without restarting.
_WAIT_PUMP_POLL_MS = 50


WORKER_MAIN_END = "# END boc_export"


class Behaviors:
    """Coordinator that starts workers and schedules behaviors."""

    def __init__(self, num_workers: Optional[int]):
        """Creates a new Behaviors runtime.

        :param num_workers: The number of worker interpreters to start.  If
            None, defaults to the number of available cores minus one.
        :type num_workers: Optional[int]
        """
        self.num_workers = WORKER_COUNT if num_workers is None else num_workers
        self.worker_script = None
        self.classes = set()
        self.worker_threads = []
        self.behavior_lookup: Mapping[int, BehaviorInfo] = {}
        # Main-side namespace of transpiled __behavior__N thunks; pump resolves pinned bodies against it.
        self.export_module: Optional[types.ModuleType] = None
        self.logger = logging.getLogger("behaviors")
        self.logger.debug("behaviors init")
        self.noticeboard = None
        self._noticeboard_start_error: Optional[BaseException] = None
        # True after full teardown; lets wait()/__exit__ tell a dead runtime from one that can retry stop().
        self._teardown_complete = False
        self._stop_drain_errors: list[BaseException] = []
        # True once workers are down; a stop() retry must skip re-stopping or scheduler_request_stop_all hangs.
        self._workers_stopped = False
        # Snapshots taken before the C side frees the underlying arrays; surfaced via wait(stats=/noticeboard=).
        self._final_stats: Optional[list[dict]] = None
        self._final_noticeboard: Optional[dict[str, Any]] = None
        self.final_cowns: tuple[Cown, ...] = ()
        self.bid = 0
        # Synthetic linecache key + saved sys.modules['__bocmain__'] so stop()/abort undo start()'s main-side state.
        self._main_export_file: Optional[str] = None
        self._installed_bocmain = False
        self._prior_bocmain: Optional[types.ModuleType] = None
        # (name, path) of the module pinned by start(); used to detect mismatched re-start requests.
        self._started_module: Optional[tuple[str, str]] = None

    def lookup_behavior(self, line_number: int,  max_decorator_stack=32) -> BehaviorInfo:
        """Resolve behavior info from a source line number.

        ``behavior_lookup`` is keyed by the line of the ``@when(...)``
        decorator as it appears in the AST. The runtime frame line we
        get from ``inspect.currentframe().f_back.f_lineno`` depends on
        the CPython version:

        - Python >= 3.11 attributes each decorator's application to
          that decorator's own source line, so the frame line equals
          the lookup key.
        - Python <= 3.10 attributes all decorator applications on a
          ``def`` to the ``def`` line itself, so the frame line is
          ``def_line``, which is ``len(decorators)`` greater than the
          ``@when`` decorator's line.

        Walking from ``line_number`` downward to the largest key
        ``<= line_number`` covers both cases for any decorator stack
        height. We bound the walk so a stale frame deep in unrelated
        code cannot silently mis-resolve to a distant earlier
        behavior.
        """
        if line_number in self.behavior_lookup:
            return self.behavior_lookup[line_number]

        for offset in range(1, max_decorator_stack + 1):
            if line_number - offset in self.behavior_lookup:
                return self.behavior_lookup[line_number - offset]

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
            # Every failure path below MUST reply on "boc_behavior"; start_workers() blocks in a bounded receive().
            import traceback as _tb
            interp = None
            try:
                interp = interpreters.create()
            except BaseException as ex:  # noqa: B036
                _core.send(
                    "boc_behavior",
                    "interpreters.create() failed: "
                    + "".join(_tb.format_exception(ex)),
                )
                return

            try:
                result = interpreters.run_string(
                    interp, dedent(self.worker_script),
                )
            except BaseException as ex:  # noqa: B036
                _core.send(
                    "boc_behavior",
                    "interpreters.run_string() failed: "
                    + "".join(_tb.format_exception(ex)),
                )
                result = None

            if result is not None:
                # Truthy result == ExecutionFailed; .formatted carries the traceback captured inside the worker.
                try:
                    formatted = result.formatted
                except AttributeError:
                    formatted = repr(result)
                _core.send("boc_behavior", formatted)

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
        for i in range(self.num_workers):
            match _core.receive("boc_behavior", _LIFECYCLE_RECEIVE_TIMEOUT):
                case ["boc_behavior", "started"]:
                    self.logger.debug("boc_behavior/started")

                case ["boc_behavior", error]:
                    print(error)
                    num_errors += 1

                case [_core.TIMEOUT, _]:
                    self.teardown_workers()
                    raise RuntimeError(
                        f"start_workers: worker {i} did not signal "
                        f"readiness within {_LIFECYCLE_RECEIVE_TIMEOUT}s; "
                        "the worker thread is hung or its sub-interpreter "
                        "failed to register. Check worker stderr for a "
                        "sub-interpreter init error or C-level deadlock."
                    )

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
                    self.logger.debug("acquiring %s", name)
                    val.acquire()

            for name in list(frame.f_locals):
                val = frame.f_locals[name]
                if isinstance(val, Cown) or isinstance(val, _core.CownCapsule):
                    self.logger.debug("acquiring %s", name)
                    val.acquire()

            frame = frame.f_back

        for cown in self.final_cowns:
            cown.acquire()

        self.logger.debug("stopping workers")
        _core.scheduler_request_stop_all()
        try:
            for i in range(self.num_workers):
                tag, contents = _core.receive(
                    "boc_behavior", _LIFECYCLE_RECEIVE_TIMEOUT,
                )
                if tag == _core.TIMEOUT:
                    self.logger.error(
                        "stop_workers: worker %d did not reply 'shutdown' "
                        "within %.1fs; proceeding with teardown anyway. "
                        "The wedged worker thread may outlive the runtime.",
                        i, _LIFECYCLE_RECEIVE_TIMEOUT,
                    )
                    break
                assert contents == "shutdown"

            for _ in range(self.num_workers):
                _core.send("boc_cleanup", True)

            self.teardown_workers()
            # Alternate pump-drain and orphan-drain until both report empty: release_all routes pinned successors
            # onto the pinned queue, so draining only one would strand them and wedge the next start(). The cap
            # bounds a runaway producer; only the primary interpreter owns the pinned queue.
            accumulated_drain_errors = []
            try:
                if _core.is_primary():
                    for _round in range(_MAX_STOP_DRAIN_ROUNDS):
                        try:
                            pump_drained = _core.main_pump_drain_all()
                        except Exception as drain_ex:
                            self.logger.exception(drain_ex)
                            pump_drained = 0
                        errors_this_round, orphan_drained = (
                            self._drain_orphan_behaviors()
                        )
                        accumulated_drain_errors.extend(errors_this_round)
                        if pump_drained == 0 and orphan_drained == 0:
                            break
                    else:
                        try:
                            depth = _core.main_pump_queue_depth()
                        except Exception:
                            depth = -1
                        self.logger.error(
                            "stop_workers(): drain loop did not converge "
                            "within %d rounds; main_pump_queue_depth=%d "
                            "at give-up. Pinned-cown leak likely.",
                            _MAX_STOP_DRAIN_ROUNDS, depth,
                        )
                else:
                    errors_this_round, _ = self._drain_orphan_behaviors()
                    accumulated_drain_errors.extend(errors_this_round)
            finally:
                # extend (not assign): _drain_orphan_behaviors may have pushed in-flight errors before re-raising.
                if accumulated_drain_errors:
                    self._stop_drain_errors.extend(
                        accumulated_drain_errors)
        finally:
            try:
                try:
                    self._final_stats = _core.scheduler_stats()
                except Exception as snap_ex:
                    self.logger.warning(
                        "stop_workers(): failed to snapshot scheduler_stats: %r",
                        snap_ex,
                    )
                    self._final_stats = None
                _core.scheduler_runtime_stop()
            finally:
                self._workers_stopped = True
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
            try:
                _core.set_noticeboard_thread()
            except BaseException as ex:  # noqa: B036
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
                            # Fresh snapshot for the RMW: this thread is not a behavior, so clear the cache first.
                            _core.noticeboard_cache_clear()
                            snap = _core.noticeboard_snapshot()
                            current = snap.get(key, _ABSENT)
                            if current is _ABSENT:
                                current = default
                            new_value = fn(current)
                            if new_value is REMOVED:
                                _core.noticeboard_delete(key)
                            else:
                                # write_direct transfers these INCREFs into the entry, keeping the cowns alive.
                                pin_ptrs = _core.cown_pin_pointers(
                                    _gather_pins(new_value))
                                _core.noticeboard_write_direct(
                                    key, new_value, pin_ptrs)
                        except Exception as ex:
                            self.logger.warning(f"noticeboard_update({key!r}) failed: {ex}")
                        finally:
                            # Re-arm the version check so later snapshots from this thread see committed state.
                            _core.noticeboard_cache_clear()

                    case ["boc_noticeboard", ("noticeboard_delete", key)]:
                        try:
                            _core.noticeboard_delete(key)
                        except Exception as ex:
                            self.logger.warning(f"noticeboard_delete({key!r}) failed: {ex}")

        self.noticeboard = threading.Thread(target=noticeboard)
        self.noticeboard.start()
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

        with open(path, encoding="utf-8") as file:
            worker_script = file.read()

        worker_script = worker_script.replace("logging.NOTSET", str(logging.getLogger().level))

        if module is None:
            export = export_main()
            module_name = "__main__"
        else:
            export = export_module_from_file(module[1])
            module_name = f"{module[0]}"

        # module_name is interpolated into worker source; reject non-dotted-identifier names to block injection.
        if not all(part.isidentifier() for part in module_name.split(".")):
            raise ValueError(
                f"module_name must be a dotted Python module path; "
                f"got {module_name!r}"
            )

        self.behavior_lookup = export.behaviors

        main_export_name = f"__bocpy_main_export__{module_name}"
        main_export_file = f"<bocpy:main:{module_name}>"
        main_export = types.ModuleType(main_export_name)
        main_export.__file__ = main_export_file
        linecache.cache[main_export_file] = (
            len(export.code), None,
            export.code.splitlines(keepends=True),
            main_export_file,
        )
        self._main_export_file = main_export_file
        exec(
            compile(export.code, main_export_file, "exec"),
            main_export.__dict__,
        )
        self.export_module = main_export

        # repr() embeds the transpiled source as a literal; repr(module_name) blocks quote/backslash break-out.
        src_literal = repr(export.code)
        bocmain_alias = "__bocmain__" if module_name == "__main__" else module_name
        sysmod_key = repr(bocmain_alias)
        linecache_key = repr(f"<bocpy:{bocmain_alias}>")

        main_start = worker_script.find(WORKER_MAIN_END)

        bootstrap = [
            # Wrap the user-module load so import/syntax errors surface on boc_behavior instead of a silent hang.
            "import linecache",
            "import traceback as _bocpy_tb",
            "import types",
            # Bind the module name outside the try so the diagnostic can name it even if the src literal fails.
            f"_bocpy_modname = {sysmod_key}",
            "try:",
            f"    _bocpy_src = {src_literal}",
            "    _bocpy_mod = types.ModuleType(_bocpy_modname)",
            f"    _bocpy_mod.__file__ = {linecache_key}",
            (
                "    linecache.cache["
                f"{linecache_key}"
                "] = (len(_bocpy_src), None, "
                "_bocpy_src.splitlines(keepends=True), "
                f"{linecache_key})"
            ),
            (
                "    exec(compile(_bocpy_src, "
                f"{linecache_key}, 'exec'), _bocpy_mod.__dict__)"
            ),
            "    sys.modules[_bocpy_modname] = _bocpy_mod",
            "    boc_export = _bocpy_mod",
            "except BaseException as _bocpy_boot_ex:",
            "    _bocpy_boot_msg = (",
            "        'worker bootstrap failed loading user module '",
            "        + repr(_bocpy_modname) + ': '",
            "        + ''.join(_bocpy_tb.format_exception(_bocpy_boot_ex))",
            "    )",
            "    try:",
            "        send('boc_behavior', _bocpy_boot_msg)",
            "    except BaseException:",
            "        sys.stderr.write(_bocpy_boot_msg + '\\n')",
            "        sys.stderr.flush()",
            "    raise",
        ]

        if module_name == "__main__":
            self._prior_bocmain = sys.modules.get("__bocmain__")
            self._installed_bocmain = True
            sys.modules["__bocmain__"] = sys.modules["__main__"]
            for cls in export.classes:
                bootstrap.append(f'\n\nclass {cls}(sys.modules["__bocmain__"].{cls}):')
                bootstrap.append("    pass")

        bootstrap.append("")

        self.worker_script = (
            worker_script[:main_start]
            + "\n".join(bootstrap)
            + worker_script[main_start:]
        )

        set_tags(["boc_behavior", "boc_cleanup", "boc_noticeboard"])
        # Allocate the WORKERS array before spawning workers so each can claim a slot; freed by runtime_stop.
        _core.scheduler_runtime_start(self.num_workers)
        try:
            self.start_workers()
            try:
                self.start_noticeboard()
            except BaseException:
                # Close the terminator first so a racing whencall is refused before the abort tears workers down.
                _core.terminator_close()
                self._abort_workers()
                raise

            # reset() returns the prior (count, seeded); a non-zero pair means drift from a crashed run, so refuse.
            prior_count, prior_seeded = _core.terminator_reset()
            if prior_count != 0 or prior_seeded != 0:
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
        except BaseException:
            # Defence in depth: free WORKERS in case an abort path missed it (scheduler_runtime_stop is idempotent).
            try:
                _core.scheduler_runtime_stop()
            except Exception as ex:
                self.logger.exception(ex)
            self._restore_main_aliases()
            raise

    def _restore_main_aliases(self):
        mef = self._main_export_file
        if mef is not None:
            linecache.cache.pop(mef, None)
            self._main_export_file = None
        if self._installed_bocmain:
            prior = self._prior_bocmain
            if prior is None:
                sys.modules.pop("__bocmain__", None)
            else:
                sys.modules["__bocmain__"] = prior
            self._installed_bocmain = False
            self._prior_bocmain = None

    def _abort_workers(self):
        """Tear down the worker pool after a partial-startup failure.

        Issues the same ``scheduler_request_stop_all`` + cleanup
        handshake as :py:meth:`stop_workers` but without the cown
        round-up, which is unsafe before the runtime is fully alive.
        Used only on the error path of :py:meth:`start`; on the normal
        path :py:meth:`stop_workers` performs the equivalent work.
        """
        self.logger.debug("aborting workers after failed startup")
        _core.scheduler_request_stop_all()
        for i in range(self.num_workers):
            try:
                tag, contents = _core.receive(
                    "boc_behavior", _LIFECYCLE_RECEIVE_TIMEOUT,
                )
                if tag == _core.TIMEOUT:
                    self.logger.error(
                        "_abort_workers: worker %d did not reply "
                        "'shutdown' within %.1fs; continuing abort.",
                        i, _LIFECYCLE_RECEIVE_TIMEOUT,
                    )
                    break
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

    def cycle_noticeboard(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """Capture a noticeboard snapshot by cycling the mutator thread (sentinel -> join -> snapshot -> restart).

        :param timeout: Upper bound on the join. ``None`` waits forever.
        :type timeout: Optional[float]
        :returns: The noticeboard contents as a plain ``dict``.
        :raises TimeoutError: If the noticeboard thread does not exit within ``timeout``.
        """
        if self.noticeboard is None or not self.noticeboard.is_alive():
            _core.noticeboard_cache_clear()
            return dict(_core.noticeboard_snapshot())

        _core.send("boc_noticeboard", "shutdown")
        self.noticeboard.join(timeout)
        if self.noticeboard.is_alive():
            raise TimeoutError(
                "cycle_noticeboard: noticeboard thread did not exit "
                f"within timeout={timeout!r}; the in-flight mutation has "
                "not finished. Retry once it has."
            )

        _core.clear_noticeboard_thread()
        try:
            _core.noticeboard_cache_clear()
            snap = dict(_core.noticeboard_snapshot())
        finally:
            # Restart unconditionally so a failed snapshot does not strand the runtime without a mutator thread.
            self.start_noticeboard()
        return snap

    def quiesce(self, timeout: Optional[float] = None) -> bool:
        """Wait for terminator quiescence without tearing down workers; re-arms the Pyrona seed on exit.

        :param timeout: Upper bound on the wait. ``None`` waits forever.
        :type timeout: Optional[float]
        :returns: ``True`` on quiescence, ``False`` on timeout.
        """
        if not _core.is_primary():
            raise RuntimeError(
                "Behaviors.quiesce() must be called from the primary "
                "interpreter."
            )
        # Only re-arm the seed on exit if seed_dec actually dropped it, so we never over-increment.
        seed_dropped = _core.terminator_seed_dec()
        try:
            return self._wait_for_quiescence(timeout)
        finally:
            if seed_dropped:
                _core.terminator_seed_inc()

    def __enter__(self):
        """Enter context by starting the runtime."""
        self.start()
        return self

    def _auto_pump_loop(self, timeout: Optional[float]) -> bool:
        """Pump the pinned queue while waiting for terminator quiescence.

        Used by :meth:`stop` whenever live ``PinnedCown`` handles
        exist. On each iteration: block on
        ``terminator_wait_pumpable`` for the current
        ``_WAIT_PUMP_POLL_MS`` budget (re-read every iteration so a
        mid-wait ``set_wait_pump_poll`` change is honoured);
        if it wakes with ``PUMP_READY``, drain up to 64 behaviors via
        ``main_pump_bounded`` with ``raise_on_error=False`` so body
        exceptions surface on result cowns instead of aborting the
        wait. Returns ``True`` on terminator quiescence, ``False`` on
        deadline expiry — matching ``_core.terminator_wait``'s bool
        contract.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while _core.terminator_count() > 0:
            poll_s = _WAIT_PUMP_POLL_MS / 1000.0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                poll_s = min(poll_s, remaining)
            outcome = _core.terminator_wait_pumpable(poll_s)
            if outcome == _core.TERMINATED:
                return True
            if outcome == _core.PUMP_READY:
                _core.main_pump_bounded(
                    None, 64, False, self.export_module,
                )
            # WAIT_TIMED_OUT falls through to the next iteration's deadline check.
        return True

    def _wait_for_quiescence(self, timeout: Optional[float]) -> bool:
        """Wait for terminator quiescence, auto-pumping if pinned.

        Picks between the byte-equivalent fast path
        (``_core.terminator_wait``) and the auto-pump loop based on
        the live pinned-cown count. Refuses to run from a non-primary
        interpreter when pinned cowns exist — the pinned queue is
        single-consumer by design.
        """
        if _core.pinned_cown_count() == 0:
            return _core.terminator_wait(timeout)
        if not _core.is_primary():
            raise RuntimeError(
                f"wait() with pinned cowns must run on the main "
                f"interpreter (called from a non-main interpreter; "
                f"{_core.pinned_cown_count()} pinned cown(s) live). "
                f"Call wait() from the original main thread."
            )
        return self._auto_pump_loop(timeout)

    def stop(self, timeout: Optional[float] = None):
        """Quiesce all behaviors and tear the runtime down.

        :param timeout: Upper bound on the **quiescence** and
            **noticeboard-drain** phases (steps 1, 2, and 4 below).
            The worker shutdown handshake (step 5) and orphan-behavior
            drain that follow run to completion regardless;
            ``timeout`` does not bound total ``stop()`` runtime.
            ``None`` means wait forever for quiescence. Values above
            ``1e9`` seconds (~31.7 years) are clamped to wait-forever
            to avoid platform ``time_t`` / ``DWORD`` overflow inside
            the underlying condition-variable wait.
        :type timeout: Optional[float]
        :raises RuntimeError: If the noticeboard thread does not exit
            before the timeout (or, on a retry call, is still alive).
            The first failure carries the message prefix
            ``"noticeboard thread did not shut down within timeout=..."``;
            subsequent retry failures carry
            ``"noticeboard thread still pinned on retry ..."``.
            Workers and the orphan-behavior drain have already
            completed by the time either is raised, so the runtime
            is intentionally left re-drivable: callers may retry
            ``stop()`` / ``wait()`` once the in-flight noticeboard
            mutation finishes.

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
        5. Stop workers and release the C-level noticeboard slot.

        After ``terminator_wait`` returns we assert ``terminator_count
        == 0 and terminator_seeded == 0``; any non-zero value indicates
        a bookkeeping bug (a missed decrement, or a scheduling-after-
        wait that slipped past ``terminator_close``).

        The retry path is internally gated on ``_workers_stopped`` so
        the worker pool is not torn down twice; a second ``stop()``
        after a noticeboard-timeout abort retries only the
        noticeboard drain.
        """
        # One deadline up front so each stage gets the remaining budget, not a fresh timeout (else the bound is 3*T).
        if timeout is None:
            deadline = None
        else:
            deadline = time.monotonic() + timeout

        def _remaining():
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        if not self._workers_stopped:
            _core.terminator_seed_dec()
            self._wait_for_quiescence(_remaining())

            c_count = _core.terminator_count()
            c_seeded = _core.terminator_seeded()
            quiesced = (c_count == 0 and c_seeded == 0)
            _core.terminator_close()
            if not quiesced:
                self.logger.warning(
                    "stop(): terminator did not reach quiescence "
                    f"(count={c_count}, seeded={c_seeded}). "
                    "This typically means stop() was invoked with a timeout "
                    "that elapsed while behaviors were still in flight."
                )

            _core.send("boc_noticeboard", "shutdown")
            self.noticeboard.join(_remaining())
            if self.noticeboard.is_alive():
                try:
                    self.stop_workers()
                except Exception as drain_ex:
                    self.logger.exception(drain_ex)
                # Reset so a later stop() does not double-report; the drain already ran on this branch.
                self._stop_drain_errors = []
                raise RuntimeError(
                    "stop(): noticeboard thread did not shut down within "
                    f"timeout={timeout!r}. Workers were shut down and "
                    "orphan behaviors drained, but the noticeboard slot "
                    "is still pinned; a later stop() call may complete "
                    "the cleanup once the in-flight mutation finishes."
                )
            self.stop_workers()
            drain_errors = self._stop_drain_errors
            self._stop_drain_errors = []
        else:
            if self.noticeboard.is_alive():
                _core.send("boc_noticeboard", "shutdown")
            self.noticeboard.join(_remaining())
            if self.noticeboard.is_alive():
                raise RuntimeError(
                    "stop(): noticeboard thread still pinned on retry "
                    f"(timeout={timeout!r}). The in-flight mutation "
                    "has not finished; retry once it has."
                )
            drain_errors = []
        if not self._teardown_complete:
            _core.clear_noticeboard_thread()
            # Snapshot before clearing while entries are stable; cache_clear() drops a stale main-thread proxy.
            try:
                _core.noticeboard_cache_clear()
                self._final_noticeboard = dict(_core.noticeboard_snapshot())
            except Exception as snap_ex:
                self.logger.warning(
                    "stop(): failed to snapshot noticeboard: %r", snap_ex,
                )
                self._final_noticeboard = None
            _core.noticeboard_clear()
            self._teardown_complete = True
        self._restore_main_aliases()
        if drain_errors:
            raise RuntimeError(
                "stop(): release_all failed for "
                f"{len(drain_errors)} orphan behavior(s) during drain; "
                "cowns may be leaked"
            ) from drain_errors[0]

    def _drain_orphan_behaviors(self):
        """Release any BehaviorCapsules left in per-worker queues post-shutdown.

        Called from :py:meth:`stop_workers` after the worker threads
        have joined but BEFORE :py:func:`_core.scheduler_runtime_stop`
        frees the per-worker queues. Each orphan has had its cowns
        scheduled (MCS links established) but never acquired by a
        worker. ``release_all`` walks the MCS queues, hands off to any
        waiting successors, and frees the request array;
        ``terminator_dec`` drops the hold the ``whencall`` caller took
        before ``behavior_schedule``.

        Before ``release_all`` runs, ``set_drop_exception`` marks the
        result Cown with a :class:`RuntimeError` so a caller awaiting
        ``cown.value`` / ``cown.exception`` after :py:meth:`stop` sees
        a diagnostic instead of a permanent ``None``. Mirrors the
        worker exception path (:py:func:`worker.run_behavior`):
        ``acquire`` → ``set_exception`` → ``release``, condensed into
        one C call (`_core.c::BehaviorCapsule_set_drop_exception`).

        ``release_all`` may dispatch a successor into the per-task
        queues (the off-worker arm of ``boc_sched_dispatch`` runs
        because the calling thread is the main thread, not a worker).
        That successor will not be consumed -- workers are gone --
        so the loop drains again until
        ``scheduler_drain_all_queues`` returns an empty list.

        :returns: A ``(errors, drained_count)`` tuple. ``errors`` is
            a list of exceptions captured from ``release_all``
            failures, or ``[]`` on a clean drain. ``drained_count``
            is the total number of capsules processed by this call;
            ``stop_workers`` uses it to detect when the alternating
            pump / orphan drain loop has converged. ``stop()``
            re-raises if ``errors`` is non-empty so a release-side
            leak is visible at the failure site rather than later as
            a mysterious deadlock on the affected cowns.
        """
        errors = []
        drained_count = 0
        # A KeyboardInterrupt/SystemExit mid-drain must not abort partway: orphaned behaviors would leak their MCS
        # chains and terminator holds, so the next start() would diagnose drift forever. Defer it, finish the drain,
        # then re-raise the first after the loop returns clean.
        deferred_base_exc = None
        while True:
            capsules = _core.scheduler_drain_all_queues()
            if not capsules:
                if deferred_base_exc is not None:
                    if errors:
                        # Stash errors so a KeyboardInterrupt unwinding past stop() does not erase release_all failures.
                        self._stop_drain_errors.extend(errors)
                        note = (
                            f"_drain_orphan_behaviors deferred "
                            f"{len(errors)} release_all error(s); "
                            "see Behaviors._stop_drain_errors"
                        )
                        # add_note is PEP 678 (3.11+); fall back to writing __notes__ directly on 3.10.
                        add_note = getattr(
                            deferred_base_exc, "add_note", None)
                        if add_note is not None:
                            add_note(note)
                        else:
                            existing = getattr(
                                deferred_base_exc, "__notes__", None)
                            if existing is None:
                                deferred_base_exc.__notes__ = [note]
                            else:
                                existing.append(note)
                    raise deferred_base_exc
                return errors, drained_count
            for payload in capsules:
                drained_count += 1
                self.logger.warning(
                    "behavior dropped during stop(); the runtime was "
                    "torn down before this behavior could acquire its cowns"
                )
                try:
                    payload.set_drop_exception(RuntimeError(
                        "behavior dropped during stop(); the runtime "
                        "was torn down before this behavior could "
                        "acquire its cowns"
                    ))
                except Exception as ex:
                    self.logger.exception(ex)
                except (KeyboardInterrupt, SystemExit) as ex:
                    self.logger.exception(ex)
                    if deferred_base_exc is None:
                        deferred_base_exc = ex
                try:
                    payload.release_all()
                except Exception as ex:
                    self.logger.exception(ex)
                    errors.append(ex)
                except (KeyboardInterrupt, SystemExit) as ex:
                    self.logger.exception(ex)
                    errors.append(ex)
                    if deferred_base_exc is None:
                        deferred_base_exc = ex
                try:
                    _core.terminator_dec()
                except Exception as ex:
                    self.logger.exception(ex)
                except (KeyboardInterrupt, SystemExit) as ex:
                    self.logger.exception(ex)
                    if deferred_base_exc is None:
                        deferred_base_exc = ex

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
    logging.debug(
        "whencall:behavior=Behavior(thunk=%s, result=%r, args=%r, captures=%r)",
        thunk, result, args, captures,
    )
    # Take the terminator hold before scheduling so a concurrent stop()/terminator_close() refuses the schedule
    # rather than racing teardown; the matching dec runs on the worker thread once the body completes.
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
          module: Optional[tuple[str, str]] = None):
    """Start the behavior runtime: worker pool plus noticeboard thread.

    Idempotent: bare ``start()`` on a running runtime is a silent no-op; mismatched ``worker_count``/``module`` raise.

    The runtime distributes scheduling (2PL link/release) across
    caller and worker threads; there is no central scheduler thread.

    The runtime distributes scheduling (2PL link/release) across
    caller and worker threads; there is no central scheduler thread.

    :param worker_count: The number of worker interpreters to start.  If
        None, defaults to the number of available cores minus one.
    :type worker_count: Optional[int]
    :param module: A tuple of the target module name and file path to export
        for worker import.  If None, the caller's module will be used.
    :type module: Optional[tuple[str, str]]
    :raises RuntimeError: If called from a non-primary interpreter,
        or if the runtime is already up under a different
        ``worker_count`` / ``module`` than the one supplied.
    """
    global BEHAVIORS

    if not _core.is_primary():
        raise RuntimeError("start() can only be called from the main interpreter")

    if BEHAVIORS is not None:
        if worker_count is not None and worker_count != BEHAVIORS.num_workers:
            raise RuntimeError(
                f"bocpy.start(worker_count={worker_count}) was called "
                f"but the runtime is already up with worker_count="
                f"{BEHAVIORS.num_workers}. Call wait() (or stop()) to "
                f"tear the existing runtime down before starting a new "
                f"one with a different worker_count."
            )
        if module is not None and module != BEHAVIORS._started_module:
            raise RuntimeError(
                f"bocpy.start(module={module!r}) was called but the "
                f"runtime is already up with module="
                f"{BEHAVIORS._started_module!r}. Call wait() (or "
                f"stop()) to tear the existing runtime down before "
                f"starting a new one with a different module."
            )
        return

    if worker_count is None:
        worker_count = WORKER_COUNT

    if module is None:
        module = get_caller_module()
    BEHAVIORS = Behaviors(worker_count)
    BEHAVIORS._started_module = module
    try:
        BEHAVIORS.start(module)
    except BaseException:
        # Clear the global on failure so the next @when re-runs start() instead of using a half-initialised runtime.
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

    The transpiler recognises module-level aliases for the decorator,
    so ``from bocpy import when as boc_when`` and ``@boc_when(...)``,
    as well as ``import bocpy [as alias]`` followed by
    ``@bocpy.when(...)`` / ``@alias.when(...)``, are all supported.
    Aliases declared inside a function body are not tracked.
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

        logging.debug("when:behavior=%s", binfo)
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


def quiesce(timeout: Optional[float] = None, *,
            stats: bool = False, noticeboard: bool = False):
    """Block until in-flight behaviors complete without tearing down the runtime.

    :param timeout: Upper bound (seconds). ``None`` waits forever.
    :type timeout: Optional[float]
    :param stats: If True, capture per-worker scheduler stats.
    :type stats: bool
    :param noticeboard: If True, capture a noticeboard snapshot via a thread cycle.
    :type noticeboard: bool
    :raises TimeoutError: If quiescence is not reached within ``timeout``.
    :raises RuntimeError: If called from a non-primary interpreter while pinned cowns are live.
    """
    def _format(stats_snap, nb_snap):
        if stats and noticeboard:
            return WaitResult(stats=stats_snap, noticeboard=nb_snap)
        if stats:
            return stats_snap
        if noticeboard:
            return nb_snap
        return None

    if BEHAVIORS is None:
        return _format([], {})

    if timeout is None:
        deadline = None
    else:
        deadline = time.monotonic() + timeout

    def _remaining() -> Optional[float]:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    if not BEHAVIORS.quiesce(_remaining()):
        raise TimeoutError(
            f"quiesce(): runtime did not reach quiescence within "
            f"timeout={timeout!r}"
        )
    # Sample stats after quiescence so the per-worker counts are stable.
    stats_snap = list(_core.scheduler_stats()) if stats else None
    nb_snap = BEHAVIORS.cycle_noticeboard(_remaining()) if noticeboard else None
    return _format(stats_snap, nb_snap)


def wait(timeout: Optional[float] = None, *,
         stats: bool = False, noticeboard: bool = False):
    """Block until all behaviors complete, with optional timeout.

    When ``stats=True``, captures the per-worker
    ``_core.scheduler_stats`` snapshot. When
    ``noticeboard=True``, captures the noticeboard contents as a
    plain ``dict`` at the quiescence point (NOT after teardown — the
    two are equivalent in single-caller programs but the quiescence
    snapshot is the documented one). See the stub in
    ``__init__.pyi`` for the full contract.

    Return value:

    - neither flag: ``None``.
    - ``stats=True`` only: ``list[dict]`` (or ``[]``).
    - ``noticeboard=True`` only: ``dict[str, Any]`` (or ``{}``).
    - both flags: :class:`WaitResult`.

    Internally a thin wrapper around :func:`quiesce` +
    ``Behaviors.stop``; quiescence timeout warns rather than
    raising.
    """
    global BEHAVIORS

    def _format(stats_snap, nb_snap):
        if stats and noticeboard:
            return WaitResult(stats=stats_snap, noticeboard=nb_snap)
        if stats:
            return stats_snap
        if noticeboard:
            return nb_snap
        return None

    if BEHAVIORS is None:
        return _format([], {})

    if BEHAVIORS._teardown_complete:
        stats_snap = BEHAVIORS._final_stats if BEHAVIORS._final_stats is not None else []
        nb_snap = BEHAVIORS._final_noticeboard if BEHAVIORS._final_noticeboard is not None else {}
        BEHAVIORS = None
        return _format(stats_snap, nb_snap)

    if timeout is None:
        deadline = None
    else:
        deadline = time.monotonic() + timeout

    def _remaining() -> Optional[float]:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    quiesce_snapshots = None
    quiesce_timed_out = False
    try:
        quiesce_snapshots = quiesce(
            _remaining(), stats=stats, noticeboard=noticeboard,
        )
    except TimeoutError as ex:
        quiesce_timed_out = True
        BEHAVIORS.logger.warning(
            "wait(): quiesce() timed out (%s); proceeding to stop().", ex,
        )

    try:
        BEHAVIORS.stop(_remaining())
    except BaseException:
        # Only clear the global once stop() completed teardown; on its noticeboard-join-timeout path the runtime is
        # left running for a retry, and nulling the handle there would strand the live workers / noticeboard thread.
        if BEHAVIORS._teardown_complete:
            if quiesce_snapshots is not None:
                BEHAVIORS = None
                if stats or noticeboard:
                    return quiesce_snapshots
                return None
            stats_snap = BEHAVIORS._final_stats if BEHAVIORS._final_stats is not None else []
            nb_snap = BEHAVIORS._final_noticeboard if BEHAVIORS._final_noticeboard is not None else {}
            BEHAVIORS = None
            if stats or noticeboard:
                return _format(stats_snap, nb_snap)
        raise

    if quiesce_snapshots is not None and not quiesce_timed_out:
        BEHAVIORS = None
        return quiesce_snapshots

    # Quiesce timed out: return stop()'s post-teardown snapshot instead of an empty result.
    stats_snap = BEHAVIORS._final_stats if BEHAVIORS._final_stats is not None else []
    nb_snap = BEHAVIORS._final_noticeboard if BEHAVIORS._final_noticeboard is not None else {}
    BEHAVIORS = None
    return _format(stats_snap, nb_snap)


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
    d = getattr(obj, "__dict__", None)
    if d is not None:
        _collect_cown_capsules(d, out, seen)
    cls = type(obj)
    # Walk __slots__ up the MRO too: slot-only classes (e.g. @dataclass(slots=True)) have no __dict__, so cowns in
    # slot attributes would otherwise be silently missed and recycled out from under the noticeboard entry.
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__")
        if not slots:
            continue
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            # __dict__ / __weakref__ are reserved slot names exposing the mapping itself, not stored values.
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

    Values may embed :class:`Cown` references; the noticeboard keeps
    each embedded cown alive for as long as the entry remains in the
    noticeboard.

    :param key: The noticeboard key (max 63 UTF-8 bytes).
    :type key: str
    :param value: The value to store.
    :type value: Any
    """
    _require_noticeboard_ready(key, "write to")
    # Pre-pin every reachable cown on the writer thread (cown_pin_pointers INCREFs and returns raw pointers): the
    # noticeboard thread transfers ownership without a second INCREF, closing the window where the writer could drop
    # its pins before the message is dequeued and the value's cowns get recycled to dangling pointers.
    pin_ptrs = _core.cown_pin_pointers(_gather_pins(value))
    _core.send("boc_noticeboard", ("noticeboard_write", key, value, pin_ptrs))


def notice_seed(key: str, value: Any) -> None:
    """Synchronously write a value to the noticeboard from the primary interpreter.

    Unlike :func:`notice_write`, this commits **before it returns**:
    the value is serialized and applied under the noticeboard mutex on
    the calling thread, so once :func:`notice_seed` returns the entry
    is live and visible to every behavior scheduled afterwards (and to
    the calling thread's own subsequent :func:`notice_read`). It is the
    recommended way to install read-mostly configuration on the
    noticeboard *before* scheduling the behaviors that read it.

    If the runtime is not yet running, :func:`notice_seed` starts it
    (just like the first ``@when``), so seeding can be the first bocpy
    call a program makes — no explicit :func:`start` is required.

    **Primary interpreter only.** :func:`notice_seed` may be called only
    from the primary interpreter (never from inside a ``@when`` body,
    which runs on a worker). Calling it from a worker raises
    :class:`RuntimeError`. Use :func:`notice_write` for fire-and-forget
    writes from within behaviors.

    :func:`notice_seed` is a plain overwrite and is intended for
    *seeding* — installing values before the behaviors and noticeboard
    mutations that read them are in flight. It is **not** a concurrent
    update primitive: it does not provide the read-modify-write
    atomicity of :func:`notice_update`, and a seed that races an
    in-flight :func:`notice_update` on the same key may be lost (the
    update's read-modify-write can overwrite it). Seed once, up front,
    rather than interleaving seeds with concurrent updates.

    The noticeboard supports up to 64 distinct keys. Values may embed
    :class:`Cown` references; the noticeboard keeps each embedded cown
    alive for as long as the entry remains.

    :param key: The noticeboard key (max 63 UTF-8 bytes).
    :type key: str
    :param value: The value to store.
    :type value: Any
    :raises RuntimeError: If called from a worker interpreter.
    """
    if not _core.is_primary():
        raise RuntimeError("notice_seed may only be called from the primary interpreter")
    _validate_noticeboard_key(key)
    if BEHAVIORS is None:
        start(module=get_caller_module())
    # Pre-pin every reachable cown (cown_pin_pointers INCREFs and returns raw pointers); the C entry adopts those refs
    # under the noticeboard mutex, so the strong refs are taken while the originals are still live.
    pin_ptrs = _core.cown_pin_pointers(_gather_pins(value))
    _core.noticeboard_seed(key, value, pin_ptrs)


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

    The value returned by *fn* may embed :class:`Cown` references; the
    noticeboard retains them until the entry is overwritten or deleted,
    identical to :func:`notice_write`.

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

    The noticeboard is a behavior-scope read surface. The supported
    use is from inside a ``@when`` body: the first call captures all
    entries under mutex and caches them, and every subsequent call
    in the same behavior returns the same cached view.

    The returned mapping is read-only.

    The only supported way to read the noticeboard from the main
    thread is to ask :func:`wait` for it via ``wait(noticeboard=True)``
    (or ``wait(stats=True, noticeboard=True)``); that snapshot is taken
    on the main thread between joining the noticeboard mutator thread
    and clearing the C-side entries.

    Calling :func:`noticeboard` or :func:`notice_read` from any other
    main-thread context (outside a behavior, outside
    ``wait(noticeboard=True)``) is **undefined behavior**: the cached
    proxy is never re-anchored on a behavior boundary, so subsequent
    calls may observe either a stale snapshot or partially-applied
    writes.

    Seeding the noticeboard with :func:`notice_write` from the main
    thread *before* scheduling behaviors is fine and is the
    recommended pattern for installing read-mostly configuration.

    :return: A read-only mapping of keys to their stored values.
    :rtype: Mapping[str, Any]
    """
    return MappingProxyType(_core.noticeboard_snapshot())


def notice_read(key: str, default: Any = None) -> Any:
    """Read a single key from the noticeboard.

    Convenience wrapper over :func:`noticeboard` that takes a snapshot
    and returns one value. The same supported-usage contract applies:
    call from inside a ``@when`` behavior, or read the final state on
    main via ``wait(noticeboard=True)``. Calling :func:`notice_read`
    from any other main-thread context is **undefined behavior**.

    :param key: The noticeboard key to read.
    :type key: str
    :param default: Value returned when key is absent.
    :type default: Any
    :return: The stored value, or *default* if the key does not exist.
    :rtype: Any
    """
    _validate_noticeboard_key(key)
    return _core.noticeboard_snapshot().get(key, default)
