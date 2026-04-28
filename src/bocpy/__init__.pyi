from typing import Any, Callable, Generic, Iterator, Mapping, Optional, Sequence, TypeVar, Union


TIMEOUT: str
"""Sentinel value returned by :func:`receive` when a timeout occurs."""

REMOVED: object
"""Sentinel returned by a ``notice_update`` fn to delete the entry."""


def drain(tags: Union[str, Sequence[str]]) -> None:
    """Drain all messages associated with one or more tags.

    Note that if new messages with this tag are being constantly created, this method
    may not return.

    :param tag: The tags to drain. All messages associated with this tag will
                be cleared.
    """


def send(tag: str, contents: Any):
    """Sends a message.

    :param tag: The tag is an arbitrary label that can be used to receive this message.
    :type tag: str
    :param contents: The contents of the message.
    :type contents: Any
    """


def receive(tags: Union[Sequence[str], str],
            timeout: float = -1,
            after: Optional[Callable[[], Any]] = None) -> Optional[Any]:
    """Receives a message.

    :param tags: One or more tags. The received message will be tagged with one of these.
    :type tags: Union[list[str], tuple[str, ...], str]
    :param timeout: A non-negative value indicates how many seconds receive should wait before returning.
                    A negative value indicates to wait until a message is received.
    :type timeout: float
    :param after: Optional callback which should be called to produce a value if receive times out.
    :type after: Optional[Callable[[], Any]]
    :return: The contents of the received message
    :rtype: Any | None
    """


def set_tags(tags: Sequence[str]):
    """Set the tags for the message queues.

    This function (which can only be called from the main interpreter) will
    temporarily disable the messaging queues and assign new tags.  All messages
    in the queues will be cleared before the queues are re-enabled.  Passing an
    empty list resets all queues to auto-assign mode.

    :param tags: The tags to assign to the queues, or an empty list to
        reset all queues.
    :type tags: Sequence[str]
    """


