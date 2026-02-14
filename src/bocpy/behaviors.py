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

# Generous deadline (seconds) for every worker-lifecycle handshake
# receive (start_workers, stop_workers, _abort_workers). The handshakes
# normally complete in microseconds; the only reason to wait longer is
# pathological I/O or a wedged sub-interpreter. Promoting a wedge to a
# loud failure here means CI fails in minutes instead of hours.
_LIFECYCLE_RECEIVE_TIMEOUT = 120.0

# Self-defence cap on the alternating pump / orphan drain loop in
# `stop_workers`. A pathological producer that keeps re-feeding
# MAIN_PINNED_QUEUE between rounds would otherwise wedge teardown
# forever; on overflow we log and give up rather than spin.
_MAX_STOP_DRAIN_ROUNDS = 64

# Upper bound on any millisecond-valued pump argument
# (`deadline_ms`, `warn_ms`). The C side converts ms to ns via
# `value * 1_000_000`; without a guard, a caller passing
# `2**63` quietly wraps to a small or negative deadline. The bound
# corresponds to the largest ms that fits in an int64 once scaled by
# 1_000_000 — ~9.2e12 ms (~292 years), enough that no real program
# should hit it but small enough to reject programmer-error inputs
# like `sys.maxsize` cleanly.
_MAX_PUMP_MS = (1 << 63) // 1_000_000 - 1

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
        # Skip super().__init__: the value must not go through XIData.
        # Thread affinity lives entirely in C: PinnedCownCapsule refuses
        # non-main construction, and pump's CAS enforces single-pumper
        # on free-threaded builds. The capsule sets owner = main
        # interpreter id permanently, which makes worker cown_acquire
        # structurally fail.
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

    # Pinned behaviors look up their `__behavior__N` thunk on the
    # runtime's export_module (same shape contract as the worker
    # bootstrap's `boc_export`). A NULL export here means the runtime
    # is not initialised -- pinned schedules cannot work in that
    # state, so fail loud rather than letting every behavior fall
    # over with `AttributeError` on thunk lookup.
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
    # Validate before crossing the C boundary so callers get a clear
    # TypeError with the offending arg rather than a generic C-side
    # parse failure.
    if warn_ms is not None:
        if (not isinstance(warn_ms, int) or isinstance(warn_ms, bool)
                or warn_ms <= 0):
            # Reject 0 alongside negatives. The C side treats 0 as
            # the disable sentinel, which would silently turn the
            # watchdog off and surprise the caller; require explicit
            # ``None`` to disable.
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


