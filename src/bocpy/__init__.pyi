from typing import Any, Callable, Generic, Iterator, Optional, Sequence, TypeVar, Union


TIMEOUT: str
"""Sentinel value returned by :func:`receive` when a timeout occurs."""


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
    def acquired(self) -> bool:
        """Whether the cown is currently acquired."""

    def __lt__(self, other: "Cown") -> bool:
        """Order by the underying capsule for deterministic ordering."""

    def __eq__(self, other: "Cown") -> bool:
        """Equality based on the wrapped capsule."""

    def __hash__(self) -> int:
        """Hash of the underlying capsule."""

    def __str__(self) -> str:
        """Readable string form."""

    def __repr__(self) -> str:
        """Debug representation."""


def wait(timeout: Optional[float] = None):
    """Block until all behaviors complete, with optional timeout.

    Note that holding on to references to Cown objects such that they
    are deallocated after wait() is called results in undefined behavior.

    :param timeout: Maximum number of seconds to wait, or ``None`` to
        wait indefinitely.
    :type timeout: Optional[float]
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
    """Start the behavior scheduler and worker pool.

    :param worker_count: The number of worker interpreters to start.  If
        ``None``, defaults to the number of available cores minus one.
    :type worker_count: Optional[int]
    :param export_dir: The directory to which the target module will be
        exported for worker import.  If ``None``, a temporary directory
        will be created and removed on shutdown.
    :type export_dir: Optional[str]
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