class Matrix:
    """A dense 2-D matrix of double-precision floats backed by a C implementation.

    Supports element-wise arithmetic (``+``, ``-``, ``*``, ``/``), matrix
    multiplication (``@``), in-place variants (``+=``, ``-=``, ``*=``, ``/=``),
    unary ``-`` and ``abs()``, and subscript indexing with integers or slices.
    """

    def __init__(self, rows: int, columns: int,
                 values: Optional[Union[int, float, Sequence[Union[int, float]]]] = None):
        """Create a new *rows* x *columns* matrix.

        :param rows: Number of rows (must be ≥ 1).
        :param columns: Number of columns (must be ≥ 1).
        :param values: Initial values.  May be ``None`` (zero-filled), a scalar
            (broadcast to every element), or a flat sequence of *rows* x *columns*
            numbers in row-major order.
        """

    @property
    def rows(self) -> int:
        """The number of rows in the matrix."""

    @property
    def columns(self) -> int:
        """The number of columns in the matrix."""

    @property
    def T(self) -> "Matrix":
        """Return a new matrix that is the transpose of this one."""

    @property
    def x(self) -> float:
        """The first vector component (element at flat index 0)."""

    @x.setter
    def x(self, value):
        """Set the first vector component."""

    @property
    def y(self) -> float:
        """The second vector component (element at flat index 1)."""

    @y.setter
    def y(self, value):
        """Set the second vector component."""

    @property
    def z(self) -> float:
        """The third vector component (element at flat index 2)."""

    @z.setter
    def z(self, value):
        """Set the third vector component."""

    @property
    def w(self) -> float:
        """The fourth vector component (element at flat index 3)."""

    @w.setter
    def w(self, value):
        """Set the fourth vector component."""

    @property
    def acquired(self):
        """Whether the matrix is currently acquired."""

    @property
    def shape(self) -> tuple[int, int]:
        """The ``(rows, columns)`` shape of the matrix."""

    def transpose(self) -> "Matrix":
        """Return a new matrix that is the transpose of this one."""

    def transpose_in_place(self):
        """Transpose this matrix in place, swapping its rows and columns."""

    def sum(self, axis: Optional[int] = None) -> Union[float, "Matrix"]:
        """Sum of matrix elements.

        :param axis: If ``None``, return the total sum as a float.
            If ``0``, return a 1 x *columns* row vector of column sums.
            If ``1``, return a *rows* x 1 column vector of row sums.
        """

    def mean(self, axis: Optional[int] = None) -> Union[float, "Matrix"]:
        """Arithmetic mean of matrix elements.

        :param axis: If ``None``, return the overall mean as a float.
            If ``0``, return a 1 x *columns* row vector of column means.
            If ``1``, return a *rows* x 1 column vector of row means.
        """

    def magnitude(self, axis: Optional[int] = None) -> Union[float, "Matrix"]:
        """Euclidean magnitude (L2 norm) of matrix elements.

        :param axis: If ``None``, return the total magnitude as a float.
            If ``0``, return a 1 x *columns* row vector of column magnitudes.
            If ``1``, return a *rows* x 1 column vector of row magnitudes.
        """

    def min(self, axis: Optional[int] = None) -> Union[float, "Matrix"]:
        """Minimum of matrix elements.

        :param axis: If ``None``, return the overall minimum as a float.
            If ``0``, return a 1 x *columns* row vector of column minima.
            If ``1``, return a *rows* x 1 column vector of row minima.
        """

    def max(self, axis: Optional[int] = None) -> Union[float, "Matrix"]:
        """Maximum of matrix elements.

        :param axis: If ``None``, return the overall maximum as a float.
            If ``0``, return a 1 x *columns* row vector of column maxima.
            If ``1``, return a *rows* x 1 column vector of row maxima.
        """

    def ceil(self) -> "Matrix":
        """Return a new matrix with each element rounded up to the nearest integer."""

    def floor(self) -> "Matrix":
        """Return a new matrix with each element rounded down to the nearest integer."""

    def round(self) -> "Matrix":
        """Return a new matrix with each element rounded to the nearest integer."""

    def negate(self) -> "Matrix":
        """Return a new matrix with every element negated."""

    def abs(self) -> "Matrix":
        """Return a new matrix with the absolute value of every element."""

    def clip(self, min_or_maxval: float, maxval: Optional[float] = None) -> "Matrix":
        """Clip every element to a given range.

        :param min_or_maxval: If *maxval* is provided, this is the minimum
            clipping value.  Otherwise, this is the maximum and the minimum
            defaults to zero.
        :param maxval: The maximum clipping value, or ``None`` to treat
            *min_or_maxval* as the maximum.
        :return: A new clipped :class:`Matrix`.
        """

    def copy(self) -> "Matrix":
        """Return a deep copy of this matrix."""

    def select(self, indices: Union[list[int], tuple[int]], axis=0):
        """Return a new matrix containing only the selected rows or columns.

        :param indices: The row or column indices to select.
        :param axis: ``0`` to select rows, ``1`` to select columns.
        """

    def __add__(self, other: Union["Matrix", int, float]) -> "Matrix":
        """Element-wise addition."""

    def __radd__(self, other: Union["Matrix", int, float]) -> "Matrix":
        """Reflected element-wise addition."""

    def __sub__(self, other: Union["Matrix", int, float]) -> "Matrix":
        """Element-wise subtraction."""

    def __rsub__(self, other: Union["Matrix", int, float]) -> "Matrix":
        """Reflected element-wise subtraction."""

    def __mul__(self, other: Union["Matrix", int, float]) -> "Matrix":
        """Element-wise multiplication."""

    def __rmul__(self, other: Union["Matrix", int, float]) -> "Matrix":
        """Reflected element-wise multiplication."""

    def __truediv__(self, other: Union["Matrix", int, float]):
        """Element-wise division."""

    def __rtruediv__(self, other: Union["Matrix", int, float]):
        """Reflected element-wise division."""

    def __abs__(self) -> "Matrix":
        """Element-wise absolute value (``abs(m)``)."""

    def __neg__(self) -> "Matrix":
        """Element-wise negation (``-m``)."""

    def __len__(self) -> int:
        """Return the number of rows."""

    def __getitem__(self, key: Union[int, tuple[int, int]]) -> Union["Matrix", float]:
        """Retrieve a row, element, or sub-matrix by index or slice."""

    def __setitem__(self, key: Union[int, tuple[int, int]], value: Union[int,
                    float, "Matrix", Sequence[Union[int, float]]]):
        """Set a row, element, or sub-matrix by index or slice."""

    def __iter__(self) -> Iterator[Union[float, "Matrix"]]:
        """Iterate over rows of the matrix."""

    @classmethod
    def allclose(cls, lhs: "Matrix", rhs: "Matrix", rtol: float = 1e-05, atol: float = 1e-08, equal_nan: bool = False):
        """Check whether two matrices are element-wise equal within a tolerance.

        Uses the formula ``|a - b| <= atol + rtol * |b|`` for each element pair.

        :param lhs: First matrix.
        :param rhs: Second matrix (must have the same shape as *lhs*).
        :param rtol: Relative tolerance.
        :param atol: Absolute tolerance.
        :param equal_nan: If ``True``, two ``NaN`` values are considered equal.
        :return: ``True`` if every element pair satisfies the tolerance.
        """

    @classmethod
    def zeros(cls, size: tuple[int, int]):
        """Create a matrix filled with zeros.

        :param size: A ``(rows, columns)`` tuple specifying the shape.
        :return: A new zero-filled :class:`Matrix`.
        """

    @classmethod
    def ones(cls, size: tuple[int, int]):
        """Create a matrix filled with ones.

        :param size: A ``(rows, columns)`` tuple specifying the shape.
        :return: A new :class:`Matrix` with every element set to ``1.0``.
        """

    @classmethod
    def normal(cls, mean: Optional[float], stddev: Optional[float],
               size: Optional[tuple[int, int]] = None) -> Union[float, "Matrix"]:
        """Sample from a normal (Gaussian) distribution.

        :param mean: Mean of the distribution (default ``0.0``).
        :param stddev: Standard deviation of the distribution (default ``1.0``).
        :param size: A ``(rows, columns)`` tuple.  If ``None``, return a single
            ``float`` instead of a :class:`Matrix`.
        """

    @classmethod
    def uniform(cls, minval: Optional[float], maxval: Optional[float],
                size: Optional[tuple[int, int]] = None) -> Union[float, "Matrix"]:
        """Sample from a continuous uniform distribution over ``[minval, maxval)``.

        :param minval: Lower bound (inclusive, default ``0.0``).
        :param maxval: Upper bound (exclusive, default ``1.0``).
        :param size: A ``(rows, columns)`` tuple.  If ``None``, return a single
            ``float`` instead of a :class:`Matrix`.
        """

    @classmethod
    def vector(cls, values: Sequence[Union[float, int]], as_column=False) -> "Matrix":
        """Create a matrix from a flat sequence of values.

        :param values: The elements of the vector.
        :param as_column: If ``True``, return a *n* x 1 column vector instead
            of the default 1 x *n* row vector.
        :return: A new :class:`Matrix` with a single row or column.
        """

    @classmethod
    def concat(cls, values: Sequence[Union["Matrix",
                                           Sequence[Union[float, int]]]], axis=0) -> "Matrix":
        """Concatenate matrices along the given axis.

        :param values: The matrices or sequences to concatenate.
        :param axis: ``0`` to concatenate vertically (stack rows),
            ``1`` to concatenate horizontally (stack columns).
        :return: A new :class:`Matrix` containing the concatenated data.
        """