# Re-read on every iteration of the wait() auto-pump loop so a
# mid-wait `set_wait_pump_poll(...)` change is honoured without
# restarting the wait.
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
        # Main-side namespace holding the transpiled ``__behavior__N``
        # thunks. :func:`pump` reads this so pinned-behavior bodies
        # scheduled via ``@when`` resolve on main the same way they
        # resolve on workers. Populated by :meth:`start`.
        self.export_module: Optional[types.ModuleType] = None
        self.logger = logging.getLogger("behaviors")
        self.logger.debug("behaviors init")
        # The runtime has no central scheduler thread. Caller threads do 2PL
        # inline (whencall -> behavior_schedule), workers release inline,
        # and the C-level terminator is the only pending counter.
        self.noticeboard = None
        self._noticeboard_start_error: Optional[BaseException] = None
        # Set to True by stop() once worker shutdown, noticeboard
        # tear-down, and the C-level noticeboard slot release have
        # all completed. The warned-stop / drain-error raise from
        # stop() happens *after* this flips, so wait()/__exit__ can
        # use the flag to distinguish "stop() raised but the runtime
        # is dead -- clear the global handle" from "stop() raised
        # mid-teardown and the runtime is still alive -- retain the
        # handle so the caller can retry stop()".
        self._teardown_complete = False
        # Populated by stop_workers() with any release_all() failures
        # observed during the per-task-queue orphan drain. stop()
        # consumes the list and clears it; on a clean stop this stays
        # empty.
        self._stop_drain_errors: list[BaseException] = []
        # Set True when stop_workers() has run to completion (whether
        # from the clean path or the noticeboard-timeout branch). A
        # subsequent stop() retry must NOT re-invoke stop_workers --
        # the worker pool is gone and `_core.scheduler_request_stop_all`
        # would block forever waiting for shutdown replies that never
        # come. The retry path skips straight to the noticeboard
        # cleanup that the prior attempt could not complete.
        self._workers_stopped = False
        # Per-worker scheduler_stats() snapshot captured at the moment
        # workers have replied "shutdown" but BEFORE
        # `_core.scheduler_runtime_stop()` frees the per-worker array.
        # Surfaced to the caller via `wait(stats=True)`. ``None`` means
        # no snapshot was captured (e.g. start_workers failed before any
        # worker registered, or stop_workers raised before reaching the
        # capture point).
        self._final_stats: Optional[list[dict]] = None
        # Plain-dict snapshot of the noticeboard captured by stop()
        # after the noticeboard thread has exited but BEFORE
        # `_core.noticeboard_clear()` frees the entries. Surfaced to
        # the caller via `wait(noticeboard=True)`. ``None`` means no
        # snapshot was captured (e.g. the noticeboard-timeout branch
        # left the thread alive, or start_noticeboard failed).
        self._final_noticeboard: Optional[dict[str, Any]] = None
        self.final_cowns: tuple[Cown, ...] = ()
        self.bid = 0
        # Set by :meth:`start` to the synthetic linecache key for the
        # main-side transpiled export, so :meth:`stop` (and the
        # abort path) can pop the entry symmetrically.
        self._main_export_file: Optional[str] = None
        # Set by :meth:`start` to the prior value of
        # ``sys.modules['__bocmain__']`` (None if no entry existed)
        # so :meth:`stop` can restore it instead of unconditionally
        # popping a slot we never owned.
        self._installed_bocmain = False
        self._prior_bocmain: Optional[types.ModuleType] = None
        # (name, path) of the module pinned by start(); used to detect
        # mismatched re-start requests.
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

        # Bound the backward search: a decorator stack of depth N
        # leaves the @when line N below the def line in 3.10, but
        # realistic stacks are tiny. 32 is plenty and still small
        # enough to catch a stale-frame mis-resolution before it
        # silently returns the wrong behavior.
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
            # Every failure path below MUST send a reply on
            # "boc_behavior": `start_workers` is blocked in a bounded
            # receive() per worker, and a missed reply turns into a
            # silent timeout with no traceback about why the worker
            # died.
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
                # `run_string` itself raised (distinct from the worker
                # script raising, which is surfaced via the returned
                # ExecutionFailed below).
                _core.send(
                    "boc_behavior",
                    "interpreters.run_string() failed: "
                    + "".join(_tb.format_exception(ex)),
                )
                result = None

            if result is not None:
                # Truthy result == ExecutionFailed; `.formatted` carries
                # the traceback captured inside the worker script.
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
                    # A worker thread failed to send "started" within
                    # the deadline. Most likely cause: the sub-
                    # interpreter wedged during `interpreters.create()`
                    # or `scheduler_worker_register`, or a C-level
                    # init deadlock blocked the worker before it could
                    # signal readiness. Without this branch the runtime
                    # would block indefinitely; raising promotes the
                    # deadlock to a loud RuntimeError so CI fails fast.
                    # NOTE: `teardown_workers` may still block on a
                    # wedged sub-interpreter's `t.join()`; the receive
                    # timeout at least guarantees we report the failure
                    # instead of silently hanging at this call site.
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
        # Single C-level fan-out: flips stop_requested on every
        # worker and signals each cv. Each worker observes the
        # flag inside scheduler_worker_pop, exits its do_work loop,
        # and sends "shutdown" back on boc_behavior.
        #
        # Once `scheduler_request_stop_all()` has been called the
        # worker pool is committed to shutting down: re-entering this
        # function on a retry would issue a second fan-out and then
        # block forever in `receive("boc_behavior")` waiting for
        # shutdown replies from workers that have already replied (or
        # exited). Wrap everything past the fan-out in try/finally
        # that pins `_workers_stopped = True` so any exception from
        # the handshake, teardown, drain, or runtime_stop still
        # routes a subsequent stop() down the retry-only branch.
        #
        # The retry-only branch in `stop()` does NOT itself call
        # `scheduler_runtime_stop`, so we must guarantee it runs here
        # even when the handshake / teardown / drain above raised --
        # otherwise the per-worker `WORKERS` array leaks until the
        # next `start()`. The C-side stop is idempotent (covered by
        # `test_scheduler_runtime_stop_is_idempotent`), so running it
        # unconditionally inside `finally` is safe.
        _core.scheduler_request_stop_all()
        try:
            for i in range(self.num_workers):
                tag, contents = _core.receive(
                    "boc_behavior", _LIFECYCLE_RECEIVE_TIMEOUT,
                )
                if tag == _core.TIMEOUT:
                    # A worker failed to reply "shutdown" after the
                    # C-level stop fan-out. Most likely cause: the
                    # worker's do_work() loop is wedged inside
                    # `scheduler_worker_pop`, or a behavior body
                    # deadlocked. Log and proceed: the outer `finally`
                    # below still runs `scheduler_runtime_stop`, which
                    # is idempotent and tears down the C-side state
                    # regardless. Without this branch stop() would
                    # block forever and the runtime could never be
                    # retried.
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
            # Alternate `main_pump_drain_all` and
            # `_drain_orphan_behaviors` until both report empty in
            # the same iteration. `release_all` inside the orphan
            # drain dispatches successors through
            # `boc_sched_dispatch`, whose pinned fast path routes
            # pinned-bearing successors onto MAIN_PINNED_QUEUE; a
            # single pump-then-orphan ordering would leave those
            # successors enqueued and their terminator_inc holds
            # undecremented, wedging the next `start()`. The cap is
            # a self-defence against a runaway producer that keeps
            # re-feeding the queues: log + give up rather than spin
            # forever. Main-interp only; skip the pump-side drain
            # on sub-interpreter shutdown paths where the pinned
            # queue is provably empty (only main can enqueue).
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
                # KeyboardInterrupt/SystemExit re-raised mid-drain must
                # not erase already-captured release_all failures.
                # extend (not assign) because _drain_orphan_behaviors
                # also pushes its in-flight errors before the re-raise.
                if accumulated_drain_errors:
                    self._stop_drain_errors.extend(
                        accumulated_drain_errors)
        finally:
            try:
                # Snapshot the per-worker scheduler counters before
                # the per-worker array is freed. Workers have already
                # replied "shutdown" and exited their do_work loops,
                # so their counters are stable. Surfaced to the
                # caller via `wait(stats=True)`. Best-effort: any
                # failure here must not block teardown.
                try:
                    self._final_stats = _core.scheduler_stats()
                except Exception as snap_ex:
                    self.logger.warning(
                        "stop_workers(): failed to snapshot scheduler_stats: %r",
                        snap_ex,
                    )
                    self._final_stats = None
                # Free the per-worker scheduler array now that no
                # worker thread can observe it. Paired with the
                # `scheduler_runtime_start` call in `start()`. Run
                # inside the outer `finally` so the WORKERS array is
                # reclaimed even when an earlier step raised --
                # without this the retry-only branch in `stop()`
                # would never reach this call site.
                _core.scheduler_runtime_stop()
            finally:
                # Mark workers as stopped so a retried stop() (after
                # the noticeboard-timeout branch raises, or after a
                # failure anywhere in the handshake/teardown/drain
                # above) does not try to shut down a worker pool that
                # is already gone.
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

        with open(path, encoding="utf-8") as file:
            worker_script = file.read()

        worker_script = worker_script.replace("logging.NOTSET", str(logging.getLogger().level))

        if module is None:
            export = export_main()
            module_name = "__main__"
        else:
            export = export_module_from_file(module[1])
            module_name = f"{module[0]}"

        # Defence in depth: the transpiler emits identifier-shaped
        # names, but `module_name` is interpolated into worker
        # bootstrap source -- reject anything that is not a valid
        # dotted Python module path at the boundary so a hostile or
        # malformed name cannot reach the `repr()`-protected
        # interpolation below. Dotted names (``pkg.sub.mod``) are
        # accepted because users may invoke bocpy from a
        # package-qualified module; each dotted component must
        # itself be a valid identifier. ``__main__`` falls through
        # naturally because ``"__main__".isidentifier()`` is True
        # and ``"__main__".split(".") == ["__main__"]``.
        if not all(part.isidentifier() for part in module_name.split(".")):
            raise ValueError(
                f"module_name must be a dotted Python module path; "
                f"got {module_name!r}"
            )

        self.behavior_lookup = export.behaviors

        # Compile the transpiled source into a fresh module on the
        # main interpreter so :func:`pump` can resolve
        # ``__behavior__N`` thunks the same way workers do. Workers
        # bootstrap their own copy inside a sub-interpreter
        # (``_bocpy_mod`` in the worker_script below); main needs an
        # equivalent namespace because pinned-behavior bodies execute
        # under ``main_pump_bounded`` on the main interpreter and
        # ``behavior_execute_impl`` looks up the thunk via
        # ``PyObject_GetAttrString(boc_export, ...)``. Without this
        # the lookup falls back to ``sys.modules["__main__"]`` (which
        # under pytest is the test runner, not the test module) and
        # every pinned ``@when`` body fails with ``AttributeError``.
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

        # Embed the transpiled source as a Python string literal
        # (via ``repr()``) into the worker bootstrap. Each worker
        # compiles and exec's the literal into a fresh
        # ``types.ModuleType``; no file is written to disk. The
        # synthetic filename ``<bocpy:NAME>`` is registered with
        # ``linecache`` so tracebacks still surface the transpiled
        # source line. Every interpolated occurrence of the module
        # name uses ``repr(module_name)`` so quote / backslash /
        # non-ASCII content cannot break out of the string literal
        # (the prior path interpolated ``module_name`` raw via
        # f-string into ``r"..."``).
        src_literal = repr(export.code)
        bocmain_alias = "__bocmain__" if module_name == "__main__" else module_name
        sysmod_key = repr(bocmain_alias)
        linecache_key = repr(f"<bocpy:{bocmain_alias}>")

        main_start = worker_script.find(WORKER_MAIN_END)

        bootstrap = [
            # The user-module load below is wrapped in try/except so an
            # import error, syntax error, or wedging top-level statement
            # surfaces as a traceback on `boc_behavior` instead of a
            # silent hang. `send` is already imported at the top of
            # worker.py and is guaranteed available here. The except
            # block re-raises so `interpreters.run_string` also reports
            # the failure via its return value.
            "import linecache",
            "import traceback as _bocpy_tb",
            "import types",
            # Module name is bound outside the try so the diagnostic can
            # name it even if the src-literal assignment fails.
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
        # Allocate the per-worker scheduler array before spawning any
        # workers so each worker's first action (registering its slot)
        # has a non-empty WORKERS array to claim from. Mirrored by
        # `_core.scheduler_runtime_stop()` in `stop_workers()` after
        # the workers are joined, and by every abort path below so
        # the C-side WORKERS array is reclaimed and the next
        # `start()` does not observe stale per-task queues.
        _core.scheduler_runtime_start(self.num_workers)
        try:
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
                # behavior into a per-task queue between our scheduler
                # stop request and the worker shutdown handshake.
                # TERMINATOR_CLOSED is 0 on the very first start() of
                # the process and 1 after any prior stop()/abort;
                # either way, set it to 1 explicitly. terminator_close()
                # is idempotent.
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
        except BaseException:
            # Defence in depth: if any abort path above failed to call
            # `_core.scheduler_runtime_stop` (or if `start_workers`
            # raised before reaching the inner try), free the C-side
            # WORKERS array here. `scheduler_runtime_stop` is
            # idempotent — calling it twice on a successful abort is
            # a no-op on the second call.
            try:
                _core.scheduler_runtime_stop()
            except Exception as ex:
                self.logger.exception(ex)
            # Drop the __bocmain__ alias if we installed one, so a
            # follow-up start() observes a clean sys.modules. Same
            # rationale as in the successful stop() path.
            self._restore_main_aliases()
            raise

    def _restore_main_aliases(self):
        # Symmetric cleanup of the main-side state ``start()`` may
        # have installed: the synthetic ``linecache`` entry that
        # backs tracebacks for the transpiled export, and the
        # ``__bocmain__`` alias used by worker bootstrap to subclass
        # user classes defined in ``__main__``. Restoring the prior
        # ``__bocmain__`` (instead of unconditionally popping it)
        # preserves an alias the host had set before the runtime
        # started.
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
                    # Same wedge as in `stop_workers`, on the abort
                    # path. Continue the abort regardless -- the
                    # caller is already error-handling a failed start.
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
            # Restart unconditionally so a failed snapshot does not strand the runtime.
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
        # Track whether seed_dec actually dropped the seed so we only re-arm it ourselves.
        seed_dropped = _core.terminator_seed_dec()
        try:
            return self._wait_for_quiescence(timeout)
        finally:
            if seed_dropped:
                # Re-arm so a future stop()/quiesce() can drop the seed again; CAS 0->1 is idempotent.
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
            # WAIT_TIMED_OUT: fall through to the deadline check at
            # the top of the next iteration.
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

        # Idempotent retry: if a prior stop() reached the
        # noticeboard-timeout branch, it already drove the
        # terminator to quiescence and shut the workers down.
        # Re-running ``stop_workers`` would block forever in
        # ``scheduler_request_stop_all`` waiting for shutdown
        # replies from a worker pool that is gone. Skip straight
        # to the noticeboard cleanup the prior attempt could not
        # complete.
        if not self._workers_stopped:
            _core.terminator_seed_dec()
            self._wait_for_quiescence(_remaining())

            # Post-wait reconciliation. If wait() timed out the count is
            # still > 0 -- skip the assertion in that case so a partial
            # teardown does not mask the underlying timeout.
            c_count = _core.terminator_count()
            c_seeded = _core.terminator_seeded()
            quiesced = (c_count == 0 and c_seeded == 0)
            # Close the terminator unconditionally before any further drain
            # work. On the clean path this is the documented refusal point;
            # on the warned path it MUST happen before stop_workers's
            # orphan drain so a late whencall caller cannot slip a fresh
            # behavior into a per-task queue between the drain pass and
            # scheduler_runtime_stop. terminator_close() is idempotent.
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
                # join() timed out. The noticeboard thread still owns the
                # single-writer slot and may be holding NB_MUTEX while
                # processing an in-flight mutation. We do not call
                # `clear_noticeboard_thread` / `noticeboard_clear` (those
                # would race with the live thread), but we MUST still drain
                # orphan behaviors so the C-side terminator_count returns
                # to 0 — otherwise a caller-supplied finite timeout that
                # fires here permanently strands every behavior currently
                # parked in a per-task queue. Worker shutdown itself does
                # not touch NB_MUTEX, so it is safe under a wedged
                # noticeboard thread.
                try:
                    self.stop_workers()
                except Exception as drain_ex:
                    # Surface drain failures via logging; the outer
                    # RuntimeError below remains the primary failure
                    # signal because the noticeboard timeout is what got
                    # us into this branch.
                    self.logger.exception(drain_ex)
                # Reset the drain errors list so a subsequent stop() does
                # not double-report; the drain has already happened.
                self._stop_drain_errors = []
                raise RuntimeError(
                    "stop(): noticeboard thread did not shut down within "
                    f"timeout={timeout!r}. Workers were shut down and "
                    "orphan behaviors drained, but the noticeboard slot "
                    "is still pinned; a later stop() call may complete "
                    "the cleanup once the in-flight mutation finishes."
                )
            # Shut workers down and reset noticeboard ownership.
            # stop_workers() now owns the orphan-drain (must happen before
            # the per-task queues are freed); it stashes any release_all
            # exceptions on `self._stop_drain_errors` for stop() to re-raise.
            self.stop_workers()
            drain_errors = self._stop_drain_errors
            self._stop_drain_errors = []
        else:
            # Retry path: workers are already gone. Re-attempt the
            # noticeboard drain that timed out previously. ``join()``
            # without a timeout waits forever -- by this point the
            # in-flight noticeboard fn must have finished or the
            # caller is no closer to making progress than they were
            # before. We surface the join via a remaining-budget
            # join so a caller-supplied timeout still bounds the
            # retry. The ``is_alive()`` check below is best-effort:
            # if the thread has already exited it skips the
            # redundant sentinel send. There is a residual TOCTOU
            # window (alive at check, exits before the send lands)
            # in which a stale sentinel can linger in the
            # ``boc_noticeboard`` queue, but correctness rests on
            # ``Behaviors.start_runtime`` calling ``set_tags(["...",
            # "boc_noticeboard"])`` on the next ``start()``, which
            # clears the queue per the public ``set_tags`` contract.
            # The guard reduces the frequency of the stale-sentinel
            # case but is not itself the correctness fence.
            if self.noticeboard.is_alive():
                _core.send("boc_noticeboard", "shutdown")
            self.noticeboard.join(_remaining())
            if self.noticeboard.is_alive():
                # Still pinned. Re-raise the same diagnostic so the
                # caller can keep retrying. ``_workers_stopped`` is
                # unchanged so a subsequent retry stays on this path.
                raise RuntimeError(
                    "stop(): noticeboard thread still pinned on retry "
                    f"(timeout={timeout!r}). The in-flight mutation "
                    "has not finished; retry once it has."
                )
            drain_errors = []
        # The block below is single-shot per Behaviors instance. A
        # second `stop()` (or `wait()`-triggered re-entry) MUST NOT
        # re-snapshot, or it would overwrite ``_final_noticeboard``
        # with ``{}`` because the first call already ran
        # ``noticeboard_clear()``. This mirrors the natural gating
        # of ``_final_stats`` inside ``stop_workers()``, which is
        # itself guarded by ``_workers_stopped``.
        if not self._teardown_complete:
            _core.clear_noticeboard_thread()
            # Snapshot before clearing. The noticeboard thread has
            # exited and workers are joined, so entries are stable.
            # `cache_clear()` is required because the main thread
            # may hold a stale `noticeboard()` proxy from earlier
            # user code. Best-effort: any failure must not block
            # teardown.
            try:
                _core.noticeboard_cache_clear()
                self._final_noticeboard = dict(_core.noticeboard_snapshot())
            except Exception as snap_ex:
                self.logger.warning(
                    "stop(): failed to snapshot noticeboard: %r", snap_ex,
                )
                self._final_noticeboard = None
            _core.noticeboard_clear()
            # Teardown is complete: workers are joined, the
            # noticeboard thread has exited, and the C-level slot is
            # released. The transpiled module is exec'd in-memory in
            # each worker, so there is no on-disk artifact to clean
            # up.
            self._teardown_complete = True
        # Drop the __bocmain__ alias we installed in start() so a
        # subsequent bocpy.start() observes a clean sys.modules
        # (and so the main module isn't pinned in sys.modules under
        # an alias after the runtime has shut down).
        self._restore_main_aliases()
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
        # KeyboardInterrupt / SystemExit raised mid-drain must not
        # abort the drain partway -- the orphaned behaviors would
        # leak their MCS chains and terminator holds, so the next
        # start() would diagnose terminator drift forever. Capture
        # them, finish the drain, and re-raise the first after the
        # loop returns clean.
        deferred_base_exc = None
        while True:
            capsules = _core.scheduler_drain_all_queues()
            if not capsules:
                if deferred_base_exc is not None:
                    if errors:
                        # Stash current-round errors so a
                        # KeyboardInterrupt unwinding past stop() does
                        # not silently erase release_all failures.
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
                # Surface the drop to anyone awaiting the result Cown.
                # Best-effort: failures here only degrade UX (the user
                # sees None instead of a diagnostic), so log and
                # continue with release_all so MCS chains still
                # unwind.
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

    # Idempotent: bare start() no-ops; mismatched explicit args raise.
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
    # Sample stats post-quiescence so the per-worker counts are stable.
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
        # Idempotent: prior stop() already stashed final snapshots;
        # return them rather than running on an empty runtime.
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

    # quiesce() first for a pre-teardown snapshot; on TimeoutError fall
    # back to stop()'s post-teardown one (historical warn-and-tear-down).
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

    # Clear BEHAVIORS only if stop() drove the runtime all the
    # way through teardown (workers joined, noticeboard exited,
    # C-level noticeboard slot released). On stop()'s
    # noticeboard-join-timeout path the runtime is intentionally
    # left running so the caller can diagnose the leak and
    # retry; nulling the global handle there would strand the
    # live workers / noticeboard thread with no Python-side
    # reference.
    try:
        BEHAVIORS.stop(_remaining())
    except BaseException:
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

    Values may embed :class:`Cown` references; the noticeboard keeps
    each embedded cown alive for as long as the entry remains in the
    noticeboard.

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


def notice_sync(timeout: Optional[float] = 30.0) -> None:
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
    """
    if _core.is_primary() and BEHAVIORS is None:
        raise RuntimeError("cannot notice_sync before the runtime is started")
    seq = _core.notice_sync_request()
    _core.send("boc_noticeboard", ("sync", seq))
    if not _core.notice_sync_wait(seq, timeout):
        raise TimeoutError(f"notice_sync({timeout}s) timed out waiting for seq={seq}")
