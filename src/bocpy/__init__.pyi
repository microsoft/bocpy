"""Type stubs for the bocpy public API.

These stubs document the runtime surface exported by :mod:`bocpy` and
are consumed by static type checkers (mypy, pyright, pylance). Keep
entries in sync with the implementations in :mod:`bocpy.behaviors`,
:mod:`bocpy._core`, and :mod:`bocpy._math`.
"""

from typing import (Any, Callable, Generic, Iterator, Literal, Mapping,
                    NamedTuple, Optional, overload, Sequence, TypeVar, Union)


__version__: str
"""The installed bocpy distribution's version string.

Resolved at import time via :func:`importlib.metadata.version` so the
value tracks ``pyproject.toml`` without a second source-of-truth. Falls
back to ``"0.0.0+unknown"`` when running from an uninstalled source
checkout."""


TIMEOUT: str
"""Sentinel value returned by :func:`receive` when a timeout occurs."""

REMOVED: object
"""Sentinel returned by a ``notice_update`` fn to delete the entry."""

WORKER_COUNT: int
"""Default worker-pool size used when :func:`start` is called without an
explicit ``workers`` argument (CPU count - 1, minimum 1)."""


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
    def size(self) -> int:
        """The total element count of the matrix (``rows * columns``)."""

    @property
    def T(self) -> "Matrix":
        """Return a new matrix that is the transpose of this one."""

    @property
    def length(self) -> float:
        """Frobenius (L2) magnitude of the matrix as a read-only property.

        Equivalent to :meth:`magnitude` called with no axis argument:
        ``sqrt(sum(x**2 for x in m))`` over all elements. Exposed as a
        ``@property`` so that vector-like code reads naturally
        (``direction.length``, ``velocity.length``) without the extra
        parentheses of a method call.

        Note: this is **not** the element count. For ``rows * columns``
        use :attr:`size` (or :func:`len`, which returns :attr:`rows`).
        """

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

    def transpose(self, in_place: bool = False) -> "Matrix":
        """Return a transposed matrix.

        :param in_place: When ``True``, transpose ``self`` in place and
            return it (no allocation). When ``False`` (the default),
            return a new transposed :class:`Matrix`.
        """

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

    def magnitude_squared(self, axis: Optional[int] = None) -> Union[float, "Matrix"]:
        """Sum of squared elements (squared L2 norm), avoiding the square-root step.

        :param axis: If ``None``, return the total squared magnitude as a float.
            If ``0``, return a 1 x *columns* row vector of column squared magnitudes.
            If ``1``, return a *rows* x 1 column vector of row squared magnitudes.
        """

    def vecdot(self, other: "Matrix",
               axis: Optional[int] = None) -> Union[float, "Matrix"]:
        """Axis-aware inner product: sum of element-wise products.

        **Not** equivalent to :func:`numpy.dot` — use ``@`` /
        :func:`numpy.matmul` for matrix multiplication. This matches
        :func:`numpy.linalg.vecdot` for 1-D inputs with ``axis=None``.

        :param other: A matrix of compatible shape. Same-shape inputs sum
            over every element. Row-vector broadcast (``1xN`` vs ``MxN``)
            and column-vector broadcast (``Mx1`` vs ``MxN``) are supported.
        :param axis: If ``None``, return the total inner product as a float.
            If ``0``, return a 1 x *columns* row vector of per-column dots.
            If ``1``, return a *rows* x 1 column vector of per-row dots.
        :return: A float when ``axis is None``, otherwise a :class:`Matrix`.
        :seealso: ``@`` / :func:`numpy.matmul` for matrix multiplication.
        """

    def cross(self, other: "Matrix",
              axis: Optional[int] = None) -> Union[float, "Matrix"]:
        """2D or 3D cross product against another vector or batch.

        Five paths share one method:

        * ``1x2`` / ``2x1`` -- returns the scalar z-component
          ``self.x * other.y - self.y * other.x`` as a float.
        * ``1x3`` / ``3x1`` -- returns a same-shape :class:`Matrix`
          preserving ``self``'s row/column orientation.
        * ``Nx2`` / ``2xN`` -- per-vector scalars collected in an
          ``Mx1`` (rows) or ``1xN`` (cols) :class:`Matrix`.
        * ``Nx3`` / ``3xN`` -- a same-shape :class:`Matrix` of
          per-vector 3D cross products.

        ``other``'s orientation is irrelevant for the scalar inputs
        (only the flat element count matters).

        For batch ``self``, ``other`` may be either a same-shape batch
        or a single 2D / 3D vector (``1xK`` or ``Kx1``) which is
        broadcast against every per-vector slot. Because cross is
        anticommutative, the broadcast convention is one-directional:
        ``self`` must be the batch operand. To compute ``v.cross(batch)``,
        write ``-batch.cross(v)`` (cross is anticommutative) or build
        the right-shaped operand on the caller side.

        :param other: A vector or matrix with a compatible shape.
        :param axis: Disambiguates the ambiguous square ``2x2`` and
            ``3x3`` shapes. Default (``None``) and ``axis=1`` treat
            rows as components; ``axis=0`` treats columns. Ignored on
            every other shape, including the doubly-valid ``2x3`` and
            ``3x2`` batches — those always use the 2D-batch
            interpretation (the 3D-batch reading is not reachable
            through this method).
        :return: A float for ``1x2`` / ``2x1`` inputs; otherwise a
            :class:`Matrix`.
        :raises NotImplementedError: on incompatible shapes or
            mismatched batch sizes.
        """

    def normalize(self, axis: Optional[int] = None,
                  in_place: bool = False) -> "Matrix":
        """Divide every element by its magnitude.

        Zero-magnitude rows/columns are returned as exact zeros — no
        division by zero and no NaN. Sub-normal magnitudes may overflow
        during division; threshold with :meth:`magnitude_squared` if
        safety matters.

        :param axis: If ``None``, divide every element by the matrix's total
            magnitude. If ``0``, divide each column by its own magnitude.
            If ``1``, divide each row by its own magnitude.
        :param in_place: When ``True``, mutate ``self`` and return it.
            When ``False`` (the default), return a new normalised
            :class:`Matrix`.
        :return: A :class:`Matrix` (``self`` when ``in_place=True``).
        """

    def perpendicular(self, axis: Optional[int] = None,
                      in_place: bool = False) -> "Matrix":
        """Rotate every 2D vector 90 degrees counter-clockwise: ``(x, y) -> (-y, x)``.

        Accepts a single 2D vector (``1x2`` or ``2x1``), a row batch
        (``Nx2``), or a column batch (``2xN``). On the ambiguous ``2x2``
        input this method treats rows as components (``axis=1``). Pass
        ``axis=0`` explicitly if you mean columns.

        :param axis: Axis override for the ambiguous ``2x2`` shape; ignored
            on unambiguous shapes.
        :param in_place: When ``True``, mutate ``self`` and return it.
            When ``False`` (the default), return a new :class:`Matrix`
            with the rotated vectors.
        :return: A :class:`Matrix` (``self`` when ``in_place=True``).
        :raises NotImplementedError: on any shape that is not a 2D vector
            or a ``Nx2`` / ``2xN`` batch.
        """

    def angle(self, axis: Optional[int] = None) -> Union[float, "Matrix"]:
        """Polar angle (``atan2(y, x)``) of every 2D vector.

        Returns a float for a single 2D vector, an ``Mx1`` column matrix
        for an ``Nx2`` row batch, or a ``1xN`` row matrix for a ``2xN``
        column batch. On the ambiguous ``2x2`` input this method treats
        rows as components (``axis=1``). Pass ``axis=0`` explicitly if
        you mean columns.

        :param axis: Axis override for the ambiguous ``2x2`` shape; ignored
            on unambiguous shapes.
        :return: A float for a single 2D vector input, otherwise a
            :class:`Matrix` of per-vector angles.
        :raises NotImplementedError: on any shape that is not a 2D vector
            or a ``Nx2`` / ``2xN`` batch.
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

    def ceil(self, in_place: bool = False) -> "Matrix":
        """Round each element up to the nearest integer.

        :param in_place: When ``True``, mutate ``self`` and return it.
        """

    def floor(self, in_place: bool = False) -> "Matrix":
        """Round each element down to the nearest integer.

        :param in_place: When ``True``, mutate ``self`` and return it.
        """

    def round(self, in_place: bool = False) -> "Matrix":
        """Round each element to the nearest integer (banker's rounding).

        :param in_place: When ``True``, mutate ``self`` and return it.
        """

    def negate(self, in_place: bool = False) -> "Matrix":
        """Negate every element.

        :param in_place: When ``True``, mutate ``self`` and return it.
        """

    def abs(self, in_place: bool = False) -> "Matrix":
        """Take the absolute value of every element.

        :param in_place: When ``True``, mutate ``self`` and return it.
        """

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

    Values may embed :class:`Cown` references; the noticeboard keeps
    each embedded cown alive for as long as the entry remains in the
    noticeboard.

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

    The value returned by *fn* may embed :class:`Cown` references; the
    noticeboard retains them until the entry is overwritten or deleted,
    identical to :func:`notice_write`.

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


def notice_sync(timeout: Optional[float] = 30.0) -> None:
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
    """


@overload
def wait(timeout: Optional[float] = None, *,
         stats: Literal[False] = False,
         noticeboard: Literal[False] = False) -> None: ...


@overload
def wait(timeout: Optional[float] = None, *,
         stats: Literal[True],
         noticeboard: Literal[False] = False) -> list[dict]: ...


@overload
def wait(timeout: Optional[float] = None, *,
         stats: Literal[False] = False,
         noticeboard: Literal[True]) -> dict[str, Any]: ...


@overload
def wait(timeout: Optional[float] = None, *,
         stats: Literal[True],
         noticeboard: Literal[True]) -> "WaitResult": ...


def wait(timeout: Optional[float] = None, *,
         stats: bool = False,
         noticeboard: bool = False
         ) -> Union[None, list[dict], dict[str, Any], "WaitResult"]:
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
    :param stats: If ``True``, capture the per-worker
        :func:`_core.scheduler_stats` snapshot at shutdown
        (after every behavior has run, before the per-worker array
        is freed). This is the only reliable way to read the
        scheduler counters for the session that just ended --
        calling :func:`_core.scheduler_stats` after :func:`wait`
        returns ``[]`` because the per-worker array has already been
        reclaimed. Falls back to ``[]`` if the runtime was never
        started or the snapshot could not be captured. Each dict has
        the keys documented on :func:`_core.scheduler_stats`
        (``worker_index``, ``pushed_local``,
        ``dispatched_to_pending``, ``pushed_remote``,
        ``popped_local``, ``popped_via_steal``,
        ``enqueue_cas_retries``, ``dequeue_cas_retries``,
        ``batch_resets``, ``steal_attempts``, ``steal_failures``,
        ``parked``, ``last_steal_attempt_ns``,
        ``fairness_arm_fires``, plus the per-sub-queue
        ``boc_bq_t`` counters).
    :type stats: bool
    :param noticeboard: If ``True``, capture the final noticeboard
        contents as a plain ``dict`` at shutdown (after the
        noticeboard thread exits, before :func:`noticeboard`
        entries are freed). Useful for lifting a final result an
        early-stopping behavior wrote to the noticeboard before
        the runtime quiesced. Falls back to ``{}`` if the runtime
        was never started or the snapshot could not be captured.
    :type noticeboard: bool
    :return: ``None`` when neither flag is set; the per-worker stats
        list when only ``stats=True``; the noticeboard dict when only
        ``noticeboard=True``; and a :class:`WaitResult` ``NamedTuple``
        carrying both when both flags are set.
    :rtype: Union[None, list[dict], dict[str, Any], WaitResult]
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
        either ``stats=True`` or ``noticeboard=True`` is set and
        ``stop()`` raises *after* runtime teardown has already
        completed (i.e. workers joined and the noticeboard closed),
        the exception is suppressed and the captured snapshot(s) are
        returned instead -- callers who require the exception to
        propagate should call :func:`wait` without either flag.
    """


class WaitResult(NamedTuple):
    """Result bundle returned by :func:`wait`.

    Produced only when both ``stats=True`` and ``noticeboard=True``
    are set.

    :ivar stats: Per-worker scheduler-stats snapshot.
    :ivar noticeboard: Final noticeboard contents as a plain ``dict``.
    """

    stats: list[dict]
    noticeboard: dict[str, Any]


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

    Decorators **below** ``@when`` compose with the behavior body and run
    on the worker (e.g. ``@when(x) @my_decorator def f(x): ...``).
    Decorators **above** ``@when`` are not supported and will raise a
    ``SyntaxError`` at transpile time.  ``async def`` functions are also
    rejected — there is no event loop on workers to drive coroutines.
    ``@staticmethod`` / ``@classmethod`` / ``@property`` below ``@when``
    are also rejected because the generated behavior runs as a
    module-level function, where these descriptors are not callable.

    .. note::

       The transpiler matches ``@when`` by literal name. Aliasing the
       import (``from bocpy import when as boc_when``) is not
       supported — the rewrite will not fire and the worker will fail.

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


def get_include() -> str:
    """Return the absolute path to the bocpy public C header root.

    Use the returned path as an additional ``include_dirs`` entry on a
    downstream :class:`setuptools.Extension` so its translation units
    can ``#include <bocpy/bocpy.h>``. The directory contains a single
    ``bocpy/`` subdirectory holding the public ABI surface; bocpy's
    private headers are not exposed.

    :return: Absolute filesystem path to the include root (the parent
        of the ``bocpy/`` subdirectory containing ``bocpy.h`` and
        ``xidata.h``).
    :rtype: str
    """


def get_sources() -> list[str]:
    """Return platform-specific extra C sources for downstream extensions.

    On Windows the returned list contains the absolute path to
    ``bocpy_msvc.c``, which provides MSVC out-of-line bodies for the
    atomic ops declared in ``<bocpy/bocpy.h>``. On non-Windows
    platforms the list is empty (``<stdatomic.h>`` provides
    everything).

    :return: A list of absolute paths to add to a downstream
        :class:`setuptools.Extension`'s ``sources=`` list.
    :rtype: list[str]
    """