T = TypeVar("T")


class Cown(Generic[T]):
    """Lightweight wrapper around the underlying cown capsule."""

    def __init__(self, value: T):
        """Create a cown.

        :param value: The initial value to wrap.
        """

    def __enter__(self):
        """Acquire the cown for a context manager block."""

    def __exit__(self, exc_type, exc_value, traceback):
        """Release the cown after a context manager block."""

    @property
    def value(self) -> T:
        """Return the current stored value."""

    @value.setter
    def value(self, value: T):
        """Set a new stored value."""

    def acquire(self):
        """Acquires the cown (required for reading and writing)."""

    def release(self):
        """Releases the cown."""

    @property
    def exception(self) -> bool:
        """Whether the held value is the result of an unhandled exception."""

    @exception.setter
    def exception(self, value: bool):
        """Set or clear the exception flag."""

    @property
    def acquired(self) -> bool:
        """Whether the cown is currently acquired."""

    def __lt__(self, other: "Cown") -> bool:
        """Order by the underlying capsule for deterministic ordering."""

    def __eq__(self, other: "Cown") -> bool:
        """Equality based on the wrapped capsule."""

    def __hash__(self) -> int:
        """Hash of the underlying capsule."""

    def __str__(self) -> str:
        """Readable string form."""

    def __repr__(self) -> str:
        """Debug representation."""


def notice_write(key: str, value: Any) -> None:
    """Write a value to the noticeboard.

    The write is fire-and-forget: the value is serialized immediately and
    handed to a dedicated noticeboard thread, which applies it under
    mutex.

    **No ordering guarantee.** A subsequent behavior — even one that
    chains directly off the writer through a shared cown — is *not*
    guaranteed to observe this write. Treat the noticeboard as
    eventually consistent shared state, never as a synchronization
    channel between behaviors.

    The noticeboard supports up to 64 distinct keys.  Writes beyond the
    limit are not applied; the noticeboard thread catches the resulting
    error and logs a warning.  No exception propagates to the caller.

    :param key: The noticeboard key (max 63 UTF-8 bytes).
    :type key: str
    :param value: The value to store.
    :type value: Any
    """


