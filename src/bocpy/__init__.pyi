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

    def fma(self, b: Union["Matrix", int, float],
            c: Union["Matrix", int, float],
            /, in_place: bool = False) -> "Matrix":
        """Fused multiply-add: single-rounding ``self * b + c``.

        Uses C99 ``fma`` so the product is rounded once, unlike
        ``self * b + c`` which rounds twice (the build sets
        ``-ffp-contract=off`` for bit-reproducibility, so even an explicit
        ``self * b + c`` never fuses). Results may differ from the
        two-rounding form by up to half a ULP -- compare with
        :meth:`allclose`, never ``==``.

        :param b: Multiplier: a same-shape :class:`Matrix`, a ``1x1``
            matrix, a ``1xN`` row vector or ``Mx1`` column vector that
            broadcasts against ``self``, or a scalar.
        :param c: Addend, with the same shape rules as *b*.
        :param in_place: When ``True``, write into ``self`` and return it.
        :return: ``self * b + c`` (``self`` itself when ``in_place=True``).
        :raises ValueError: if *b* / *c* is a matrix whose shape neither
            matches ``self`` nor broadcasts against it.
        :raises TypeError: if *b* / *c* is neither a matrix nor a real
            number.
        """

    def scaled_add(self, s: Union["Matrix", int, float], x: "Matrix",
                   /, in_place: bool = False) -> "Matrix":
        """Scaled add ``self + s * x`` -- the two-rounding sibling of :meth:`fma`.

        The product ``s * x`` is rounded to ``double`` and then the sum with
        ``self`` is rounded again, so the result is bit-for-bit identical to
        ``self + s * x`` (the build sets ``-ffp-contract=off``, so the two
        statements never fuse). This is exactly the rounding :meth:`fma`
        avoids: use :meth:`scaled_add` when you need a result identical to the
        plain ``self + s * x`` expression, and :meth:`fma` for the
        single-rounding fused form.

        :param s: Scale: a same-shape :class:`Matrix`, a ``1x1`` matrix, a
            ``1xN`` row vector or ``Mx1`` column vector that broadcasts against
            ``self``, or a scalar.
        :param x: Addend term, a :class:`Matrix` with the same shape as
            ``self``.
        :param in_place: When ``True``, write into ``self``'s buffer
            (allocating nothing) and return it.
        :return: ``self + s * x`` (``self`` itself when ``in_place=True``).
        :raises ValueError: if *s* is a matrix whose shape neither matches
            ``self`` nor broadcasts against it, or if *x*'s shape does not
            match ``self``.
        :raises TypeError: if *s* is neither a matrix nor a real number.
        """

    def add(self, other: Union["Matrix", int, float], /, *,
            out: Optional["Matrix"] = None) -> "Matrix":
        """Element-wise ``self + other`` -- the method form of ``+``.

        Uses the same broadcasting as the ``+`` operator (*other* may be a
        same-shape matrix, a ``1x1`` matrix, a ``1xN``/``Mx1`` vector that
        broadcasts, or a scalar) and is bit-for-bit identical to it.

        :param other: The right-hand operand.
        :param out: A :class:`Matrix` matching the result shape to write the
            result into (allocation-free); returned in place of a fresh
            matrix. May alias an input operand.
        :return: ``self + other`` (``out`` itself when ``out`` is given).
        :raises ValueError: if *other*'s shape does not broadcast against
            ``self``, or if *out*'s shape does not match the result.
        :raises TypeError: if *out* is given and is not a :class:`Matrix`.
        """

    def subtract(self, other: Union["Matrix", int, float], /, *,
                 out: Optional["Matrix"] = None) -> "Matrix":
        """Element-wise ``self - other`` -- the method form of ``-``.

        Uses the same broadcasting as the ``-`` operator (*other* may be a
        same-shape matrix, a ``1x1`` matrix, a ``1xN``/``Mx1`` vector that
        broadcasts, or a scalar) and is bit-for-bit identical to it.

        :param other: The right-hand operand.
        :param out: A :class:`Matrix` matching the result shape to write the
            result into (allocation-free); returned in place of a fresh
            matrix. May alias an input operand.
        :return: ``self - other`` (``out`` itself when ``out`` is given).
        :raises ValueError: if *other*'s shape does not broadcast against
            ``self``, or if *out*'s shape does not match the result.
        :raises TypeError: if *out* is given and is not a :class:`Matrix`.
        """

    def multiply(self, other: Union["Matrix", int, float], /, *,
                 out: Optional["Matrix"] = None) -> "Matrix":
        """Element-wise ``self * other`` -- the method form of ``*``.

        Uses the same broadcasting as the ``*`` operator (*other* may be a
        same-shape matrix, a ``1x1`` matrix, a ``1xN``/``Mx1`` vector that
        broadcasts, or a scalar) and is bit-for-bit identical to it.

        :param other: The right-hand operand.
        :param out: A :class:`Matrix` matching the result shape to write the
            result into (allocation-free); returned in place of a fresh
            matrix. May alias an input operand.
        :return: ``self * other`` (``out`` itself when ``out`` is given).
        :raises ValueError: if *other*'s shape does not broadcast against
            ``self``, or if *out*'s shape does not match the result.
        :raises TypeError: if *out* is given and is not a :class:`Matrix`.
        """

    def divide(self, other: Union["Matrix", int, float], /, *,
               out: Optional["Matrix"] = None) -> "Matrix":
        """Element-wise ``self / other`` -- the method form of ``/``.

        Uses the same broadcasting as the ``/`` operator (*other* may be a
        same-shape matrix, a ``1x1`` matrix, a ``1xN``/``Mx1`` vector that
        broadcasts, or a scalar) and is bit-for-bit identical to it.

        :param other: The right-hand operand.
        :param out: A :class:`Matrix` matching the result shape to write the
            result into (allocation-free); returned in place of a fresh
            matrix. May alias an input operand.
        :return: ``self / other`` (``out`` itself when ``out`` is given).
        :raises ValueError: if *other*'s shape does not broadcast against
            ``self``, or if *out*'s shape does not match the result.
        :raises TypeError: if *out* is given and is not a :class:`Matrix`.
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
        :raises ValueError: on incompatible shapes or
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
        :raises ValueError: on any shape that is not a 2D vector
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
        :raises ValueError: on any shape that is not a 2D vector
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

    def argmin(self, axis: Optional[int] = None) -> Union[int, "Matrix"]:
        """Index of the minimum element (first occurrence on ties).

        :param axis: If ``None``, return the flat row-major index of the
            overall minimum as an ``int``.  If ``0``, return a 1 x *columns*
            row vector of per-column row indices.  If ``1``, return a
            *rows* x 1 column vector of per-row column indices.

        .. note::
           NaN elements are skipped unless the running extreme starts at
           NaN (element 0 along the reduced axis), which pins the result
           to that position.  This differs from NumPy, which propagates NaN.
        """

    def argmax(self, axis: Optional[int] = None) -> Union[int, "Matrix"]:
        """Index of the maximum element (first occurrence on ties).

        :param axis: If ``None``, return the flat row-major index of the
            overall maximum as an ``int``.  If ``0``, return a 1 x *columns*
            row vector of per-column row indices.  If ``1``, return a
            *rows* x 1 column vector of per-row column indices.

        .. note::
           NaN elements are skipped unless the running extreme starts at
           NaN (element 0 along the reduced axis), which pins the result
           to that position.  This differs from NumPy, which propagates NaN.
        """

    def ceil(self, in_place: bool = False, *,
             out: Optional["Matrix"] = None) -> "Matrix":
        """Round each element up to the nearest integer.

        :param in_place: When ``True``, mutate ``self`` and return it.
        :param out: A same-shape :class:`Matrix` to write the result into
            (allocation-free); returned in place of a fresh matrix. Mutually
            exclusive with ``in_place``.
        """

    def floor(self, in_place: bool = False, *,
              out: Optional["Matrix"] = None) -> "Matrix":
        """Round each element down to the nearest integer.

        :param in_place: When ``True``, mutate ``self`` and return it.
        :param out: A same-shape :class:`Matrix` to write the result into
            (allocation-free); returned in place of a fresh matrix. Mutually
            exclusive with ``in_place``.
        """

    def round(self, in_place: bool = False, *,
              out: Optional["Matrix"] = None) -> "Matrix":
        """Round each element to the nearest integer (banker's rounding).

        :param in_place: When ``True``, mutate ``self`` and return it.
        :param out: A same-shape :class:`Matrix` to write the result into
            (allocation-free); returned in place of a fresh matrix. Mutually
            exclusive with ``in_place``.
        """

    def negate(self, in_place: bool = False, *,
               out: Optional["Matrix"] = None) -> "Matrix":
        """Negate every element.

        :param in_place: When ``True``, mutate ``self`` and return it.
        :param out: A same-shape :class:`Matrix` to write the result into
            (allocation-free); returned in place of a fresh matrix. Mutually
            exclusive with ``in_place``.
        """

    def abs(self, in_place: bool = False, *,
            out: Optional["Matrix"] = None) -> "Matrix":
        """Take the absolute value of every element.

        :param in_place: When ``True``, mutate ``self`` and return it.
        :param out: A same-shape :class:`Matrix` to write the result into
            (allocation-free); returned in place of a fresh matrix. Mutually
            exclusive with ``in_place``.
        """

    def sqrt(self, in_place: bool = False, *,
             out: Optional["Matrix"] = None) -> "Matrix":
        """Take the square root of every element.

        Negative elements yield ``NaN`` (no exception is raised), matching
        :func:`numpy.sqrt`.

        :param in_place: When ``True``, mutate ``self`` and return it.
        :param out: A same-shape :class:`Matrix` to write the result into
            (allocation-free); returned in place of a fresh matrix. Mutually
            exclusive with ``in_place``.
        """

    def less(self, other: Union["Matrix", int, float,
                                Sequence[Union[int, float]]]) -> "Matrix":
        """Element-wise ``self < other`` as a 0/1 mask matrix.

        Distinct from the ``<`` operator, which returns a single
        :class:`bool` (see :meth:`__lt__`).

        :param other: A same-shape matrix, a scalar (including ``bool``), a
            ``1x1`` matrix, a row/column vector that broadcasts (same rules
            as arithmetic), or a list/tuple of numbers.
        :return: A new :class:`Matrix` of ``1.0``/``0.0``. NaN comparisons
            yield ``0.0``.
        :raises ValueError: on a non-broadcastable shape or an empty
            list/tuple operand.
        :raises TypeError: if *other* is a list/tuple holding a non-number,
            or is not a matrix, scalar, or list/tuple.
        """

    def less_equal(self, other: Union["Matrix", int, float,
                                      Sequence[Union[int, float]]]) -> "Matrix":
        """Element-wise ``self <= other`` as a 0/1 mask matrix.

        Distinct from the ``<=`` operator, which returns a single
        :class:`bool` (see :meth:`__le__`).

        :param other: A same-shape matrix, a scalar (including ``bool``), a
            ``1x1`` matrix, a row/column vector that broadcasts, or a
            list/tuple of numbers.
        :return: A new :class:`Matrix` of ``1.0``/``0.0``. NaN comparisons
            yield ``0.0``.
        :raises ValueError: on a non-broadcastable shape or an empty
            list/tuple operand.
        :raises TypeError: on a non-number list/tuple element or an
            unsupported operand type (see :meth:`less`).
        """

    def greater(self, other: Union["Matrix", int, float,
                                   Sequence[Union[int, float]]]) -> "Matrix":
        """Element-wise ``self > other`` as a 0/1 mask matrix.

        Distinct from the ``>`` operator, which returns a single
        :class:`bool` (see :meth:`__gt__`).

        :param other: A same-shape matrix, a scalar (including ``bool``), a
            ``1x1`` matrix, a row/column vector that broadcasts, or a
            list/tuple of numbers.
        :return: A new :class:`Matrix` of ``1.0``/``0.0``. NaN comparisons
            yield ``0.0``.
        :raises ValueError: on a non-broadcastable shape or an empty
            list/tuple operand.
        :raises TypeError: on a non-number list/tuple element or an
            unsupported operand type (see :meth:`less`).
        """

    def greater_equal(self, other: Union["Matrix", int, float,
                                         Sequence[Union[int, float]]]) -> "Matrix":
        """Element-wise ``self >= other`` as a 0/1 mask matrix.

        Distinct from the ``>=`` operator, which returns a single
        :class:`bool` (see :meth:`__ge__`).

        :param other: A same-shape matrix, a scalar (including ``bool``), a
            ``1x1`` matrix, a row/column vector that broadcasts, or a
            list/tuple of numbers.
        :return: A new :class:`Matrix` of ``1.0``/``0.0``. NaN comparisons
            yield ``0.0``.
        :raises ValueError: on a non-broadcastable shape or an empty
            list/tuple operand.
        :raises TypeError: on a non-number list/tuple element or an
            unsupported operand type (see :meth:`less`).
        """

    def equal(self, other: Union["Matrix", int, float,
                                 Sequence[Union[int, float]]]) -> "Matrix":
        """Element-wise ``self == other`` as a 0/1 mask matrix.

        Distinct from the ``==`` operator, which returns a single
        :class:`bool` (see :meth:`__eq__`).

        :param other: A same-shape matrix, a scalar (including ``bool``), a
            ``1x1`` matrix, a row/column vector that broadcasts, or a
            list/tuple of numbers.
        :return: A new :class:`Matrix` of ``1.0``/``0.0``. NaN comparisons
            yield ``0.0``.
        :raises ValueError: on a non-broadcastable shape or an empty
            list/tuple operand.
        :raises TypeError: on a non-number list/tuple element or an
            unsupported operand type (see :meth:`less`).
        """

    def not_equal(self, other: Union["Matrix", int, float,
                                     Sequence[Union[int, float]]]) -> "Matrix":
        """Element-wise ``self != other`` as a 0/1 mask matrix.

        Distinct from the ``!=`` operator, which returns a single
        :class:`bool` (see :meth:`__ne__`).

        :param other: A same-shape matrix, a scalar (including ``bool``), a
            ``1x1`` matrix, a row/column vector that broadcasts, or a
            list/tuple of numbers.
        :return: A new :class:`Matrix` of ``1.0``/``0.0``. NaN comparisons
            yield ``1.0``.
        :raises ValueError: on a non-broadcastable shape or an empty
            list/tuple operand.
        :raises TypeError: on a non-number list/tuple element or an
            unsupported operand type (see :meth:`less`).
        """

    def clip(self, min: Optional[float] = None,
             max: Optional[float] = None) -> "Matrix":
        """Clamp every element to ``[min, max]``.

        The first argument is the lower bound and the second the upper
        bound. Either may be ``None`` (or omitted) to leave that side
        unbounded: ``m.clip(min=0.0)`` clamps only below and
        ``m.clip(max=255.0)`` only above.

        :param min: Lower clipping bound, or ``None`` for no lower bound.
        :param max: Upper clipping bound, or ``None`` for no upper bound.
        :return: A new clipped :class:`Matrix`.
        :raises ValueError: if both *min* and *max* are ``None``.
        :raises AssertionError: if both bounds are given and *max* < *min*.
        :raises TypeError: if a given bound is not a real number.
        """

    def copy(self) -> "Matrix":
        """Return a deep copy of this matrix."""

    def __reduce__(self) -> tuple:
        """Support pickling and :func:`copy.deepcopy`.

        Serializes the matrix to its native-endian raw ``double`` buffer
        so reconstruction is a single copy with no per-element Python
        object overhead. The current interpreter must own the matrix.
        """

    def take(self, indices: Union[list[int], tuple[int]], axis=0) -> "Matrix":
        """Return a new matrix containing only the selected rows or columns.

        *indices* is a 1-D list or tuple of ints. Negative indices count
        from the end. Duplicate indices repeat the corresponding row or
        column. Equivalent to ``m[indices]`` (rows) and ``m[:, indices]``
        (columns). A ``bool`` element is treated as the integer ``0``/``1``.

        :param indices: The row or column indices to take. Must be a
            non-empty list or tuple of ints.
        :param axis: ``0`` to take rows, ``1`` to take columns.
        :raises IndexError: if an index is out of range, or if *indices*
            is empty.
        :raises KeyError: if *axis* is not ``0`` or ``1``.
        :raises TypeError: if an index is not an int.
        :raises OverflowError: if an index exceeds the platform word size.
        """

    def put(self, indices: Union[list[int], tuple[int]],
            value: Union[int, float, "Matrix"], axis=0,
            accumulate: bool = False) -> "Matrix":
        """Assign *value* into the selected rows or columns in place.

        The write-side counterpart of :meth:`take`. *value* may be a
        scalar (a real number or a ``1x1`` matrix, broadcast over the
        selection) or a matrix whose shape matches the selection exactly
        (``len(indices)`` rows by this matrix's column count for a row
        assignment; this matrix's row count by ``len(indices)`` columns for
        a column assignment). Equivalent to ``m[indices] = value`` (rows)
        and ``m[:, indices] = value`` (columns).

        All indices and the *value* shape are validated before any element
        is written, so a rejected call leaves the matrix unchanged. Negative
        indices count from the end. A ``bool`` index element is treated as
        the integer ``0``/``1``.

        With ``accumulate=False`` (the default) duplicate indices follow
        last-write-wins. With ``accumulate=True`` the values are *added*
        into the selection, so duplicate indices fold additively
        (``m.put([0, 0], v, accumulate=True)`` adds twice into row 0). This
        is the only way to fold duplicates: ``m[indices] += value`` desugars
        to gather/iadd/scatter and so collapses to last-write-wins.

        :param indices: The row or column indices to assign. Must be a
            non-empty list or tuple of ints.
        :param value: The scalar or matrix to assign into the selection.
        :param axis: ``0`` to assign rows, ``1`` to assign columns.
        :param accumulate: When ``True``, add into the selection instead of
            overwriting, so duplicate indices accumulate.
        :return: ``self`` (to allow chaining).
        :raises IndexError: if an index is out of range, or if *indices*
            is empty.
        :raises KeyError: if *axis* is not ``0`` or ``1``.
        :raises ValueError: if a matrix *value* shape does not match the
            selection shape.
        :raises TypeError: if *value* is neither a real number nor a matrix,
            or if an index is not an int.
        :raises OverflowError: if an index exceeds the platform word size.
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

    def __getitem__(self, key: Union[int, slice, tuple, list[int]]) -> Union["Matrix", float]:
        """Retrieve a row, element, sub-matrix, or fancy-index gather.

        A list key gathers rows or columns: ``m[[r0, r1]]`` and
        ``m[[r0, r1], :]`` select rows, and ``m[:, [c0, c1]]`` selects
        columns. A list key always returns a :class:`Matrix` (even a
        single-element list such as ``m[[0]]``), whereas ``m[0]`` and
        ``m[0, 0]`` return a Python ``float``. Negative indices count from
        the end; duplicates repeat the row or column; an out-of-range index
        raises :class:`IndexError` and an empty list raises
        :class:`IndexError`.

        Column gather requires the bare ``:`` row selector: ``m[0:R, [c]]``
        (a full *range* rather than ``:``) raises
        :class:`IndexError`. Paired lists (``m[[r], [c]]``),
        list-with-int (``m[[r], 0]``), and a list paired with a non-full
        slice all raise :class:`IndexError`; use :meth:`take`
        for those. A ``bool`` element in a list is treated as ``0``/``1``.

        :raises IndexError: if a list index is out of range, the list
            is empty, or for unsupported list/slice combinations.
        """

    def __setitem__(self, key: Union[int, slice, tuple, list[int]],
                    value: Union[int, float, "Matrix",
                                 Sequence[Union[int, float]]]):
        """Set a row, element, sub-matrix, or fancy-index scatter.

        A list key scatters into rows or columns, mirroring the
        :meth:`__getitem__` gather shapes: ``m[[r0, r1]] = v`` and
        ``m[[r0, r1], :] = v`` assign rows, and ``m[:, [c0, c1]] = v``
        assigns columns. The right-hand side may be a scalar (a real number
        or a ``1x1`` matrix, broadcast over the selection) or a matrix whose
        shape matches the selection exactly (``count`` rows by the receiver's
        column count for a row scatter; the receiver's row count by ``count``
        columns for a column scatter).

        All indices and the RHS shape are validated *before* any element is
        written, so a rejected assignment leaves the matrix unchanged (no
        partial writes). Duplicate indices follow last-write-wins, **not**
        accumulation — ``m[[0, 0]] += v`` increments row 0 once, not twice;
        use :meth:`put` with ``accumulate=True`` to fold duplicates. The
        augmented forms (``+=``, ``-=``, ``*=``, ``/=``) desugar to a
        gather, an in-place op, and a scatter.

        Column scatter requires the bare ``:`` row selector: ``m[0:R, [c]]``
        raises :class:`IndexError`. Paired lists (``m[[r], [c]]``),
        list-with-int (``m[[r], 0]``), and a list paired with a non-full
        slice all raise :class:`IndexError`. A ``bool`` element in
        a list is treated as ``0``/``1``.

        :raises IndexError: if a list index is out of range, the list
            is empty, or for unsupported list/slice combinations.
        :raises ValueError: if a matrix RHS shape does not match the
            selection shape.
        :raises TypeError: if the RHS is neither a real number nor a matrix.
        """

    def __iter__(self) -> Iterator[Union[float, "Matrix"]]:
        """Iterate over rows of the matrix."""

    def __lt__(self, other: Union["Matrix", int, float,
                                  Sequence[Union[int, float]]]) -> bool:
        """Lexicographic ``self < other`` returning a single :class:`bool`.

        Compares element by element in row-major order, like a list or
        tuple: the first element where a strict ordering holds decides.
        *other* must be a same-shape matrix, a scalar (a scalar
        broadcasts to ``self``'s shape), or a list/tuple of numbers (coerced
        to a ``1xN`` row matrix). A ``1x1`` matrix is **not** treated
        as a scalar here and only compares against another ``1x1``. A
        ``NaN`` element never decides the ordering; comparison continues
        past it, so two all-``NaN`` matrices compare neither ``<`` nor ``>``.

        For an element-wise 0/1 mask instead, use :meth:`less`.

        :raises ValueError: if *other* is a matrix (or coerced sequence) of
            a different shape, or an empty list/tuple.
        :raises TypeError: if *other* is a list/tuple holding a non-number.
        """

    def __le__(self, other: Union["Matrix", int, float,
                                  Sequence[Union[int, float]]]) -> bool:
        """Lexicographic ``self <= other`` returning a single :class:`bool`.

        See :meth:`__lt__` for the comparison rules. For an element-wise
        0/1 mask instead, use :meth:`less_equal`.

        :raises ValueError: if *other* is a matrix (or coerced sequence) of
            a different shape, or an empty list/tuple.
        :raises TypeError: if *other* is a list/tuple holding a non-number.
        """

    def __gt__(self, other: Union["Matrix", int, float,
                                  Sequence[Union[int, float]]]) -> bool:
        """Lexicographic ``self > other`` returning a single :class:`bool`.

        See :meth:`__lt__` for the comparison rules. For an element-wise
        0/1 mask instead, use :meth:`greater`.

        :raises ValueError: if *other* is a matrix (or coerced sequence) of
            a different shape, or an empty list/tuple.
        :raises TypeError: if *other* is a list/tuple holding a non-number.
        """

    def __ge__(self, other: Union["Matrix", int, float,
                                  Sequence[Union[int, float]]]) -> bool:
        """Lexicographic ``self >= other`` returning a single :class:`bool`.

        See :meth:`__lt__` for the comparison rules. For an element-wise
        0/1 mask instead, use :meth:`greater_equal`.

        :raises ValueError: if *other* is a matrix (or coerced sequence) of
            a different shape, or an empty list/tuple.
        :raises TypeError: if *other* is a list/tuple holding a non-number.
        """

    def __eq__(self, other: object) -> bool:
        """Lexicographic ``self == other`` returning a single :class:`bool`.

        ``True`` only when *other* is a same-shape matrix equal element by
        element, a list/tuple of numbers (coerced to a ``1xN`` row matrix)
        equal element by element, or a scalar that every element equals.
        Total over non-equality: a shape mismatch, a non-matrix / non-scalar
        / non-list-tuple operand, or a list/tuple that cannot be coerced
        (empty, or holding a non-number) all return ``False`` rather than
        raising, so ``matrix in some_list`` still works. A ``NaN`` element
        never decides the comparison (it is skipped), so an all-``NaN``
        matrix compares ``==`` equal to itself; this diverges from the
        :meth:`equal` mask, where ``NaN == x`` is ``0.0``. Defining value
        equality makes :class:`Matrix` unhashable (so it cannot be a dict
        key or set member), which is correct for a mutable type. For an
        element-wise 0/1 mask instead, use :meth:`equal`.

        :raises RuntimeError: if *other* is a matrix owned by a different
            interpreter.
        """

    def __ne__(self, other: object) -> bool:
        """Lexicographic ``self != other`` returning a single :class:`bool`.

        The (total) negation of :meth:`__eq__`. For an element-wise
        0/1 mask instead, use :meth:`not_equal`.

        :raises RuntimeError: if *other* is a matrix owned by a different
            interpreter.
        """

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
    def where(cls, mask: "Matrix",
              a: Union["Matrix", int, float, Sequence[Union[int, float]]],
              b: Union["Matrix", int, float,
                       Sequence[Union[int, float]]]) -> "Matrix":
        """Select element-wise from *a* or *b* on a truthy mask.

        Returns a fresh matrix taking *a* where the corresponding *mask*
        element is non-zero and *b* elsewhere, like :func:`numpy.where`.

        :param mask: A :class:`Matrix` whose non-zero elements select *a*.
            ``NaN`` mask elements count as non-zero and select *a*.
        :param a: A scalar (including ``bool``), a list/tuple of numbers
            (coerced to a ``1xN`` row matrix), or a :class:`Matrix` matching
            *mask*'s shape.
        :param b: A scalar (including ``bool``), a list/tuple of numbers, or
            a :class:`Matrix` matching *mask*'s shape.
        :return: A new :class:`Matrix` with *mask*'s shape.
        :raises TypeError: if *a* or *b* is neither a matrix, a scalar, nor
            a list/tuple of numbers, or is a list/tuple holding a non-number.
        :raises ValueError: if a matrix (or coerced sequence) operand's
            shape differs from *mask*'s shape, or is an empty list/tuple. A
            ``1x1`` matrix is treated as a matrix (not a scalar) and so must
            match the mask shape.
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
    def seed(cls, value: int) -> None:
        """Seed the random generator used by :meth:`normal` and :meth:`uniform`.

        :param value: The seed value.

        .. note::
           The generator is the process-global C library PRNG shared by
           every sub-interpreter, so a seed only makes subsequent draws
           reproducible when random generation stays on a single thread;
           concurrent draws interleave on the shared state.  The sequence
           is also not portable across platforms.
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

    def unwrap(self) -> T:
        """Consume and return the stored value, or re-raise a captured behavior exception.

        Mirrors Rust's ``Result::unwrap``: on success the stored value
        is returned; if the cown carries an unhandled behavior
        exception (``self.exception`` is ``True``) that exception is
        cleared from the cown and re-raised on the caller's thread.

        ``unwrap`` **consumes** the cown: the stored payload is handed
        to the caller and the cown is emptied to ``None``. The returned
        value is therefore owned by the caller, and a second
        :meth:`unwrap` returns ``None``. Consuming is what makes
        move-type values (e.g. :class:`Matrix`) usable after the call --
        the cown no longer aliases the value's single backing store, so
        the value keeps its ownership on the caller's interpreter rather
        than being released back into the cown. The emptied cown remains
        schedulable, so a fresh value may be stored into it again.

        The cown is acquired for the duration of the read, so call
        :meth:`unwrap` from the caller's thread once the runtime is
        globally quiescent -- after :func:`quiesce` or :func:`wait`, not
        merely after this cown's own producer.

        Calling :meth:`unwrap` while behaviors are still in flight
        raises :class:`RuntimeError`: reading a result before its
        producer completes would race the worker still mutating the
        cown. Call :func:`quiesce` (or :func:`wait`) first.

        :returns: The stored value when no exception is held.
        :rtype: T
        :raises BaseException: The captured exception, re-raised verbatim
            with its original type and message.
        :raises RuntimeError: If the runtime is not quiescent.
        """

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
        raises :class:`RuntimeError`.

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
        raises :class:`RuntimeError`.

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
        :class:`BaseException` propagation, :func:`pump` raises
        and no :class:`PumpResult` is returned.
    :rtype: PumpResult
    :raises TypeError: if ``deadline_ms`` or ``max_behaviors`` is
        not ``None``, a positive :class:`int`, or is :class:`bool`.
    :raises RuntimeError: wrong interpreter, concurrent pump on
        free-threaded, nested pump, no live runtime
        (:func:`start` has not been called), or watchdog raise
        threshold tripped.
    """


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


def notice_seed(key: str, value: Any) -> None:
    """Synchronously write a value to the noticeboard from the primary interpreter.

    Unlike :func:`notice_write`, this commits **before it returns**: the
    value is applied under the noticeboard mutex on the calling thread,
    so once :func:`notice_seed` returns the entry is live and visible to
    every behavior scheduled afterwards (and to the calling thread's own
    subsequent :func:`notice_read`). It is the recommended way to install
    read-mostly configuration before scheduling the behaviors that read
    it.

    If the runtime is not yet running, :func:`notice_seed` starts it,
    so seeding can be the first bocpy call a program makes — no explicit
    :func:`start` is required.

    **Primary interpreter only.** Calling :func:`notice_seed` from a
    worker raises :class:`RuntimeError`; use :func:`notice_write` for
    fire-and-forget writes from within behaviors.

    It is a plain overwrite intended for *seeding* before concurrent
    noticeboard mutations are in flight. It does **not** provide the
    read-modify-write atomicity of :func:`notice_update`, and a seed
    that races an in-flight :func:`notice_update` on the same key may be
    lost. Seed once, up front, rather than interleaving seeds with
    concurrent updates.

    :param key: The noticeboard key (max 63 UTF-8 bytes).
    :type key: str
    :param value: The value to store.
    :type value: Any
    :raises RuntimeError: If called from a worker interpreter.
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


@overload
def quiesce(timeout: Optional[float] = None, *,
            stats: Literal[False] = False,
            noticeboard: Literal[False] = False) -> None: ...


@overload
def quiesce(timeout: Optional[float] = None, *,
            stats: Literal[True],
            noticeboard: Literal[False] = False) -> list[dict]: ...


@overload
def quiesce(timeout: Optional[float] = None, *,
            stats: Literal[False] = False,
            noticeboard: Literal[True]) -> dict[str, Any]: ...


@overload
def quiesce(timeout: Optional[float] = None, *,
            stats: Literal[True],
            noticeboard: Literal[True]) -> "WaitResult": ...


def quiesce(timeout: Optional[float] = None, *,
            stats: bool = False,
            noticeboard: bool = False
            ) -> Union[None, list[dict], dict[str, Any], "WaitResult"]:
    """Block until in-flight behaviors complete **without** teardown.

    Unlike :func:`wait`, this leaves the runtime fully usable:
    workers remain running, the noticeboard thread remains
    registered, and the terminator is **not** closed. Further
    ``@when`` calls work immediately after ``quiesce()`` returns.

    Typical use is to lift a result out of a long-running parallel
    job at a defined synchronization point — e.g. a parallel search
    that periodically wants to inspect its best-so-far state — and
    then keep working. The flags mirror :func:`wait`:

    - neither flag set: returns ``None`` once the runtime is quiescent.
    - ``stats=True`` only: returns the per-worker scheduler-stats
      snapshot as ``list[dict]`` (same shape as :func:`wait`).
    - ``noticeboard=True`` only: returns a plain ``dict[str, Any]``
      with the noticeboard contents at the quiescence point.
    - both flags set: returns :class:`WaitResult`.

    The noticeboard snapshot is captured by cycling the dedicated
    mutator thread: a shutdown sentinel is enqueued on the FIFO
    ``boc_noticeboard`` tag, the thread is joined (guaranteeing
    every prior mutation has been committed), the live state is
    read, and the thread is restarted. The result is a true
    cross-interpreter point-in-time view that reflects every
    ``notice_write`` / ``notice_update`` / ``notice_delete`` posted
    by a behavior that completed before the quiesce point.

    Single-caller: like :func:`wait`, ``quiesce`` assumes one
    thread at a time on the primary interpreter. Concurrent
    ``@when`` calls from secondary threads during a ``quiesce`` are
    waited for (their behaviors are part of the quiescence
    condition); concurrent ``notice_write`` calls have undefined
    ordering with respect to the returned snapshot.

    :param timeout: Maximum seconds to wait. ``None`` means wait
        forever. The same deadline bounds both the terminator wait
        and the noticeboard-cycle join.
    :type timeout: Optional[float]
    :param stats: If ``True``, capture per-worker scheduler stats
        AFTER quiescence so the counts are stable.
    :type stats: bool
    :param noticeboard: If ``True``, capture a noticeboard snapshot
        via the thread-cycle protocol described above.
    :type noticeboard: bool
    :return: ``None`` when neither flag is set; the scheduler-stats
        list when only ``stats=True``; the noticeboard dict when
        only ``noticeboard=True``; a :class:`WaitResult` when both
        flags are set.
    :rtype: Union[None, list[dict], dict[str, Any], WaitResult]
    :raises TimeoutError: If quiescence is not reached within
        ``timeout`` (or if the noticeboard-cycle join times out).
        Unlike :func:`wait`, ``quiesce`` propagates this rather
        than swallowing it -- callers who need silent best-effort
        behavior should wrap the call.
    :raises RuntimeError: If called from a non-primary interpreter
        while pinned cowns are live (same constraint as :func:`wait`).
    """


def when(*cowns):
    """Decorator to schedule a function as a behavior using given cowns.

    This decorator takes a list of zero or more cown objects, which will be
    passed in the order in which they were provided to the decorated function.
    The function is registered at decoration time and run as a behavior once
    all the cowns are available (i.e., not acquired by other behaviors).
    Behaviors are scheduled such that deadlock will not occur.

    The function itself will be replaced by a :class:`Cown` which will hold
    the result of executing the behavior.  This :class:`Cown` can be used for
    further coordination.

    A behavior runs in a separate interpreter, so it **cannot capture values
    by closure**. Every parameter beyond the cown count must be a *capture*:
    a trailing parameter carrying a default value, which is snapshotted at
    schedule time. Use the ``x=x`` idiom to capture a surrounding value (for
    example a loop variable: ``def b(c, i=i): ...``). A bare extra parameter
    (``def b(c, factor): ...``) or a closure over a free variable raises an
    error at decoration time, naming the offending value.

    ``async def`` and generator functions are rejected — there is no event
    loop on workers to drive coroutines, and a behavior runs to completion as
    a plain function.

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

    Idempotent: if the runtime is already up, returns silently. A
    follow-up :func:`start` from a sibling code path in the same
    process is a no-op rather than an error, which makes "ensure
    the runtime is live before I :func:`notice_write`" usable as a
    one-liner without try/except scaffolding. Arguments supplied to
    a short-circuited call are **ignored**; callers who need a
    different ``worker_count`` or ``module`` must :func:`wait` /
    :func:`stop` the existing runtime first.

    :param worker_count: The number of worker interpreters to start.  If
        ``None``, defaults to the number of available cores minus one.
    :type worker_count: Optional[int]
    :param module: A tuple of the target module name and file path to
        export for worker import.  If ``None``, the caller's module will
        be used.
    :type module: Optional[tuple[str, str]]
    :raises RuntimeError: If called from a non-primary interpreter.
    """


def whencall(func: Callable[..., Any], args: list[Union[Cown, list[Cown]]], captures: list[Any]) -> Cown:
    """Schedule ``func`` as a behavior over ``args`` with ``captures``.

    ``whencall`` is the explicit escape hatch for scheduling a
    behavior without the :func:`when` decorator. ``func`` is registered
    under the marshalled-code registry's canonical key and scheduled over
    the cowns in ``args``. Passing a string raises a migration
    ``TypeError``: the old dispatch-by-thunk-name form was removed when
    ``@when`` became a runtime decorator.

    :param func: The behavior function object to schedule.
    :type func: Callable[..., Any]
    :param args: The cown arguments (or lists of cowns) to pass.
    :type args: list[Union[Cown, list[Cown]]]
    :param captures: Closed-over values to pass to the behavior.
    :type captures: list[Any]
    :return: A :class:`Cown` that will hold the behavior's return value.
    :rtype: Cown
    :raises TypeError: If ``func`` is a string (removed thunk-name form).
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