def notice_update(key: str, fn: Callable[[Any], Any], default: Any = None) -> None:
    """Atomically update a noticeboard entry.

    Reads the current value for *key* (or *default* if absent), applies
    *fn* to it, and writes the result back.  The read-modify-write is
    atomic because the single-threaded noticeboard mutator performs all
    three steps without interleaving. Like :func:`notice_write`, the
    call is fire-and-forget and carries **no ordering guarantee** with
    respect to other behaviors.

    Both *fn* and *default* must be picklable.  Lambdas and closures
    are **not** picklable; use ``functools.partial`` with a module-level
    function or an ``operator`` function instead.

    If *fn* returns the ``REMOVED`` sentinel, the entry is deleted from
    the noticeboard instead of being updated.

    .. warning::

       *fn* and *default* are pickled and sent to the noticeboard
       thread for execution. Anyone who can call :func:`notice_update`
       can therefore execute arbitrary Python on that thread. bocpy
       treats all runtime code as equally trusted; audit callers if
       that assumption does not hold.

    .. warning::

       More generally: bocpy worker sub-interpreters share the C-level
       runtime (terminator, MCS request queues, message queues,
       noticeboard) with the primary interpreter via ungated entry
       points such as :py:func:`bocpy._core.terminator_inc`,
       :py:func:`bocpy._core.terminator_dec`, and
       :py:meth:`bocpy._core.BehaviorCapsule.release_all`. These are
       intentionally callable from sub-interpreters because behavior
       bodies legitimately schedule nested ``@when`` calls. Any
       sub-interpreter running untrusted Python is therefore part of
       the trusted computing base: it can drive the terminator
       negative, schedule unbounded behaviors, or unlink an arbitrary
       behavior from the MCS queue. Only run code you trust inside
       behavior bodies.

    :param key: The noticeboard key (max 63 UTF-8 bytes).
    :type key: str
    :param fn: A picklable callable taking the current value, returning the new.
    :type fn: Callable[[Any], Any]
    :param default: Value used when *key* does not yet exist.
    :type default: Any
    """


def notice_delete(key: str) -> None:
    """Delete a single noticeboard entry.

    The deletion is fire-and-forget: the request is sent to the
    noticeboard thread, which removes the entry under mutex.  If the
    key does not exist, the operation is a no-op. Like
    :func:`notice_write`, this carries **no ordering guarantee** with
    respect to other behaviors.

    :param key: The noticeboard key to delete (max 63 UTF-8 bytes).
    :type key: str
    """


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


def noticeboard_version() -> int:
    """Return the current noticeboard version counter.

    The counter is incremented every time the noticeboard is
    successfully written, updated, or cleared. Two reads returning the
    same value mean no commit happened between them; a strictly larger
    value means at least one commit happened.

    The counter is global (across all threads and interpreters) and
    monotonic. Useful as a *hint* for detecting noticeboard changes
    without taking a full snapshot.

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


def notice_sync(timeout: Optional[float] = 30.0) -> int:
    """Block until the caller's prior noticeboard mutations are committed.

    Because :func:`notice_write`, :func:`notice_update`, and
    :func:`notice_delete` are fire-and-forget, a behavior that wants
    read-your-writes ordering against a *subsequent* behavior must call
    ``notice_sync()`` after its writes. By the time this returns, every
    write/update/delete posted from the calling thread before the call
    has been applied to the noticeboard.

    The barrier carries **no ordering guarantee** with respect to
    writes posted from other threads or behaviors interleaved with the
    caller's; it only flushes the caller's own queued mutations.

    :param timeout: Maximum seconds to wait. ``None`` waits forever.
        Defaults to 30 seconds.
    :type timeout: Optional[float]
    :raises TimeoutError: If the barrier does not complete within
        *timeout* seconds.
    :raises RuntimeError: If the runtime is not started.
    :return: The :func:`noticeboard_version` after the flush.
    :rtype: int
    """


def wait(timeout: Optional[float] = None, *, stats: bool = False):
    """Block until all behaviors complete, with optional timeout.

    On a successful return the runtime is **stopped**: workers are
    joined, the noticeboard thread exits, the C-level noticeboard
    slot is released, and the terminator is closed. The next
    ``@when`` call (or explicit :func:`start`) will spin up a fresh
    runtime.

    Note that holding on to references to Cown objects such that they
    are deallocated after wait() is called results in undefined behavior.

    :param timeout: Maximum number of seconds to wait, or ``None`` to
        wait indefinitely. The timeout bounds only the quiescence and
        noticeboard-drain phases; worker shutdown runs to completion
        regardless. Values above ``1e9`` seconds (~31.7 years) are
        clamped to wait-forever to avoid platform ``time_t`` /
        ``DWORD`` overflow inside the underlying condition-variable
        wait.
    :type timeout: Optional[float]
    :param stats: If ``True``, return the per-worker
        :func:`_core.scheduler_stats` snapshot captured at shutdown
        (after every behavior has run, before the per-worker array
        is freed). This is the only reliable way to read the
        scheduler counters for the session that just ended --
        calling :func:`_core.scheduler_stats` after :func:`wait`
        returns ``[]`` because the per-worker array has already been
        reclaimed. Returns ``[]`` if the runtime was never started
        or the snapshot could not be captured. Each dict has the
        keys documented on :func:`_core.scheduler_stats`
        (``worker_index``, ``pushed_local``,
        ``dispatched_to_pending``, ``pushed_remote``,
        ``popped_local``, ``popped_via_steal``,
        ``enqueue_cas_retries``, ``dequeue_cas_retries``,
        ``batch_resets``, ``steal_attempts``, ``steal_failures``,
        ``parked``, ``last_steal_attempt_ns``,
        ``fairness_arm_fires``, plus the per-sub-queue
        ``boc_bq_t`` counters).
    :type stats: bool
    :return: ``None`` when ``stats=False``; otherwise the per-worker
        stats list (same shape as :func:`_core.scheduler_stats`).
    :rtype: Optional[list[dict]]
    :raises RuntimeError: If the noticeboard thread does not exit
        before the timeout (or, on a retry call, is still alive).
        The first failure carries the message prefix
        ``"noticeboard thread did not shut down within timeout=..."``;
        subsequent retry failures carry
        ``"noticeboard thread still pinned on retry ..."``. Workers
        and the orphan-behavior drain have already completed by the
        time either is raised, so the runtime is intentionally left
        re-drivable: callers may retry ``wait()`` / ``stop()`` once
        the in-flight noticeboard mutation finishes. **Note:** when
        ``stats=True`` and ``stop()`` raises *after* runtime
        teardown has already completed (i.e. workers joined and the
        noticeboard closed), the exception is suppressed and the
        captured snapshot is returned instead — callers who require
        the exception to propagate should call :func:`wait` (without
        ``stats``) and read :func:`_core.scheduler_stats` from a
        prior in-session call.
    """


def when(*cowns):
    """Decorator to schedule a function as a behavior using given cowns.

    This decorator takes a list of zero or more cown objects, which will be
    passed in the order in which they were provided to the decorated function.
    The function itself is extracted and run as a behavior once all the cowns
    are available (i.e., not acquired by other behaviors).  Behaviors are
    scheduled such that deadlock will not occur.

    The function itself will be replaced by a :class:`Cown` which will hold
    the result of executing the behavior.  This :class:`Cown` can be used for
    further coordination.

    :param cowns: Zero or more :class:`Cown` objects or ``list[Cown]`` groups
        to acquire before running the decorated function.  Each argument
        becomes one parameter of the decorated function: a single
        :class:`Cown` is passed directly, while a list is delivered as a
        ``list[Cown]``.
    :type cowns: Union[Cown, list[Cown]]
    :return: A :class:`Cown` holding the result of the behavior.
    """


def start(**kwargs):
    """Start the bocpy runtime and worker pool.

    Spawns the worker sub-interpreters and the dedicated noticeboard
    thread. Scheduling and release run on the caller and worker
    threads themselves — there is no central scheduler thread.

    :param worker_count: The number of worker interpreters to start.  If
        ``None``, defaults to the number of available cores minus one.
    :type worker_count: Optional[int]
    :param module: A tuple of the target module name and file path to
        export for worker import.  If ``None``, the caller's module will
        be used.
    :type module: Optional[tuple[str, str]]
    """


def whencall(thunk: str, args: list[Union[Cown, list[Cown]]], captures: list[Any]) -> Cown:
    """Invoke a behavior by name with cown args and captured values.

    :param thunk: The name of the exported behavior function to call.
    :type thunk: str
    :param args: The cown arguments (or lists of cowns) to pass.
    :type args: list[Union[Cown, list[Cown]]]
    :param captures: Closed-over values to pass to the behavior.
    :type captures: list[Any]
    :return: A :class:`Cown` that will hold the behavior's return value.
    :rtype: Cown
    """
