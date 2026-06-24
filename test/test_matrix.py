"""Tests for the bocpy Matrix class using fuzzed inputs across multiple sizes."""

import copy
from fractions import Fraction
import math
import operator
import pickle
import random
import struct
import sys

import pytest

from bocpy import Cown, Matrix, quiesce, wait, when

QUIESCE_TIMEOUT = 5


def ref_fma(a, b, c):
    """Correctly-rounded ``a * b + c`` reference, matching C99 ``fma``.

    ``math.fma`` is only available on CPython 3.13+, but the suite must run
    on the supported 3.10-3.12 legs too. Exact rational arithmetic followed
    by a single round-to-nearest (``float(Fraction)`` rounds half-to-even,
    the same rule as IEEE-754) reproduces the single-rounding ``fma`` result
    bit-for-bit for finite operands whose exact ``a * b + c`` lies within
    double's range. (An exact result above ``DBL_MAX`` raises ``OverflowError``
    here where C99 ``fma`` returns an infinity; the test inputs stay well
    inside range, so this boundary never bites.)
    """
    return float(Fraction(a) * Fraction(b) + Fraction(c))


def _flatten(m):
    """Row-major list of every element in a Matrix."""
    return [m[i, j] for i in range(m.rows) for j in range(m.columns)]


MATRIX_SIZES = [
    (1, 1),
    (1, 5),
    (5, 1),
    (3, 3),
    (4, 7),
    (7, 4),
    (10, 10),
    (16, 32),
]


@pytest.fixture(params=MATRIX_SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def shape(request):
    """Parametrized fixture that yields various (rows, cols) shapes."""
    return request.param


@pytest.fixture
def rng():
    """Seeded random generator for reproducible fuzzed data."""
    return random.Random(42)


@pytest.fixture
def random_values(shape, rng):
    """List of random float values sized for the current shape."""
    rows, cols = shape
    return [rng.uniform(-100, 100) for _ in range(rows * cols)]


@pytest.fixture
def mat(shape, random_values):
    """A Matrix filled with the fuzzed random values."""
    rows, cols = shape
    return Matrix(rows, cols, random_values)


@pytest.fixture
def mat_pair(shape, rng):
    """Two independent random matrices of the same shape."""
    rows, cols = shape
    vals_a = [rng.uniform(-50, 50) for _ in range(rows * cols)]
    vals_b = [rng.uniform(-50, 50) for _ in range(rows * cols)]
    return Matrix(rows, cols, vals_a), Matrix(rows, cols, vals_b)


class TestConstruction:
    """Tests for Matrix construction and initialization."""

    def test_dimensions(self, shape):
        """Verify matrix dimensions match the given shape."""
        rows, cols = shape
        m = Matrix(rows, cols)
        assert m.rows == rows
        assert m.columns == cols

    def test_zero_init(self, shape):
        """Verify default initialization produces a zero matrix."""
        rows, cols = shape
        m = Matrix(rows, cols)
        expected = Matrix.zeros(shape)
        assert Matrix.allclose(m, expected)

    def test_scalar_init(self, shape, rng):
        """Verify scalar initialization fills the matrix uniformly."""
        rows, cols = shape
        val = rng.uniform(-100, 100)
        m = Matrix(rows, cols, val)
        expected = Matrix(rows, cols, [val] * (rows * cols))
        assert Matrix.allclose(m, expected)

    def test_list_init(self, mat, shape, random_values):
        """Verify list initialization sets all elements correctly."""
        rows, cols = shape
        assert mat.rows == rows
        assert mat.columns == cols
        for i in range(rows):
            for j in range(cols):
                assert mat[i, j] == pytest.approx(random_values[i * cols + j])

    def test_invalid_dimensions(self):
        """Verify that invalid dimensions raise AssertionError."""
        with pytest.raises(AssertionError):
            Matrix(0, 5)
        with pytest.raises(AssertionError):
            Matrix(5, 0)
        with pytest.raises(AssertionError):
            Matrix(-1, 3)

    def test_wrong_value_count(self, shape):
        """Verify that wrong number of values raises TypeError."""
        rows, cols = shape
        with pytest.raises(TypeError):
            Matrix(rows, cols, [1.0] * (rows * cols + 1))


class TestFactories:
    """Tests for factory functions (zeros, ones, normal, uniform)."""

    def test_zeros(self, shape):
        """Verify Matrix.zeros() creates a zero-filled matrix."""
        rows, cols = shape
        m = Matrix.zeros(shape)
        assert m.rows == rows
        assert m.columns == cols
        assert m.sum() == pytest.approx(0.0)

    def test_ones(self, shape):
        """Verify Matrix.ones() creates a matrix filled with ones."""
        rows, cols = shape
        m = Matrix.ones(shape)
        assert m.rows == rows
        assert m.columns == cols
        assert m.sum() == pytest.approx(rows * cols)

    def test_normal_shape(self, shape):
        """Verify Matrix.normal() produces a matrix of the given shape."""
        rows, cols = shape
        m = Matrix.normal(0.0, 1.0, size=(rows, cols))
        assert m.rows == rows
        assert m.columns == cols

    def test_uniform_shape(self, shape):
        """Verify Matrix.uniform() produces a matrix of the given shape."""
        rows, cols = shape
        m = Matrix.uniform(0.0, 1.0, size=(rows, cols))
        assert m.rows == rows
        assert m.columns == cols

    def test_normal_defaults(self):
        """Matrix.normal() with no size returns a scalar float."""
        val = Matrix.normal()
        assert isinstance(val, float)

    def test_seed_makes_uniform_reproducible(self):
        """Seeding before uniform() reproduces the same matrix."""
        Matrix.seed(12345)
        a = Matrix.uniform(0.0, 1.0, size=(4, 4))
        Matrix.seed(12345)
        b = Matrix.uniform(0.0, 1.0, size=(4, 4))
        assert Matrix.allclose(a, b)

    def test_seed_makes_normal_reproducible(self):
        """Seeding before normal() reproduces the same matrix."""
        Matrix.seed(999)
        a = Matrix.normal(0.0, 1.0, size=(5, 3))
        Matrix.seed(999)
        b = Matrix.normal(0.0, 1.0, size=(5, 3))
        assert Matrix.allclose(a, b)

    def test_different_seeds_differ(self):
        """Different seeds produce different sequences."""
        Matrix.seed(1)
        a = Matrix.uniform(0.0, 1.0, size=(8, 8))
        Matrix.seed(2)
        b = Matrix.uniform(0.0, 1.0, size=(8, 8))
        assert not Matrix.allclose(a, b)

    def test_seed_requires_argument(self):
        """seed() with no argument raises TypeError."""
        with pytest.raises(TypeError):
            Matrix.seed()


class TestIndexing:
    """Tests for element and row indexing."""

    def test_single_element_get(self, mat, shape, random_values):
        """Verify single-element read access."""
        rows, cols = shape
        for _ in range(min(rows * cols, 20)):
            r = random.randint(0, rows - 1)
            c = random.randint(0, cols - 1)
            assert mat[r, c] == pytest.approx(random_values[r * cols + c])

    def test_single_element_set(self, shape, rng):
        """Verify single-element write access."""
        rows, cols = shape
        m = Matrix.zeros(shape)
        val = rng.uniform(-999, 999)
        r, c = rng.randint(0, rows - 1), rng.randint(0, cols - 1)
        m[r, c] = val
        assert m[r, c] == pytest.approx(val)

    def test_row_slice(self, mat, shape):
        """Verify row-slicing returns a 1-row matrix or scalar."""
        rows, cols = shape
        if rows < 2:
            pytest.skip("need ≥2 rows")
        row_mat = mat[0]
        if cols == 1:
            assert isinstance(row_mat, float)
        else:
            assert row_mat.rows == 1
            assert row_mat.columns == cols

    def test_len_returns_rows(self, mat, shape):
        """Verify len() always returns the number of rows."""
        rows, cols = shape
        assert len(mat) == rows


class TestArithmetic:
    """Tests for element-wise arithmetic operators."""

    def test_add_matrices(self, mat_pair, shape):
        """Verify element-wise matrix addition."""
        a, b = mat_pair
        c = a + b
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(a[i, j] + b[i, j])

    def test_subtract_matrices(self, mat_pair, shape):
        """Verify element-wise matrix subtraction."""
        a, b = mat_pair
        c = a - b
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(a[i, j] - b[i, j])

    def test_elementwise_multiply(self, mat_pair, shape):
        """Verify element-wise matrix multiplication."""
        a, b = mat_pair
        c = a * b
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(a[i, j] * b[i, j])

    def test_elementwise_divide(self, mat_pair, shape):
        """Verify element-wise matrix division."""
        a, b = mat_pair
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                if b[i, j] == 0.0:
                    b[i, j] = 1.0
        c = a / b
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(a[i, j] / b[i, j])

    def test_scalar_add(self, mat, shape, rng):
        """Verify adding a scalar matrix to a matrix."""
        val = rng.uniform(1, 50)
        scalar_mat = Matrix(shape[0], shape[1], val)
        c = mat + scalar_mat
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(mat[i, j] + val)

    def test_scalar_multiply(self, mat, shape, rng):
        """Verify multiplying a matrix by a scalar matrix."""
        val = rng.uniform(0.1, 10)
        scalar_mat = Matrix(shape[0], shape[1], val)
        c = mat * scalar_mat
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(mat[i, j] * val)


class TestInplaceOps:
    """Tests for in-place arithmetic operators."""

    def test_iadd(self, shape, rng):
        """Verify in-place addition."""
        rows, cols = shape
        vals_a = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        vals_b = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        a = Matrix(rows, cols, vals_a)
        b = Matrix(rows, cols, vals_b)
        expected = [x + y for x, y in zip(vals_a, vals_b)]
        a += b
        for i in range(rows):
            for j in range(cols):
                assert a[i, j] == pytest.approx(expected[i * cols + j])

    def test_isub(self, shape, rng):
        """Verify in-place subtraction."""
        rows, cols = shape
        vals_a = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        vals_b = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        a = Matrix(rows, cols, vals_a)
        b = Matrix(rows, cols, vals_b)
        expected = [x - y for x, y in zip(vals_a, vals_b)]
        a -= b
        for i in range(rows):
            for j in range(cols):
                assert a[i, j] == pytest.approx(expected[i * cols + j])

    def test_imul(self, shape, rng):
        """Verify in-place multiplication."""
        rows, cols = shape
        vals_a = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        vals_b = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        a = Matrix(rows, cols, vals_a)
        b = Matrix(rows, cols, vals_b)
        expected = [x * y for x, y in zip(vals_a, vals_b)]
        a *= b
        for i in range(rows):
            for j in range(cols):
                assert a[i, j] == pytest.approx(expected[i * cols + j])

    def test_itruediv(self, shape, rng):
        """Verify in-place division."""
        rows, cols = shape
        vals_a = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        vals_b = [rng.uniform(1, 50) for _ in range(rows * cols)]
        a = Matrix(rows, cols, vals_a)
        b = Matrix(rows, cols, vals_b)
        expected = [x / y for x, y in zip(vals_a, vals_b)]
        a /= b
        for i in range(rows):
            for j in range(cols):
                assert a[i, j] == pytest.approx(expected[i * cols + j])


class TestOneByOneBroadcast:
    """A 1x1 matrix acts as a scalar and broadcasts against any shape."""

    OPS = [
        ("add", lambda a, b: a + b),
        ("sub", lambda a, b: a - b),
        ("mul", lambda a, b: a * b),
        ("div", lambda a, b: a / b),
    ]

    @pytest.mark.parametrize("name,op", OPS, ids=[o[0] for o in OPS])
    def test_matrix_op_scalar_matrix(self, name, op, mat, shape, rng):
        """``MxN op 1x1`` matches the same op against a Python float."""
        scalar = rng.uniform(1, 10)
        result = op(mat, Matrix(1, 1, scalar))
        rows, cols = shape
        assert result.rows == rows and result.columns == cols
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(op(mat[i, j], scalar))

    @pytest.mark.parametrize("name,op", OPS, ids=[o[0] for o in OPS])
    def test_scalar_matrix_op_matrix(self, name, op, mat, shape, rng):
        """``1x1 op MxN`` (1x1 on the left) keeps the reflected operand order."""
        scalar = rng.uniform(1, 10)
        result = op(Matrix(1, 1, scalar), mat)
        rows, cols = shape
        assert result.rows == rows and result.columns == cols
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(op(scalar, mat[i, j]))

    @pytest.mark.parametrize("name,op", OPS, ids=[o[0] for o in OPS])
    def test_matches_python_float_operand(self, name, op, mat, shape, rng):
        """A 1x1 operand is bit-for-bit equivalent to a Python float operand."""
        scalar = rng.uniform(1, 10)
        from_matrix = op(mat, Matrix(1, 1, scalar))
        from_float = op(mat, scalar)
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert from_matrix[i, j] == from_float[i, j]

    def test_inplace_matrix_op_scalar_matrix(self, shape, rng):
        """``MxN op= 1x1`` mutates the MxN operand in place."""
        rows, cols = shape
        vals = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        scalar = rng.uniform(1, 10)
        a = Matrix(rows, cols, vals)
        a += Matrix(1, 1, scalar)
        for i in range(rows):
            for j in range(cols):
                assert a[i, j] == pytest.approx(vals[i * cols + j] + scalar)

    def test_inplace_scalar_matrix_op_matrix_rejected(self):
        """``1x1 op= MxN`` would change the operand shape and is rejected."""
        a = Matrix(1, 1, 5.0)
        b = Matrix(2, 3, [1, 2, 3, 4, 5, 6])
        with pytest.raises(ValueError,
                           match="in-place scalar broadcast"):
            a += b

    def test_one_by_one_op_one_by_one(self):
        """``1x1 op 1x1`` stays elementwise and yields a 1x1 result."""
        a = Matrix(1, 1, 7.0)
        b = Matrix(1, 1, 2.0)
        assert (a + b)[0, 0] == pytest.approx(9.0)
        assert (a - b)[0, 0] == pytest.approx(5.0)
        assert (a * b)[0, 0] == pytest.approx(14.0)
        assert (a / b)[0, 0] == pytest.approx(3.5)


class TestMatmul:
    """Tests for matrix multiplication (@)."""

    @pytest.fixture(
        params=[(1, 1, 1), (2, 3, 4), (3, 3, 3), (5, 2, 7), (8, 16, 4)],
        ids=lambda s: f"{s[0]}x{s[1]}@{s[1]}x{s[2]}",
    )
    def matmul_pair(self, request, rng):
        """Fixture providing two compatible matrices for multiplication."""
        m, k, n = request.param
        vals_a = [rng.uniform(-10, 10) for _ in range(m * k)]
        vals_b = [rng.uniform(-10, 10) for _ in range(k * n)]
        return Matrix(m, k, vals_a), Matrix(k, n, vals_b), m, k, n

    def test_matmul_shape(self, matmul_pair):
        """Verify result shape of matrix multiplication."""
        a, b, m, k, n = matmul_pair
        c = a @ b
        assert c.rows == m
        assert c.columns == n

    def test_matmul_values(self, matmul_pair):
        """Verify element values of matrix multiplication."""
        a, b, m, k, n = matmul_pair
        c = a @ b
        for i in range(m):
            for j in range(n):
                expected = sum(a[i, p] * b[p, j] for p in range(k))
                assert c[i, j] == pytest.approx(expected, rel=1e-9)

    def test_matmul_bitwise_reproducible(self):
        """matmul is deterministic and accumulates k in ascending order.

        Guards the ikj loop reorder: the inner product for each output
        element must still sum ``p = 0..k-1`` in order, so the result is
        bit-for-bit identical to an ascending-p Python reference and to a
        repeat run. A future loop reorder that changed accumulation order
        would perturb the low bits and trip this test.
        """
        rng = random.Random(0xBEEF)
        m, k, n = 6, 7, 5
        vals_a = [rng.uniform(-10, 10) for _ in range(m * k)]
        vals_b = [rng.uniform(-10, 10) for _ in range(k * n)]
        a = Matrix(m, k, vals_a)
        b = Matrix(k, n, vals_b)

        c1 = a @ b
        c2 = a @ b

        def bits(x):
            return struct.pack("<d", x)

        for i in range(m):
            for j in range(n):
                acc = 0.0
                for p in range(k):
                    acc += vals_a[i * k + p] * vals_b[p * n + j]
                assert bits(c1[i, j]) == bits(acc)
                assert bits(c2[i, j]) == bits(c1[i, j])


@pytest.mark.parametrize("in_place_mode", [False, True], ids=["copy", "in_place"])
class TestTranspose:
    """Tests for matrix transpose operations.

    Class-level parametrize runs every test against both the copy form
    (``transpose()``) and the self-mutating form (``transpose(in_place=True)``).
    """

    def test_transpose_shape(self, mat, shape, in_place_mode):
        """Verify transposed shape swaps rows and columns."""
        t = mat.transpose(in_place=in_place_mode)
        assert t.rows == shape[1]
        assert t.columns == shape[0]

    def test_transpose_values(self, shape, random_values, in_place_mode):
        """Verify transposed element positions."""
        rows, cols = shape
        m = Matrix(rows, cols, random_values)
        t = m.transpose(in_place=in_place_mode)
        for i in range(rows):
            for j in range(cols):
                assert t[j, i] == pytest.approx(random_values[i * cols + j])

    def test_double_transpose(self, shape, random_values, in_place_mode):
        """Verify double transpose returns the original values."""
        rows, cols = shape
        m = Matrix(rows, cols, random_values)
        tt = m.transpose(in_place=in_place_mode).transpose(in_place=in_place_mode)
        for i in range(rows):
            for j in range(cols):
                assert tt[i, j] == pytest.approx(random_values[i * cols + j])

    def test_return_value_contract(self, shape, random_values, in_place_mode):
        """Copy form returns a new Matrix and preserves the input; in-place returns self."""
        rows, cols = shape
        m = Matrix(rows, cols, random_values)
        result = m.transpose(in_place=in_place_mode)
        if in_place_mode:
            assert result is m
        else:
            assert result is not m
            for i in range(rows):
                for j in range(cols):
                    assert m[i, j] == pytest.approx(random_values[i * cols + j])


class TestAggregation:
    """Tests for sum, mean, and magnitude aggregations."""

    def test_sum_total(self, mat, random_values):
        """Verify total sum of all elements."""
        assert mat.sum() == pytest.approx(sum(random_values))

    def test_sum_axis0(self, mat, shape, random_values):
        """sum(axis=0) → 1 x cols vector (column sums)."""
        s = mat.sum(0)
        rows, cols = shape
        assert s.rows == 1
        assert s.columns == cols
        for j in range(cols):
            expected = sum(random_values[i * cols + j] for i in range(rows))
            assert s[0, j] == pytest.approx(expected)

    def test_sum_axis1(self, mat, shape, random_values):
        """sum(axis=1) → rows x 1 vector (row sums)."""
        s = mat.sum(1)
        rows, cols = shape
        assert s.rows == rows
        assert s.columns == 1
        for i in range(rows):
            expected = sum(random_values[i * cols + j] for j in range(cols))
            assert s[i, 0] == pytest.approx(expected)

    def test_mean_total(self, mat, shape, random_values):
        """Verify total mean of all elements."""
        rows, cols = shape
        expected = sum(random_values) / (rows * cols)
        assert mat.mean() == pytest.approx(expected)

    def test_mean_axis0(self, mat, shape, random_values):
        """Verify mean along axis 0 (column means)."""
        m = mat.mean(0)
        rows, cols = shape
        assert m.rows == 1
        assert m.columns == cols
        for j in range(cols):
            expected = sum(random_values[i * cols + j] for i in range(rows)) / rows
            assert m[0, j] == pytest.approx(expected)

    def test_mean_axis1(self, mat, shape, random_values):
        """Verify mean along axis 1 (row means)."""
        m = mat.mean(1)
        rows, cols = shape
        assert m.rows == rows
        assert m.columns == 1
        for i in range(rows):
            expected = sum(random_values[i * cols + j] for j in range(cols)) / cols
            assert m[i, 0] == pytest.approx(expected)

    def test_magnitude_total(self, mat, random_values):
        """Verify total magnitude (Frobenius norm)."""
        expected = math.sqrt(sum(v * v for v in random_values))
        assert mat.magnitude() == pytest.approx(expected)

    def test_magnitude_axis0(self, mat, shape, random_values):
        """Verify magnitude along axis 0."""
        mag = mat.magnitude(0)
        rows, cols = shape
        assert mag.rows == 1
        assert mag.columns == cols
        for j in range(cols):
            expected = math.sqrt(
                sum(random_values[i * cols + j] ** 2 for i in range(rows))
            )
            assert mag[0, j] == pytest.approx(expected)

    def test_magnitude_axis1(self, mat, shape, random_values):
        """Verify magnitude along axis 1."""
        mag = mat.magnitude(1)
        rows, cols = shape
        assert mag.rows == rows
        assert mag.columns == 1
        for i in range(rows):
            expected = math.sqrt(
                sum(random_values[i * cols + j] ** 2 for j in range(cols))
            )
            assert mag[i, 0] == pytest.approx(expected)


class TestMagnitudeSquared:
    """Tests for `magnitude_squared(axis=None)`."""

    def test_total(self, mat, random_values):
        """Sum of squared elements without the sqrt."""
        expected = sum(v * v for v in random_values)
        assert mat.magnitude_squared() == pytest.approx(expected)

    def test_axis0(self, mat, shape, random_values):
        """Squared magnitudes along axis 0 (per column)."""
        ms = mat.magnitude_squared(0)
        rows, cols = shape
        assert ms.rows == 1
        assert ms.columns == cols
        for j in range(cols):
            expected = sum(random_values[i * cols + j] ** 2 for i in range(rows))
            assert ms[0, j] == pytest.approx(expected)

    def test_axis1(self, mat, shape, random_values):
        """Squared magnitudes along axis 1 (per row)."""
        ms = mat.magnitude_squared(1)
        rows, cols = shape
        assert ms.rows == rows
        assert ms.columns == 1
        for i in range(rows):
            expected = sum(random_values[i * cols + j] ** 2 for j in range(cols))
            assert ms[i, 0] == pytest.approx(expected)

    def test_matches_magnitude_squared(self, mat, shape):
        """`magnitude_squared(k)` == `magnitude(k) ** 2` for each axis."""
        assert mat.magnitude_squared() == pytest.approx(mat.magnitude() ** 2)
        for axis in (0, 1):
            ms = mat.magnitude_squared(axis)
            mg = mat.magnitude(axis)
            rows, cols = ms.rows, ms.columns
            for i in range(rows):
                for j in range(cols):
                    assert ms[i, j] == pytest.approx(mg[i, j] ** 2)

    def test_invalid_axis_raises(self, mat):
        """Out-of-range axis surfaces the same error as `magnitude`."""
        with pytest.raises(ValueError, match="axis must be -2, -1, 0, or 1"):
            mat.magnitude_squared(2)


class TestLengthProperty:
    """Tests for the read-only `length` property."""

    def test_length_matches_magnitude(self, mat):
        """`mat.length` matches `mat.magnitude()` across the shape fixture."""
        assert mat.length == pytest.approx(mat.magnitude())

    def test_length_row_vector(self):
        """`[3, 4]` row vector has length 5.0."""
        v = Matrix(1, 2, [3.0, 4.0])
        assert v.length == pytest.approx(5.0)

    def test_length_column_vector(self):
        """`[3, 4]` column vector has length 5.0."""
        v = Matrix(2, 1, [3.0, 4.0])
        assert v.length == pytest.approx(5.0)

    def test_length_zero_matrix(self, shape):
        """A freshly-zeroed Matrix has length 0.0."""
        rows, cols = shape
        m = Matrix(rows, cols)
        assert m.length == 0.0

    def test_length_is_property_not_method(self):
        """`length` is a data descriptor; reading returns a float, not a method."""
        import types
        assert isinstance(Matrix.length, types.GetSetDescriptorType)
        assert isinstance(Matrix(1, 1).length, float)
        with pytest.raises(TypeError):
            Matrix(1, 1).length()  # type: ignore[operator]


class TestVecdot:
    """Tests for `vecdot(other, axis=None)`."""

    def test_same_shape_total_golden(self):
        """1x3 . 1x3 axis=None matches hand-computed sum-of-products."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        assert a.vecdot(b) == pytest.approx(32.0)

    def test_same_shape_axis0_golden(self):
        """1x3 . 1x3 axis=0 produces a 1xN element-wise product (one row)."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        out = a.vecdot(b, 0)
        assert out.rows == 1 and out.columns == 3
        for j, want in enumerate([4.0, 10.0, 18.0]):
            assert out[0, j] == pytest.approx(want)

    def test_same_shape_axis1_golden(self):
        """1x3 . 1x3 axis=1 produces a 1x1 sum (one row)."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        out = a.vecdot(b, 1)
        assert out.rows == 1 and out.columns == 1
        assert out[0, 0] == pytest.approx(32.0)

    def test_row_broadcast_total_golden(self):
        """2x3 . 1x3 axis=None sums per-row dot products."""
        m = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        rv = Matrix(1, 3, [2.0, 3.0, 4.0])
        assert m.vecdot(rv) == pytest.approx(67.0)

    def test_row_broadcast_axis0_golden(self):
        """2x3 . 1x3 axis=0 sums each column after multiplying by the broadcast row."""
        m = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        rv = Matrix(1, 3, [2.0, 3.0, 4.0])
        out = m.vecdot(rv, 0)
        assert (out.rows, out.columns) == (1, 3)
        for j, want in enumerate([10.0, 21.0, 36.0]):
            assert out[0, j] == pytest.approx(want)

    def test_row_broadcast_axis1_golden(self):
        """2x3 . 1x3 axis=1 produces per-row dot products in an Mx1 column."""
        m = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        rv = Matrix(1, 3, [2.0, 3.0, 4.0])
        out = m.vecdot(rv, 1)
        assert (out.rows, out.columns) == (2, 1)
        for i, want in enumerate([20.0, 47.0]):
            assert out[i, 0] == pytest.approx(want)

    def test_col_broadcast_total_golden(self):
        """2x3 . 2x1 axis=None scales each row by its scalar and sums."""
        m = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        cv = Matrix(2, 1, [10.0, 20.0])
        assert m.vecdot(cv) == pytest.approx(360.0)

    def test_col_broadcast_axis0_golden(self):
        """2x3 . 2x1 axis=0 sums per-column with the row scalar."""
        m = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        cv = Matrix(2, 1, [10.0, 20.0])
        out = m.vecdot(cv, 0)
        assert (out.rows, out.columns) == (1, 3)
        for j, want in enumerate([90.0, 120.0, 150.0]):
            assert out[0, j] == pytest.approx(want)

    def test_col_broadcast_axis1_golden(self):
        """2x3 . 2x1 axis=1 produces per-row sum scaled by the column scalar."""
        m = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        cv = Matrix(2, 1, [10.0, 20.0])
        out = m.vecdot(cv, 1)
        assert (out.rows, out.columns) == (2, 1)
        for i, want in enumerate([60.0, 300.0]):
            assert out[i, 0] == pytest.approx(want)

    def test_vector_vector_same_orientation(self):
        """1xN . 1xN returns a scalar."""
        a = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(1, 4, [5.0, 6.0, 7.0, 8.0])
        assert a.vecdot(b) == pytest.approx(70.0)

    def test_vector_vector_mixed_orientation(self):
        """1xN . Nx1 walks the flat buffers and returns a scalar."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        c = Matrix(3, 1, [1.0, 2.0, 3.0])
        assert a.vecdot(c) == pytest.approx(14.0)

    def test_vector_length_mismatch_raises(self):
        """Mismatched vector lengths surface the dimension-mismatch error."""
        a = Matrix(1, 2, [1.0, 2.0])
        b = Matrix(1, 3, [3.0, 4.0, 5.0])
        with pytest.raises(ValueError, match=r"vecdot: lhs \d+x\d+ incompatible with rhs \d+x\d+"):
            a.vecdot(b)

    def test_incompatible_matrix_shapes_raises(self):
        """Incompatible matrix shapes (no broadcast match) surface the same error."""
        a = Matrix(2, 3, [1.0] * 6)
        b = Matrix(4, 5, [1.0] * 20)
        with pytest.raises(ValueError, match=r"vecdot: lhs \d+x\d+ incompatible with rhs \d+x\d+"):
            a.vecdot(b)

    def test_invalid_axis_raises(self):
        """Out-of-range axis surfaces the same error as `magnitude`."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        with pytest.raises(ValueError, match="axis must be -2, -1, 0, or 1"):
            a.vecdot(b, 2)

    def test_axis_wrong_type_raises(self):
        """Non-int, non-None `axis` surfaces a TypeError."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        with pytest.raises(TypeError, match="axis must be an int or None"):
            a.vecdot(b, axis="hello")

    def test_axis_none_returns_float(self):
        """`vecdot(..., axis=None)` returns a Python float."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        assert isinstance(a.vecdot(b), float)

    def test_axis_int_returns_matrix(self):
        """`vecdot(..., axis=0|1)` returns a Matrix."""
        a = Matrix(2, 3, [1.0] * 6)
        b = Matrix(2, 3, [1.0] * 6)
        assert isinstance(a.vecdot(b, 0), Matrix)
        assert isinstance(a.vecdot(b, 1), Matrix)

    def test_equivalence_axis_none(self, mat_pair):
        """`a.vecdot(b)` equals `(a * b).sum()` for any same-shape pair."""
        a, b = mat_pair
        assert a.vecdot(b) == pytest.approx((a * b).sum())

    def test_equivalence_axis0(self, mat_pair):
        """`a.vecdot(b, 0)` equals `(a * b).sum(0)` columnwise."""
        a, b = mat_pair
        ax = a.vecdot(b, 0)
        ref = (a * b).sum(0)
        for j in range(ax.columns):
            assert ax[0, j] == pytest.approx(ref[0, j])

    def test_equivalence_axis1(self, mat_pair):
        """`a.vecdot(b, 1)` equals `(a * b).sum(1)` rowwise."""
        a, b = mat_pair
        ax = a.vecdot(b, 1)
        ref = (a * b).sum(1)
        for i in range(ax.rows):
            assert ax[i, 0] == pytest.approx(ref[i, 0])

    def test_vecdot_commutative_row_broadcast(self):
        """`mat.vecdot(row_vec) == row_vec.vecdot(mat)` across all axes."""
        m = Matrix(3, 4, list(range(12)))
        rv = Matrix(1, 4, [0.5, 1.5, 2.5, 3.5])
        assert m.vecdot(rv) == pytest.approx(rv.vecdot(m))
        for axis in (0, 1):
            a, b = m.vecdot(rv, axis), rv.vecdot(m, axis)
            assert (a.rows, a.columns) == (b.rows, b.columns)
            for i in range(a.rows):
                for j in range(a.columns):
                    assert a[i, j] == pytest.approx(b[i, j])

    def test_vecdot_commutative_col_broadcast(self):
        """`mat.vecdot(col_vec) == col_vec.vecdot(mat)` across all axes."""
        m = Matrix(3, 4, list(range(12)))
        cv = Matrix(3, 1, [10.0, 20.0, 30.0])
        assert m.vecdot(cv) == pytest.approx(cv.vecdot(m))
        for axis in (0, 1):
            a, b = m.vecdot(cv, axis), cv.vecdot(m, axis)
            assert (a.rows, a.columns) == (b.rows, b.columns)
            for i in range(a.rows):
                for j in range(a.columns):
                    assert a[i, j] == pytest.approx(b[i, j])

    def test_vecdot_does_not_free_self_impl(self):
        """Repeated calls do not drop `self->impl`'s C-internal refcount.

        The hazard is on ``matrix_impl->rc`` (a C atomic counter), NOT on
        ``Py_REFCNT(mat)``. ``sys.getrefcount`` cannot see it. The
        probe-by-use pattern: drive enough calls that an extra
        IMPL_DECREF(self->impl) would zero the counter, then read from
        the matrix and confirm the result is sane (a freed impl would
        crash or return garbage).
        """
        mat = Matrix(1, 3, [1.0, 2.0, 3.0])
        other = Matrix(1, 3, [4.0, 5.0, 6.0])
        for _ in range(100):
            mat.vecdot(other)
            mat.vecdot(other, axis=0)
            mat.vecdot(other, axis=1)
        assert (mat.rows, mat.columns) == (1, 3)
        assert mat.magnitude() == pytest.approx(math.sqrt(14.0))

    def test_vecdot_keyword_axis_matches_positional(self):
        """`mat.vecdot(other, axis=1)` matches `mat.vecdot(other, 1)`."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        assert a.vecdot(b, axis=1)[0, 0] == pytest.approx(a.vecdot(b, 1)[0, 0])

    def test_vecdot_keyword_axis_none_matches_default(self):
        """Explicit `axis=None` matches the no-axis default."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        assert a.vecdot(b, axis=None) == pytest.approx(a.vecdot(b))


class TestFma:
    """Tests for `fma(b, c, /, in_place=False)` single-rounding multiply-add."""

    def test_exact_small(self):
        """A hand-computed 2x2 matches a single-rounding fma cell-by-cell."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        c = Matrix(2, 2, [0.5, 0.5, 0.5, 0.5])
        out = a.fma(b, c)
        for i in range(2):
            for j in range(2):
                assert out[i, j] == ref_fma(a[i, j], b[i, j], c[i, j])

    def test_parity_same_shape(self, shape, rng):
        """fma(b, c) is close to a*b + c for same-shape matrix operands."""
        rows, cols = shape
        n = rows * cols
        a = Matrix(rows, cols, [rng.uniform(-100, 100) for _ in range(n)])
        b = Matrix(rows, cols, [rng.uniform(-100, 100) for _ in range(n)])
        c = Matrix(rows, cols, [rng.uniform(-100, 100) for _ in range(n)])
        # One rounding vs two: compare with allclose, never ==.
        assert Matrix.allclose(a.fma(b, c), a * b + c)

    def test_exact_matches_math_fma_fuzz(self, shape, rng):
        """Every cell of fma(b, c) equals a single-rounding reference fma."""
        rows, cols = shape
        n = rows * cols
        av = [rng.uniform(-100, 100) for _ in range(n)]
        bv = [rng.uniform(-100, 100) for _ in range(n)]
        cv = [rng.uniform(-100, 100) for _ in range(n)]
        a = Matrix(rows, cols, av)
        b = Matrix(rows, cols, bv)
        c = Matrix(rows, cols, cv)
        out = a.fma(b, c)
        for i in range(rows):
            for j in range(cols):
                k = i * cols + j
                assert out[i, j] == ref_fma(av[k], bv[k], cv[k])

    def test_scalar_both(self):
        """Scalar b and c broadcast across every cell."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        out = a.fma(2.0, 1.0)
        for i in range(2):
            for j in range(2):
                assert out[i, j] == ref_fma(a[i, j], 2.0, 1.0)

    def test_scalar_1x1_is_scalar(self):
        """A 1x1 matrix operand broadcasts like a scalar."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(1, 1, [2.0])
        c = Matrix(1, 1, [1.0])
        out = a.fma(b, c)
        for i in range(2):
            for j in range(2):
                assert out[i, j] == ref_fma(a[i, j], 2.0, 1.0)

    def test_headline_mx_plus_b(self):
        """The m*x + b headline form: full multiplier, scalar addend."""
        m = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        x = Matrix(2, 3, [2.0, 2.0, 2.0, 3.0, 3.0, 3.0])
        out = m.fma(x, 1.0)
        assert Matrix.allclose(out, m * x + 1.0)

    def test_mixed_scalar_b_matrix_c(self):
        """A scalar b with a matrix c routes through the one kernel."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        c = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        out = a.fma(2.0, c)
        for i in range(2):
            for j in range(2):
                assert out[i, j] == ref_fma(a[i, j], 2.0, c[i, j])

    def test_mixed_matrix_b_scalar_c(self):
        """A matrix b with a scalar c routes through the one kernel."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        out = a.fma(b, 3.0)
        for i in range(2):
            for j in range(2):
                assert out[i, j] == ref_fma(a[i, j], b[i, j], 3.0)

    def test_in_place_returns_self(self):
        """in_place=True writes into self and returns it; b/c unchanged."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        c = Matrix(2, 2, [0.5, 0.5, 0.5, 0.5])
        b_before = [b[i, j] for i in range(2) for j in range(2)]
        c_before = [c[i, j] for i in range(2) for j in range(2)]
        expected = [ref_fma(a[i, j], b[i, j], c[i, j])
                    for i in range(2) for j in range(2)]
        out = a.fma(b, c, in_place=True)
        assert out is a
        assert [a[i, j] for i in range(2) for j in range(2)] == expected
        assert [b[i, j] for i in range(2) for j in range(2)] == b_before
        assert [c[i, j] for i in range(2) for j in range(2)] == c_before

    def test_in_place_keyword_matches_copy(self, shape, rng):
        """in_place result equals the copy result, leaving operands intact."""
        rows, cols = shape
        n = rows * cols
        av = [rng.uniform(-50, 50) for _ in range(n)]
        a_copy = Matrix(rows, cols, av)
        a_ip = Matrix(rows, cols, av)
        b = Matrix(rows, cols, [rng.uniform(-50, 50) for _ in range(n)])
        c = Matrix(rows, cols, [rng.uniform(-50, 50) for _ in range(n)])
        copied = a_copy.fma(b, c)
        a_ip.fma(b, c, in_place=True)
        assert Matrix.allclose(a_ip, copied)

    def test_rejects_broadcast_b(self):
        """A b matrix of incompatible shape raises ValueError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        bad = Matrix(3, 3, [float(i) for i in range(9)])
        with pytest.raises(ValueError,
                           match=r"fma: b shape 3x3 incompatible with self 2x2"):
            a.fma(bad, 1.0)

    def test_rejects_broadcast_c(self):
        """A c matrix of incompatible shape raises ValueError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        bad = Matrix(1, 3, [1.0, 2.0, 3.0])
        with pytest.raises(ValueError,
                           match=r"fma: c shape 1x3 incompatible with self 2x2"):
            a.fma(2.0, bad)

    def test_rejects_broadcast_leaves_self_unmodified(self):
        """A rejected operand allocates nothing and leaves self untouched."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        before = [a[i, j] for i in range(2) for j in range(2)]
        bad = Matrix(3, 3, [float(i) for i in range(9)])
        with pytest.raises(ValueError):
            a.fma(2.0, bad, in_place=True)
        assert [a[i, j] for i in range(2) for j in range(2)] == before

    def test_rejects_bad_type_b(self):
        """A non-numeric, non-matrix b raises TypeError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(TypeError,
                           match=r"fma: b must be a Matrix or a real number"):
            a.fma("x", 1.0)

    def test_rejects_bad_type_c(self):
        """A non-numeric, non-matrix c raises TypeError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(TypeError,
                           match=r"fma: c must be a Matrix or a real number"):
            a.fma(1.0, object())

    def test_operand_aliases_self(self, shape, rng):
        """fma(a, c), fma(b, a) and fma(a, a) read each cell only once."""
        rows, cols = shape
        n = rows * cols
        av = [rng.uniform(-20, 20) for _ in range(n)]
        cv = [rng.uniform(-20, 20) for _ in range(n)]
        a1 = Matrix(rows, cols, av)
        c1 = Matrix(rows, cols, cv)
        # b aliases self.
        assert Matrix.allclose(a1.fma(a1, c1), a1 * a1 + c1)
        a2 = Matrix(rows, cols, av)
        # c aliases self.
        assert Matrix.allclose(a2.fma(c1, a2), c1 * a2 + a2)
        a3 = Matrix(rows, cols, av)
        # both operands alias self, in place.
        ref = Matrix(rows, cols, av)
        out = a3.fma(a3, a3, in_place=True)
        assert out is a3
        assert Matrix.allclose(a3, ref * ref + ref)

    def test_keyword_in_place_false_is_copy(self):
        """Explicit in_place=False returns a distinct matrix."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        before = [a[i, j] for i in range(2) for j in range(2)]
        out = a.fma(2.0, 1.0, in_place=False)
        assert out is not a
        assert [a[i, j] for i in range(2) for j in range(2)] == before

    def test_boc_roundtrip(self):
        """fma runs inside a @when behavior over a Cown[Matrix]."""
        a = Cown(Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]))
        b = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        c = Matrix(2, 2, [0.5, 1.5, 2.5, 3.5])

        @when(a)
        def result(a, b=b, c=c):  # noqa: D401 — short behavior
            """Compute fma inside a behavior and return the result."""
            return a.value.fma(b, c)

        wait()
        assert result.exception is False
        expected = Matrix(2, 2, [ref_fma(v, w, x) for v, w, x in zip(
            [1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0],
            [0.5, 1.5, 2.5, 3.5])])
        assert Matrix.allclose(result.value, expected)

    def test_not_acquired_raises(self):
        """fma on a cown-resident (unacquired) matrix raises RuntimeError."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        Cown(m)
        with pytest.raises(RuntimeError):
            m.fma(2.0, 1.0)

    def test_operand_not_acquired_raises(self):
        """An unacquired matrix operand raises RuntimeError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        Cown(b)
        with pytest.raises(RuntimeError):
            a.fma(b, 1.0)


class TestScaledAdd:
    """Tests for `scaled_add(s, x, /, in_place=False)` two-rounding add.

    The reference is plain Python ``y + s * x``: CPython floats are IEEE-754
    doubles, so ``s * x`` rounds once and ``y + (s * x)`` rounds again -- the
    exact two-rounding semantics scaled_add guarantees (the build sets
    ``-ffp-contract=off`` so the C kernel never fuses), hence the cell-by-cell
    comparisons use ``==``, not allclose.
    """

    def test_exact_small(self):
        """A hand-computed 2x2 matches two-rounding self + s*x cell-by-cell."""
        y = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        x = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        out = y.scaled_add(2.0, x)
        for i in range(2):
            for j in range(2):
                assert out[i, j] == y[i, j] + 2.0 * x[i, j]

    def test_scalar_fuzz(self, shape, rng):
        """A scalar s broadcasts; every cell equals y + s*x exactly."""
        rows, cols = shape
        n = rows * cols
        yv = [rng.uniform(-100, 100) for _ in range(n)]
        xv = [rng.uniform(-100, 100) for _ in range(n)]
        s = rng.uniform(-10, 10)
        y = Matrix(rows, cols, yv)
        x = Matrix(rows, cols, xv)
        out = y.scaled_add(s, x)
        for i in range(rows):
            for j in range(cols):
                k = i * cols + j
                assert out[i, j] == yv[k] + s * xv[k]

    def test_1x1_s_is_scalar(self):
        """A 1x1 matrix scale broadcasts like a number."""
        y = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        x = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        out = y.scaled_add(Matrix(1, 1, [2.0]), x)
        for i in range(2):
            for j in range(2):
                assert out[i, j] == y[i, j] + 2.0 * x[i, j]

    def test_same_shape_s_fuzz(self, shape, rng):
        """A same-shape matrix scale multiplies element-wise."""
        rows, cols = shape
        n = rows * cols
        yv = [rng.uniform(-100, 100) for _ in range(n)]
        xv = [rng.uniform(-100, 100) for _ in range(n)]
        sv = [rng.uniform(-10, 10) for _ in range(n)]
        y = Matrix(rows, cols, yv)
        x = Matrix(rows, cols, xv)
        out = y.scaled_add(Matrix(rows, cols, sv), x)
        for i in range(rows):
            for j in range(cols):
                k = i * cols + j
                assert out[i, j] == yv[k] + sv[k] * xv[k]

    def test_row_vector_s_broadcasts(self):
        """A 1xN row-vector scale broadcasts down the rows."""
        yv = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        xv = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
        sv = [2.0, 3.0, 4.0]
        y = Matrix(2, 3, yv)
        x = Matrix(2, 3, xv)
        out = y.scaled_add(Matrix(1, 3, sv), x)
        for i in range(2):
            for j in range(3):
                k = i * 3 + j
                assert out[i, j] == yv[k] + sv[j] * xv[k]

    def test_col_vector_s_broadcasts(self):
        """An Mx1 column-vector scale broadcasts across the columns."""
        yv = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        xv = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
        sv = [2.0, 3.0]
        y = Matrix(2, 3, yv)
        x = Matrix(2, 3, xv)
        out = y.scaled_add(Matrix(2, 1, sv), x)
        for i in range(2):
            for j in range(3):
                k = i * 3 + j
                assert out[i, j] == yv[k] + sv[i] * xv[k]

    def test_distinct_from_fma(self):
        """Two roundings differ from fma's single rounding on a crafted input.

        With ``s = x = 1 + 2**-27`` and ``y = -1``, the exact ``s * x`` is
        ``1 + 2**-26 + 2**-54``; the first rounding drops the ``2**-54`` bit, so
        ``y + round(s*x) == 2**-26``. The fused ``x*s + y`` rounds once and
        keeps the bit, landing on ``2**-26 + 2**-54``.
        """
        s = 1.0 + 2.0 ** -27
        xval = 1.0 + 2.0 ** -27
        yval = -1.0
        y = Matrix(1, 1, [yval])
        x = Matrix(1, 1, [xval])
        two_rounding = y.scaled_add(s, x)
        one_rounding = x.fma(s, y)  # x*s + y, single rounding
        assert two_rounding[0, 0] == yval + s * xval
        assert two_rounding[0, 0] == 2.0 ** -26
        assert one_rounding[0, 0] == 2.0 ** -26 + 2.0 ** -54
        assert two_rounding[0, 0] != one_rounding[0, 0]

    def test_in_place_returns_self(self):
        """in_place=True writes into self and returns it; s/x unchanged."""
        yv = [1.0, 2.0, 3.0, 4.0]
        xv = [10.0, 20.0, 30.0, 40.0]
        s = 2.0
        y = Matrix(2, 2, yv)
        x = Matrix(2, 2, xv)
        x_before = [x[i, j] for i in range(2) for j in range(2)]
        expected = [yv[i * 2 + j] + s * xv[i * 2 + j]
                    for i in range(2) for j in range(2)]
        out = y.scaled_add(s, x, in_place=True)
        assert out is y
        assert [y[i, j] for i in range(2) for j in range(2)] == expected
        assert [x[i, j] for i in range(2) for j in range(2)] == x_before

    def test_in_place_matches_copy(self, shape, rng):
        """in_place result equals the copy result cell-for-cell."""
        rows, cols = shape
        n = rows * cols
        yv = [rng.uniform(-50, 50) for _ in range(n)]
        y_copy = Matrix(rows, cols, yv)
        y_ip = Matrix(rows, cols, yv)
        x = Matrix(rows, cols, [rng.uniform(-50, 50) for _ in range(n)])
        s = Matrix(rows, cols, [rng.uniform(-10, 10) for _ in range(n)])
        copied = y_copy.scaled_add(s, x)
        y_ip.scaled_add(s, x, in_place=True)
        for i in range(rows):
            for j in range(cols):
                assert y_ip[i, j] == copied[i, j]

    def test_keyword_in_place_false_is_copy(self):
        """Explicit in_place=False returns a distinct matrix; self unchanged."""
        y = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        before = [y[i, j] for i in range(2) for j in range(2)]
        out = y.scaled_add(2.0, Matrix(2, 2, [5.0, 6.0, 7.0, 8.0]),
                           in_place=False)
        assert out is not y
        assert [y[i, j] for i in range(2) for j in range(2)] == before

    def test_operands_alias_self(self, shape, rng):
        """s or x aliasing self reads every cell only once."""
        rows, cols = shape
        n = rows * cols
        yv = [rng.uniform(-20, 20) for _ in range(n)]
        xv = [rng.uniform(-20, 20) for _ in range(n)]
        # s aliases self: out = y + y*x.
        y1 = Matrix(rows, cols, yv)
        x1 = Matrix(rows, cols, xv)
        out = y1.scaled_add(y1, x1)
        for i in range(rows):
            for j in range(cols):
                k = i * cols + j
                assert out[i, j] == yv[k] + yv[k] * xv[k]
        # x aliases self: out = y + s*y.
        y2 = Matrix(rows, cols, yv)
        out = y2.scaled_add(3.0, y2)
        for i in range(rows):
            for j in range(cols):
                k = i * cols + j
                assert out[i, j] == yv[k] + 3.0 * yv[k]
        # all three alias, in place: y = y + y*y.
        y3 = Matrix(rows, cols, yv)
        res = y3.scaled_add(y3, y3, in_place=True)
        assert res is y3
        for i in range(rows):
            for j in range(cols):
                k = i * cols + j
                assert y3[i, j] == yv[k] + yv[k] * yv[k]

    def test_rejects_x_shape_mismatch(self):
        """An x of the wrong shape raises ValueError naming both shapes."""
        y = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        bad = Matrix(3, 3, [float(i) for i in range(9)])
        with pytest.raises(
                ValueError,
                match=r"scaled_add: x shape 3x3 does not match self 2x2"):
            y.scaled_add(2.0, bad)

    def test_rejects_x_mismatch_leaves_self_unmodified(self):
        """A rejected x allocates nothing and leaves self untouched."""
        y = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        before = [y[i, j] for i in range(2) for j in range(2)]
        bad = Matrix(3, 3, [float(i) for i in range(9)])
        with pytest.raises(ValueError):
            y.scaled_add(2.0, bad, in_place=True)
        assert [y[i, j] for i in range(2) for j in range(2)] == before

    def test_rejects_broadcast_s(self):
        """An s matrix of incompatible shape raises ValueError."""
        y = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        x = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        bad = Matrix(1, 3, [1.0, 2.0, 3.0])
        with pytest.raises(
                ValueError,
                match=r"scaled_add: s shape 1x3 incompatible with self 2x2"):
            y.scaled_add(bad, x)

    def test_rejects_bad_type_s(self):
        """A non-numeric, non-matrix s raises TypeError."""
        y = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        x = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        with pytest.raises(
                TypeError,
                match=r"scaled_add: s must be a Matrix or a real number"):
            y.scaled_add("x", x)

    def test_boc_roundtrip(self):
        """scaled_add runs inside a @when behavior over a Cown[Matrix]."""
        y = Cown(Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]))
        x = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])

        @when(y)
        def result(y, x=x):  # noqa: D401 — short behavior
            """Compute scaled_add inside a behavior and return the result."""
            return y.value.scaled_add(2.0, x)

        wait()
        assert result.exception is False
        yv = [1.0, 2.0, 3.0, 4.0]
        xv = [10.0, 20.0, 30.0, 40.0]
        expected = Matrix(2, 2, [yv[k] + 2.0 * xv[k] for k in range(4)])
        for i in range(2):
            for j in range(2):
                assert result.value[i, j] == expected[i, j]

    def test_not_acquired_raises(self):
        """scaled_add on a cown-resident (unacquired) matrix raises."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        Cown(m)
        with pytest.raises(RuntimeError):
            m.scaled_add(2.0, Matrix(2, 2, [10.0, 20.0, 30.0, 40.0]))

    def test_operand_not_acquired_raises(self):
        """An unacquired matrix operand raises RuntimeError."""
        y = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        x = Matrix(2, 2, [10.0, 20.0, 30.0, 40.0])
        Cown(x)
        with pytest.raises(RuntimeError):
            y.scaled_add(2.0, x)


# Named binary methods (add/subtract/multiply/divide) and their operators.
_NAMED_OPS = {
    "add": operator.add,
    "subtract": operator.sub,
    "multiply": operator.mul,
    "divide": operator.truediv,
}

# Broadcastable (lhs_shape, rhs_shape) pairs the operators accept, covering
# every routing branch in Matrix_binary_op.
_BROADCAST_PAIRS = [
    ((3, 3), (3, 3)),   # same-shape element-wise
    ((3, 3), (1, 1)),   # 1x1 rhs scalar-broadcast
    ((1, 1), (3, 3)),   # 1x1 lhs scalar-broadcast
    ((3, 3), (1, 3)),   # row-vector broadcast
    ((3, 3), (3, 1)),   # column-vector broadcast
    ((3, 1), (1, 3)),   # outer product
    ((1, 3), (3, 1)),   # outer product (reflected)
]


@pytest.mark.parametrize("name", list(_NAMED_OPS))
class TestNamedBinaryMethods:
    """add/subtract/multiply/divide mirror the +/-/*// operators exactly."""

    def _operands(self, lhs_shape, rhs_shape, rng):
        """Two matrices with nonzero data (so divide never hits 0)."""
        lhs = Matrix(*lhs_shape,
                     [rng.uniform(1.0, 10.0)
                      for _ in range(lhs_shape[0] * lhs_shape[1])])
        rhs = Matrix(*rhs_shape,
                     [rng.uniform(1.0, 10.0)
                      for _ in range(rhs_shape[0] * rhs_shape[1])])
        return lhs, rhs

    @pytest.mark.parametrize("lhs_shape,rhs_shape", _BROADCAST_PAIRS)
    def test_parity_with_operator(self, name, lhs_shape, rhs_shape, rng):
        """a.<name>(b) is bit-for-bit identical to ``a <op> b``."""
        op = _NAMED_OPS[name]
        lhs, rhs = self._operands(lhs_shape, rhs_shape, rng)
        expected = op(lhs, rhs)
        actual = getattr(lhs, name)(rhs)
        assert actual is not lhs
        assert _flatten(actual) == _flatten(expected)

    def test_parity_with_scalar_operand(self, name, rng):
        """A Python scalar operand matches the operator form."""
        op = _NAMED_OPS[name]
        lhs = Matrix(2, 3, [rng.uniform(1.0, 10.0) for _ in range(6)])
        expected = op(lhs, 2.5)
        actual = getattr(lhs, name)(2.5)
        assert _flatten(actual) == _flatten(expected)

    @pytest.mark.parametrize("lhs_shape,rhs_shape", _BROADCAST_PAIRS)
    def test_out_writes_in_place_and_returns_it(self, name, lhs_shape,
                                                rhs_shape, rng):
        """out= writes the result into the target and returns that object."""
        op = _NAMED_OPS[name]
        lhs, rhs = self._operands(lhs_shape, rhs_shape, rng)
        expected = op(lhs, rhs)
        out = Matrix(expected.rows, expected.columns,
                     [0.0] * (expected.rows * expected.columns))
        result = getattr(lhs, name)(rhs, out=out)
        assert result is out
        assert _flatten(out) == _flatten(expected)

    def test_out_may_alias_an_input(self, name, rng):
        """out= aliasing an operand still produces the correct result."""
        op = _NAMED_OPS[name]
        a = Matrix(2, 2, [rng.uniform(1.0, 10.0) for _ in range(4)])
        b = Matrix(2, 2, [rng.uniform(1.0, 10.0) for _ in range(4)])
        expected = _flatten(op(a, b))
        result = getattr(a, name)(b, out=a)
        assert result is a
        assert _flatten(a) == expected

    def test_out_shape_mismatch_raises_and_leaves_target(self, name, rng):
        """A wrong-shape out= raises ValueError before any write."""
        a = Matrix(2, 2, [rng.uniform(1.0, 10.0) for _ in range(4)])
        b = Matrix(2, 2, [rng.uniform(1.0, 10.0) for _ in range(4)])
        bad = Matrix(3, 3, [7.0] * 9)
        before = _flatten(bad)
        with pytest.raises(ValueError,
                           match=r"out shape 3x3 does not match result 2x2"):
            getattr(a, name)(b, out=bad)
        assert _flatten(bad) == before

    def test_out_wrong_type_raises(self, name):
        """A non-Matrix out= raises TypeError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [5.0, 6.0, 7.0, 8.0])
        with pytest.raises(TypeError, match=r"out must be a Matrix"):
            getattr(a, name)(b, out=[0.0, 0.0, 0.0, 0.0])

    def test_out_is_keyword_only(self, name):
        """out cannot be passed positionally."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [5.0, 6.0, 7.0, 8.0])
        out = Matrix(2, 2, [0.0, 0.0, 0.0, 0.0])
        with pytest.raises(TypeError):
            getattr(a, name)(b, out)

    def test_out_on_unacquired_cown_raises(self, name):
        """An out= target resident in a cown raises RuntimeError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [5.0, 6.0, 7.0, 8.0])
        out = Matrix(2, 2, [0.0, 0.0, 0.0, 0.0])
        Cown(out)
        with pytest.raises(RuntimeError):
            getattr(a, name)(b, out=out)


class TestSequenceCoercionRegression:
    """Regression tests: a sequence operand must never be cast to a Matrix.

    A coerced sequence operand is a raw Python object, not a MatrixObject;
    set_output and the outer-product / matmul wrap paths derive the result
    type from the canonical Matrix type rather than casting the caller's
    object. Each case must return a genuine Matrix with correct values.
    """

    def test_named_add_1x1_with_list(self):
        """1x1 receiver + list operand via the named method (new surface)."""
        r = Matrix(1, 1, [5.0]).add([1.0, 2.0, 3.0])
        assert isinstance(r, Matrix)
        assert _flatten(r) == [6.0, 7.0, 8.0]

    def test_named_add_1x1_with_list_out(self):
        """1x1 receiver + list operand with an out= target (new surface)."""
        out = Matrix(1, 3, [0.0, 0.0, 0.0])
        r = Matrix(1, 1, [5.0]).add([1.0, 2.0, 3.0], out=out)
        assert r is out
        assert _flatten(r) == [6.0, 7.0, 8.0]

    def test_1x1_plus_list_operator(self):
        """1x1 receiver + list via the + operator (scalar-broadcast branch)."""
        r = Matrix(1, 1, [5.0]) + [1.0, 2.0, 3.0]
        assert isinstance(r, Matrix)
        assert _flatten(r) == [6.0, 7.0, 8.0]

    def test_list_plus_matrix_same_shape(self):
        """list + 1xN matrix via __radd__ (same-shape ewise branch)."""
        r = [1.0, 2.0, 3.0] + Matrix(1, 3, [4.0, 5.0, 6.0])
        assert isinstance(r, Matrix)
        assert _flatten(r) == [5.0, 7.0, 9.0]

    def test_list_times_column_outer(self):
        """list * column vector via __rmul__ (reflected outer-product branch)."""
        r = [1.0, 2.0, 3.0] * Matrix(3, 1, [10.0, 20.0, 30.0])
        assert isinstance(r, Matrix)
        assert r.rows == 3 and r.columns == 3
        expected = [col * row
                    for col in (10.0, 20.0, 30.0)
                    for row in (1.0, 2.0, 3.0)]
        assert _flatten(r) == expected

    def test_list_matmul_column(self):
        """list @ column vector via __rmatmul__ (matmul wrap path)."""
        r = [1.0, 2.0, 3.0] @ Matrix(3, 1, [10.0, 20.0, 30.0])
        assert isinstance(r, Matrix)
        assert r.rows == 1 and r.columns == 1
        assert r[0, 0] == 1.0 * 10.0 + 2.0 * 20.0 + 3.0 * 30.0

    def test_tuple_matmul_column(self):
        """tuple @ column vector must also wrap as Matrix, not tuple."""
        r = (1.0, 2.0, 3.0) @ Matrix(3, 1, [10.0, 20.0, 30.0])
        assert isinstance(r, Matrix)
        assert r[0, 0] == 1.0 * 10.0 + 2.0 * 20.0 + 3.0 * 30.0

    def test_list_minus_matrix_operand_order(self):
        """[seq] - matrix computes seq - matrix (not matrix - seq).

        Subtraction is non-commutative; the reflected coercion branch must
        preserve operand order.
        """
        r = [10.0, 20.0, 30.0] - Matrix(1, 3, [1.0, 2.0, 3.0])
        assert isinstance(r, Matrix)
        assert _flatten(r) == [9.0, 18.0, 27.0]

    def test_list_div_matrix_operand_order(self):
        """[seq] / matrix computes seq / matrix (not matrix / seq).

        Division is non-commutative; the reflected coercion branch must
        preserve operand order.
        """
        r = [10.0, 20.0, 30.0] / Matrix(1, 3, [2.0, 4.0, 5.0])
        assert isinstance(r, Matrix)
        assert _flatten(r) == [5.0, 5.0, 6.0]


class TestUnaryOutTarget:
    """out= on the unary methods (ceil/floor/round/negate/abs/sqrt)."""

    UNARY = {
        "negate": lambda v: -v,
        "abs": abs,
        "ceil": math.ceil,
        "floor": math.floor,
        "round": round,
        "sqrt": math.sqrt,
    }

    @pytest.mark.parametrize("name", list(UNARY))
    def test_out_writes_and_returns_target(self, name):
        """out= writes the result and returns the target object."""
        ref = self.UNARY[name]
        vals = [0.25, 1.5, 2.75, 4.0]
        m = Matrix(2, 2, vals)
        out = Matrix(2, 2, [0.0, 0.0, 0.0, 0.0])
        result = getattr(m, name)(out=out)
        assert result is out
        assert _flatten(out) == pytest.approx([ref(v) for v in vals])
        # self is untouched (out is a distinct buffer).
        assert _flatten(m) == vals

    @pytest.mark.parametrize("name", list(UNARY))
    def test_out_and_in_place_mutually_exclusive(self, name):
        """Passing both out= and in_place raises ValueError."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        out = Matrix(2, 2, [0.0, 0.0, 0.0, 0.0])
        with pytest.raises(ValueError,
                           match=r"out and in_place are mutually exclusive"):
            getattr(m, name)(True, out=out)

    @pytest.mark.parametrize("name", list(UNARY))
    def test_out_is_keyword_only(self, name):
        """out cannot be passed positionally on the unary methods."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        out = Matrix(2, 2, [0.0, 0.0, 0.0, 0.0])
        with pytest.raises(TypeError):
            getattr(m, name)(False, out)

    def test_out_shape_mismatch_raises(self):
        """A wrong-shape out= raises ValueError before any write."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        bad = Matrix(3, 3, [9.0] * 9)
        before = _flatten(bad)
        with pytest.raises(ValueError,
                           match=r"out shape 3x3 does not match result 2x2"):
            m.negate(out=bad)
        assert _flatten(bad) == before

    def test_out_wrong_type_raises(self):
        """A non-Matrix out= raises TypeError."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(TypeError, match=r"out must be a Matrix"):
            m.negate(out=[0.0, 0.0, 0.0, 0.0])

    def test_out_on_unacquired_cown_raises(self):
        """An out= target resident in a cown raises RuntimeError."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        out = Matrix(2, 2, [0.0, 0.0, 0.0, 0.0])
        Cown(out)
        with pytest.raises(RuntimeError):
            m.negate(out=out)


class TestCross:
    """Tests for the 2D / 3D ``cross`` method."""

    def test_2d_returns_float(self):
        """``[1,2].cross([3,4]) == 1*4 - 2*3 == -2.0`` and returns a float."""
        a = Matrix(1, 2, [1.0, 2.0])
        b = Matrix(1, 2, [3.0, 4.0])
        result = a.cross(b)
        assert isinstance(result, float)
        assert result == pytest.approx(-2.0)

    def test_2d_column_inputs_also_return_float(self):
        """2D cross is orientation-agnostic for ``self`` too."""
        a = Matrix(2, 1, [1.0, 2.0])
        b = Matrix(2, 1, [3.0, 4.0])
        assert a.cross(b) == pytest.approx(-2.0)

    def test_2d_other_orientation_irrelevant(self):
        """Row.cross(col) reads operands flat — result still scalar."""
        a = Matrix(1, 2, [1.0, 2.0])
        b = Matrix(2, 1, [3.0, 4.0])
        assert a.cross(b) == pytest.approx(-2.0)

    def test_2d_anticommutativity_fuzz(self):
        """``a.cross(b) == -b.cross(a)`` for random 2D pairs."""
        rng = random.Random(1234)
        for _ in range(100):
            xs = [rng.uniform(-10.0, 10.0) for _ in range(2)]
            ys = [rng.uniform(-10.0, 10.0) for _ in range(2)]
            a = Matrix(1, 2, xs)
            b = Matrix(1, 2, ys)
            assert a.cross(b) == pytest.approx(-b.cross(a))

    def test_3d_basis_ijk_identity(self):
        """i x j = k, j x k = i, k x i = j."""
        i = Matrix(1, 3, [1.0, 0.0, 0.0])
        j = Matrix(1, 3, [0.0, 1.0, 0.0])
        k = Matrix(1, 3, [0.0, 0.0, 1.0])
        for lhs, rhs, expected in [(i, j, k), (j, k, i), (k, i, j)]:
            out = lhs.cross(rhs)
            assert isinstance(out, Matrix)
            assert (out.rows, out.columns) == (1, 3)
            assert out[0, 0] == pytest.approx(expected[0, 0])
            assert out[0, 1] == pytest.approx(expected[0, 1])
            assert out[0, 2] == pytest.approx(expected[0, 2])

    def test_3d_anticommutativity_fuzz(self):
        """``a x b == -(b x a)`` element-wise for random 3D pairs."""
        rng = random.Random(5678)
        for _ in range(100):
            xs = [rng.uniform(-10.0, 10.0) for _ in range(3)]
            ys = [rng.uniform(-10.0, 10.0) for _ in range(3)]
            a = Matrix(1, 3, xs)
            b = Matrix(1, 3, ys)
            ab = a.cross(b)
            ba = b.cross(a)
            for idx in range(3):
                assert ab[0, idx] == pytest.approx(-ba[0, idx])

    def test_3d_orthogonal_to_both(self):
        """``a x b`` is orthogonal to both ``a`` and ``b`` (via vecdot)."""
        rng = random.Random(9012)
        for _ in range(50):
            xs = [rng.uniform(-10.0, 10.0) for _ in range(3)]
            ys = [rng.uniform(-10.0, 10.0) for _ in range(3)]
            a = Matrix(1, 3, xs)
            b = Matrix(1, 3, ys)
            c = a.cross(b)
            assert a.vecdot(c) == pytest.approx(0.0, abs=1e-9)
            assert b.vecdot(c) == pytest.approx(0.0, abs=1e-9)

    def test_3d_row_in_row_out(self):
        """Row vector input produces row vector output."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        out = a.cross(b)
        assert (out.rows, out.columns) == (1, 3)
        assert out[0, 0] == pytest.approx(-3.0)
        assert out[0, 1] == pytest.approx(6.0)
        assert out[0, 2] == pytest.approx(-3.0)

    def test_3d_column_in_column_out(self):
        """Column vector input produces column vector output."""
        a = Matrix(3, 1, [1.0, 2.0, 3.0])
        b = Matrix(3, 1, [4.0, 5.0, 6.0])
        out = a.cross(b)
        assert (out.rows, out.columns) == (3, 1)
        assert out[0, 0] == pytest.approx(-3.0)
        assert out[1, 0] == pytest.approx(6.0)
        assert out[2, 0] == pytest.approx(-3.0)

    def test_3d_other_orientation_irrelevant(self):
        """``other``'s orientation does not affect the result — only its data."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b_row = Matrix(1, 3, [4.0, 5.0, 6.0])
        b_col = Matrix(3, 1, [4.0, 5.0, 6.0])
        out_row = a.cross(b_row)
        out_col = a.cross(b_col)
        assert (out_row.rows, out_row.columns) == (1, 3)
        assert (out_col.rows, out_col.columns) == (1, 3)
        for idx in range(3):
            assert out_row[0, idx] == pytest.approx(out_col[0, idx])

    def test_2d_rows_batch_nx2(self):
        """3x2 self cross 3x2 other -> 3x1 column of per-row scalars."""
        a = Matrix(3, 2, [1.0, 2.0,
                          3.0, 4.0,
                          5.0, 6.0])
        b = Matrix(3, 2, [7.0, 8.0,
                          9.0, 10.0,
                          11.0, 12.0])
        out = a.cross(b)
        assert isinstance(out, Matrix)
        assert (out.rows, out.columns) == (3, 1)
        assert out[0, 0] == pytest.approx(1.0 * 8.0 - 2.0 * 7.0)
        assert out[1, 0] == pytest.approx(3.0 * 10.0 - 4.0 * 9.0)
        assert out[2, 0] == pytest.approx(5.0 * 12.0 - 6.0 * 11.0)

    def test_2d_cols_batch_2xn(self):
        """2x3 self cross 2x3 other -> 1x3 row of per-column scalars."""
        a = Matrix(2, 3, [1.0, 3.0, 5.0,
                          2.0, 4.0, 6.0])
        b = Matrix(2, 3, [7.0, 9.0, 11.0,
                          8.0, 10.0, 12.0])
        out = a.cross(b)
        assert isinstance(out, Matrix)
        assert (out.rows, out.columns) == (1, 3)
        assert out[0, 0] == pytest.approx(1.0 * 8.0 - 2.0 * 7.0)
        assert out[0, 1] == pytest.approx(3.0 * 10.0 - 4.0 * 9.0)
        assert out[0, 2] == pytest.approx(5.0 * 12.0 - 6.0 * 11.0)

    def test_2d_rows_batch_anticommutativity_fuzz(self):
        """Per-row ``a x b == -(b x a)`` on a random Nx2 batch."""
        rng = random.Random(3141)
        n = 20
        xs_a = [rng.uniform(-10.0, 10.0) for _ in range(n * 2)]
        xs_b = [rng.uniform(-10.0, 10.0) for _ in range(n * 2)]
        a = Matrix(n, 2, xs_a)
        b = Matrix(n, 2, xs_b)
        ab = a.cross(b)
        ba = b.cross(a)
        for i in range(n):
            assert ab[i, 0] == pytest.approx(-ba[i, 0])

    def test_3d_rows_batch_nx3(self):
        """3x3 row batch with axis=1 -> 3x3 of per-row cross products."""
        a = Matrix(3, 3, [1.0, 0.0, 0.0,
                          0.0, 1.0, 0.0,
                          1.0, 2.0, 3.0])
        b = Matrix(3, 3, [0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0,
                          4.0, 5.0, 6.0])
        out = a.cross(b, axis=1)
        assert (out.rows, out.columns) == (3, 3)
        assert out[0, 0] == pytest.approx(0.0)
        assert out[0, 1] == pytest.approx(0.0)
        assert out[0, 2] == pytest.approx(1.0)
        assert out[1, 0] == pytest.approx(1.0)
        assert out[1, 1] == pytest.approx(0.0)
        assert out[1, 2] == pytest.approx(0.0)
        assert out[2, 0] == pytest.approx(-3.0)
        assert out[2, 1] == pytest.approx(6.0)
        assert out[2, 2] == pytest.approx(-3.0)

    def test_3d_cols_batch_3xn(self):
        """3x3 col batch with axis=0 -> 3x3 of per-column cross products."""
        a = Matrix(3, 3, [1.0, 0.0, 1.0,
                          0.0, 1.0, 2.0,
                          0.0, 0.0, 3.0])
        b = Matrix(3, 3, [0.0, 0.0, 4.0,
                          1.0, 0.0, 5.0,
                          0.0, 1.0, 6.0])
        out = a.cross(b, axis=0)
        assert (out.rows, out.columns) == (3, 3)
        assert out[0, 0] == pytest.approx(0.0)
        assert out[1, 0] == pytest.approx(0.0)
        assert out[2, 0] == pytest.approx(1.0)
        assert out[0, 1] == pytest.approx(1.0)
        assert out[1, 1] == pytest.approx(0.0)
        assert out[2, 1] == pytest.approx(0.0)
        assert out[0, 2] == pytest.approx(-3.0)
        assert out[1, 2] == pytest.approx(6.0)
        assert out[2, 2] == pytest.approx(-3.0)

    def test_3d_rows_batch_orthogonality_fuzz(self):
        """Per-row ``(a x b) . a == 0`` and ``. b == 0`` on a random Nx3 batch."""
        rng = random.Random(2718)
        n = 30
        xs_a = [rng.uniform(-10.0, 10.0) for _ in range(n * 3)]
        xs_b = [rng.uniform(-10.0, 10.0) for _ in range(n * 3)]
        a = Matrix(n, 3, xs_a)
        b = Matrix(n, 3, xs_b)
        c = a.cross(b)
        dot_ac = a.vecdot(c, axis=1)
        dot_bc = b.vecdot(c, axis=1)
        for i in range(n):
            assert dot_ac[i, 0] == pytest.approx(0.0, abs=1e-9)
            assert dot_bc[i, 0] == pytest.approx(0.0, abs=1e-9)

    def test_3d_rows_batch_anticommutativity_fuzz(self):
        """Per-row ``a x b == -(b x a)`` on a random Nx3 batch."""
        rng = random.Random(1618)
        n = 25
        xs_a = [rng.uniform(-10.0, 10.0) for _ in range(n * 3)]
        xs_b = [rng.uniform(-10.0, 10.0) for _ in range(n * 3)]
        a = Matrix(n, 3, xs_a)
        b = Matrix(n, 3, xs_b)
        ab = a.cross(b)
        ba = b.cross(a)
        for i in range(n):
            for j in range(3):
                assert ab[i, j] == pytest.approx(-ba[i, j])

    def test_2d_rows_batch_broadcast_other_row(self):
        """Nx2 self cross 1x2 other -> Mx1 of per-row scalars (vec reused)."""
        a = Matrix(3, 2, [1.0, 2.0,
                          3.0, 4.0,
                          5.0, 6.0])
        b = Matrix(1, 2, [7.0, 8.0])
        out = a.cross(b)
        assert (out.rows, out.columns) == (3, 1)
        assert out[0, 0] == pytest.approx(1.0 * 8.0 - 2.0 * 7.0)
        assert out[1, 0] == pytest.approx(3.0 * 8.0 - 4.0 * 7.0)
        assert out[2, 0] == pytest.approx(5.0 * 8.0 - 6.0 * 7.0)

    def test_2d_rows_batch_broadcast_other_col(self):
        """Nx2 self cross 2x1 other -> same result (other orientation irrelevant)."""
        a = Matrix(3, 2, [1.0, 2.0,
                          3.0, 4.0,
                          5.0, 6.0])
        b_row = Matrix(1, 2, [7.0, 8.0])
        b_col = Matrix(2, 1, [7.0, 8.0])
        out_row = a.cross(b_row)
        out_col = a.cross(b_col)
        for i in range(3):
            assert out_col[i, 0] == pytest.approx(out_row[i, 0])

    def test_2d_cols_batch_broadcast_other_col(self):
        """2xN self cross 2x1 other -> 1xN of per-column scalars."""
        a = Matrix(2, 3, [1.0, 3.0, 5.0,
                          2.0, 4.0, 6.0])
        b = Matrix(2, 1, [7.0, 8.0])
        out = a.cross(b)
        assert (out.rows, out.columns) == (1, 3)
        assert out[0, 0] == pytest.approx(1.0 * 8.0 - 2.0 * 7.0)
        assert out[0, 1] == pytest.approx(3.0 * 8.0 - 4.0 * 7.0)
        assert out[0, 2] == pytest.approx(5.0 * 8.0 - 6.0 * 7.0)

    def test_2d_cols_batch_broadcast_other_row(self):
        """2xN self cross 1x2 other matches 2xN self cross 2x1 other."""
        a = Matrix(2, 3, [1.0, 3.0, 5.0,
                          2.0, 4.0, 6.0])
        b_row = Matrix(1, 2, [7.0, 8.0])
        b_col = Matrix(2, 1, [7.0, 8.0])
        out_row = a.cross(b_row)
        out_col = a.cross(b_col)
        for j in range(3):
            assert out_row[0, j] == pytest.approx(out_col[0, j])

    def test_3d_rows_batch_broadcast_other_row(self):
        """Nx3 self cross 1x3 other -> Nx3 of per-row crosses."""
        a = Matrix(3, 3, [1.0, 0.0, 0.0,
                          0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0])
        b = Matrix(1, 3, [1.0, 1.0, 1.0])
        out = a.cross(b, axis=1)
        assert (out.rows, out.columns) == (3, 3)
        assert out[0, 0] == pytest.approx(0.0)
        assert out[0, 1] == pytest.approx(-1.0)
        assert out[0, 2] == pytest.approx(1.0)
        assert out[1, 0] == pytest.approx(1.0)
        assert out[1, 1] == pytest.approx(0.0)
        assert out[1, 2] == pytest.approx(-1.0)
        assert out[2, 0] == pytest.approx(-1.0)
        assert out[2, 1] == pytest.approx(1.0)
        assert out[2, 2] == pytest.approx(0.0)

    def test_3d_rows_batch_broadcast_other_col(self):
        """Nx3 self cross 3x1 other matches Nx3 self cross 1x3 other."""
        a = Matrix(3, 3, [1.0, 0.0, 0.0,
                          0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0])
        b_row = Matrix(1, 3, [1.0, 1.0, 1.0])
        b_col = Matrix(3, 1, [1.0, 1.0, 1.0])
        out_row = a.cross(b_row, axis=1)
        out_col = a.cross(b_col, axis=1)
        for i in range(3):
            for j in range(3):
                assert out_col[i, j] == pytest.approx(out_row[i, j])

    def test_3d_cols_batch_broadcast_other_col(self):
        """3xN self cross 3x1 other -> 3xN matches per-column cross."""
        a = Matrix(3, 3, [1.0, 0.0, 0.0,
                          0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0])
        b = Matrix(3, 1, [1.0, 1.0, 1.0])
        out = a.cross(b, axis=0)
        assert (out.rows, out.columns) == (3, 3)
        assert out[0, 0] == pytest.approx(0.0)
        assert out[1, 0] == pytest.approx(-1.0)
        assert out[2, 0] == pytest.approx(1.0)
        assert out[0, 1] == pytest.approx(1.0)
        assert out[1, 1] == pytest.approx(0.0)
        assert out[2, 1] == pytest.approx(-1.0)
        assert out[0, 2] == pytest.approx(-1.0)
        assert out[1, 2] == pytest.approx(1.0)
        assert out[2, 2] == pytest.approx(0.0)

    def test_3d_cols_batch_broadcast_other_row(self):
        """3xN self cross 1x3 other matches 3xN self cross 3x1 other."""
        a = Matrix(3, 3, [1.0, 0.0, 0.0,
                          0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0])
        b_row = Matrix(1, 3, [1.0, 1.0, 1.0])
        b_col = Matrix(3, 1, [1.0, 1.0, 1.0])
        out_row = a.cross(b_row, axis=0)
        out_col = a.cross(b_col, axis=0)
        for i in range(3):
            for j in range(3):
                assert out_row[i, j] == pytest.approx(out_col[i, j])

    def test_broadcast_matches_per_row_loop_fuzz_3d(self):
        """Nx3 broadcast equals computing each row's cross individually."""
        rng = random.Random(91011)
        n = 25
        xs_a = [rng.uniform(-10.0, 10.0) for _ in range(n * 3)]
        xs_b = [rng.uniform(-10.0, 10.0) for _ in range(3)]
        a = Matrix(n, 3, xs_a)
        b = Matrix(1, 3, xs_b)
        out = a.cross(b)
        for i in range(n):
            row = Matrix(1, 3, [a[i, 0], a[i, 1], a[i, 2]])
            expected = row.cross(b)
            for j in range(3):
                assert out[i, j] == pytest.approx(expected[0, j])

    def test_broadcast_matches_per_row_loop_fuzz_2d(self):
        """Nx2 broadcast equals computing each row's cross individually."""
        rng = random.Random(121314)
        n = 25
        xs_a = [rng.uniform(-10.0, 10.0) for _ in range(n * 2)]
        xs_b = [rng.uniform(-10.0, 10.0) for _ in range(2)]
        a = Matrix(n, 2, xs_a)
        b = Matrix(1, 2, xs_b)
        out = a.cross(b)
        for i in range(n):
            row = Matrix(1, 2, [a[i, 0], a[i, 1]])
            assert out[i, 0] == pytest.approx(row.cross(b))

    def test_2x2_broadcast_against_1x2_other(self):
        """2x2 (default row batch) cross 1x2 other -> 2x1 of per-row scalars."""
        a = Matrix(2, 2, [1.0, 2.0,
                          3.0, 4.0])
        b = Matrix(1, 2, [5.0, 6.0])
        out = a.cross(b)
        assert (out.rows, out.columns) == (2, 1)
        assert out[0, 0] == pytest.approx(1.0 * 6.0 - 2.0 * 5.0)
        assert out[1, 0] == pytest.approx(3.0 * 6.0 - 4.0 * 5.0)

    def test_reverse_broadcast_vector_self_batch_other_raises(self):
        """Cross is anticommutative; reverse broadcast (vec.cross(batch)) is rejected."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(5, 3, [float(i) for i in range(15)])
        with pytest.raises(ValueError,
                           match=r"cross: .* incompatible with rhs \d+x\d+"):
            a.cross(b)

    def test_broadcast_wrong_size_raises(self):
        """Broadcast other must have the matching flat size (2 or 3)."""
        a = Matrix(5, 3, [float(i) for i in range(15)])
        b = Matrix(1, 2, [1.0, 2.0])
        with pytest.raises(ValueError,
                           match=r"cross: .* incompatible with rhs \d+x\d+"):
            a.cross(b)

    def test_broadcast_other_must_be_vector_raises(self):
        """Non-vector other with matching size still rejected (no inferred shape)."""
        a = Matrix(5, 3, [float(i) for i in range(15)])
        b = Matrix(3, 3, [float(i) for i in range(9)])
        with pytest.raises(ValueError,
                           match=r"cross: .* incompatible with rhs \d+x\d+"):
            a.cross(b)

    def test_2x2_default_per_row(self):
        """2x2 default treats rows as 2D vectors (matches perpendicular/angle)."""
        a = Matrix(2, 2, [1.0, 2.0,
                          3.0, 4.0])
        b = Matrix(2, 2, [5.0, 6.0,
                          7.0, 8.0])
        out = a.cross(b)
        assert (out.rows, out.columns) == (2, 1)
        assert out[0, 0] == pytest.approx(1.0 * 6.0 - 2.0 * 5.0)
        assert out[1, 0] == pytest.approx(3.0 * 8.0 - 4.0 * 7.0)

    def test_2x2_axis_0_per_col(self):
        """2x2 with axis=0 treats columns as 2D vectors."""
        a = Matrix(2, 2, [1.0, 2.0,
                          3.0, 4.0])
        b = Matrix(2, 2, [5.0, 6.0,
                          7.0, 8.0])
        out = a.cross(b, axis=0)
        assert (out.rows, out.columns) == (1, 2)
        assert out[0, 0] == pytest.approx(1.0 * 7.0 - 3.0 * 5.0)
        assert out[0, 1] == pytest.approx(2.0 * 8.0 - 4.0 * 6.0)

    def test_2x2_axis_1_matches_default(self):
        """2x2 with axis=1 is equivalent to the default."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [5.0, 6.0, 7.0, 8.0])
        default = a.cross(b)
        explicit = a.cross(b, axis=1)
        for r in range(2):
            assert explicit[r, 0] == pytest.approx(default[r, 0])

    def test_3x3_default_per_row(self):
        """3x3 default treats rows as 3D vectors."""
        a = Matrix(3, 3, [1.0, 0.0, 0.0,
                          0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0])
        b = Matrix(3, 3, [0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0,
                          1.0, 0.0, 0.0])
        out = a.cross(b)
        assert (out.rows, out.columns) == (3, 3)
        assert out[0, 0] == pytest.approx(0.0)
        assert out[0, 2] == pytest.approx(1.0)
        assert out[1, 0] == pytest.approx(1.0)
        assert out[2, 1] == pytest.approx(1.0)

    def test_3x3_axis_0_per_col(self):
        """3x3 with axis=0 treats columns as 3D vectors."""
        a = Matrix(3, 3, [1.0, 0.0, 0.0,
                          0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0])
        b = Matrix(3, 3, [0.0, 0.0, 1.0,
                          1.0, 0.0, 0.0,
                          0.0, 1.0, 0.0])
        out = a.cross(b, axis=0)
        assert (out.rows, out.columns) == (3, 3)
        assert out[0, 0] == pytest.approx(0.0)
        assert out[1, 0] == pytest.approx(0.0)
        assert out[2, 0] == pytest.approx(1.0)
        assert out[0, 1] == pytest.approx(1.0)
        assert out[1, 2] == pytest.approx(1.0)

    def test_axis_negative_normalizes(self):
        """``axis=-1`` is equivalent to ``axis=1`` and ``axis=-2`` to ``axis=0``."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [5.0, 6.0, 7.0, 8.0])
        ax1 = a.cross(b, axis=1)
        ax_neg1 = a.cross(b, axis=-1)
        ax0 = a.cross(b, axis=0)
        ax_neg2 = a.cross(b, axis=-2)
        assert (ax1.rows, ax1.columns) == (ax_neg1.rows, ax_neg1.columns)
        for r in range(ax1.rows):
            for c in range(ax1.columns):
                assert ax_neg1[r, c] == pytest.approx(ax1[r, c])
        assert (ax0.rows, ax0.columns) == (ax_neg2.rows, ax_neg2.columns)
        for r in range(ax0.rows):
            for c in range(ax0.columns):
                assert ax_neg2[r, c] == pytest.approx(ax0[r, c])

    def test_axis_none_keyword_matches_default(self):
        """``axis=None`` matches the no-axis default."""
        a = Matrix(3, 3, [1.0, 0.0, 0.0,
                          0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0])
        b = Matrix(3, 3, [0.0, 1.0, 0.0,
                          0.0, 0.0, 1.0,
                          1.0, 0.0, 0.0])
        default = a.cross(b)
        explicit = a.cross(b, axis=None)
        for r in range(3):
            for c in range(3):
                assert explicit[r, c] == pytest.approx(default[r, c])

    @pytest.mark.parametrize("rows,cols", [(1, 4), (4, 1), (1, 1), (1, 5), (4, 4), (4, 5)])
    def test_invalid_shape_raises(self, rows, cols):
        """Shapes that aren't 1x2/2x1/Nx2/2xN/1x3/3x1/Nx3/3xN raise ValueError."""
        n = rows * cols
        a = Matrix(rows, cols, [float(i) for i in range(n)])
        b = Matrix(rows, cols, [float(i + 1) for i in range(n)])
        with pytest.raises(
                ValueError,
                match="cross requires a 2D or 3D vector or Nx2 or 2xN or Nx3 or 3xN matrix"):
            a.cross(b)

    def test_invalid_axis_raises(self):
        """axis outside {None, 0, 1, -1, -2} raises ValueError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [5.0, 6.0, 7.0, 8.0])
        with pytest.raises(ValueError,
                           match="axis must be -2, -1, 0, or 1"):
            a.cross(b, axis=2)

    def test_size_mismatch_scalar_2d_raises(self):
        """2D-scalar self with size-3 other raises ValueError."""
        a = Matrix(1, 2, [1.0, 2.0])
        b = Matrix(1, 3, [3.0, 4.0, 5.0])
        with pytest.raises(ValueError,
                           match=r"cross: 2D vector lhs \d+x\d+ incompatible with rhs \d+x\d+"):
            a.cross(b)

    def test_size_mismatch_batch_raises(self):
        """Batch self with mismatched-shape other raises ValueError."""
        a = Matrix(5, 3, [float(i) for i in range(15)])
        b = Matrix(7, 3, [float(i) for i in range(21)])
        with pytest.raises(ValueError,
                           match=r"cross: Nx3 batch lhs \d+x\d+ incompatible with rhs \d+x\d+"):
            a.cross(b)

    def test_in_behavior_propagates_exception(self):
        """A cross-shape error inside ``@when`` lands on the result cown."""
        a = Cown(Matrix(1, 4, [1.0, 2.0, 3.0, 4.0]))
        other = Matrix(1, 4, [5.0, 6.0, 7.0, 8.0])

        @when(a)
        def result(a, other=other):  # noqa: D401 — short behavior
            """Trigger a ValueError from inside a behavior."""
            a.value.cross(other)

        wait()
        assert result.exception is True
        assert isinstance(result.value, ValueError)
        assert "cross requires a 2D or 3D vector" in str(result.value)


@pytest.mark.parametrize("in_place_mode", [False, True], ids=["copy", "in_place"])
class TestNormalize:
    """Tests for the ``normalize`` method.

    Class-level parametrize runs every test against both the copy form
    (``normalize()``) and the self-mutating form (``normalize(in_place=True)``).
    """

    def test_golden_3_4_vector(self, in_place_mode):
        """``[3, 4]`` normalizes to ``[0.6, 0.8]``."""
        v = Matrix(1, 2, [3.0, 4.0])
        n = v.normalize(in_place=in_place_mode)
        assert n[0, 0] == pytest.approx(0.6)
        assert n[0, 1] == pytest.approx(0.8)

    def test_total_unit_magnitude_fuzz(self, in_place_mode):
        """Random non-zero 3-vectors produce a unit-magnitude result."""
        rng = random.Random(42)
        for _ in range(50):
            xs = [rng.uniform(-10.0, 10.0) for _ in range(3)]
            mat = Matrix(1, 3, xs)
            if mat.magnitude() == 0.0:
                continue
            n = mat.normalize(in_place=in_place_mode)
            assert n.magnitude() == pytest.approx(1.0)

    def test_axis1_per_row_unit_magnitude(self, in_place_mode):
        """axis=1 yields per-row unit magnitudes for any non-zero row."""
        rng = random.Random(43)
        values = [rng.uniform(-10.0, 10.0) for _ in range(5 * 3)]
        mat = Matrix(5, 3, values)
        n = mat.normalize(axis=1, in_place=in_place_mode)
        mags = n.magnitude(1)
        for row in range(5):
            assert mags[row, 0] == pytest.approx(1.0)

    def test_axis0_per_column_unit_magnitude(self, in_place_mode):
        """axis=0 yields per-column unit magnitudes for any non-zero column."""
        rng = random.Random(44)
        values = [rng.uniform(-10.0, 10.0) for _ in range(3 * 5)]
        mat = Matrix(3, 5, values)
        n = mat.normalize(axis=0, in_place=in_place_mode)
        mags = n.magnitude(0)
        for col in range(5):
            assert mags[0, col] == pytest.approx(1.0)

    def test_zero_vector_stays_zero(self, in_place_mode):
        """normalize(zero) returns the zero vector, not NaN."""
        z = Matrix(1, 3, [0.0, 0.0, 0.0])
        n = z.normalize(in_place=in_place_mode)
        for idx in range(3):
            assert n[0, idx] == 0.0

    def test_zero_row_in_batch_stays_zero(self, in_place_mode):
        """axis=1: a zero row in a mixed batch survives normalization."""
        mat = Matrix(2, 2, [0.0, 0.0, 3.0, 4.0])
        n = mat.normalize(axis=1, in_place=in_place_mode)
        assert n[0, 0] == 0.0
        assert n[0, 1] == 0.0
        assert n[1, 0] == pytest.approx(0.6)
        assert n[1, 1] == pytest.approx(0.8)

    def test_zero_column_in_batch_stays_zero(self, in_place_mode):
        """axis=0: a zero column in a mixed batch survives normalization."""
        mat = Matrix(2, 2, [0.0, 3.0, 0.0, 4.0])
        n = mat.normalize(axis=0, in_place=in_place_mode)
        assert n[0, 0] == 0.0
        assert n[1, 0] == 0.0
        assert n[0, 1] == pytest.approx(0.6)
        assert n[1, 1] == pytest.approx(0.8)

    def test_return_value_contract(self, in_place_mode):
        """Copy form returns a new Matrix and preserves the input; in-place returns self."""
        v = Matrix(1, 3, [1.0, 2.0, 3.0])
        result = v.normalize(in_place=in_place_mode)
        if in_place_mode:
            assert result is v
        else:
            assert result is not v
            assert v[0, 0] == 1.0
            assert v[0, 1] == 2.0
            assert v[0, 2] == 3.0

    def test_invalid_axis_raises(self, in_place_mode):
        """axis=2 raises ValueError with the canonical message."""
        mat = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        with pytest.raises(ValueError,
                           match="axis must be -2, -1, 0, or 1"):
            mat.normalize(axis=2, in_place=in_place_mode)

    def test_bad_axis_type_raises(self, in_place_mode):
        """Non-int axis raises TypeError."""
        mat = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        with pytest.raises(TypeError, match="axis must be an int or None"):
            mat.normalize(axis="foo", in_place=in_place_mode)

    def test_normalize_finite_for_nonzero_inputs(self, in_place_mode):
        """Result is finite for inputs whose magnitude is well above DBL_MIN.

        The docstring warns about subnormal magnitudes overflowing during
        division, so we restrict the assertion to inputs whose magnitude
        is at least ``2 * sys.float_info.min``. This catches non-subnormal
        divide-path regressions without contradicting the documented
        caveat.
        """
        rng = random.Random(45)
        floor = 2.0 * sys.float_info.min
        cases = 0
        for _ in range(50):
            values = [rng.uniform(-10.0, 10.0) for _ in range(4 * 3)]
            mat = Matrix(4, 3, values)
            if mat.magnitude() < floor:
                continue
            cases += 1
            for axis in (None, 0, 1):
                m = Matrix(4, 3, values)
                if axis is None:
                    n = m.normalize(in_place=in_place_mode)
                else:
                    n = m.normalize(axis=axis, in_place=in_place_mode)
                for r in range(4):
                    for c in range(3):
                        assert math.isfinite(n[r, c])
        assert cases > 0

    def test_normalize_keyword_axis(self, in_place_mode):
        """``mat.normalize(axis=1)`` keyword form works as advertised."""
        mat_kw = Matrix(2, 2, [3.0, 4.0, 6.0, 8.0])
        mat_no = Matrix(2, 2, [3.0, 4.0, 6.0, 8.0])
        positional_kw = mat_kw.normalize(axis=1, in_place=in_place_mode)
        no_axis = mat_no.normalize(in_place=in_place_mode)
        for r in range(2):
            assert positional_kw[r, 0] == pytest.approx(0.6)
            assert positional_kw[r, 1] == pytest.approx(0.8)
        assert no_axis[0, 0] != pytest.approx(positional_kw[0, 0])

    def test_in_place_matches_copy(self, in_place_mode):
        """Cross-form equivalence: in-place result matches copy result, fuzzed across axes."""
        if not in_place_mode:
            pytest.skip("equivalence is verified once in in_place mode")
        rng = random.Random(46)
        for axis in (None, 0, 1):
            for _ in range(20):
                values = [rng.uniform(-10.0, 10.0) for _ in range(3 * 4)]
                src = Matrix(3, 4, values)
                clone = Matrix(3, 4, values)
                expected = src.normalize() if axis is None else src.normalize(axis=axis)
                if axis is None:
                    clone.normalize(in_place=True)
                else:
                    clone.normalize(axis=axis, in_place=True)
                for r in range(3):
                    for c in range(4):
                        assert clone[r, c] == pytest.approx(expected[r, c])


@pytest.mark.parametrize("in_place_mode", [False, True], ids=["copy", "in_place"])
class TestPerpendicular:
    """Tests for the ``perpendicular`` method.

    Class-level parametrize runs every test against both the copy form
    (``perpendicular()``) and the self-mutating form
    (``perpendicular(in_place=True)``).
    """

    def test_unit_x_to_unit_y(self, in_place_mode):
        """``(1, 0).perpendicular() == (0, 1)``."""
        v = Matrix(1, 2, [1.0, 0.0])
        p = v.perpendicular(in_place=in_place_mode)
        assert p[0, 0] == pytest.approx(0.0)
        assert p[0, 1] == pytest.approx(1.0)

    def test_round_trip_180(self, in_place_mode):
        """Applying ``perpendicular`` four times returns the original.

        Two applications negate; four applications restore.
        """
        rng = random.Random(60)
        for _ in range(50):
            xs = [rng.uniform(-10.0, 10.0) for _ in range(2)]
            v = Matrix(1, 2, xs)
            p = v
            for _ in range(4):
                p = p.perpendicular(in_place=in_place_mode)
            assert p[0, 0] == pytest.approx(xs[0])
            assert p[0, 1] == pytest.approx(xs[1])

    def test_magnitude_preserved(self, in_place_mode):
        """Rotation preserves magnitude_squared.

        Uses Step 3's ``magnitude_squared`` to close the loop.
        """
        rng = random.Random(61)
        for _ in range(50):
            xs = [rng.uniform(-10.0, 10.0) for _ in range(2)]
            v = Matrix(1, 2, xs)
            expected_mag2 = v.magnitude_squared()
            p = v.perpendicular(in_place=in_place_mode)
            assert p.magnitude_squared() == pytest.approx(expected_mag2)

    def test_orthogonal_to_input(self, in_place_mode):
        """``v . perpendicular(v) == 0`` (uses Step 4's vecdot)."""
        rng = random.Random(62)
        for _ in range(50):
            xs = [rng.uniform(-10.0, 10.0) for _ in range(2)]
            v = Matrix(1, 2, xs)
            v_snapshot = Matrix(1, 2, xs)
            p = v.perpendicular(in_place=in_place_mode)
            assert v_snapshot.vecdot(p) == pytest.approx(0.0, abs=1e-9)

    def test_row_batch_nx2(self, in_place_mode):
        """``Nx2`` matrix rotates every row independently."""
        m = Matrix(3, 2, [1.0, 0.0, 0.0, 1.0, 3.0, 4.0])
        p = m.perpendicular(in_place=in_place_mode)
        assert (p.rows, p.columns) == (3, 2)
        expected = [(0.0, 1.0), (-1.0, 0.0), (-4.0, 3.0)]
        for r, (ex, ey) in enumerate(expected):
            assert p[r, 0] == pytest.approx(ex)
            assert p[r, 1] == pytest.approx(ey)

    def test_column_batch_2xn(self, in_place_mode):
        """``2xN`` matrix rotates every column independently."""
        m = Matrix(2, 3, [1.0, 0.0, 3.0, 0.0, 1.0, 4.0])
        p = m.perpendicular(in_place=in_place_mode)
        assert (p.rows, p.columns) == (2, 3)
        expected = [(0.0, 1.0), (-1.0, 0.0), (-4.0, 3.0)]
        for c, (ex, ey) in enumerate(expected):
            assert p[0, c] == pytest.approx(ex)
            assert p[1, c] == pytest.approx(ey)

    def test_1x2_returns_1x2(self, in_place_mode):
        """Single ``1x2`` input returns a ``1x2`` result."""
        p = Matrix(1, 2, [3.0, 4.0]).perpendicular(in_place=in_place_mode)
        assert (p.rows, p.columns) == (1, 2)
        assert p[0, 0] == pytest.approx(-4.0)
        assert p[0, 1] == pytest.approx(3.0)

    def test_2x1_returns_2x1(self, in_place_mode):
        """Single ``2x1`` input returns a ``2x1`` result."""
        p = Matrix(2, 1, [3.0, 4.0]).perpendicular(in_place=in_place_mode)
        assert (p.rows, p.columns) == (2, 1)
        assert p[0, 0] == pytest.approx(-4.0)
        assert p[1, 0] == pytest.approx(3.0)

    def test_2x2_default_is_per_row(self, in_place_mode):
        """Default for the ambiguous 2x2 shape is per-row (decision #3)."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        p = m.perpendicular(in_place=in_place_mode)
        assert p[0, 0] == pytest.approx(-2.0)
        assert p[0, 1] == pytest.approx(1.0)
        assert p[1, 0] == pytest.approx(-4.0)
        assert p[1, 1] == pytest.approx(3.0)

    def test_2x2_axis0_explicit_per_column(self, in_place_mode):
        """Explicit ``axis=0`` overrides the 2x2 default to per-column."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        p = m.perpendicular(axis=0, in_place=in_place_mode)
        assert p[0, 0] == pytest.approx(-3.0)
        assert p[1, 0] == pytest.approx(1.0)
        assert p[0, 1] == pytest.approx(-4.0)
        assert p[1, 1] == pytest.approx(2.0)

    def test_2x2_axis1_explicit_per_row(self, in_place_mode):
        """Explicit ``axis=1`` matches the default per-row behavior."""
        default = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]).perpendicular()
        explicit_src = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        explicit = explicit_src.perpendicular(axis=1, in_place=in_place_mode)
        for r in range(2):
            for c in range(2):
                assert explicit[r, c] == pytest.approx(default[r, c])

    def test_2x2_axis_minus_one_matches_axis_one(self, in_place_mode):
        """``axis=-1`` maps to ``axis=1`` (per-row) on 2x2."""
        pos = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]).perpendicular(axis=1)
        neg_src = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        neg = neg_src.perpendicular(axis=-1, in_place=in_place_mode)
        for r in range(2):
            for c in range(2):
                assert neg[r, c] == pytest.approx(pos[r, c])

    def test_2x2_axis_minus_two_matches_axis_zero(self, in_place_mode):
        """``axis=-2`` maps to ``axis=0`` (per-column) on 2x2."""
        pos = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]).perpendicular(axis=0)
        neg_src = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        neg = neg_src.perpendicular(axis=-2, in_place=in_place_mode)
        for r in range(2):
            for c in range(2):
                assert neg[r, c] == pytest.approx(pos[r, c])

    @pytest.mark.parametrize("rows,cols", [(3, 3), (1, 5), (5, 1), (3, 4), (1, 1)])
    def test_invalid_shape_raises(self, rows, cols, in_place_mode):
        """Any shape that is not a 2D vector or N-by-2/2-by-N raises."""
        n = rows * cols
        m = Matrix(rows, cols, [float(i) for i in range(n)])
        with pytest.raises(ValueError,
                           match="perpendicular requires a 2D vector or Nx2 or 2xN matrix"):
            m.perpendicular(in_place=in_place_mode)

    def test_invalid_axis_raises(self, in_place_mode):
        """Out-of-range axis raises ValueError."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(ValueError,
                           match="axis must be -2, -1, 0, or 1"):
            m.perpendicular(axis=2, in_place=in_place_mode)

    def test_axis_none_keyword(self, in_place_mode):
        """``axis=None`` matches the no-axis default form."""
        default = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]).perpendicular()
        explicit_src = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        explicit = explicit_src.perpendicular(axis=None, in_place=in_place_mode)
        for r in range(2):
            for c in range(2):
                assert explicit[r, c] == pytest.approx(default[r, c])

    def test_return_value_contract(self, in_place_mode):
        """Copy form returns a new Matrix and preserves the input; in-place returns self."""
        v = Matrix(1, 2, [3.0, 4.0])
        result = v.perpendicular(in_place=in_place_mode)
        if in_place_mode:
            assert result is v
        else:
            assert result is not v
            assert v[0, 0] == 3.0
            assert v[0, 1] == 4.0

    def test_in_place_matches_copy(self, in_place_mode):
        """Cross-form equivalence: in-place result matches copy result, fuzzed across flavors."""
        if not in_place_mode:
            pytest.skip("equivalence is verified once in in_place mode")
        rng = random.Random(63)
        flavors = [(1, 2), (2, 1), (3, 2), (2, 3), (2, 2)]
        for rows, cols in flavors:
            for _ in range(10):
                values = [rng.uniform(-10.0, 10.0) for _ in range(rows * cols)]
                src = Matrix(rows, cols, values)
                clone = Matrix(rows, cols, values)
                expected = src.perpendicular()
                clone.perpendicular(in_place=True)
                for r in range(rows):
                    for c in range(cols):
                        assert clone[r, c] == pytest.approx(expected[r, c])


class TestAngle:
    """Tests for the ``angle`` method (``atan2(y, x)`` per 2D vector)."""

    def test_unit_x_is_zero(self):
        """``(1, 0).angle() == 0``."""
        assert Matrix(1, 2, [1.0, 0.0]).angle() == pytest.approx(0.0)

    def test_unit_y_is_half_pi(self):
        """``(0, 1).angle() == pi/2``."""
        assert Matrix(1, 2, [0.0, 1.0]).angle() == pytest.approx(math.pi / 2.0)

    def test_negative_x_is_pi(self):
        """``(-1, 0).angle() == pi``."""
        assert Matrix(1, 2, [-1.0, 0.0]).angle() == pytest.approx(math.pi)

    def test_negative_y_is_minus_half_pi(self):
        """``(0, -1).angle() == -pi/2``."""
        assert Matrix(1, 2, [0.0, -1.0]).angle() == pytest.approx(-math.pi / 2.0)

    def test_2x1_column_vector_returns_float(self):
        """``2x1`` column input returns a Python float."""
        a = Matrix(2, 1, [1.0, 1.0]).angle()
        assert isinstance(a, float)
        assert a == pytest.approx(math.pi / 4.0)

    def test_row_batch_nx2(self):
        """``Nx2`` input returns an ``Mx1`` column of angles."""
        m = Matrix(3, 2, [1.0, 0.0, 0.0, 1.0, 1.0, 1.0])
        a = m.angle()
        assert (a.rows, a.columns) == (3, 1)
        assert a[0, 0] == pytest.approx(0.0)
        assert a[1, 0] == pytest.approx(math.pi / 2.0)
        assert a[2, 0] == pytest.approx(math.pi / 4.0)

    def test_column_batch_2xn(self):
        """``2xN`` input returns a ``1xN`` row of angles."""
        m = Matrix(2, 3, [1.0, 0.0, 1.0, 0.0, 1.0, 1.0])
        a = m.angle()
        assert (a.rows, a.columns) == (1, 3)
        assert a[0, 0] == pytest.approx(0.0)
        assert a[0, 1] == pytest.approx(math.pi / 2.0)
        assert a[0, 2] == pytest.approx(math.pi / 4.0)

    def test_matches_python_atan2_fuzz(self):
        """Per-element equivalence against ``math.atan2`` for random Nx2."""
        rng = random.Random(80)
        for _ in range(50):
            rows = rng.randint(3, 8)
            values = []
            expected = []
            for _r in range(rows):
                x = rng.uniform(-10.0, 10.0)
                y = rng.uniform(-10.0, 10.0)
                values.extend([x, y])
                expected.append(math.atan2(y, x))
            mat = Matrix(rows, 2, values)
            out = mat.angle()
            for r in range(rows):
                assert out[r, 0] == pytest.approx(expected[r])

    def test_2x2_default_is_per_row(self):
        """Default on the 2x2 ambiguous shape is per-row (decision #3)."""
        m = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        a = m.angle()
        assert (a.rows, a.columns) == (2, 1)
        assert a[0, 0] == pytest.approx(0.0)
        assert a[1, 0] == pytest.approx(math.pi / 2.0)

    def test_2x2_axis0_explicit_per_column(self):
        """Explicit ``axis=0`` on 2x2 gives per-column angles."""
        m = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        a = m.angle(axis=0)
        assert (a.rows, a.columns) == (1, 2)
        assert a[0, 0] == pytest.approx(0.0)
        assert a[0, 1] == pytest.approx(math.pi / 2.0)

    def test_2x2_axis_minus_one_matches_axis_one(self):
        """``axis=-1`` matches ``axis=1`` on 2x2."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        neg = m.angle(axis=-1)
        pos = m.angle(axis=1)
        for r in range(2):
            assert neg[r, 0] == pytest.approx(pos[r, 0])

    def test_2x2_axis_minus_two_matches_axis_zero(self):
        """``axis=-2`` matches ``axis=0`` on 2x2."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        neg = m.angle(axis=-2)
        pos = m.angle(axis=0)
        for c in range(2):
            assert neg[0, c] == pytest.approx(pos[0, c])

    @pytest.mark.parametrize("rows,cols", [(3, 3), (1, 5), (5, 1), (3, 4), (1, 1)])
    def test_invalid_shape_raises(self, rows, cols):
        """Non 2D-vector shapes raise ValueError."""
        n = rows * cols
        m = Matrix(rows, cols, [float(i) for i in range(n)])
        with pytest.raises(ValueError,
                           match="angle requires a 2D vector or Nx2 or 2xN matrix"):
            m.angle()

    def test_invalid_axis_raises(self):
        """Out-of-range axis raises ValueError."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(ValueError,
                           match="axis must be -2, -1, 0, or 1"):
            m.angle(axis=2)

    def test_axis_none_keyword(self):
        """``axis=None`` matches the no-axis default."""
        m = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        default = m.angle()
        explicit = m.angle(axis=None)
        for r in range(2):
            assert explicit[r, 0] == pytest.approx(default[r, 0])


_AXIS_METHODS = [
    (lambda: Matrix(2, 2, [1.0, 0.0, 0.0, 1.0]), "normalize"),
    (lambda: Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]), "perpendicular"),
    (lambda: Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]), "angle"),
    (lambda: Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]), "sum"),
    (lambda: Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]), "mean"),
    (lambda: Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]), "magnitude"),
    (lambda: Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]), "magnitude_squared"),
    (lambda: Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]), "min"),
    (lambda: Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]), "max"),
]


class TestAxisDecoder:
    """Tests for the unified axis decoder (rejects bool / overflow / sentinel collision)."""

    @pytest.mark.parametrize("factory,method", _AXIS_METHODS,
                             ids=[m for _, m in _AXIS_METHODS])
    def test_bool_axis_rejected(self, factory, method):
        """``axis=True`` (a PyLong subclass) raises TypeError mentioning bool."""
        mat = factory()
        with pytest.raises(TypeError, match="bool"):
            getattr(mat, method)(axis=True)

    @pytest.mark.parametrize("factory,method", _AXIS_METHODS,
                             ids=[m for _, m in _AXIS_METHODS])
    def test_overflow_axis_above_int_range(self, factory, method):
        """``axis`` that exceeds C ``int`` raises OverflowError.

        Accepts either the explicit ``"out of int range"`` message from the
        decoder's own range check (LP64: ``2**32`` fits ``long``) or
        CPython's generic ``"too large to convert to C long"`` message
        (LLP64 / Windows: ``2**32`` overflows ``long`` itself).
        """
        mat = factory()
        with pytest.raises(OverflowError,
                           match="out of int range|too large to convert"):
            getattr(mat, method)(axis=2**32)

    @pytest.mark.parametrize("factory,method", _AXIS_METHODS,
                             ids=[m for _, m in _AXIS_METHODS])
    def test_overflow_axis_above_long_range(self, factory, method):
        """``axis`` that exceeds C ``long`` raises OverflowError via ``PyLong_AsLong``."""
        mat = factory()
        with pytest.raises(OverflowError):
            getattr(mat, method)(axis=2**100)

    @pytest.mark.parametrize("factory,method", _AXIS_METHODS,
                             ids=[m for _, m in _AXIS_METHODS])
    def test_former_sentinel_no_longer_silent(self, factory, method):
        """``axis=-1000`` raises rather than being silently treated as a no-axis sentinel."""
        mat = factory()
        with pytest.raises(ValueError, match="axis must be -2, -1, 0, or 1"):
            getattr(mat, method)(axis=-1000)

    @pytest.mark.parametrize("factory,method", _AXIS_METHODS,
                             ids=[m for _, m in _AXIS_METHODS])
    def test_axis_keyword_matches_positional(self, factory, method):
        """``axis=`` keyword form is accepted across every axis-aware method."""
        mat_kw = factory()
        mat_pos = factory()
        kw = getattr(mat_kw, method)(axis=1)
        pos = getattr(mat_pos, method)(1)
        if isinstance(kw, Matrix):
            assert (kw.rows, kw.columns) == (pos.rows, pos.columns)
            for r in range(kw.rows):
                for c in range(kw.columns):
                    assert kw[r, c] == pytest.approx(pos[r, c])
        else:
            assert kw == pytest.approx(pos)

    def test_vecdot_bool_axis_rejected(self):
        """``vecdot`` runs the same decoder."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        with pytest.raises(TypeError, match="bool"):
            a.vecdot(b, axis=True)

    def test_vecdot_overflow_axis_rejected(self):
        """``vecdot`` rejects out-of-int-range axes (portable across LP64 / LLP64)."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        with pytest.raises(OverflowError,
                           match="out of int range|too large to convert"):
            a.vecdot(b, axis=2**32)

    def test_cross_bool_axis_rejected(self):
        """``cross`` runs the same decoder."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        with pytest.raises(TypeError, match="bool"):
            a.cross(b, axis=True)

    def test_cross_overflow_axis_rejected(self):
        """``cross`` rejects out-of-int-range axes (portable across LP64 / LLP64)."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        with pytest.raises(OverflowError,
                           match="out of int range|too large to convert"):
            a.cross(b, axis=2**32)

    def test_cross_axis_accepts_positional(self):
        """``cross(other, 1)`` accepts ``axis`` positionally."""
        a = Matrix(1, 3, [1.0, 0.0, 0.0])
        b = Matrix(1, 3, [0.0, 1.0, 0.0])
        positional = a.cross(b, 1)
        keyword = a.cross(b, axis=1)
        assert (positional.rows, positional.columns) == (keyword.rows, keyword.columns)
        for r in range(positional.rows):
            for c in range(positional.columns):
                assert positional[r, c] == pytest.approx(keyword[r, c])

    @pytest.mark.parametrize("method", ["normalize", "perpendicular"])
    @pytest.mark.parametrize("bad_axis,expected_exc,expected_match", [
        (True, TypeError, "bool"),
        (2**32, OverflowError, "out of int range|too large to convert"),
        (-1000, ValueError, "axis must be -2, -1, 0, or 1"),
    ], ids=["bool", "overflow", "sentinel"])
    def test_rejected_axis_does_not_mutate_in_place(
            self, method, bad_axis, expected_exc, expected_match):
        """A rejected ``axis=`` with ``in_place=True`` must not mutate ``self``.

        Pins the parse-before-mutate ordering in ``Matrix_normalize`` and
        ``Matrix_perpendicular``: ``parse_validate_normalise_axis`` runs
        before any kernel call, so an invalid axis raises with ``self``
        untouched. A future refactor that reorders them (validates after
        mutating) would silently leave the matrix in a half-mutated state
        on the error path.
        """
        mat = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        before = [mat[r, c] for r in range(mat.rows) for c in range(mat.columns)]
        with pytest.raises(expected_exc, match=expected_match):
            getattr(mat, method)(axis=bad_axis, in_place=True)
        after = [mat[r, c] for r in range(mat.rows) for c in range(mat.columns)]
        assert before == after, (
            f"{method}(axis={bad_axis!r}, in_place=True) raised but left "
            f"self mutated: before={before} after={after}"
        )


class TestShapeDisambiguation:
    """Explicit-axis contradictions on unique-orientation shapes raise rather than silently fall through."""

    @pytest.mark.parametrize("method", ["perpendicular", "angle"])
    def test_1x2_axis0_rejected(self, method):
        """``1x2`` is row-oriented; ``axis=0`` contradicts and raises."""
        m = Matrix(1, 2, [1.0, 2.0])
        with pytest.raises(ValueError,
                           match=f"{method} requires a 2D vector or Nx2 or 2xN matrix"):
            getattr(m, method)(axis=0)

    @pytest.mark.parametrize("method", ["perpendicular", "angle"])
    def test_2x1_axis1_rejected(self, method):
        """``2x1`` is column-oriented; ``axis=1`` contradicts and raises."""
        m = Matrix(2, 1, [1.0, 2.0])
        with pytest.raises(ValueError,
                           match=f"{method} requires a 2D vector or Nx2 or 2xN matrix"):
            getattr(m, method)(axis=1)

    @pytest.mark.parametrize("method", ["perpendicular", "angle"])
    def test_Nx2_axis0_rejected(self, method):  # noqa: N802 (shape name)
        """``Nx2`` with ``N>2`` is row-oriented; ``axis=0`` contradicts and raises."""
        m = Matrix(5, 2, [float(i) for i in range(10)])
        with pytest.raises(ValueError,
                           match=f"{method} requires a 2D vector or Nx2 or 2xN matrix"):
            getattr(m, method)(axis=0)

    @pytest.mark.parametrize("method", ["perpendicular", "angle"])
    def test_2xN_axis1_rejected(self, method):  # noqa: N802 (shape name)
        """``2xN`` with ``N>2`` is column-oriented; ``axis=1`` contradicts and raises."""
        m = Matrix(2, 5, [float(i) for i in range(10)])
        with pytest.raises(ValueError,
                           match=f"{method} requires a 2D vector or Nx2 or 2xN matrix"):
            getattr(m, method)(axis=1)

    def test_cross_1x2_axis0_rejected(self):
        """1x2 scalar 2D rejects ``axis=0``."""
        a = Matrix(1, 2, [1.0, 2.0])
        b = Matrix(1, 2, [3.0, 4.0])
        with pytest.raises(ValueError, match="cross requires a 2D or 3D vector"):
            a.cross(b, axis=0)

    def test_cross_2x1_axis1_rejected(self):
        """2x1 scalar 2D rejects ``axis=1``."""
        a = Matrix(2, 1, [1.0, 2.0])
        b = Matrix(2, 1, [3.0, 4.0])
        with pytest.raises(ValueError, match="cross requires a 2D or 3D vector"):
            a.cross(b, axis=1)

    def test_cross_1x3_axis0_rejected(self):
        """1x3 scalar 3D rejects ``axis=0``."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [4.0, 5.0, 6.0])
        with pytest.raises(ValueError, match="cross requires a 2D or 3D vector"):
            a.cross(b, axis=0)

    def test_cross_3x1_axis1_rejected(self):
        """3x1 scalar 3D rejects ``axis=1``."""
        a = Matrix(3, 1, [1.0, 2.0, 3.0])
        b = Matrix(3, 1, [4.0, 5.0, 6.0])
        with pytest.raises(ValueError, match="cross requires a 2D or 3D vector"):
            a.cross(b, axis=1)

    def test_cross_Nx2_axis0_rejected(self):  # noqa: N802 (shape name)
        """Nx2 batch (N>3) is row-oriented; ``axis=0`` contradicts."""
        a = Matrix(5, 2, [float(i) for i in range(10)])
        b = Matrix(5, 2, [float(i) for i in range(10)])
        with pytest.raises(ValueError, match="cross requires a 2D or 3D vector"):
            a.cross(b, axis=0)

    def test_cross_2xN_axis1_rejected(self):  # noqa: N802 (shape name)
        """2xN batch (N>3) is column-oriented; ``axis=1`` contradicts."""
        a = Matrix(2, 5, [float(i) for i in range(10)])
        b = Matrix(2, 5, [float(i) for i in range(10)])
        with pytest.raises(ValueError, match="cross requires a 2D or 3D vector"):
            a.cross(b, axis=1)

    def test_cross_Nx3_axis0_rejected(self):  # noqa: N802 (shape name)
        """Nx3 batch (N>2) is row-oriented; ``axis=0`` contradicts."""
        a = Matrix(5, 3, [float(i) for i in range(15)])
        b = Matrix(5, 3, [float(i) for i in range(15)])
        with pytest.raises(ValueError, match="cross requires a 2D or 3D vector"):
            a.cross(b, axis=0)

    def test_cross_3xN_axis1_rejected(self):  # noqa: N802 (shape name)
        """3xN batch (N>2) is column-oriented; ``axis=1`` contradicts."""
        a = Matrix(3, 5, [float(i) for i in range(15)])
        b = Matrix(3, 5, [float(i) for i in range(15)])
        with pytest.raises(ValueError, match="cross requires a 2D or 3D vector"):
            a.cross(b, axis=1)

    def test_2x2_both_axes_accepted(self):
        """``2x2`` is ambiguous: both axes succeed (axis picks orientation)."""
        m = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        assert m.perpendicular(axis=0) is not None
        assert m.perpendicular(axis=1) is not None
        assert m.angle(axis=0) is not None
        assert m.angle(axis=1) is not None

    def test_cross_2x3_axis_ignored_always_2D_batch(self):  # noqa: N802
        """``2x3`` always uses the 2D-batch interpretation; ``axis=`` is silently ignored.

        Pins the doubly-valid contract: a ``2x3`` input could in principle
        be read as either "2 rows of 3D vectors" (axis=1) or "3 columns of
        2D vectors" (axis=0). The implementation always takes the latter
        (yielding 3 scalar 2D cross products) regardless of ``axis``.
        Both forms must therefore produce identical output.
        """
        a = Matrix(2, 3, [1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
        b = Matrix(2, 3, [0.0, 1.0, 0.0, 0.0, 0.0, 1.0])
        default = a.cross(b)
        with_axis_0 = a.cross(b, axis=0)
        with_axis_1 = a.cross(b, axis=1)
        assert (default.rows, default.columns) == (1, 3)
        assert (with_axis_0.rows, with_axis_0.columns) == (1, 3)
        assert (with_axis_1.rows, with_axis_1.columns) == (1, 3)
        for c in range(3):
            assert with_axis_0[0, c] == pytest.approx(default[0, c])
            assert with_axis_1[0, c] == pytest.approx(default[0, c])

    def test_cross_3x2_axis_ignored_always_2D_batch(self):  # noqa: N802
        """``3x2`` always uses the 2D-batch interpretation; ``axis=`` is silently ignored.

        Mirror of the ``2x3`` case for the row-batch orientation. Both
        ``axis=0`` and ``axis=1`` must produce identical output of shape
        ``3x1`` (three scalar 2D cross products laid out per row).
        """
        a = Matrix(3, 2, [1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
        b = Matrix(3, 2, [0.0, 1.0, 0.0, 0.0, 1.0, 0.0])
        default = a.cross(b)
        with_axis_0 = a.cross(b, axis=0)
        with_axis_1 = a.cross(b, axis=1)
        assert (default.rows, default.columns) == (3, 1)
        assert (with_axis_0.rows, with_axis_0.columns) == (3, 1)
        assert (with_axis_1.rows, with_axis_1.columns) == (3, 1)
        for r in range(3):
            assert with_axis_0[r, 0] == pytest.approx(default[r, 0])
            assert with_axis_1[r, 0] == pytest.approx(default[r, 0])


@pytest.mark.parametrize("in_place_mode", [False, True], ids=["copy", "in_place"])
class TestUnaryOps:
    """Tests for element-wise unary operations.

    Class-level parametrize runs every test against both the copy form and
    the self-mutating form. Python operator protocols (``-mat``, ``abs(mat)``)
    live in :class:`TestUnaryOperators` because they only have a copy form.
    """

    def test_negate(self, shape, random_values, in_place_mode):
        """Verify element-wise negation."""
        rows, cols = shape
        m = Matrix(rows, cols, random_values)
        n = m.negate(in_place=in_place_mode)
        for i in range(rows):
            for j in range(cols):
                assert n[i, j] == pytest.approx(-random_values[i * cols + j])

    def test_abs(self, shape, rng, in_place_mode):
        """Verify element-wise absolute value."""
        rows, cols = shape
        vals = [rng.uniform(-100, 100) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        a = m.abs(in_place=in_place_mode)
        for i in range(rows):
            for j in range(cols):
                assert a[i, j] == pytest.approx(abs(vals[i * cols + j]))

    def test_ceil(self, shape, rng, in_place_mode):
        """Verify element-wise ceiling."""
        rows, cols = shape
        vals = [rng.uniform(-10, 10) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        c = m.ceil(in_place=in_place_mode)
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(math.ceil(vals[i * cols + j]))

    def test_floor(self, shape, rng, in_place_mode):
        """Verify element-wise floor."""
        rows, cols = shape
        vals = [rng.uniform(-10, 10) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        f = m.floor(in_place=in_place_mode)
        for i in range(rows):
            for j in range(cols):
                assert f[i, j] == pytest.approx(math.floor(vals[i * cols + j]))

    def test_round(self, shape, rng, in_place_mode):
        """Verify element-wise rounding."""
        rows, cols = shape
        vals = [rng.uniform(-10, 10) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        r = m.round(in_place=in_place_mode)
        for i in range(rows):
            for j in range(cols):
                assert r[i, j] == pytest.approx(round(vals[i * cols + j]))

    def test_round_halfway_banker(self, in_place_mode):
        """Pin banker's rounding (half-to-even) at exact halfway inputs."""
        vals = [-2.5, -1.5, -0.5, 0.5, 1.5, 2.5, 3.5]
        expected = [-2.0, -2.0, 0.0, 0.0, 2.0, 2.0, 4.0]
        m = Matrix(1, len(vals), vals)
        r = m.round(in_place=in_place_mode)
        actual = [r[0, j] for j in range(len(vals))]
        assert actual == expected

    @pytest.mark.parametrize("method", ["abs", "ceil", "floor", "round", "negate"])
    def test_return_value_contract(self, method, in_place_mode):
        """Copy form returns a new Matrix and preserves the input; in-place returns self."""
        m = Matrix(2, 2, [-1.5, 2.5, -3.5, 4.5])
        result = getattr(m, method)(in_place=in_place_mode)
        if in_place_mode:
            assert result is m
        else:
            assert result is not m
            assert m[0, 0] == -1.5
            assert m[0, 1] == 2.5
            assert m[1, 0] == -3.5
            assert m[1, 1] == 4.5


class TestUnaryOperators:
    """Tests for the Python unary operator protocols (always copy form)."""

    def test_neg_operator(self, mat):
        """Verify the unary minus operator (always allocates a fresh Matrix)."""
        assert Matrix.allclose(-mat, mat.negate())

    def test_abs_operator(self, mat):
        """Verify the abs() built-in operator (always allocates a fresh Matrix)."""
        assert Matrix.allclose(abs(mat), mat.abs())


class TestAllclose:
    """Tests for the allclose comparison function."""

    def test_identical(self, mat):
        """Verify a matrix is close to itself."""
        assert Matrix.allclose(mat, mat)

    def test_copy_equal(self, shape, random_values):
        """Verify two matrices with the same values are close."""
        rows, cols = shape
        a = Matrix(rows, cols, random_values)
        b = Matrix(rows, cols, random_values)
        assert Matrix.allclose(a, b)

    def test_not_close(self, shape, rng):
        """Verify matrices with large differences are not close."""
        rows, cols = shape
        vals = [rng.uniform(-10, 10) for _ in range(rows * cols)]
        a = Matrix(rows, cols, vals)
        modified = [v + 1.0 for v in vals]
        b = Matrix(rows, cols, modified)
        assert not Matrix.allclose(a, b, atol=0.01)

    def test_within_tolerance(self, shape, rng):
        """Verify matrices with tiny perturbations are close."""
        rows, cols = shape
        vals = [rng.uniform(-10, 10) for _ in range(rows * cols)]
        a = Matrix(rows, cols, vals)
        perturbed = [v + 1e-9 for v in vals]
        b = Matrix(rows, cols, perturbed)
        assert Matrix.allclose(a, b)


class TestRepr:
    """Smoke tests for string representations."""

    def test_str_does_not_crash(self, mat):
        """Verify str() produces a non-empty string."""
        s = str(mat)
        assert isinstance(s, str)
        assert len(s) > 0

    def test_repr_does_not_crash(self, mat):
        """Verify repr() produces a non-empty string."""
        r = repr(mat)
        assert isinstance(r, str)
        assert len(r) > 0


class TestEdgeCases:
    """Tests for edge cases and algebraic properties."""

    def test_add_zero_identity(self, mat, shape):
        """Verify adding zero is the identity operation."""
        z = Matrix.zeros(shape)
        assert Matrix.allclose(mat + z, mat)

    def test_multiply_one_identity(self, mat, shape):
        """Verify multiplying by ones is the identity operation."""
        o = Matrix.ones(shape)
        assert Matrix.allclose(mat * o, mat)

    def test_subtract_self_is_zero(self, mat, shape):
        """Verify subtracting a matrix from itself yields zero."""
        result = mat - mat
        z = Matrix.zeros(shape)
        assert Matrix.allclose(result, z)

    def test_negate_negate_roundtrip(self, mat):
        """Verify double negation returns the original matrix."""
        assert Matrix.allclose(mat.negate().negate(), mat)

    def test_abs_nonnegative(self, mat, shape):
        """Verify all absolute values are non-negative."""
        a = mat.abs()
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert a[i, j] >= 0.0

    @pytest.mark.parametrize("n", [1, 3, 5, 10])
    def test_square_matmul_identity(self, n):
        """A @ I == A for square matrices."""
        vals = [random.uniform(-10, 10) for _ in range(n * n)]
        a = Matrix(n, n, vals)
        identity_vals = [1.0 if i == j else 0.0 for i in range(n) for j in range(n)]
        ident = Matrix(n, n, identity_vals)
        assert Matrix.allclose(a @ ident, a)

    def test_transpose_matmul_symmetry(self):
        """(A @ B)^T == B^T @ A^T."""
        m, k, n = 4, 5, 3
        a = Matrix.uniform(0.0, 1.0, size=(m, k))
        b = Matrix.uniform(0.0, 1.0, size=(k, n))
        lhs = (a @ b).transpose()
        rhs = b.transpose() @ a.transpose()
        assert Matrix.allclose(lhs, rhs)


class TestTake:
    """Tests for Matrix.take() — row and column sub-selection."""

    def test_take_rows_with_list(self, mat, shape):
        """take([indices], axis=0) returns the requested rows."""
        rows, cols = shape
        if rows < 2:
            pytest.skip("need at least 2 rows")
        indices = [0, rows - 1]
        result = mat.take(indices)
        assert result.rows == len(indices)
        assert result.columns == cols
        for out_r, src_r in enumerate(indices):
            for c in range(cols):
                assert result[out_r, c] == pytest.approx(mat[src_r, c])

    def test_take_rows_with_tuple(self, mat, shape):
        """take((indices,), axis=0) also accepts a tuple."""
        rows, cols = shape
        if rows < 3:
            pytest.skip("need at least 3 rows")
        indices = (1, 0, 2)
        result = mat.take(indices)
        assert result.rows == len(indices)
        for out_r, src_r in enumerate(indices):
            for c in range(cols):
                assert result[out_r, c] == pytest.approx(mat[src_r, c])

    def test_take_columns_with_list(self, mat, shape):
        """take([indices], axis=1) returns the requested columns."""
        rows, cols = shape
        if cols < 2:
            pytest.skip("need at least 2 columns")
        indices = [cols - 1, 0]
        result = mat.take(indices, 1)
        assert result.rows == rows
        assert result.columns == len(indices)
        for r in range(rows):
            for out_c, src_c in enumerate(indices):
                assert result[r, out_c] == pytest.approx(mat[r, src_c])

    def test_take_columns_with_tuple(self, mat, shape):
        """take((indices,), axis=1) also accepts a tuple."""
        rows, cols = shape
        if cols < 3:
            pytest.skip("need at least 3 columns")
        indices = (2, 0, 1)
        result = mat.take(indices, 1)
        assert result.columns == len(indices)
        for r in range(rows):
            for out_c, src_c in enumerate(indices):
                assert result[r, out_c] == pytest.approx(mat[r, src_c])

    def test_take_negative_axis(self, mat, shape):
        """axis=-1 should behave like axis=1 (columns)."""
        rows, cols = shape
        if cols < 2:
            pytest.skip("need at least 2 columns")
        indices = [0, cols - 1]
        result_pos = mat.take(indices, 1)
        result_neg = mat.take(indices, -1)
        assert Matrix.allclose(result_pos, result_neg)

    def test_take_duplicate_indices(self, mat, shape):
        """Duplicate indices should duplicate the corresponding rows."""
        rows, cols = shape
        indices = [0, 0, 0]
        result = mat.take(indices, 0)
        assert result.rows == 3
        for r in range(3):
            for c in range(cols):
                assert result[r, c] == pytest.approx(mat[0, c])

    def test_take_single_row(self, mat, shape):
        """Taking a single row returns a 1xcols matrix."""
        rows, cols = shape
        result = mat.take([0])
        assert result.rows == 1
        assert result.columns == cols
        for c in range(cols):
            assert result[0, c] == pytest.approx(mat[0, c])

    def test_take_single_column(self, mat, shape):
        """Taking a single column returns a rowsx1 matrix."""
        rows, cols = shape
        result = mat.take([0], 1)
        assert result.rows == rows
        assert result.columns == 1
        for r in range(rows):
            assert result[r, 0] == pytest.approx(mat[r, 0])

    def test_take_all_rows_preserves_matrix(self, mat, shape):
        """Taking all rows in order yields an equal matrix."""
        rows, cols = shape
        indices = list(range(rows))
        result = mat.take(indices)
        assert Matrix.allclose(result, mat)

    def test_take_all_columns_preserves_matrix(self, mat, shape):
        """Taking all columns in order yields an equal matrix."""
        rows, cols = shape
        indices = list(range(cols))
        result = mat.take(indices, 1)
        assert Matrix.allclose(result, mat)

    def test_take_empty_indices_raises(self):
        """Taking with an empty list raises IndexError (Option A)."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            m.take([])
        with pytest.raises(IndexError):
            m.take([], 1)

    def test_take_negative_index(self, mat, shape):
        """A negative index counts from the end of the axis."""
        rows, cols = shape
        last_row = mat.take([-1], 0)
        assert last_row.rows == 1
        for c in range(cols):
            assert last_row[0, c] == pytest.approx(mat[rows - 1, c])
        last_col = mat.take([-1], 1)
        assert last_col.columns == 1
        for r in range(rows):
            assert last_col[r, 0] == pytest.approx(mat[r, cols - 1])

    def test_take_out_of_range_raises(self, mat, shape):
        """An out-of-range index raises IndexError naming the value and dim."""
        rows, cols = shape
        with pytest.raises(IndexError) as excinfo:
            mat.take([rows], 0)
        message = str(excinfo.value)
        assert str(rows) in message
        assert "row" in message
        with pytest.raises(IndexError) as excinfo:
            mat.take([cols], 1)
        message = str(excinfo.value)
        assert str(cols) in message
        assert "column" in message

    def test_take_negative_boundary(self, mat, shape):
        """``-len`` is the first element; ``-len-1`` is out of range."""
        rows, cols = shape
        first_row = mat.take([-rows], 0)
        for c in range(cols):
            assert first_row[0, c] == pytest.approx(mat[0, c])
        with pytest.raises(IndexError):
            mat.take([-rows - 1], 0)
        first_col = mat.take([-cols], 1)
        for r in range(rows):
            assert first_col[r, 0] == pytest.approx(mat[r, 0])
        with pytest.raises(IndexError):
            mat.take([-cols - 1], 1)

    def test_take_overflow_raises(self):
        """A value beyond Py_ssize_t raises OverflowError, not IndexError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(OverflowError):
            m.take([10**100], 0)
        with pytest.raises(OverflowError):
            m.take([10**100], 1)

    def test_take_bool_item(self):
        """bool items resolve as 0/1 (bool is an int subclass)."""
        m = Matrix(3, 3, 0.0)
        for r in range(3):
            for c in range(3):
                m[r, c] = float(r * 3 + c)
        result = m.take([True, False], 0)
        assert result.rows == 2
        for c in range(3):
            assert result[0, c] == pytest.approx(m[1, c])
            assert result[1, c] == pytest.approx(m[0, c])

    def test_take_bad_type_raises(self):
        """A non-int item raises TypeError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(TypeError):
            m.take(["x"], 0)

    def test_take_int_subscript_parity(self):
        """The scalar-int subscript path shares take's boundary outcomes."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            _ = m[-4, 0]
        with pytest.raises(OverflowError):
            _ = m[10**100, 0]

    def test_take_invalid_axis_raises(self):
        """axis >= 2 should raise KeyError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(KeyError):
            m.take([0], 2)

    def test_take_axis_below_neg2_raises(self):
        """An out-of-range negative axis (<= -3) raises KeyError, not a column take."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(KeyError):
            m.take([0], -3)


class TestFancyIndexing:
    """List-key gather through __getitem__ (rows and columns)."""

    def test_getitem_list_rows_matches_take(self, mat, shape):
        """m[[r0, r1]] gathers rows, matching take(..., 0)."""
        rows, cols = shape
        indices = [0, rows - 1]
        result = mat[indices]
        assert result.rows == len(indices)
        assert result.columns == cols
        assert Matrix.allclose(result, mat.take(indices, 0))

    def test_getitem_rows_explicit_full_slice(self, mat, shape):
        """m[[r], :] is the same as m[[r]]."""
        rows, cols = shape
        indices = [0, rows - 1] if rows > 1 else [0]
        assert Matrix.allclose(mat[indices, :], mat[indices])

    def test_getitem_list_cols_matches_take(self, mat, shape):
        """m[:, [c0, c1]] gathers columns, matching take(..., 1)."""
        rows, cols = shape
        indices = [cols - 1, 0] if cols > 1 else [0]
        result = mat[:, indices]
        assert result.rows == rows
        assert result.columns == len(indices)
        assert Matrix.allclose(result, mat.take(indices, 1))

    def test_getitem_negative(self, mat, shape):
        """A negative list index counts from the end."""
        rows, cols = shape
        result = mat[[-1]]
        for c in range(cols):
            assert result[0, c] == pytest.approx(mat[rows - 1, c])

    def test_getitem_duplicates(self, mat, shape):
        """Duplicate list indices repeat the row."""
        rows, cols = shape
        result = mat[[0, 0, 0]]
        assert result.rows == 3
        for r in range(3):
            for c in range(cols):
                assert result[r, c] == pytest.approx(mat[0, c])

    def test_getitem_out_of_range(self, mat, shape):
        """An out-of-range list index raises IndexError naming value+dim."""
        rows, cols = shape
        with pytest.raises(IndexError) as excinfo:
            _ = mat[[rows]]
        message = str(excinfo.value)
        assert str(rows) in message
        assert "row" in message
        with pytest.raises(IndexError) as excinfo:
            _ = mat[:, [cols]]
        message = str(excinfo.value)
        assert str(cols) in message
        assert "column" in message

    def test_getitem_empty_list_raises(self):
        """An empty list key raises IndexError (Option A)."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            _ = m[[]]
        with pytest.raises(IndexError):
            _ = m[:, []]

    def test_getitem_tuple_is_2d_not_gather(self):
        """m[(0, 0)] returns a float (tuple is 2-D indexing, not gather)."""
        m = Matrix(3, 3, 0.0)
        m[1, 2] = 7.0
        value = m[(1, 2)]
        assert isinstance(value, float)
        assert value == pytest.approx(7.0)

    def test_getitem_2d_slice_unchanged(self):
        """m[0:2, 1:3] still returns the legacy 2-D sub-matrix."""
        m = Matrix(3, 3, 0.0)
        for r in range(3):
            for c in range(3):
                m[r, c] = float(r * 3 + c)
        sub = m[0:2, 1:3]
        assert sub.rows == 2
        assert sub.columns == 2
        assert sub[0, 0] == pytest.approx(1.0)
        assert sub[1, 1] == pytest.approx(5.0)

    def test_getitem_singleton_list_returns_matrix(self):
        """m[[0]] always yields a Matrix; m[0]/m[0,0] yields a float."""
        m = Matrix(1, 1, 3.0)
        gathered = m[[0]]
        assert isinstance(gathered, Matrix)
        assert gathered.rows == 1
        assert gathered.columns == 1
        assert isinstance(m[0, 0], float)

    def test_getitem_paired_list_unsupported(self):
        """m[[r], [c]] (paired lists) raises IndexError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            _ = m[[0, 1], [0, 1]]

    def test_getitem_mixed_list_int_unsupported(self):
        """m[[r], c] (list with int) raises IndexError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            _ = m[[0], 1]

    def test_getitem_partial_slice_list_unsupported(self):
        """A list paired with a non-full slice raises IndexError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            _ = m[1:3, [0]]
        with pytest.raises(IndexError):
            _ = m[[0], 0:2]

    def test_getitem_bad_type_raises(self):
        """A list with a non-int item raises TypeError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(TypeError):
            _ = m[["x"]]

    def test_getitem_boc_roundtrip(self):
        """List gather runs inside a @when behavior over a Cown[Matrix]."""
        a = Cown(Matrix(3, 2, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]))

        @when(a)
        def result(a):  # noqa: D401 — short behavior
            """Gather rows inside a behavior and return the result."""
            return a.value[[2, 0]]

        wait()
        assert result.exception is False
        expected = Matrix(2, 2, [5.0, 6.0, 1.0, 2.0])
        assert Matrix.allclose(result.value, expected)

    def test_getitem_fuzz_permutation(self, mat, shape, rng):
        """A random row permutation matches a manually assembled matrix."""
        rows, cols = shape
        perm = list(range(rows))
        rng.shuffle(perm)
        result = mat[perm]
        assert result.rows == rows
        assert result.columns == cols
        for out_r, src_r in enumerate(perm):
            for c in range(cols):
                assert result[out_r, c] == pytest.approx(mat[src_r, c])


def _snapshot(m, rows, cols):
    """Return a flat list snapshot of a matrix's elements."""
    return [m[r, c] for r in range(rows) for c in range(cols)]


class TestScatter:
    """List-key scatter assignment through __setitem__ (rows and columns)."""

    def test_scatter_rows_scalar(self):
        """m[[rows]] = scalar fills the selected rows."""
        m = Matrix(3, 2, 0.0)
        m[[0, 2]] = 5.0
        for c in range(2):
            assert m[0, c] == pytest.approx(5.0)
            assert m[1, c] == pytest.approx(0.0)
            assert m[2, c] == pytest.approx(5.0)

    def test_scatter_rows_matrix(self):
        """m[[rows]] = matrix copies each source row (memcpy path)."""
        m = Matrix(3, 2, 0.0)
        rhs = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m[[2, 0]] = rhs
        assert m[2, 0] == pytest.approx(1.0)
        assert m[2, 1] == pytest.approx(2.0)
        assert m[0, 0] == pytest.approx(3.0)
        assert m[0, 1] == pytest.approx(4.0)
        assert m[1, 0] == pytest.approx(0.0)

    def test_scatter_cols_scalar(self):
        """m[:, [cols]] = scalar fills the selected columns."""
        m = Matrix(2, 3, 0.0)
        m[:, [0, 2]] = 7.0
        for r in range(2):
            assert m[r, 0] == pytest.approx(7.0)
            assert m[r, 1] == pytest.approx(0.0)
            assert m[r, 2] == pytest.approx(7.0)

    def test_scatter_cols_matrix(self):
        """m[:, [cols]] = matrix copies each source column (strided)."""
        m = Matrix(2, 3, 0.0)
        rhs = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m[:, [2, 0]] = rhs
        assert m[0, 2] == pytest.approx(1.0)
        assert m[0, 0] == pytest.approx(2.0)
        assert m[1, 2] == pytest.approx(3.0)
        assert m[1, 0] == pytest.approx(4.0)
        assert m[0, 1] == pytest.approx(0.0)

    def test_scatter_rows_explicit_slice(self):
        """m[[rows], :] = v is the same as m[[rows]] = v."""
        a = Matrix(3, 2, 0.0)
        b = Matrix(3, 2, 0.0)
        a[[0, 1]] = 4.0
        b[[0, 1], :] = 4.0
        assert Matrix.allclose(a, b)

    def test_scatter_then_gather_roundtrip(self, mat, shape):
        """A scatter then a gather of the same rows returns the RHS."""
        rows, cols = shape
        indices = [0, rows - 1] if rows > 1 else [0]
        rhs = Matrix(len(indices), cols,
                     [float(i) for i in range(len(indices) * cols)])
        mat[indices] = rhs
        assert Matrix.allclose(mat[indices], rhs)

    def test_scatter_negative_index(self):
        """A negative list index assigns from the end."""
        m = Matrix(3, 2, 0.0)
        m[[-1]] = 9.0
        for c in range(2):
            assert m[2, c] == pytest.approx(9.0)
            assert m[0, c] == pytest.approx(0.0)

    def test_scatter_oob_message_matches_gather(self):
        """Read and write OOB share the resolver and the message."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError) as read_exc:
            _ = m[[3]]
        with pytest.raises(IndexError) as write_exc:
            m[[3]] = 1.0
        assert str(read_exc.value) == str(write_exc.value)

    def test_scatter_duplicate_last_write_wins(self):
        """m[[1, 1]] = matrix leaves row 1 equal to the last source row."""
        m = Matrix(2, 2, 0.0)
        rhs = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m[[1, 1]] = rhs
        assert m[1, 0] == pytest.approx(3.0)
        assert m[1, 1] == pytest.approx(4.0)

    def test_scatter_iadd_duplicate_adds_once(self):
        """m[[0, 0]] += v increments row 0 once (last-write-wins)."""
        m = Matrix(2, 2, [1.0, 1.0, 1.0, 1.0])
        m[[0, 0]] += 5.0
        for c in range(2):
            assert m[0, c] == pytest.approx(6.0)
            assert m[1, c] == pytest.approx(1.0)

    def test_iadd_whole_matrix_sanity(self):
        """m += scalar works (the slot the augmented scatter relies on)."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m += 1.0
        assert Matrix.allclose(m, Matrix(2, 2, [2.0, 3.0, 4.0, 5.0]))

    def test_scatter_augmented_ops_rows(self):
        """+= -= *= /= on a row selection match gather + manual op."""
        for op, fn in (
            ("+=", lambda a, b: a + b),
            ("-=", lambda a, b: a - b),
            ("*=", lambda a, b: a * b),
            ("/=", lambda a, b: a / b),
        ):
            m = Matrix(3, 2, [float(v) for v in range(1, 7)])
            expected = [
                [fn(m[r, c], 2.0) for c in range(2)] if r in (0, 2)
                else [m[r, c] for c in range(2)]
                for r in range(3)
            ]
            if op == "+=":
                m[[0, 2]] += 2.0
            elif op == "-=":
                m[[0, 2]] -= 2.0
            elif op == "*=":
                m[[0, 2]] *= 2.0
            else:
                m[[0, 2]] /= 2.0
            for r in range(3):
                for c in range(2):
                    assert m[r, c] == pytest.approx(expected[r][c])

    def test_scatter_augmented_op_cols(self):
        """m[:, [c]] *= v matches gather + manual op on that column."""
        m = Matrix(2, 3, [float(v) for v in range(1, 7)])
        expected = [[m[r, c] * 3.0 if c == 1 else m[r, c]
                     for c in range(3)] for r in range(2)]
        m[:, [1]] *= 3.0
        for r in range(2):
            for c in range(3):
                assert m[r, c] == pytest.approx(expected[r][c])

    def test_scatter_oob_leaves_matrix_unmodified(self):
        """A bad index found in phase 1 aborts before any write."""
        m = Matrix(3, 3, 1.0)
        before = _snapshot(m, 3, 3)
        with pytest.raises(IndexError):
            m[[0, 99]] = 5.0
        assert _snapshot(m, 3, 3) == before

    def test_scatter_shape_mismatch_leaves_unmodified(self):
        """A wrong-shaped RHS found in phase 2 aborts before any write."""
        m = Matrix(3, 3, 1.0)
        before = _snapshot(m, 3, 3)
        with pytest.raises(ValueError):
            m[[0, 1]] = Matrix(3, 3, 2.0)
        assert _snapshot(m, 3, 3) == before

    def test_scatter_shape_mismatch_message(self):
        """The shape-mismatch ValueError names both shapes."""
        m = Matrix(4, 3, 1.0)
        with pytest.raises(ValueError) as excinfo:
            m[[0, 1]] = Matrix(3, 3, 2.0)
        message = str(excinfo.value)
        assert "3x3" in message
        assert "2x3" in message

    @pytest.mark.parametrize("count", [64, 65, 100])
    def test_scatter_count_64_65_100_rows(self, count):
        """Row scatter exercises the stack/heap index-buffer boundary."""
        m = Matrix(3, 2, 0.0)
        idx = [1] * count
        rhs = Matrix(count, 2, [float(i) for i in range(count * 2)])
        m[idx] = rhs
        assert m[1, 0] == pytest.approx(float((count - 1) * 2))
        assert m[1, 1] == pytest.approx(float((count - 1) * 2 + 1))
        for c in range(2):
            assert m[0, c] == pytest.approx(0.0)
            assert m[2, c] == pytest.approx(0.0)

    @pytest.mark.parametrize("count", [64, 65, 100])
    def test_scatter_count_64_65_100_cols(self, count):
        """Column scatter exercises the stack/heap index-buffer boundary."""
        m = Matrix(2, 3, 0.0)
        idx = [1] * count
        rhs = Matrix(2, count, [float(i) for i in range(2 * count)])
        m[:, idx] = rhs
        assert m[0, 1] == pytest.approx(float(count - 1))
        assert m[1, 1] == pytest.approx(float(2 * count - 1))
        for r in range(2):
            assert m[r, 0] == pytest.approx(0.0)
            assert m[r, 2] == pytest.approx(0.0)

    def test_scatter_non_int_item(self):
        """A non-int list item raises TypeError (incl. heap-sized lists)."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(TypeError):
            m[[0, 1.5]] = 1.0
        with pytest.raises(TypeError):
            m[[0, None]] = 1.0
        with pytest.raises(TypeError):
            m[[0] * 70 + [1.5]] = 1.0

    def test_scatter_bool_item(self):
        """Bool list items are accepted as 1/0."""
        m = Matrix(2, 2, 0.0)
        m[[True, False]] = 3.0
        for c in range(2):
            assert m[0, c] == pytest.approx(3.0)
            assert m[1, c] == pytest.approx(3.0)

    def test_scatter_cols_single_row(self):
        """Column scatter degenerates correctly when R == 1."""
        m = Matrix(1, 3, 0.0)
        m[:, [0, 2]] = Matrix(1, 2, [4.0, 5.0])
        assert m[0, 0] == pytest.approx(4.0)
        assert m[0, 2] == pytest.approx(5.0)
        assert m[0, 1] == pytest.approx(0.0)

    def test_scatter_1x1_matrix_as_scalar(self):
        """A 1x1 matrix RHS broadcasts like a scalar."""
        m = Matrix(3, 2, 0.0)
        m[[0, 1]] = Matrix(1, 1, 8.0)
        for c in range(2):
            assert m[0, c] == pytest.approx(8.0)
            assert m[1, c] == pytest.approx(8.0)

    def test_scatter_empty_list_raises(self):
        """An empty list key raises IndexError (Option A)."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            m[[]] = 1.0
        with pytest.raises(IndexError):
            m[:, []] = 1.0

    def test_scatter_paired_list_unsupported(self):
        """m[[r], [c]] = v raises IndexError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            m[[0, 1], [0, 1]] = 1.0

    def test_scatter_mixed_list_int_unsupported(self):
        """m[[r], c] = v raises IndexError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            m[[0], 1] = 1.0

    def test_scatter_partial_slice_list_unsupported(self):
        """A list paired with a non-full slice raises IndexError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            m[1:3, [0]] = 1.0
        with pytest.raises(IndexError):
            m[[0], 0:2] = 1.0

    def test_scatter_bad_rhs_type(self):
        """A non-numeric, non-matrix RHS raises TypeError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(TypeError):
            m[[0]] = "x"

    def test_scatter_2d_assignment_unchanged(self):
        """Legacy int/slice/tuple assignment is unaffected."""
        m = Matrix(3, 3, 0.0)
        m[1, 2] = 5.0
        assert m[1, 2] == pytest.approx(5.0)
        m[0:2, 0:2] = 7.0
        for r in range(2):
            for c in range(2):
                assert m[r, c] == pytest.approx(7.0)

    def test_scatter_not_acquired_raises(self):
        """Scatter on a cown-resident (unacquired) matrix raises."""
        m = Matrix(3, 2, 1.0)
        Cown(m)
        with pytest.raises(RuntimeError):
            m[[0]] = 1.0

    def test_scatter_rhs_not_acquired_raises(self):
        """A cown-resident RHS matrix raises (ownership gate)."""
        m = Matrix(3, 2, 1.0)
        rhs = Matrix(1, 2, 2.0)
        Cown(rhs)
        with pytest.raises(RuntimeError):
            m[[0]] = rhs

    def test_scatter_boc_roundtrip(self):
        """Scatter runs inside a @when behavior over a Cown[Matrix]."""
        a = Cown(Matrix(3, 2, 0.0))

        @when(a)
        def result(a):  # noqa: D401 — short behavior
            """Scatter rows and a column inside a behavior."""
            a.value[[0, 2]] = 5.0
            a.value[:, [1]] = Matrix(3, 1, [7.0, 8.0, 9.0])
            return a.value[[0, 1, 2]]

        wait()
        assert result.exception is False
        expected = Matrix(3, 2, [5.0, 7.0, 0.0, 8.0, 5.0, 9.0])
        assert Matrix.allclose(result.value, expected)

    def test_scatter_fuzz(self, mat, shape, rng):
        """A random permutation scatter then gather returns the RHS."""
        rows, cols = shape
        perm = list(range(rows))
        rng.shuffle(perm)
        rhs = Matrix(rows, cols,
                     [rng.uniform(-50, 50) for _ in range(rows * cols)])
        mat[perm] = rhs
        assert Matrix.allclose(mat[perm], rhs)

    def test_scatter_self_alias_swaps_rows(self):
        """A self-aliased row scatter permutes from pre-write values."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m[[1, 0]] = m
        assert Matrix.allclose(m, Matrix(2, 2, [3.0, 4.0, 1.0, 2.0]))

    def test_scatter_self_alias_swaps_cols(self):
        """A self-aliased column scatter permutes from pre-write values."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m[:, [1, 0]] = m
        assert Matrix.allclose(m, Matrix(2, 2, [2.0, 1.0, 4.0, 3.0]))

    def test_scatter_self_alias_identity_unchanged(self):
        """A self-aliased identity scatter leaves the matrix unchanged."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m[[0, 1]] = m
        assert Matrix.allclose(m, Matrix(2, 2, [1.0, 2.0, 3.0, 4.0]))


class TestPut:
    """Explicit put() method — the write-side counterpart of take()."""

    def test_put_rows_scalar(self):
        """put(rows, scalar) fills the selected rows."""
        m = Matrix(3, 2, 0.0)
        m.put([0, 2], 5.0)
        for c in range(2):
            assert m[0, c] == pytest.approx(5.0)
            assert m[1, c] == pytest.approx(0.0)
            assert m[2, c] == pytest.approx(5.0)

    def test_put_rows_matrix(self):
        """put(rows, matrix) copies each source row."""
        m = Matrix(3, 2, 0.0)
        rhs = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m.put([2, 0], rhs)
        assert m[2, 0] == pytest.approx(1.0)
        assert m[0, 0] == pytest.approx(3.0)

    def test_put_cols_matrix(self):
        """put(cols, matrix, axis=1) copies each source column."""
        m = Matrix(2, 3, 0.0)
        rhs = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m.put([2, 0], rhs, axis=1)
        assert m[0, 2] == pytest.approx(1.0)
        assert m[0, 0] == pytest.approx(2.0)
        assert m[1, 2] == pytest.approx(3.0)
        assert m[1, 0] == pytest.approx(4.0)

    def test_put_matches_subscript_rows(self, mat, shape):
        """put(rows, v) equals m[rows] = v."""
        rows, cols = shape
        indices = [0, rows - 1] if rows > 1 else [0]
        a = mat.copy()
        b = mat.copy()
        rhs = Matrix(len(indices), cols,
                     [float(i) for i in range(len(indices) * cols)])
        a.put(indices, rhs)
        b[indices] = rhs
        assert Matrix.allclose(a, b)

    def test_put_matches_subscript_cols(self, mat, shape):
        """put(cols, v, axis=1) equals m[:, cols] = v."""
        rows, cols = shape
        indices = [0, cols - 1] if cols > 1 else [0]
        a = mat.copy()
        b = mat.copy()
        rhs = Matrix(rows, len(indices),
                     [float(i) for i in range(rows * len(indices))])
        a.put(indices, rhs, axis=1)
        b[:, indices] = rhs
        assert Matrix.allclose(a, b)

    def test_put_returns_self(self):
        """put returns self to allow chaining."""
        m = Matrix(3, 2, 0.0)
        result = m.put([0], 1.0)
        assert result is m

    def test_put_negative_axis(self):
        """A negative axis maps -1 to columns."""
        m = Matrix(2, 3, 0.0)
        m.put([0], 9.0, axis=-1)
        for r in range(2):
            assert m[r, 0] == pytest.approx(9.0)
            assert m[r, 1] == pytest.approx(0.0)

    def test_put_negative_index(self):
        """A negative row index assigns from the end."""
        m = Matrix(3, 2, 0.0)
        m.put([-1], 7.0)
        for c in range(2):
            assert m[2, c] == pytest.approx(7.0)

    def test_put_1x1_as_scalar(self):
        """A 1x1 matrix value broadcasts like a scalar."""
        m = Matrix(3, 2, 0.0)
        m.put([0, 1], Matrix(1, 1, 8.0))
        for c in range(2):
            assert m[0, c] == pytest.approx(8.0)
            assert m[1, c] == pytest.approx(8.0)

    def test_put_empty_raises(self):
        """An empty index list raises IndexError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(IndexError):
            m.put([], 1.0)

    def test_put_out_of_range_raises(self):
        """An out-of-range index raises IndexError and writes nothing."""
        m = Matrix(3, 3, 1.0)
        before = _snapshot(m, 3, 3)
        with pytest.raises(IndexError):
            m.put([0, 99], 5.0)
        assert _snapshot(m, 3, 3) == before

    def test_put_shape_mismatch_raises(self):
        """A wrong-shaped matrix value raises ValueError and writes nothing."""
        m = Matrix(3, 3, 1.0)
        before = _snapshot(m, 3, 3)
        with pytest.raises(ValueError):
            m.put([0, 1], Matrix(3, 3, 2.0))
        assert _snapshot(m, 3, 3) == before

    def test_put_invalid_axis_raises(self):
        """axis >= 2 raises KeyError, matching take()."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(KeyError):
            m.put([0], 1.0, 2)

    def test_put_axis_below_neg2_raises(self):
        """An out-of-range negative axis (<= -3) raises KeyError, not a column put."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(KeyError):
            m.put([0], 1.0, -3)

    def test_put_bad_value_type_raises(self):
        """A non-numeric, non-matrix value raises TypeError."""
        m = Matrix(3, 3, 1.0)
        with pytest.raises(TypeError):
            m.put([0], "x")

    def test_put_accumulate_scalar_rows(self):
        """accumulate=True folds a scalar additively over duplicate rows."""
        m = Matrix(3, 2, 1.0)
        m.put([0, 0, 2], 2.0, accumulate=True)
        assert Matrix.allclose(
            m, Matrix(3, 2, [5.0, 5.0, 1.0, 1.0, 3.0, 3.0]))

    def test_put_accumulate_matrix_rows(self):
        """accumulate=True adds matching value rows into duplicate rows."""
        m = Matrix(2, 2, 0.0)
        v = Matrix(3, 2, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        m.put([0, 0, 1], v, accumulate=True)
        # row 0 gets v rows 0 and 1; row 1 gets v row 2.
        assert Matrix.allclose(m, Matrix(2, 2, [4.0, 6.0, 5.0, 6.0]))

    def test_put_accumulate_scalar_cols(self):
        """accumulate=True folds a scalar additively over duplicate columns."""
        m = Matrix(2, 3, 1.0)
        m.put([1, 1], 2.0, axis=1, accumulate=True)
        assert Matrix.allclose(
            m, Matrix(2, 3, [1.0, 5.0, 1.0, 1.0, 5.0, 1.0]))

    def test_put_accumulate_matrix_cols(self):
        """accumulate=True adds matching value columns into duplicate cols."""
        m = Matrix(2, 2, 0.0)
        v = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        m.put([0, 0, 1], v, axis=1, accumulate=True)
        # col 0 gets v cols 0 and 1; col 1 gets v col 2.
        assert Matrix.allclose(m, Matrix(2, 2, [3.0, 3.0, 9.0, 6.0]))

    def test_put_accumulate_default_is_last_write_wins(self):
        """Without accumulate, duplicate indices remain last-write-wins."""
        m = Matrix(3, 2, 1.0)
        m.put([0, 0], 2.0)
        assert Matrix.allclose(
            m, Matrix(3, 2, [2.0, 2.0, 1.0, 1.0, 1.0, 1.0]))

    def test_put_accumulate_self_alias(self):
        """A self-aliased accumulate reads pre-write values via the snapshot."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m.put([0, 1], m, accumulate=True)
        # Each row doubles; the snapshot keeps reads stable under aliasing.
        assert Matrix.allclose(m, Matrix(2, 2, [2.0, 4.0, 6.0, 8.0]))

    def test_put_accumulate_self_alias_cols(self):
        """A self-aliased column accumulate reads stable pre-write columns."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        m.put([0, 1], m, axis=1, accumulate=True)
        # Each column doubles; the snapshot keeps reads stable under aliasing.
        assert Matrix.allclose(m, Matrix(2, 2, [2.0, 4.0, 6.0, 8.0]))

    def test_put_accumulate_self_alias_1x1(self):
        """A 1x1 self-slice accumulates as a scalar (captured pre-write)."""
        m = Matrix(1, 1, [3.0])
        m.put([0], m, accumulate=True)
        # The 1x1 RHS is read as a scalar before the write, so this doubles.
        assert Matrix.allclose(m, Matrix(1, 1, [6.0]))

    def test_put_accumulate_order_preserving(self):
        """Folds happen in index-list order, giving a bit-exact result."""
        m = Matrix(1, 1, [0.0])
        # Left-to-right: ((0 + 1e16) + 1.0) + -1e16 == 0.0, but a reordered
        # or contraction-fused fold would not cancel exactly.
        v = Matrix(3, 1, [1e16, 1.0, -1e16])
        m.put([0, 0, 0], v, accumulate=True)
        assert m[0, 0] == 0.0

    def test_put_not_acquired_raises(self):
        """put on a cown-resident matrix raises."""
        m = Matrix(3, 2, 1.0)
        Cown(m)
        with pytest.raises(RuntimeError):
            m.put([0], 1.0)

    def test_put_boc_roundtrip(self):
        """put runs inside a @when behavior over a Cown[Matrix]."""
        a = Cown(Matrix(3, 2, 0.0))

        @when(a)
        def result(a):  # noqa: D401 — short behavior
            """Put rows and a column inside a behavior."""
            a.value.put([0, 2], 5.0)
            a.value.put([1], Matrix(3, 1, [7.0, 8.0, 9.0]), axis=1)
            return a.value.copy()

        wait()
        assert result.exception is False
        expected = Matrix(3, 2, [5.0, 7.0, 0.0, 8.0, 5.0, 9.0])
        assert Matrix.allclose(result.value, expected)


VECTOR_LENGTHS = [1, 3, 5, 10, 32]


class TestVectorLen:
    """len() returns the number of rows for all matrices, including vectors."""

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_row_vector_len(self, n):
        """len() of a 1xN row vector returns 1 (the number of rows)."""
        v = Matrix(1, n, [float(i) for i in range(n)])
        assert len(v) == 1

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_column_vector_len(self, n):
        """len() of an Nx1 column vector returns N (the number of rows)."""
        v = Matrix(n, 1, [float(i) for i in range(n)])
        assert len(v) == n

    def test_matrix_len_returns_rows(self):
        """len() of a non-vector matrix returns rows."""
        m = Matrix(3, 4)
        assert len(m) == 3


class TestVectorItemAccess:
    """Integer indexing and iteration on vectors."""

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_row_vector_item(self, n):
        """Indexing a 1xN row vector with [0] returns the row as a Matrix."""
        vals = [float(i) * 1.5 for i in range(n)]
        v = Matrix(1, n, vals)
        row = v[0]
        if n == 1:
            assert isinstance(row, float)
            assert row == pytest.approx(vals[0])
        else:
            assert isinstance(row, Matrix)
            assert row.rows == 1
            assert row.columns == n
        for i in range(n):
            assert v[0, i] == pytest.approx(vals[i])

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_column_vector_item(self, n):
        """Indexing an Nx1 column vector with an integer returns the element as a float."""
        vals = [float(i) * 2.0 for i in range(n)]
        v = Matrix(n, 1, vals)
        for i in range(n):
            item = v[i]
            assert isinstance(item, float)
            assert item == pytest.approx(vals[i])

    @pytest.mark.parametrize("n", [3, 5, 10])
    def test_row_vector_iteration(self, n):
        """Iterating a 1xN row vector yields one Matrix (the single row)."""
        vals = [float(i) for i in range(n)]
        v = Matrix(1, n, vals)
        collected = list(v)
        assert len(collected) == 1
        assert isinstance(collected[0], Matrix)
        assert collected[0].rows == 1
        assert collected[0].columns == n

    @pytest.mark.parametrize("n", [3, 5, 10])
    def test_column_vector_iteration(self, n):
        """Iterating an Nx1 column vector yields individual float elements."""
        vals = [float(i) for i in range(n)]
        v = Matrix(n, 1, vals)
        collected = list(v)
        assert len(collected) == n
        for got, expected in zip(collected, vals):
            assert isinstance(got, float)
            assert got == pytest.approx(expected)


class TestRowVectorIndexing:
    """Subscript on a 1-row matrix behaves like any other matrix."""

    def test_row_vector_two_index_reads_element(self):
        """v[0, i] on a row vector reads column i."""
        v = Matrix(1, 5, [10.0, 20.0, 30.0, 40.0, 50.0])
        assert v[0, 2] == pytest.approx(30.0)
        assert v[0, 0] == pytest.approx(10.0)
        assert v[0, 4] == pytest.approx(50.0)

    def test_row_vector_row_slice(self):
        """v[0:1] on a 1-row matrix returns the whole row."""
        v = Matrix(1, 6, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        sub = v[0:1]
        assert sub.rows == 1
        assert sub.columns == 6
        assert sub[0, 0] == pytest.approx(1.0)
        assert sub[0, 5] == pytest.approx(6.0)


class TestVectorBroadcastArithmetic:
    """Arithmetic broadcasting with row and column vectors."""

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (10, 10)])
    def test_row_vector_add_broadcast(self, rows, cols):
        """A (1xcols) row vector added to (rowsxcols) broadcasts across rows."""
        m_vals = [float(i) for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v_vals = [float(j) * 100 for j in range(cols)]
        v = Matrix(1, cols, v_vals)
        result = m + v
        assert result.rows == rows
        assert result.columns == cols
        for i in range(rows):
            for j in range(cols):
                expected = m_vals[i * cols + j] + v_vals[j]
                assert result[i, j] == pytest.approx(expected)

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (10, 10)])
    def test_row_vector_subtract_broadcast(self, rows, cols):
        """Subtraction broadcasts a row vector across all rows."""
        m = Matrix.uniform(0.0, 10.0, size=(rows, cols))
        v = Matrix.uniform(0.0, 5.0, size=(1, cols))
        result = m - v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m[i, j] - v[0, j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (10, 10)])
    def test_row_vector_multiply_broadcast(self, rows, cols):
        """Multiplication broadcasts a row vector across all rows."""
        m = Matrix.uniform(1.0, 10.0, size=(rows, cols))
        v = Matrix.uniform(1.0, 5.0, size=(1, cols))
        result = m * v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m[i, j] * v[0, j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (10, 10)])
    def test_row_vector_divide_broadcast(self, rows, cols):
        """Division broadcasts a row vector across all rows."""
        m = Matrix.uniform(1.0, 10.0, size=(rows, cols))
        v = Matrix.uniform(1.0, 5.0, size=(1, cols))
        result = m / v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m[i, j] / v[0, j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (10, 10)])
    def test_column_vector_add_broadcast(self, rows, cols):
        """An (rowsx1) column vector added to (rowsxcols) broadcasts across columns."""
        m_vals = [float(i) for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v_vals = [float(i) * 100 for i in range(rows)]
        v = Matrix(rows, 1, v_vals)
        result = m + v
        assert result.rows == rows
        assert result.columns == cols
        for i in range(rows):
            for j in range(cols):
                expected = m_vals[i * cols + j] + v_vals[i]
                assert result[i, j] == pytest.approx(expected)

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (10, 10)])
    def test_column_vector_subtract_broadcast(self, rows, cols):
        """Subtraction broadcasts a column vector across all columns."""
        m = Matrix.uniform(0.0, 10.0, size=(rows, cols))
        v = Matrix.uniform(0.0, 5.0, size=(rows, 1))
        result = m - v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m[i, j] - v[i, 0])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (10, 10)])
    def test_column_vector_multiply_broadcast(self, rows, cols):
        """Multiplication broadcasts a column vector across all columns."""
        m = Matrix.uniform(1.0, 10.0, size=(rows, cols))
        v = Matrix.uniform(1.0, 5.0, size=(rows, 1))
        result = m * v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m[i, j] * v[i, 0])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (10, 10)])
    def test_column_vector_divide_broadcast(self, rows, cols):
        """Division broadcasts a column vector across all columns."""
        m = Matrix.uniform(1.0, 10.0, size=(rows, cols))
        v = Matrix.uniform(1.0, 5.0, size=(rows, 1))
        result = m / v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m[i, j] / v[i, 0])

    def test_row_vector_on_left_broadcasts(self):
        """v + M where v is a row vector still broadcasts correctly."""
        m = Matrix(3, 4, [float(i) for i in range(12)])
        v = Matrix(1, 4, [100.0, 200.0, 300.0, 400.0])
        result = v + m
        for i in range(3):
            for j in range(4):
                assert result[i, j] == pytest.approx(m[i, j] + v[0, j])

    def test_column_vector_on_left_broadcasts(self):
        """v + M where v is a column vector still broadcasts correctly."""
        m = Matrix(3, 4, [float(i) for i in range(12)])
        v = Matrix(3, 1, [100.0, 200.0, 300.0])
        result = v + m
        for i in range(3):
            for j in range(4):
                assert result[i, j] == pytest.approx(m[i, j] + v[i, 0])

    def test_broadcast_mismatch_raises(self):
        """Mismatched non-broadcastable shapes raise an error."""
        a = Matrix(3, 4)
        b = Matrix(2, 5)
        with pytest.raises(ValueError):
            _ = a + b


class TestVectorBroadcastAssignment:
    """Broadcasting when assigning a vector into a matrix slice."""

    def test_assign_row_vector_broadcasts(self):
        """Assigning a 1xcols vector into multiple rows broadcasts."""
        m = Matrix.zeros((4, 3))
        v = Matrix(1, 3, [10.0, 20.0, 30.0])
        m[:, :] = v
        for i in range(4):
            for j in range(3):
                assert m[i, j] == pytest.approx(v[0, j])

    def test_assign_column_vector_broadcasts(self):
        """Assigning an rowsx1 vector into multiple columns broadcasts."""
        m = Matrix.zeros((3, 5))
        v = Matrix(3, 1, [10.0, 20.0, 30.0])
        m[:, :] = v
        for i in range(3):
            for j in range(5):
                assert m[i, j] == pytest.approx(v[i, 0])

    def test_assign_row_vector_to_slice(self):
        """Assigning a row vector into a row-slice broadcasts correctly."""
        m = Matrix.zeros((5, 3))
        v = Matrix(1, 3, [1.0, 2.0, 3.0])
        m[1:4, :] = v
        for j in range(3):
            assert m[0, j] == pytest.approx(0.0)
            assert m[4, j] == pytest.approx(0.0)
        for i in range(1, 4):
            for j in range(3):
                assert m[i, j] == pytest.approx(v[0, j])

    def test_assign_column_vector_to_slice(self):
        """Assigning a column vector into a column-slice broadcasts correctly."""
        m = Matrix.zeros((3, 5))
        v = Matrix(3, 1, [7.0, 8.0, 9.0])
        m[:, 1:4] = v
        for i in range(3):
            assert m[i, 0] == pytest.approx(0.0)
            assert m[i, 4] == pytest.approx(0.0)
        for i in range(3):
            for j in range(1, 4):
                assert m[i, j] == pytest.approx(v[i, 0])


class TestTransposeProperty:
    """Tests for the .T shorthand property."""

    def test_t_equals_transpose(self, mat):
        """mat.T should give the same result as mat.transpose()."""
        assert Matrix.allclose(mat.T, mat.transpose())

    def test_t_shape(self, mat, shape):
        """mat.T should swap rows and columns."""
        t = mat.T
        assert t.rows == shape[1]
        assert t.columns == shape[0]

    def test_t_double_roundtrip(self, mat):
        """mat.T.T should equal the original matrix."""
        assert Matrix.allclose(mat.T.T, mat)


class TestVectorAggregation:
    """Aggregation methods on row and column vectors."""

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_row_vector_sum(self, n):
        """sum() on a row vector returns the sum of all elements."""
        vals = [float(i + 1) for i in range(n)]
        v = Matrix(1, n, vals)
        assert v.sum() == pytest.approx(sum(vals))

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_column_vector_sum(self, n):
        """sum() on a column vector returns the sum of all elements."""
        vals = [float(i + 1) for i in range(n)]
        v = Matrix(n, 1, vals)
        assert v.sum() == pytest.approx(sum(vals))

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_row_vector_mean(self, n):
        """mean() on a row vector returns the mean of all elements."""
        vals = [float(i + 1) for i in range(n)]
        v = Matrix(1, n, vals)
        assert v.mean() == pytest.approx(sum(vals) / n)

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_column_vector_mean(self, n):
        """mean() on a column vector returns the mean of all elements."""
        vals = [float(i + 1) for i in range(n)]
        v = Matrix(n, 1, vals)
        assert v.mean() == pytest.approx(sum(vals) / n)

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_row_vector_magnitude(self, n):
        """magnitude() on a row vector returns √(Σ v²)."""
        vals = [float(i + 1) for i in range(n)]
        v = Matrix(1, n, vals)
        expected = math.sqrt(sum(x * x for x in vals))
        assert v.magnitude() == pytest.approx(expected)

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_column_vector_magnitude(self, n):
        """magnitude() on a column vector returns √(Σ v²)."""
        vals = [float(i + 1) for i in range(n)]
        v = Matrix(n, 1, vals)
        expected = math.sqrt(sum(x * x for x in vals))
        assert v.magnitude() == pytest.approx(expected)


class TestVectorUnaryOps:
    """Unary operations on row and column vectors."""

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_row_vector_negate(self, n):
        """Negating a row vector negates every element."""
        vals = [float(i) - n / 2 for i in range(n)]
        v = Matrix(1, n, vals)
        neg = v.negate()
        assert neg.rows == 1
        assert neg.columns == n
        for i in range(n):
            assert neg[0, i] == pytest.approx(-vals[i])

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_column_vector_abs(self, n):
        """abs() on a column vector takes absolute value of every element."""
        vals = [float(i) - n / 2 for i in range(n)]
        v = Matrix(n, 1, vals)
        a = v.abs()
        assert a.rows == n
        assert a.columns == 1
        for i in range(n):
            assert a[i] == pytest.approx(abs(vals[i]))


class TestVectorMatmul:
    """Matrix multiply with row and column vectors."""

    @pytest.mark.parametrize("n", [1, 3, 5, 8])
    def test_row_times_column_dot_product(self, n):
        """(1xn) @ (nx1) yields a 1x1 matrix (dot product)."""
        a_vals = [float(i + 1) for i in range(n)]
        b_vals = [float(i + 1) * 2 for i in range(n)]
        a = Matrix(1, n, a_vals)
        b = Matrix(n, 1, b_vals)
        result = a @ b
        assert result.rows == 1
        assert result.columns == 1
        expected = sum(x * y for x, y in zip(a_vals, b_vals))
        assert result[0, 0] == pytest.approx(expected)

    @pytest.mark.parametrize("n", [1, 3, 5, 8])
    def test_column_times_row_outer_product(self, n):
        """(nx1) @ (1xn) yields an nxn matrix (outer product)."""
        a_vals = [float(i + 1) for i in range(n)]
        b_vals = [float(i + 1) * 2 for i in range(n)]
        a = Matrix(n, 1, a_vals)
        b = Matrix(1, n, b_vals)
        result = a @ b
        assert result.rows == n
        assert result.columns == n
        for i in range(n):
            for j in range(n):
                assert result[i, j] == pytest.approx(a_vals[i] * b_vals[j])

    def test_matrix_times_column_vector(self):
        """(MxN) @ (Nx1) yields an Mx1 column vector."""
        m, n = 4, 3
        mat = Matrix.uniform(0.0, 5.0, size=(m, n))
        v = Matrix.uniform(0.0, 5.0, size=(n, 1))
        result = mat @ v
        assert result.rows == m
        assert result.columns == 1
        for i in range(m):
            expected = sum(mat[i, j] * v[j] for j in range(n))
            assert result[i, 0] == pytest.approx(expected, rel=1e-9)

    def test_row_vector_times_matrix(self):
        """(1xM) @ (MxN) yields a 1xN row vector."""
        m, n = 3, 5
        v = Matrix.uniform(0.0, 5.0, size=(1, m))
        mat = Matrix.uniform(0.0, 5.0, size=(m, n))
        result = v @ mat
        assert result.rows == 1
        assert result.columns == n
        for j in range(n):
            expected = sum(v[0, i] * mat[i, j] for i in range(m))
            assert result[0, j] == pytest.approx(expected, rel=1e-9)


class TestVectorTranspose:
    """Transpose converts between row and column vectors."""

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_row_to_column(self, n):
        """Transposing a 1xn row vector produces an nx1 column vector."""
        vals = [float(i) for i in range(n)]
        v = Matrix(1, n, vals)
        t = v.transpose()
        assert t.rows == n
        assert t.columns == 1
        for i in range(n):
            assert t[i] == pytest.approx(vals[i])

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_column_to_row(self, n):
        """Transposing an nx1 column vector produces a 1xn row vector."""
        vals = [float(i) for i in range(n)]
        v = Matrix(n, 1, vals)
        t = v.transpose()
        assert t.rows == 1
        assert t.columns == n
        for i in range(n):
            assert t[0, i] == pytest.approx(vals[i])

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_vector_t_property(self, n):
        """The .T property works correctly for vectors."""
        vals = [float(i) for i in range(n)]
        row = Matrix(1, n, vals)
        col = row.T
        assert col.rows == n
        assert col.columns == 1
        assert Matrix.allclose(col.T, row)


class TestMinMax:
    """Tests for min() and max() aggregation methods."""

    def test_min_total(self, mat, random_values):
        """Verify total min of all elements."""
        assert mat.min() == pytest.approx(min(random_values))

    def test_max_total(self, mat, random_values):
        """Verify total max of all elements."""
        assert mat.max() == pytest.approx(max(random_values))

    def test_min_axis0(self, mat, shape, random_values):
        """min(axis=0) → 1 x cols vector (column minimums)."""
        s = mat.min(0)
        rows, cols = shape
        assert s.rows == 1
        assert s.columns == cols
        for j in range(cols):
            expected = min(random_values[i * cols + j] for i in range(rows))
            assert s[0, j] == pytest.approx(expected)

    def test_min_axis1(self, mat, shape, random_values):
        """min(axis=1) → rows x 1 vector (row minimums)."""
        s = mat.min(1)
        rows, cols = shape
        assert s.rows == rows
        assert s.columns == 1
        for i in range(rows):
            expected = min(random_values[i * cols + j] for j in range(cols))
            assert s[i, 0] == pytest.approx(expected)

    def test_max_axis0(self, mat, shape, random_values):
        """max(axis=0) → 1 x cols vector (column maximums)."""
        s = mat.max(0)
        rows, cols = shape
        assert s.rows == 1
        assert s.columns == cols
        for j in range(cols):
            expected = max(random_values[i * cols + j] for i in range(rows))
            assert s[0, j] == pytest.approx(expected)

    def test_max_axis1(self, mat, shape, random_values):
        """max(axis=1) → rows x 1 vector (row maximums)."""
        s = mat.max(1)
        rows, cols = shape
        assert s.rows == rows
        assert s.columns == 1
        for i in range(rows):
            expected = max(random_values[i * cols + j] for j in range(cols))
            assert s[i, 0] == pytest.approx(expected)

    def test_min_single_element(self):
        """min() on a 1x1 matrix returns the element."""
        m = Matrix(1, 1, [42.0])
        assert m.min() == pytest.approx(42.0)

    def test_max_single_element(self):
        """max() on a 1x1 matrix returns the element."""
        m = Matrix(1, 1, [42.0])
        assert m.max() == pytest.approx(42.0)

    def test_min_all_same(self, shape):
        """min() on a uniform matrix returns the uniform value."""
        rows, cols = shape
        m = Matrix(rows, cols, 7.5)
        assert m.min() == pytest.approx(7.5)

    def test_max_all_same(self, shape):
        """max() on a uniform matrix returns the uniform value."""
        rows, cols = shape
        m = Matrix(rows, cols, 7.5)
        assert m.max() == pytest.approx(7.5)


class TestClip:
    """Tests for the clip() method."""

    def test_clip_two_args(self, mat, shape, random_values):
        """clip(min, max) clamps every element to [min, max]."""
        rows, cols = shape
        lo, hi = -25.0, 25.0
        c = mat.clip(lo, hi)
        assert c.rows == rows
        assert c.columns == cols
        for i in range(rows):
            for j in range(cols):
                v = random_values[i * cols + j]
                expected = max(lo, min(hi, v))
                assert c[i, j] == pytest.approx(expected)

    def test_clip_min_only(self, shape, rng):
        """clip(min=lo) clamps only below, leaving the upper end unbounded."""
        rows, cols = shape
        vals = [rng.uniform(-10, 10) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        lo = -2.0
        c = m.clip(min=lo)
        for i in range(rows):
            for j in range(cols):
                v = vals[i * cols + j]
                assert c[i, j] == pytest.approx(max(lo, v))

    def test_clip_max_only(self, shape, rng):
        """clip(max=hi) clamps only above, leaving the lower end unbounded."""
        rows, cols = shape
        vals = [rng.uniform(-10, 10) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        hi = 5.0
        c = m.clip(max=hi)
        for i in range(rows):
            for j in range(cols):
                v = vals[i * cols + j]
                assert c[i, j] == pytest.approx(min(hi, v))

    def test_clip_first_positional_is_min(self, shape):
        """The single positional argument is the lower bound (NumPy order)."""
        rows, cols = shape
        m = Matrix(rows, cols, -5.0)
        c = m.clip(0.0)
        expected = Matrix(rows, cols, 0.0)
        assert Matrix.allclose(c, expected)

    def test_clip_no_bounds_raises(self):
        """clip() with neither bound raises ValueError."""
        m = Matrix(2, 2, 1.0)
        with pytest.raises(ValueError):
            m.clip()

    def test_clip_no_change(self, shape):
        """Values already within range are unchanged."""
        rows, cols = shape
        vals = [3.0] * (rows * cols)
        m = Matrix(rows, cols, vals)
        c = m.clip(0.0, 10.0)
        assert Matrix.allclose(c, m)

    def test_clip_all_below(self, shape):
        """All values below min are clamped to min."""
        rows, cols = shape
        m = Matrix(rows, cols, -5.0)
        c = m.clip(0.0, 10.0)
        expected = Matrix(rows, cols, 0.0)
        assert Matrix.allclose(c, expected)

    def test_clip_all_above(self, shape):
        """All values above max are clamped to max."""
        rows, cols = shape
        m = Matrix(rows, cols, 20.0)
        c = m.clip(0.0, 10.0)
        expected = Matrix(rows, cols, 10.0)
        assert Matrix.allclose(c, expected)

    def test_clip_invalid_range(self):
        """clip() raises AssertionError when max < min."""
        m = Matrix(2, 2, 1.0)
        with pytest.raises(AssertionError):
            m.clip(10.0, 0.0)


class TestCopy:
    """Tests for the copy() method."""

    def test_copy_values(self, mat):
        """copy() produces a matrix with identical values."""
        c = mat.copy()
        assert Matrix.allclose(c, mat)

    def test_copy_shape(self, mat, shape):
        """copy() preserves shape."""
        c = mat.copy()
        assert c.rows == shape[0]
        assert c.columns == shape[1]

    def test_copy_is_independent(self, shape, rng):
        """Mutating a copy does not affect the original."""
        rows, cols = shape
        vals = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        original = Matrix(rows, cols, vals)
        c = original.copy()
        c[0, 0] = 999999.0
        assert original[0, 0] == pytest.approx(vals[0])


class TestPickle:
    """Tests for pickling and copy.deepcopy support."""

    @staticmethod
    def _bit_image(m):
        """Native-endian byte image of every element, for bit-exact compares."""
        return b"".join(
            struct.pack("d", m[r, c])
            for r in range(m.rows)
            for c in range(m.columns)
        )

    # Special double bit patterns a raw-memcpy codec must preserve exactly:
    # NaN, +/-inf, negative zero, the smallest subnormal, and an ordinary value.
    SPECIAL_VALUES = [
        float("nan"),
        float("inf"),
        -float("inf"),
        -0.0,
        5e-324,
        1.5,
    ]

    @pytest.mark.parametrize("proto", range(pickle.HIGHEST_PROTOCOL + 1))
    def test_pickle_roundtrip_bit_exact(self, mat, shape, proto):
        """Pickling preserves shape and the exact bit image across protocols."""
        restored = pickle.loads(pickle.dumps(mat, protocol=proto))
        assert (restored.rows, restored.columns) == shape
        assert self._bit_image(restored) == self._bit_image(mat)

    @pytest.mark.parametrize("proto", range(pickle.HIGHEST_PROTOCOL + 1))
    def test_pickle_special_values_bit_exact(self, proto):
        """NaN, +/-inf, -0.0 and subnormals survive pickling bit-for-bit."""
        original = Matrix(1, len(self.SPECIAL_VALUES), self.SPECIAL_VALUES)
        restored = pickle.loads(pickle.dumps(original, protocol=proto))
        assert self._bit_image(restored) == self._bit_image(original)

    def test_pickle_roundtrip_independent(self, shape, rng):
        """A restored matrix does not share storage with the original."""
        rows, cols = shape
        vals = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        original = Matrix(rows, cols, vals)
        restored = pickle.loads(pickle.dumps(original))
        restored[0, 0] = 123456.0
        assert original[0, 0] == pytest.approx(vals[0])

    def test_pickle_inside_container(self, mat):
        """A Matrix nested in a container round-trips with its neighbours."""
        payload = {"m": mat, "n": 7, "nested": [Matrix(1, 1, [9.0])]}
        restored = pickle.loads(pickle.dumps(payload))
        assert restored["n"] == 7
        assert self._bit_image(restored["m"]) == self._bit_image(mat)
        assert restored["nested"][0][0, 0] == pytest.approx(9.0)

    def test_deepcopy_bit_exact(self, mat, shape):
        """copy.deepcopy reproduces shape and the exact bit image."""
        clone = copy.deepcopy(mat)
        assert (clone.rows, clone.columns) == shape
        assert self._bit_image(clone) == self._bit_image(mat)

    def test_deepcopy_special_values_bit_exact(self):
        """copy.deepcopy preserves special double bit patterns exactly."""
        original = Matrix(1, len(self.SPECIAL_VALUES), self.SPECIAL_VALUES)
        clone = copy.deepcopy(original)
        assert self._bit_image(clone) == self._bit_image(original)

    def test_deepcopy_is_independent(self, shape, rng):
        """Mutating a deepcopy does not affect the original."""
        rows, cols = shape
        vals = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        original = Matrix(rows, cols, vals)
        clone = copy.deepcopy(original)
        clone[0, 0] = 654321.0
        assert original[0, 0] == pytest.approx(vals[0])

    def test_unpickle_rejects_bad_length(self):
        """The reconstruct helper rejects a payload of the wrong size."""
        from bocpy import _math

        with pytest.raises(ValueError):
            _math._matrix_unpickle(2, 2, b"\x00" * 8)

    def test_unpickle_rejects_bad_dimensions(self):
        """The reconstruct helper rejects non-positive dimensions."""
        from bocpy import _math

        with pytest.raises(ValueError):
            _math._matrix_unpickle(0, 2, b"")

    def test_unpickle_rejects_wrong_payload_type(self):
        """The reconstruct helper rejects a non-buffer payload."""
        from bocpy import _math

        with pytest.raises(TypeError):
            _math._matrix_unpickle(1, 1, "not-bytes")

    def test_reduce_rejects_uninitialized(self):
        """__reduce__ on a __new__-only matrix raises rather than crashing."""
        bare = Matrix.__new__(Matrix)
        with pytest.raises(ValueError):
            bare.__reduce__()


class TestVector:
    """Tests for the Matrix.vector() factory function."""

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_vector_from_list(self, n):
        """Matrix.vector() creates a 1xN row vector from a list."""
        vals = [float(i) * 1.5 for i in range(n)]
        v = Matrix.vector(vals)
        assert v.rows == 1
        assert v.columns == n
        for i in range(n):
            assert v[0, i] == pytest.approx(vals[i])

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_vector_from_tuple(self, n):
        """Matrix.vector() accepts a tuple of values."""
        vals = tuple(float(i) * 2.0 for i in range(n))
        v = Matrix.vector(vals)
        assert v.rows == 1
        assert v.columns == n
        for i in range(n):
            assert v[0, i] == pytest.approx(vals[i])

    def test_vector_from_ints(self):
        """Matrix.vector() accepts integer values."""
        v = Matrix.vector([1, 2, 3, 4, 5])
        assert v.rows == 1
        assert v.columns == 5
        for i in range(5):
            assert v[0, i] == pytest.approx(float(i + 1))

    def test_vector_single_element(self):
        """Matrix.vector() with a single element creates a 1x1 matrix."""
        v = Matrix.vector([42.0])
        assert v.rows == 1
        assert v.columns == 1
        assert v[0, 0] == pytest.approx(42.0)

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_vector_as_column_from_list(self, n):
        """Matrix.vector(vals, True) creates an Nx1 column vector from a list."""
        vals = [float(i) * 1.5 for i in range(n)]
        v = Matrix.vector(vals, True)
        assert v.rows == n
        assert v.columns == 1
        for i in range(n):
            assert v[i] == pytest.approx(vals[i])

    @pytest.mark.parametrize("n", VECTOR_LENGTHS)
    def test_vector_as_column_from_tuple(self, n):
        """Matrix.vector(vals, True) accepts a tuple of values."""
        vals = tuple(float(i) * 2.0 for i in range(n))
        v = Matrix.vector(vals, True)
        assert v.rows == n
        assert v.columns == 1
        for i in range(n):
            assert v[i] == pytest.approx(vals[i])

    def test_vector_as_column_single_element(self):
        """Matrix.vector([x], True) creates a 1x1 matrix."""
        v = Matrix.vector([42.0], True)
        assert v.rows == 1
        assert v.columns == 1
        assert v[0, 0] == pytest.approx(42.0)

    def test_vector_as_column_false_is_row(self):
        """Matrix.vector(vals, False) explicitly creates a row vector."""
        vals = [1.0, 2.0, 3.0]
        v = Matrix.vector(vals, False)
        assert v.rows == 1
        assert v.columns == 3
        for i in range(3):
            assert v[0, i] == pytest.approx(vals[i])

    def test_vector_empty_raises(self):
        """Matrix.vector([]) cannot form a matrix and raises ValueError."""
        with pytest.raises(ValueError):
            Matrix.vector([])


class TestConcat:
    """Tests for Matrix.concat() — concatenation along rows or columns."""

    def test_concat_rows_two_matrices(self):
        """Concatenating two matrices along axis 0 stacks rows."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        b = Matrix(2, 3, [7.0, 8.0, 9.0, 10.0, 11.0, 12.0])
        result = Matrix.concat([a, b])
        assert result.rows == 4
        assert result.columns == 3
        for i in range(2):
            for j in range(3):
                assert result[i, j] == pytest.approx(a[i, j])
        for i in range(2):
            for j in range(3):
                assert result[i + 2, j] == pytest.approx(b[i, j])

    def test_concat_rows_three_matrices(self):
        """Concatenating three matrices along axis 0."""
        a = Matrix(1, 2, [1.0, 2.0])
        b = Matrix(1, 2, [3.0, 4.0])
        c = Matrix(1, 2, [5.0, 6.0])
        result = Matrix.concat([a, b, c])
        assert result.rows == 3
        assert result.columns == 2
        assert result[0, 0] == pytest.approx(1.0)
        assert result[1, 0] == pytest.approx(3.0)
        assert result[2, 0] == pytest.approx(5.0)

    def test_concat_columns_two_matrices(self):
        """Concatenating two matrices along axis 1 stacks columns."""
        a = Matrix(3, 2, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        b = Matrix(3, 2, [7.0, 8.0, 9.0, 10.0, 11.0, 12.0])
        result = Matrix.concat([a, b], 1)
        assert result.rows == 3
        assert result.columns == 4
        for i in range(3):
            for j in range(2):
                assert result[i, j] == pytest.approx(a[i, j])
        for i in range(3):
            for j in range(2):
                assert result[i, j + 2] == pytest.approx(b[i, j])

    def test_concat_columns_three_matrices(self):
        """Concatenating three matrices along axis 1."""
        a = Matrix(2, 1, [1.0, 2.0])
        b = Matrix(2, 1, [3.0, 4.0])
        c = Matrix(2, 1, [5.0, 6.0])
        result = Matrix.concat([a, b, c], 1)
        assert result.rows == 2
        assert result.columns == 3
        assert result[0, 0] == pytest.approx(1.0)
        assert result[0, 1] == pytest.approx(3.0)
        assert result[0, 2] == pytest.approx(5.0)

    def test_concat_single_matrix(self):
        """Concatenating a single matrix returns a copy."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        result = Matrix.concat([a])
        assert result.rows == 2
        assert result.columns == 3
        assert Matrix.allclose(result, a)

    def test_concat_with_list_as_row(self):
        """Concatenating a list along axis 0 treats it as a 1xN row vector."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        result = Matrix.concat([a, [7.0, 8.0, 9.0]])
        assert result.rows == 3
        assert result.columns == 3
        for j in range(3):
            assert result[2, j] == pytest.approx(float(j + 7))

    def test_concat_with_list_as_column(self):
        """Concatenating a list along axis 1 treats it as an Nx1 column vector."""
        a = Matrix(3, 2, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        result = Matrix.concat([a, [10.0, 20.0, 30.0]], 1)
        assert result.rows == 3
        assert result.columns == 3
        for i in range(3):
            assert result[i, 2] == pytest.approx((i + 1) * 10.0)

    def test_concat_with_tuple(self):
        """Concatenating with a tuple works like with a list."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        result = Matrix.concat((a, [7.0, 8.0, 9.0]))
        assert result.rows == 3
        assert result.columns == 3

    def test_concat_negative_axis(self):
        """axis=-1 should behave like axis=1."""
        a = Matrix(3, 2, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        b = Matrix(3, 2, [7.0, 8.0, 9.0, 10.0, 11.0, 12.0])
        result_pos = Matrix.concat([a, b], 1)
        result_neg = Matrix.concat([a, b], -1)
        assert Matrix.allclose(result_pos, result_neg)

    def test_concat_axis_as_kwarg(self):
        """axis may be passed as a keyword argument."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [5.0, 6.0, 7.0, 8.0])
        by_kw = Matrix.concat([a, b], axis=1)
        by_pos = Matrix.concat([a, b], 1)
        assert by_kw.rows == 2
        assert by_kw.columns == 4
        assert Matrix.allclose(by_kw, by_pos)

    def test_concat_values_as_kwarg(self):
        """values may also be passed as a keyword argument."""
        a = Matrix(1, 2, [1.0, 2.0])
        b = Matrix(1, 2, [3.0, 4.0])
        result = Matrix.concat(values=[a, b], axis=0)
        assert result.rows == 2
        assert result.columns == 2

    def test_concat_empty_returns_none(self):
        """Concatenating an empty list returns None."""
        result = Matrix.concat([])
        assert result is None

    def test_concat_rows_mismatched_columns_raises(self):
        """Concatenating matrices with different column counts raises."""
        a = Matrix(2, 3, 1.0)
        b = Matrix(2, 4, 1.0)
        with pytest.raises(AssertionError):
            Matrix.concat([a, b])

    def test_concat_columns_mismatched_rows_raises(self):
        """Concatenating matrices with different row counts along axis 1 raises."""
        a = Matrix(2, 3, 1.0)
        b = Matrix(3, 3, 1.0)
        with pytest.raises(AssertionError):
            Matrix.concat([a, b], 1)

    def test_concat_invalid_axis_raises(self):
        """axis >= 2 should raise KeyError."""
        a = Matrix(2, 2, 1.0)
        with pytest.raises(KeyError):
            Matrix.concat([a], 2)

    def test_concat_preserves_values_large(self):
        """Concatenation preserves all values for larger matrices."""
        a = Matrix.uniform(0.0, 10.0, size=(5, 8))
        b = Matrix.uniform(0.0, 10.0, size=(3, 8))
        result = Matrix.concat([a, b])
        assert result.rows == 8
        assert result.columns == 8
        for i in range(5):
            for j in range(8):
                assert result[i, j] == pytest.approx(a[i, j])
        for i in range(3):
            for j in range(8):
                assert result[i + 5, j] == pytest.approx(b[i, j])

    def test_concat_columns_preserves_values_large(self):
        """Column concatenation preserves all values for larger matrices."""
        a = Matrix.uniform(0.0, 10.0, size=(5, 3))
        b = Matrix.uniform(0.0, 10.0, size=(5, 4))
        result = Matrix.concat([a, b], 1)
        assert result.rows == 5
        assert result.columns == 7
        for i in range(5):
            for j in range(3):
                assert result[i, j] == pytest.approx(a[i, j])
            for j in range(4):
                assert result[i, j + 3] == pytest.approx(b[i, j])


def _flat_argextreme(values, want_max):
    """Reference flat row-major arg-extreme with first-occurrence ties."""
    best_i = 0
    best = values[0]
    for i, v in enumerate(values):
        if (v > best) if want_max else (v < best):
            best = v
            best_i = i
    return best_i


class TestArgExtreme:
    """Tests for Matrix.argmin and Matrix.argmax."""

    def test_argmin_no_axis_golden(self):
        """Flat argmin returns the row-major index of the minimum."""
        m = Matrix(2, 3, [3.0, 1.0, 2.0, 0.0, 9.0, 4.0])
        assert m.argmin() == 3
        assert isinstance(m.argmin(), int)

    def test_argmax_no_axis_golden(self):
        """Flat argmax returns the row-major index of the maximum."""
        m = Matrix(2, 3, [3.0, 1.0, 2.0, 0.0, 9.0, 4.0])
        assert m.argmax() == 4
        assert isinstance(m.argmax(), int)

    def test_argmin_ties_first_occurrence(self):
        """A tied minimum resolves to the first (lowest) flat index."""
        m = Matrix(1, 4, [5.0, 1.0, 1.0, 5.0])
        assert m.argmin() == 1

    def test_argmax_ties_first_occurrence(self):
        """A tied maximum resolves to the first (lowest) flat index."""
        m = Matrix(1, 4, [9.0, 3.0, 9.0, 1.0])
        assert m.argmax() == 0

    def test_argmin_axis0_golden(self):
        """argmin(axis=0) returns per-column row indices as a 1xcols matrix."""
        m = Matrix(2, 3, [3.0, 1.0, 2.0, 0.0, 9.0, 4.0])
        result = m.argmin(axis=0)
        assert result.rows == 1
        assert result.columns == 3
        assert [result[0, c] for c in range(3)] == [1.0, 0.0, 0.0]

    def test_argmax_axis1_golden(self):
        """argmax(axis=1) returns per-row column indices as a rowsx1 matrix."""
        m = Matrix(2, 3, [3.0, 1.0, 2.0, 0.0, 9.0, 4.0])
        result = m.argmax(axis=1)
        assert result.rows == 2
        assert result.columns == 1
        assert [result[r, 0] for r in range(2)] == [0.0, 1.0]

    def test_argmin_negative_axis(self):
        """axis=-1 behaves like axis=1 and axis=-2 like axis=0."""
        m = Matrix(3, 2, [4.0, 1.0, 2.0, 8.0, 7.0, 3.0])
        assert Matrix.allclose(m.argmin(axis=-1), m.argmin(axis=1))
        assert Matrix.allclose(m.argmin(axis=-2), m.argmin(axis=0))

    @pytest.mark.parametrize("want_max", [False, True])
    def test_argextreme_no_axis_fuzz(self, mat, shape, random_values, want_max):
        """Flat arg-extreme matches a Python reference across many shapes."""
        result = mat.argmax() if want_max else mat.argmin()
        assert result == _flat_argextreme(random_values, want_max)

    @pytest.mark.parametrize("want_max", [False, True])
    def test_argextreme_axis0_fuzz(self, mat, shape, random_values, want_max):
        """Per-column arg-extreme matches a Python reference."""
        rows, cols = shape
        result = mat.argmax(axis=0) if want_max else mat.argmin(axis=0)
        assert result.rows == 1
        assert result.columns == cols
        for c in range(cols):
            column = [random_values[r * cols + c] for r in range(rows)]
            assert result[0, c] == _flat_argextreme(column, want_max)

    @pytest.mark.parametrize("want_max", [False, True])
    def test_argextreme_axis1_fuzz(self, mat, shape, random_values, want_max):
        """Per-row arg-extreme matches a Python reference."""
        rows, cols = shape
        result = mat.argmax(axis=1) if want_max else mat.argmin(axis=1)
        assert result.rows == rows
        assert result.columns == 1
        for r in range(rows):
            row = [random_values[r * cols + c] for c in range(cols)]
            assert result[r, 0] == _flat_argextreme(row, want_max)

    def test_argmin_invalid_axis_raises(self):
        """An out-of-range axis raises ValueError."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(ValueError):
            m.argmin(axis=2)

    def test_argextreme_nan_in_middle_is_skipped(self):
        """A NaN that is not the running extreme is ignored (strict compares)."""
        nan = float("nan")
        m = Matrix(1, 4, [3.0, nan, 1.0, 2.0])
        assert m.argmin() == 2
        assert m.argmax() == 0

    def test_argextreme_leading_nan_pins_result(self):
        """A NaN at element 0 pins the index there (differs from NumPy)."""
        nan = float("nan")
        m = Matrix(1, 3, [nan, 1.0, 2.0])
        assert m.argmin() == 0
        assert m.argmax() == 0


def _outer_op(op, row_vals, col_vals):
    """Reference RxC outer broadcast: out[r,c] = op(col[r], row[c])."""
    return [[op(col_vals[r], row_vals[c]) for c in range(len(row_vals))]
            for r in range(len(col_vals))]


class TestOuterBroadcast:
    """Full (outer) broadcast of a 1xC row vector against an Rx1 column vector."""

    @pytest.mark.parametrize("rows,cols", [(2, 3), (4, 1), (1, 4), (5, 6)])
    def test_outer_multiply_col_times_row(self, rows, cols):
        """colvec * rowvec yields RxC where out[r,c] = col[r] * row[c]."""
        col_vals = [float(r + 1) for r in range(rows)]
        row_vals = [float(c + 1) * 10 for c in range(cols)]
        col = Matrix(rows, 1, col_vals)
        row = Matrix(1, cols, row_vals)
        result = col * row
        assert result.rows == rows
        assert result.columns == cols
        expected = _outer_op(lambda a, b: a * b, row_vals, col_vals)
        for r in range(rows):
            for c in range(cols):
                assert result[r, c] == pytest.approx(expected[r][c])

    @pytest.mark.parametrize("rows,cols", [(2, 3), (5, 6)])
    def test_outer_multiply_row_times_col(self, rows, cols):
        """rowvec * colvec is commutative and yields the same RxC result."""
        col_vals = [float(r + 1) for r in range(rows)]
        row_vals = [float(c + 1) * 10 for c in range(cols)]
        col = Matrix(rows, 1, col_vals)
        row = Matrix(1, cols, row_vals)
        result = row * col
        expected = _outer_op(lambda a, b: a * b, row_vals, col_vals)
        for r in range(rows):
            for c in range(cols):
                assert result[r, c] == pytest.approx(expected[r][c])

    @pytest.mark.parametrize("rows,cols", [(2, 3), (5, 6)])
    def test_outer_subtract_preserves_operand_order(self, rows, cols):
        """col - row uses out[r,c] = col[r] - row[c] (non-commutative)."""
        col_vals = [float(r + 1) for r in range(rows)]
        row_vals = [float(c + 1) * 10 for c in range(cols)]
        col = Matrix(rows, 1, col_vals)
        row = Matrix(1, cols, row_vals)
        result = col - row
        expected = _outer_op(lambda a, b: a - b, row_vals, col_vals)
        for r in range(rows):
            for c in range(cols):
                assert result[r, c] == pytest.approx(expected[r][c])

    @pytest.mark.parametrize("rows,cols", [(2, 3), (5, 6)])
    def test_outer_subtract_row_minus_col(self, rows, cols):
        """row - col uses out[r,c] = row[c] - col[r] (reflected order)."""
        col_vals = [float(r + 1) for r in range(rows)]
        row_vals = [float(c + 1) * 10 for c in range(cols)]
        col = Matrix(rows, 1, col_vals)
        row = Matrix(1, cols, row_vals)
        result = row - col
        for r in range(rows):
            for c in range(cols):
                assert result[r, c] == pytest.approx(row_vals[c] - col_vals[r])

    @pytest.mark.parametrize("rows,cols", [(2, 3), (5, 6)])
    def test_outer_divide_operand_order(self, rows, cols):
        """col / row uses out[r,c] = col[r] / row[c]."""
        col_vals = [float(r + 2) for r in range(rows)]
        row_vals = [float(c + 3) for c in range(cols)]
        col = Matrix(rows, 1, col_vals)
        row = Matrix(1, cols, row_vals)
        result = col / row
        for r in range(rows):
            for c in range(cols):
                assert result[r, c] == pytest.approx(col_vals[r] / row_vals[c])

    @pytest.mark.parametrize("rows,cols", [(2, 3), (5, 6)])
    def test_outer_add_commutative(self, rows, cols):
        """col + row equals row + col element-wise."""
        col = Matrix(rows, 1, [float(r + 1) for r in range(rows)])
        row = Matrix(1, cols, [float(c + 1) * 10 for c in range(cols)])
        assert Matrix.allclose(col + row, row + col)

    def test_outer_inplace_raises(self):
        """In-place outer broadcast would change shape and is rejected."""
        col = Matrix(3, 1, [1.0, 2.0, 3.0])
        row = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(ValueError):
            col *= row

    def test_incompatible_shapes_still_raise(self):
        """A row vector whose width mismatches a full matrix still raises."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(2, 4, 1.0)
        with pytest.raises(ValueError):
            _ = a * b


class TestAllcloseExtended:
    """Extended tests for allclose: tolerances, NaN, shape mismatch."""

    def test_nan_allclose_default(self):
        """NaN pairs pass allclose by default (IEEE 754: NaN > x is always False)."""
        a = Matrix(2, 2, [1.0, float("nan"), 3.0, float("nan")])
        b = Matrix(2, 2, [1.0, float("nan"), 3.0, float("nan")])
        assert Matrix.allclose(a, b)

    def test_equal_nan_true(self):
        """allclose with equal_nan=True accepts NaN-containing matrices."""
        a = Matrix(2, 2, [1.0, float("nan"), 3.0, float("nan")])
        b = Matrix(2, 2, [1.0, float("nan"), 3.0, float("nan")])
        assert Matrix.allclose(a, b, equal_nan=True)

    def test_equal_nan_false(self):
        """allclose with equal_nan=False on NaN elements."""
        a = Matrix(2, 2, [1.0, float("nan"), 3.0, float("nan")])
        b = Matrix(2, 2, [1.0, float("nan"), 3.0, float("nan")])
        result = Matrix.allclose(a, b, equal_nan=False)
        assert isinstance(result, bool)

    def test_allclose_custom_tolerances(self):
        """allclose with explicit rtol and atol parameters."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [1.1, 2.1, 3.1, 4.1])
        assert not Matrix.allclose(a, b, rtol=0.0, atol=0.05)
        assert Matrix.allclose(a, b, rtol=0.0, atol=0.15)

    def test_allclose_shape_mismatch(self):
        """allclose returns False for different shapes."""
        a = Matrix(2, 3)
        b = Matrix(3, 2)
        assert not Matrix.allclose(a, b)

    def test_allclose_rtol(self):
        """allclose with relative tolerance."""
        a = Matrix(1, 3, [100.0, 200.0, 300.0])
        b = Matrix(1, 3, [101.0, 202.0, 303.0])
        assert Matrix.allclose(a, b, rtol=0.02, atol=0.0)


class TestUniformDefaults:
    """Tests for Matrix.uniform() with default arguments."""

    def test_uniform_no_size_returns_float(self):
        """Matrix.uniform() with no size returns a scalar float."""
        val = Matrix.uniform()
        assert isinstance(val, float)

    def test_uniform_defaults_range(self):
        """Matrix.uniform() with defaults returns a value in [0, 1)."""
        for _ in range(100):
            val = Matrix.uniform()
            assert 0.0 <= val < 1.0

    def test_uniform_custom_range_no_size(self):
        """Matrix.uniform(minval, maxval) with no size returns a scalar float."""
        for _ in range(100):
            val = Matrix.uniform(5.0, 10.0)
            assert isinstance(val, float)
            assert 5.0 <= val < 10.0


class TestMatrixIteration:
    """Tests for __iter__ on multi-row (non-vector) matrices."""

    def test_iter_yields_rows(self):
        """Iterating a multi-row matrix yields 1-row matrices."""
        m = Matrix(3, 4, [float(i) for i in range(12)])
        rows = list(m)
        assert len(rows) == 3
        for r in rows:
            assert isinstance(r, Matrix)
            assert r.rows == 1
            assert r.columns == 4

    def test_iter_row_values(self):
        """Each iterated row contains the correct values."""
        vals = [float(i) for i in range(12)]
        m = Matrix(3, 4, vals)
        for i, row in enumerate(m):
            for j in range(4):
                assert row[0, j] == pytest.approx(vals[i * 4 + j])

    def test_iter_single_column_matrix(self):
        """Iterating an Nx1 column vector yields floats."""
        vals = [10.0, 20.0, 30.0]
        m = Matrix(3, 1, vals)
        collected = list(m)
        assert len(collected) == 3
        for got, expected in zip(collected, vals):
            assert isinstance(got, float)
            assert got == pytest.approx(expected)


class TestScalarBinaryArithmetic:
    """Verify scalar arithmetic in both orderings.

    Plain int and float scalars work with all four operators
    in both orderings (matrix op scalar AND scalar op matrix).
    """

    @pytest.mark.parametrize("scalar", [3, 2.5])
    def test_matrix_add_scalar(self, mat, shape, scalar):
        """mat + scalar broadcasts the scalar to every element."""
        c = mat + scalar
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(mat[i, j] + scalar)

    @pytest.mark.parametrize("scalar", [3, 2.5])
    def test_scalar_add_matrix(self, mat, shape, scalar):
        """scalar + mat (reflected add) broadcasts the scalar."""
        c = scalar + mat
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(scalar + mat[i, j])

    @pytest.mark.parametrize("scalar", [3, 2.5])
    def test_matrix_sub_scalar(self, mat, shape, scalar):
        """mat - scalar broadcasts the scalar to every element."""
        c = mat - scalar
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(mat[i, j] - scalar)

    @pytest.mark.parametrize("scalar", [3, 2.5])
    def test_scalar_sub_matrix(self, mat, shape, scalar):
        """scalar - mat (reflected subtract) is scalar minus each element."""
        c = scalar - mat
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(scalar - mat[i, j])

    @pytest.mark.parametrize("scalar", [3, 2.5])
    def test_matrix_mul_scalar(self, mat, shape, scalar):
        """mat * scalar broadcasts the scalar to every element."""
        c = mat * scalar
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(mat[i, j] * scalar)

    @pytest.mark.parametrize("scalar", [3, 2.5])
    def test_scalar_mul_matrix(self, mat, shape, scalar):
        """scalar * mat (reflected multiply) broadcasts the scalar."""
        c = scalar * mat
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(scalar * mat[i, j])

    @pytest.mark.parametrize("scalar", [3, 2.5])
    def test_matrix_div_scalar(self, mat, shape, scalar):
        """mat / scalar broadcasts the scalar to every element."""
        c = mat / scalar
        rows, cols = shape
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(mat[i, j] / scalar)

    @pytest.mark.parametrize("scalar", [3, 2.5])
    def test_scalar_div_matrix(self, shape, rng, scalar):
        """scalar / mat (reflected divide) is scalar divided by each element."""
        rows, cols = shape
        vals = [rng.uniform(1, 50) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        c = scalar / m
        for i in range(rows):
            for j in range(cols):
                assert c[i, j] == pytest.approx(scalar / vals[i * cols + j])


class TestScalarInplaceArithmetic:
    """Verify in-place operators with plain int/float scalars."""

    @pytest.mark.parametrize("scalar", [7, 1.5])
    def test_iadd_scalar(self, shape, rng, scalar):
        """mat += scalar adds the scalar to every element in-place."""
        rows, cols = shape
        vals = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        m += scalar
        for i in range(rows):
            for j in range(cols):
                assert m[i, j] == pytest.approx(vals[i * cols + j] + scalar)

    @pytest.mark.parametrize("scalar", [7, 1.5])
    def test_isub_scalar(self, shape, rng, scalar):
        """mat -= scalar subtracts the scalar from every element in-place."""
        rows, cols = shape
        vals = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        m -= scalar
        for i in range(rows):
            for j in range(cols):
                assert m[i, j] == pytest.approx(vals[i * cols + j] - scalar)

    @pytest.mark.parametrize("scalar", [7, 1.5])
    def test_imul_scalar(self, shape, rng, scalar):
        """mat *= scalar multiplies every element by the scalar in-place."""
        rows, cols = shape
        vals = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        m *= scalar
        for i in range(rows):
            for j in range(cols):
                assert m[i, j] == pytest.approx(vals[i * cols + j] * scalar)

    @pytest.mark.parametrize("scalar", [7, 1.5])
    def test_itruediv_scalar(self, shape, rng, scalar):
        """mat /= scalar divides every element by the scalar in-place."""
        rows, cols = shape
        vals = [rng.uniform(-50, 50) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        m /= scalar
        for i in range(rows):
            for j in range(cols):
                assert m[i, j] == pytest.approx(vals[i * cols + j] / scalar)


class TestListTupleBinaryArithmetic:
    """Verify list/tuple broadcast arithmetic.

    A plain list or tuple of numbers is interpreted as a 1xN row vector
    and broadcast across rows for all four operators.
    """

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (1, 5)])
    def test_matrix_add_list(self, rows, cols):
        """mat + [list] broadcasts the list as a row vector."""
        m_vals = [float(i) for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = [float(j) * 100 for j in range(cols)]
        result = m + v
        assert result.rows == rows
        assert result.columns == cols
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m_vals[i * cols + j] + v[j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (1, 5)])
    def test_matrix_sub_list(self, rows, cols):
        """mat - [list] broadcasts the list as a row vector."""
        m_vals = [float(i) for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = [float(j) * 10 for j in range(cols)]
        result = m - v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m_vals[i * cols + j] - v[j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (1, 5)])
    def test_matrix_mul_list(self, rows, cols):
        """mat * [list] broadcasts the list as a row vector."""
        m_vals = [float(i) + 1 for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = [float(j) + 1 for j in range(cols)]
        result = m * v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m_vals[i * cols + j] * v[j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3), (1, 5)])
    def test_matrix_div_list(self, rows, cols):
        """mat / [list] broadcasts the list as a row vector."""
        m_vals = [float(i) + 1 for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = [float(j) + 1 for j in range(cols)]
        result = m / v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m_vals[i * cols + j] / v[j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3)])
    def test_matrix_add_tuple(self, rows, cols):
        """mat + (tuple,) broadcasts the tuple as a row vector."""
        m_vals = [float(i) for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = tuple(float(j) * 100 for j in range(cols))
        result = m + v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m_vals[i * cols + j] + v[j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3)])
    def test_matrix_sub_tuple(self, rows, cols):
        """mat - (tuple,) broadcasts the tuple as a row vector."""
        m_vals = [float(i) for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = tuple(float(j) * 10 for j in range(cols))
        result = m - v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m_vals[i * cols + j] - v[j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3)])
    def test_matrix_mul_tuple(self, rows, cols):
        """mat * (tuple,) broadcasts the tuple as a row vector."""
        m_vals = [float(i) + 1 for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = tuple(float(j) + 1 for j in range(cols))
        result = m * v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m_vals[i * cols + j] * v[j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3)])
    def test_matrix_div_tuple(self, rows, cols):
        """mat / (tuple,) broadcasts the tuple as a row vector."""
        m_vals = [float(i) + 1 for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = tuple(float(j) + 1 for j in range(cols))
        result = m / v
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(m_vals[i * cols + j] / v[j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3)])
    def test_list_add_matrix(self, rows, cols):
        """[list] + mat (reflected) broadcasts the list as a row vector."""
        m_vals = [float(i) for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = [float(j) * 100 for j in range(cols)]
        result = v + m
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(v[j] + m_vals[i * cols + j])

    @pytest.mark.parametrize("rows,cols", [(3, 4), (5, 3)])
    def test_list_sub_matrix(self, rows, cols):
        """[list] - mat (reflected) broadcasts the list as a row vector."""
        m_vals = [float(i) + 1 for i in range(rows * cols)]
        m = Matrix(rows, cols, m_vals)
        v = [float(j) * 100 for j in range(cols)]
        result = v - m
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(v[j] - m_vals[i * cols + j])

    def test_inplace_add_list(self):
        """mat += [list] broadcasts the list as a row vector in-place."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        v = [100.0, 200.0, 300.0]
        m += v
        for i in range(3):
            for j in range(3):
                assert m[i, j] == pytest.approx(float(i * 3 + j) + v[j])

    def test_inplace_mul_tuple(self):
        """mat *= (tuple,) broadcasts the tuple as a row vector in-place."""
        m = Matrix(3, 3, [float(i) + 1 for i in range(9)])
        v = (2.0, 3.0, 4.0)
        m *= v
        for i in range(3):
            for j in range(3):
                assert m[i, j] == pytest.approx((float(i * 3 + j) + 1) * v[j])


class TestListTupleAssignment:
    """Verify list/tuple assignment into matrix slices.

    A list or tuple can be assigned into a matrix row, a column slice,
    or a full slice, being interpreted as a 1xN row vector.
    """

    def test_set_row_from_list(self):
        """m[i] = [list] sets the i-th row."""
        m = Matrix.zeros((3, 4))
        m[1] = [10.0, 20.0, 30.0, 40.0]
        for j in range(4):
            assert m[0, j] == pytest.approx(0.0)
            assert m[1, j] == pytest.approx((j + 1) * 10.0)
            assert m[2, j] == pytest.approx(0.0)

    def test_set_row_from_tuple(self):
        """m[i] = (tuple,) sets the i-th row."""
        m = Matrix.zeros((3, 4))
        m[2] = (5.0, 6.0, 7.0, 8.0)
        for j in range(4):
            assert m[0, j] == pytest.approx(0.0)
            assert m[1, j] == pytest.approx(0.0)
            assert m[2, j] == pytest.approx(float(j + 5))

    def test_set_row_slice_from_list(self):
        """m[start:stop, :] = [list] broadcasts the list across selected rows."""
        m = Matrix.zeros((5, 3))
        m[1:4, :] = [10.0, 20.0, 30.0]
        for j in range(3):
            assert m[0, j] == pytest.approx(0.0)
            assert m[4, j] == pytest.approx(0.0)
        for i in range(1, 4):
            for j in range(3):
                assert m[i, j] == pytest.approx((j + 1) * 10.0)

    def test_set_full_slice_from_list(self):
        """m[:, :] = [list] broadcasts the list across all rows."""
        m = Matrix.zeros((4, 3))
        m[:, :] = [7.0, 8.0, 9.0]
        for i in range(4):
            assert m[i, 0] == pytest.approx(7.0)
            assert m[i, 1] == pytest.approx(8.0)
            assert m[i, 2] == pytest.approx(9.0)

    def test_set_full_slice_from_tuple(self):
        """m[:, :] = (tuple,) broadcasts the tuple across all rows."""
        m = Matrix.zeros((4, 3))
        m[:, :] = (1.0, 2.0, 3.0)
        for i in range(4):
            for j in range(3):
                assert m[i, j] == pytest.approx(float(j + 1))

    def test_set_scalar_to_slice(self):
        """m[start:stop, :] = scalar fills the selected rows."""
        m = Matrix.zeros((4, 3))
        m[1:3, :] = 42.0
        for j in range(3):
            assert m[0, j] == pytest.approx(0.0)
            assert m[3, j] == pytest.approx(0.0)
        for i in range(1, 3):
            for j in range(3):
                assert m[i, j] == pytest.approx(42.0)

    def test_set_int_scalar_to_element(self):
        """m[i, j] = int sets a single element."""
        m = Matrix.zeros((3, 3))
        m[1, 2] = 99
        assert m[1, 2] == pytest.approx(99.0)
        assert m[0, 0] == pytest.approx(0.0)


class TestXYZWProperties:
    """Tests for the x, y, z, w shorthand properties that alias data[0..3]."""

    def test_x_getter_1x1(self):
        """x on a 1x1 matrix returns data[0]."""
        m = Matrix(1, 1, [42.0])
        assert m.x == pytest.approx(42.0)

    def test_x_getter_row_vector(self):
        """x on a row vector returns the first element."""
        m = Matrix(1, 4, [10.0, 20.0, 30.0, 40.0])
        assert m.x == pytest.approx(10.0)

    def test_x_getter_column_vector(self):
        """x on a column vector returns the first element."""
        m = Matrix(4, 1, [10.0, 20.0, 30.0, 40.0])
        assert m.x == pytest.approx(10.0)

    def test_x_getter_matrix(self):
        """x on a larger matrix returns data[0] (i.e. element [0,0])."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        assert m.x == pytest.approx(0.0)

    def test_y_getter(self):
        """y returns data[1]."""
        m = Matrix(1, 4, [10.0, 20.0, 30.0, 40.0])
        assert m.y == pytest.approx(20.0)

    def test_y_getter_column_vector(self):
        """y on a column vector returns data[1]."""
        m = Matrix(4, 1, [10.0, 20.0, 30.0, 40.0])
        assert m.y == pytest.approx(20.0)

    def test_z_getter(self):
        """z returns data[2]."""
        m = Matrix(1, 4, [10.0, 20.0, 30.0, 40.0])
        assert m.z == pytest.approx(30.0)

    def test_z_getter_column_vector(self):
        """z on a column vector returns data[2]."""
        m = Matrix(4, 1, [10.0, 20.0, 30.0, 40.0])
        assert m.z == pytest.approx(30.0)

    def test_w_getter(self):
        """w returns data[3]."""
        m = Matrix(1, 4, [10.0, 20.0, 30.0, 40.0])
        assert m.w == pytest.approx(40.0)

    def test_w_getter_column_vector(self):
        """w on a column vector returns data[3]."""
        m = Matrix(4, 1, [10.0, 20.0, 30.0, 40.0])
        assert m.w == pytest.approx(40.0)

    def test_xyzw_on_2d_matrix(self):
        """x/y/z/w on a 2x2 matrix read the flat data in row-major order."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        assert m.x == pytest.approx(1.0)
        assert m.y == pytest.approx(2.0)
        assert m.z == pytest.approx(3.0)
        assert m.w == pytest.approx(4.0)

    def test_x_setter(self):
        """Setting x modifies data[0]."""
        m = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        m.x = 99.0
        assert m.x == pytest.approx(99.0)
        assert m[0, 0] == pytest.approx(99.0)
        assert m.y == pytest.approx(2.0)

    def test_y_setter(self):
        """Setting y modifies data[1]."""
        m = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        m.y = 99.0
        assert m.y == pytest.approx(99.0)
        assert m[0, 1] == pytest.approx(99.0)
        assert m.x == pytest.approx(1.0)

    def test_z_setter(self):
        """Setting z modifies data[2]."""
        m = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        m.z = 99.0
        assert m.z == pytest.approx(99.0)
        assert m[0, 2] == pytest.approx(99.0)

    def test_w_setter(self):
        """Setting w modifies data[3]."""
        m = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        m.w = 99.0
        assert m.w == pytest.approx(99.0)
        assert m[0, 3] == pytest.approx(99.0)

    def test_setter_on_column_vector(self):
        """Setting x/y/z/w on a column vector modifies the correct element."""
        m = Matrix(4, 1, [0.0, 0.0, 0.0, 0.0])
        m.x = 1.0
        m.y = 2.0
        m.z = 3.0
        m.w = 4.0
        assert m[0, 0] == pytest.approx(1.0)
        assert m[1, 0] == pytest.approx(2.0)
        assert m[2, 0] == pytest.approx(3.0)
        assert m[3, 0] == pytest.approx(4.0)

    def test_setter_on_2d_matrix(self):
        """Setting x/y/z/w on a 2x2 matrix modifies flat data positions."""
        m = Matrix(2, 2, 0.0)
        m.x = 10.0
        m.y = 20.0
        m.z = 30.0
        m.w = 40.0
        assert m[0, 0] == pytest.approx(10.0)
        assert m[0, 1] == pytest.approx(20.0)
        assert m[1, 0] == pytest.approx(30.0)
        assert m[1, 1] == pytest.approx(40.0)

    def test_setter_with_int(self):
        """x/y/z/w setters accept int values."""
        m = Matrix(1, 4, 0.0)
        m.x = 1
        m.y = 2
        m.z = 3
        m.w = 4
        assert m.x == pytest.approx(1.0)
        assert m.y == pytest.approx(2.0)
        assert m.z == pytest.approx(3.0)
        assert m.w == pytest.approx(4.0)

    def test_y_getter_raises_on_1_element(self):
        """y raises IndexError when the matrix has fewer than 2 elements."""
        m = Matrix(1, 1, [5.0])
        with pytest.raises(IndexError):
            _ = m.y

    def test_z_getter_raises_on_2_elements(self):
        """z raises IndexError when the matrix has fewer than 3 elements."""
        m = Matrix(1, 2, [1.0, 2.0])
        with pytest.raises(IndexError):
            _ = m.z

    def test_w_getter_raises_on_3_elements(self):
        """w raises IndexError when the matrix has fewer than 4 elements."""
        m = Matrix(1, 3, [1.0, 2.0, 3.0])
        with pytest.raises(IndexError):
            _ = m.w

    def test_y_setter_raises_on_1_element(self):
        """Setting y raises IndexError when the matrix has fewer than 2 elements."""
        m = Matrix(1, 1, [5.0])
        with pytest.raises(IndexError):
            m.y = 10.0

    def test_z_setter_raises_on_2_elements(self):
        """Setting z raises IndexError when the matrix has fewer than 3 elements."""
        m = Matrix(1, 2, [1.0, 2.0])
        with pytest.raises(IndexError):
            m.z = 10.0

    def test_w_setter_raises_on_3_elements(self):
        """Setting w raises IndexError when the matrix has fewer than 4 elements."""
        m = Matrix(1, 3, [1.0, 2.0, 3.0])
        with pytest.raises(IndexError):
            m.w = 10.0

    def test_xyzw_roundtrip(self):
        """Set all four properties and read them back."""
        m = Matrix(1, 4, 0.0)
        m.x = -1.5
        m.y = 2.25
        m.z = 100.0
        m.w = -0.001
        assert m.x == pytest.approx(-1.5)
        assert m.y == pytest.approx(2.25)
        assert m.z == pytest.approx(100.0)
        assert m.w == pytest.approx(-0.001)

    def test_x_on_scalar_matrix(self):
        """x works on a 1x1 matrix (the minimum size)."""
        m = Matrix(1, 1, [7.7])
        assert m.x == pytest.approx(7.7)
        m.x = -3.3
        assert m.x == pytest.approx(-3.3)

    def test_x_matches_subscript(self):
        """x/y/z/w match two-index subscript access on the same positions."""
        vals = [11.0, 22.0, 33.0, 44.0, 55.0]
        m = Matrix(1, 5, vals)
        assert m.x == pytest.approx(m[0, 0])
        assert m.y == pytest.approx(m[0, 1])
        assert m.z == pytest.approx(m[0, 2])
        assert m.w == pytest.approx(m[0, 3])

    @pytest.mark.parametrize("size", [(1, 4), (4, 1), (2, 2), (2, 3)])
    def test_xyzw_getter_parametrized(self, size):
        """x/y/z/w getters work across various matrix shapes with ≥4 elements."""
        rows, cols = size
        vals = [float(i + 1) for i in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        assert m.x == pytest.approx(vals[0])
        assert m.y == pytest.approx(vals[1])
        assert m.z == pytest.approx(vals[2])
        assert m.w == pytest.approx(vals[3])


class TestNegativeIndexing:
    """Tests for negative integer indices in __getitem__ and __setitem__.

    NOTE: Negative integer indices are currently broken in the C extension.
    ``range_read()`` computes ``stop = start + 1`` *before*
    ``PySlice_AdjustIndices`` converts the negative ``start``, which leaves
    ``stop`` at a small positive value while ``start`` is adjusted to a large
    positive value, producing an underflowing ``count`` that causes a
    segfault.  These tests are skipped until the bug is fixed.
    """

    def test_negative_row_index(self):
        """m[-1] returns the last row."""
        m = Matrix(3, 4, [float(i) for i in range(12)])
        last = m[-1]
        assert isinstance(last, Matrix)
        assert last.rows == 1
        assert last.columns == 4
        for j in range(4):
            assert last[0, j] == pytest.approx(m[2, j])

    def test_negative_element_index(self):
        """m[-1, -1] returns the bottom-right element."""
        m = Matrix(3, 4, [float(i) for i in range(12)])
        assert m[-1, -1] == pytest.approx(11.0)

    def test_negative_row_negative_col(self):
        """m[-2, -3] accesses the correct element."""
        m = Matrix(3, 4, [float(i) for i in range(12)])
        assert m[-2, -3] == pytest.approx(5.0)

    def test_set_negative_indices(self):
        """m[-1, -1] = val sets the bottom-right element."""
        m = Matrix.zeros((3, 4))
        m[-1, -1] = 99.0
        assert m[2, 3] == pytest.approx(99.0)

    def test_negative_row_column_vector(self):
        """Negative indexing on a column vector returns the correct float."""
        m = Matrix(5, 1, [10.0, 20.0, 30.0, 40.0, 50.0])
        assert m[-1] == pytest.approx(50.0)
        assert m[-3] == pytest.approx(30.0)


class TestSliceIndexing:
    """Tests for slice-based __getitem__ on matrices."""

    def test_row_slice_basic(self):
        """m[1:3] returns rows 1 and 2."""
        m = Matrix(5, 3, [float(i) for i in range(15)])
        sub = m[1:3]
        assert sub.rows == 2
        assert sub.columns == 3
        for i in range(2):
            for j in range(3):
                assert sub[i, j] == pytest.approx(m[i + 1, j])

    def test_row_slice_with_step(self):
        """m[::2] returns every other row."""
        m = Matrix(6, 2, [float(i) for i in range(12)])
        sub = m[::2]
        assert sub.rows == 3
        assert sub.columns == 2
        for out_r, src_r in enumerate([0, 2, 4]):
            for j in range(2):
                assert sub[out_r, j] == pytest.approx(m[src_r, j])

    def test_column_slice(self):
        """m[:, 1:3] returns columns 1 and 2."""
        m = Matrix(4, 5, [float(i) for i in range(20)])
        sub = m[:, 1:3]
        assert sub.rows == 4
        assert sub.columns == 2
        for i in range(4):
            for j in range(2):
                assert sub[i, j] == pytest.approx(m[i, j + 1])

    def test_row_and_column_slice(self):
        """m[1:3, 2:4] returns a 2x2 sub-matrix."""
        m = Matrix(5, 6, [float(i) for i in range(30)])
        sub = m[1:3, 2:4]
        assert sub.rows == 2
        assert sub.columns == 2
        for i in range(2):
            for j in range(2):
                assert sub[i, j] == pytest.approx(m[i + 1, j + 2])

    def test_reversed_step_slice(self):
        """m[::-1] reverses the rows."""
        m = Matrix(4, 2, [float(i) for i in range(8)])
        sub = m[::-1]
        assert sub.rows == 4
        assert sub.columns == 2
        for out_r, src_r in enumerate([3, 2, 1, 0]):
            for j in range(2):
                assert sub[out_r, j] == pytest.approx(m[src_r, j])

    def test_negative_bound_slice(self):
        """m[-2:] selects the trailing rows."""
        m = Matrix(4, 3, [float(i) for i in range(12)])
        sub = m[-2:]
        assert sub.rows == 2
        assert sub.columns == 3
        for out_r, src_r in enumerate([2, 3]):
            for j in range(3):
                assert sub[out_r, j] == pytest.approx(m[src_r, j])

    @pytest.mark.parametrize("sl", [
        slice(-1, 1),
        slice(1, 1),
        slice(3, 1),
        slice(2, 0),
    ])
    def test_empty_slice_raises_indexerror(self, sl):
        """An empty or reversed-bound slice raises IndexError, never segfaults."""
        m = Matrix(4, 3, [float(i) for i in range(12)])
        with pytest.raises(IndexError):
            _ = m[sl]

    def test_empty_column_slice_raises_indexerror(self):
        """An empty column slice raises IndexError rather than crashing."""
        m = Matrix(4, 3, [float(i) for i in range(12)])
        with pytest.raises(IndexError):
            _ = m[:, -1:0]


class TestReprFormat:
    """Tests for __repr__ format and content."""

    def test_repr_contains_matrix(self):
        """repr() output starts with 'Matrix('."""
        m = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        assert repr(m).startswith("Matrix(")

    def test_repr_contains_dimensions(self):
        """repr() output includes the matrix dimensions."""
        m = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        r = repr(m)
        assert "2" in r
        assert "3" in r

    def test_repr_small_matrix_roundtrip(self):
        """repr() of a small matrix can be eval'd back to an equal matrix."""
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        m = Matrix(2, 3, vals)
        m2 = eval(repr(m))
        assert Matrix.allclose(m, m2)


class TestMatmulErrors:
    """Tests for error handling in matrix multiplication."""

    def test_matmul_incompatible_shapes_raises(self):
        """@ raises when inner dimensions don't match."""
        a = Matrix(2, 3)
        b = Matrix(4, 2)
        with pytest.raises(ValueError):
            _ = a @ b


class TestNormalDistribution:
    """Statistical sanity checks for Matrix.normal()."""

    def test_normal_mean_approx(self):
        """Large sample from normal(5.0, 1.0) has mean near 5.0."""
        m = Matrix.normal(5.0, 1.0, size=(1000, 1))
        assert m.mean() == pytest.approx(5.0, abs=0.2)

    def test_normal_zero_stddev(self):
        """normal(mu, 0.0) returns constant mu."""
        m = Matrix.normal(3.0, 0.0, size=(10, 10))
        expected = Matrix(10, 10, 3.0)
        assert Matrix.allclose(m, expected)


class TestUniformDistributionMatrix:
    """Verify uniform() matrix output respects the given bounds."""

    def test_uniform_matrix_in_range(self):
        """All elements of uniform(lo, hi, size=...) are within [lo, hi)."""
        lo, hi = -5.0, 5.0
        m = Matrix.uniform(lo, hi, size=(50, 50))
        assert m.min() >= lo
        assert m.max() < hi


class TestMatrixInCown:
    """A Matrix placed in a Cown is released and cannot be accessed directly."""

    def test_acquired_is_false(self):
        """After placing a matrix in a Cown its acquired flag is False."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        assert m.acquired is False

    def test_read_element_raises(self):
        """Reading an element from an unacquired matrix raises RuntimeError."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        with pytest.raises(RuntimeError):
            _ = m[0, 0]

    def test_write_element_raises(self):
        """Writing an element to an unacquired matrix raises RuntimeError."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        with pytest.raises(RuntimeError):
            m[0, 0] = 42.0

    def test_read_row_raises(self):
        """Slicing a row from an unacquired matrix raises RuntimeError."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        with pytest.raises(RuntimeError):
            _ = m[0]

    def test_sum_raises(self):
        """Calling sum() on an unacquired matrix raises RuntimeError."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        with pytest.raises(RuntimeError):
            m.sum()

    def test_transpose_raises(self):
        """transpose() on an unacquired matrix raises RuntimeError."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        with pytest.raises(RuntimeError):
            m.transpose()

    def test_pickle_raises(self):
        """Pickling an unacquired (cown-resident) matrix raises RuntimeError."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        with pytest.raises(RuntimeError):
            pickle.dumps(m)

    def test_second_cown_raises_not_segfault(self):
        """Wrapping an already-released matrix in a second Cown raises cleanly.

        The first Cown moves the matrix to NO_OWNER, so the second Cown
        cannot re-serialize the move-typed payload and must surface a
        RuntimeError rather than crash.
        """
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        with pytest.raises(RuntimeError):
            Cown(m)

    def test_x_getter_raises(self):
        """Reading .x on an unacquired matrix raises RuntimeError."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        with pytest.raises(RuntimeError):
            _ = m.x

    def test_x_setter_raises(self):
        """Setting .x on an unacquired matrix raises RuntimeError."""
        m = Matrix(3, 3, [float(i) for i in range(9)])
        Cown(m)
        with pytest.raises(RuntimeError):
            m.x = 1.0

    def test_acquired_true_inside_context(self):
        """Acquiring the Cown via context manager re-enables access."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        c = Cown(m)
        assert m.acquired is False
        with c as val:
            assert val.acquired is True
            assert val.x == pytest.approx(1.0)
            assert val.sum() == pytest.approx(10.0)
        assert m.acquired is False

    def test_write_inside_context(self):
        """Writing to the matrix inside a Cown context manager works."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        c = Cown(m)
        with c as val:
            val.x = 99.0
            assert val.x == pytest.approx(99.0)
        assert m.acquired is False

    def test_not_acquired_after_context_exit(self):
        """Matrix is not acquired after the context manager exits."""
        m = Matrix(1, 3, [10.0, 20.0, 30.0])
        c = Cown(m)
        with c as val:
            val.x = 42.0
        assert m.acquired is False
        with pytest.raises(RuntimeError):
            _ = m.x

    def test_sequential_acquires(self):
        """Two sequential context-manager enters each see updated state."""
        m = Matrix(1, 2, [1.0, 2.0])
        c = Cown(m)
        with c as val:
            val.x = 10.0
            assert val.x == pytest.approx(10.0)
        with c as val:
            assert val.x == pytest.approx(10.0)
            val.y = 20.0
            assert val.y == pytest.approx(20.0)


class TestUnwrapMatrix:
    """unwrap() on a Cown[Matrix] hands back a caller-owned, readable matrix.

    unwrap consumes the cown, so the returned matrix keeps its ownership and
    is readable by the caller.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_unwrap_returns_readable_matrix(self):
        """The unwrapped matrix is owned by the caller and readable."""
        m = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        c = Cown(m)

        @when(c)
        def _(c):
            c.value[0, 0] = 42.0

        quiesce(QUIESCE_TIMEOUT)
        res = c.unwrap()
        assert res.acquired is True
        assert res[0, 0] == pytest.approx(42.0)
        assert res[1, 1] == pytest.approx(4.0)

    def test_unwrap_consumes_cown(self):
        """A second unwrap of the same cown returns None (consumed)."""
        m = Matrix(1, 3, [5.0, 6.0, 7.0])
        c = Cown(m)

        @when(c)
        def _(c):
            c.value[0, 0] = 99.0

        quiesce(QUIESCE_TIMEOUT)
        first = c.unwrap()
        assert first.acquired is True
        assert first[0, 0] == pytest.approx(99.0)
        assert c.unwrap() is None

    def test_emptied_cown_is_reschedulable(self):
        """After consuming, the cown still accepts a fresh behavior."""
        m = Matrix(1, 2, [1.0, 2.0])
        c = Cown(m)

        @when(c)
        def _(c):
            c.value[0, 0] = 3.0

        quiesce(QUIESCE_TIMEOUT)
        taken = c.unwrap()
        assert taken[0, 0] == pytest.approx(3.0)

        # The cown now holds None. A fresh behavior must still acquire it
        # and install a new matrix, which a later unwrap then consumes.
        @when(c)
        def _(c):
            assert c.value is None
            c.value = Matrix(1, 2, [10.0, 20.0])

        @when(c)
        def _(c):
            c.value[0, 1] = 30.0

        quiesce(QUIESCE_TIMEOUT)
        again = c.unwrap()
        assert again[0, 0] == pytest.approx(10.0)
        assert again[0, 1] == pytest.approx(30.0)


class TestVectorMethodsInCown:
    """Exercise each new vector method through a ``@when`` behavior on ``Cown[Matrix]``.

    Mirrors the in-process Matrix-vector tests but routes every call
    through the worker dispatch path so Matrix XIData round-trip plus
    in-cown mutation are both exercised. Each behavior returns its
    result; the test thread calls :func:`quiesce` and then reads the
    result via :meth:`Cown.unwrap`, which re-raises any exception the
    behavior captured.
    """

    @classmethod
    def teardown_class(cls):
        wait()

    def test_vecdot_in_behavior(self):
        """``[1,2,3]·[4,5,6] == 32`` via worker dispatch."""
        a = Cown(Matrix(1, 3, [1.0, 2.0, 3.0]))
        b = Matrix(1, 3, [4.0, 5.0, 6.0])

        @when(a)
        def result(a, b=b):
            return a.value.vecdot(b)

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == pytest.approx(32.0)

    def test_length_in_behavior(self):
        """``length`` getter ``[3, 4] == 5`` via worker dispatch (it's a property)."""
        v = Cown(Matrix(1, 2, [3.0, 4.0]))

        @when(v)
        def result(v):
            return v.value.length

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == pytest.approx(5.0)

    def test_magnitude_squared_in_behavior(self):
        """``magnitude_squared([3, 4]) == 25`` via worker dispatch."""
        v = Cown(Matrix(1, 2, [3.0, 4.0]))

        @when(v)
        def result(v):
            return v.value.magnitude_squared()

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == pytest.approx(25.0)

    def test_angle_in_behavior(self):
        """``angle([0, 1]) == pi/2`` via worker dispatch."""
        v = Cown(Matrix(1, 2, [0.0, 1.0]))

        @when(v)
        def result(v):
            return v.value.angle()

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == pytest.approx(math.pi / 2.0)

    def test_cross_3d_in_behavior(self):
        """``[1,2,3] × [4,5,6] == [-3, 6, -3]`` via worker dispatch."""
        a = Cown(Matrix(1, 3, [1.0, 2.0, 3.0]))
        b = Matrix(1, 3, [4.0, 5.0, 6.0])

        @when(a)
        def result(a, b=b):
            out = a.value.cross(b)
            return (out[0, 0], out[0, 1], out[0, 2])

        quiesce(QUIESCE_TIMEOUT)
        out = result.unwrap()
        assert out[0] == pytest.approx(-3.0)
        assert out[1] == pytest.approx(6.0)
        assert out[2] == pytest.approx(-3.0)

    def test_normalize_copy_in_behavior(self):
        """``normalize([3, 4]) == [0.6, 0.8]``; original cown value untouched."""
        v = Cown(Matrix(1, 2, [3.0, 4.0]))

        @when(v)
        def result(v):
            n = v.value.normalize()
            return (n[0, 0], n[0, 1], v.value[0, 0], v.value[0, 1])

        quiesce(QUIESCE_TIMEOUT)
        n0, n1, src0, src1 = result.unwrap()
        assert n0 == pytest.approx(0.6)
        assert n1 == pytest.approx(0.8)
        assert src0 == pytest.approx(3.0)
        assert src1 == pytest.approx(4.0)

    def test_perpendicular_copy_in_behavior(self):
        """``perpendicular([1, 0]) == [0, 1]``; original cown value untouched."""
        v = Cown(Matrix(1, 2, [1.0, 0.0]))

        @when(v)
        def result(v):
            p = v.value.perpendicular()
            return (p[0, 0], p[0, 1], v.value[0, 0], v.value[0, 1])

        quiesce(QUIESCE_TIMEOUT)
        p0, p1, src0, src1 = result.unwrap()
        assert p0 == pytest.approx(0.0)
        assert p1 == pytest.approx(1.0)
        assert src0 == pytest.approx(1.0)
        assert src1 == pytest.approx(0.0)

    def test_normalize_in_place_in_behavior(self):
        """``normalize(in_place=True)`` mutates the matrix held by the cown."""
        v = Cown(Matrix(1, 2, [3.0, 4.0]))

        @when(v)
        def _(v):
            v.value.normalize(in_place=True)

        @when(v)
        def check(v):
            return (v.value[0, 0], v.value[0, 1])

        quiesce(QUIESCE_TIMEOUT)
        r0, r1 = check.unwrap()
        assert r0 == pytest.approx(0.6)
        assert r1 == pytest.approx(0.8)

    def test_perpendicular_in_place_in_behavior(self):
        """``perpendicular(in_place=True)`` mutates the matrix held by the cown."""
        v = Cown(Matrix(1, 2, [1.0, 0.0]))

        @when(v)
        def _(v):
            v.value.perpendicular(in_place=True)

        @when(v)
        def check(v):
            return (v.value[0, 0], v.value[0, 1])

        quiesce(QUIESCE_TIMEOUT)
        r0, r1 = check.unwrap()
        assert r0 == pytest.approx(0.0)
        assert r1 == pytest.approx(1.0)

    def test_negate_in_place_in_behavior(self):
        """``negate(in_place=True)`` mutates the matrix held by the cown."""
        v = Cown(Matrix(1, 3, [1.0, -2.0, 3.0]))

        @when(v)
        def _(v):
            v.value.negate(in_place=True)

        @when(v)
        def check(v):
            return (v.value[0, 0], v.value[0, 1], v.value[0, 2])

        quiesce(QUIESCE_TIMEOUT)
        r0, r1, r2 = check.unwrap()
        assert r0 == pytest.approx(-1.0)
        assert r1 == pytest.approx(2.0)
        assert r2 == pytest.approx(-3.0)


def _flat(m):
    """Row-major flat list of a matrix's elements."""
    rows, cols = m.rows, m.columns
    return [m[i, j] for i in range(rows) for j in range(cols)]


class TestSqrt:
    """Element-wise square root."""

    def test_sqrt_matches_math(self, shape, rng):
        """sqrt() matches math.sqrt element-wise for non-negative inputs."""
        rows, cols = shape
        vals = [rng.uniform(0, 100) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        result = m.sqrt()
        for i in range(rows):
            for j in range(cols):
                assert result[i, j] == pytest.approx(math.sqrt(vals[i * cols + j]))

    def test_sqrt_negative_is_nan(self):
        """Negative elements yield NaN rather than raising."""
        m = Matrix(1, 3, [-1.0, -4.0, 9.0])
        result = m.sqrt()
        assert math.isnan(result[0, 0])
        assert math.isnan(result[0, 1])
        assert result[0, 2] == pytest.approx(3.0)

    def test_sqrt_zero(self):
        """sqrt(0) is 0."""
        m = Matrix(1, 1, [0.0])
        assert m.sqrt()[0, 0] == pytest.approx(0.0)

    def test_sqrt_out_of_place_preserves_source(self, shape, rng):
        """Default sqrt() returns a new matrix and leaves the source intact."""
        rows, cols = shape
        vals = [rng.uniform(0, 100) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        _ = m.sqrt()
        for i in range(rows):
            for j in range(cols):
                assert m[i, j] == pytest.approx(vals[i * cols + j])

    def test_sqrt_in_place(self, shape, rng):
        """sqrt(in_place=True) mutates self and returns it."""
        rows, cols = shape
        vals = [rng.uniform(0, 100) for _ in range(rows * cols)]
        m = Matrix(rows, cols, vals)
        result = m.sqrt(in_place=True)
        assert result is m
        for i in range(rows):
            for j in range(cols):
                assert m[i, j] == pytest.approx(math.sqrt(vals[i * cols + j]))


COMPARE_OPS = [
    ("less", lambda a, b: a < b),
    ("less_equal", lambda a, b: a <= b),
    ("greater", lambda a, b: a > b),
    ("greater_equal", lambda a, b: a >= b),
    ("equal", lambda a, b: a == b),
    ("not_equal", lambda a, b: a != b),
]


class TestComparisonMasks:
    """Element-wise comparison methods producing 0/1 mask matrices."""

    @pytest.mark.parametrize("name,ref", COMPARE_OPS, ids=[o[0] for o in COMPARE_OPS])
    def test_same_shape(self, name, ref, shape, rng):
        """Each mask method matches Python's element-wise comparison."""
        rows, cols = shape
        # Overlapping integer-valued draws so ties exercise == / <= / >=.
        a_vals = [float(rng.randint(0, 5)) for _ in range(rows * cols)]
        b_vals = [float(rng.randint(0, 5)) for _ in range(rows * cols)]
        a = Matrix(rows, cols, a_vals)
        b = Matrix(rows, cols, b_vals)
        result = getattr(a, name)(b)
        assert result.rows == rows
        assert result.columns == cols
        for i in range(rows):
            for j in range(cols):
                expected = 1.0 if ref(a_vals[i * cols + j], b_vals[i * cols + j]) else 0.0
                assert result[i, j] == expected

    @pytest.mark.parametrize("name,ref", COMPARE_OPS, ids=[o[0] for o in COMPARE_OPS])
    def test_scalar(self, name, ref, shape, rng):
        """Mask methods broadcast a scalar against every element."""
        rows, cols = shape
        a_vals = [float(rng.randint(0, 5)) for _ in range(rows * cols)]
        a = Matrix(rows, cols, a_vals)
        scalar = 3.0
        result = getattr(a, name)(scalar)
        for i in range(rows):
            for j in range(cols):
                expected = 1.0 if ref(a_vals[i * cols + j], scalar) else 0.0
                assert result[i, j] == expected

    @pytest.mark.parametrize("name,ref", COMPARE_OPS, ids=[o[0] for o in COMPARE_OPS])
    def test_row_vector_broadcast(self, name, ref):
        """Mask methods broadcast a 1xN row vector across rows."""
        a = Matrix(2, 3, [1.0, 5.0, 3.0, 4.0, 2.0, 3.0])
        v = Matrix(1, 3, [3.0, 3.0, 3.0])
        result = getattr(a, name)(v)
        for i in range(2):
            for j in range(3):
                expected = 1.0 if ref(a[i, j], v[0, j]) else 0.0
                assert result[i, j] == expected

    @pytest.mark.parametrize("name,ref", COMPARE_OPS, ids=[o[0] for o in COMPARE_OPS])
    def test_column_vector_broadcast(self, name, ref):
        """Mask methods broadcast an Mx1 column vector across columns."""
        a = Matrix(3, 2, [1.0, 5.0, 3.0, 4.0, 2.0, 3.0])
        v = Matrix(3, 1, [3.0, 3.0, 3.0])
        result = getattr(a, name)(v)
        for i in range(3):
            for j in range(2):
                expected = 1.0 if ref(a[i, j], v[i, 0]) else 0.0
                assert result[i, j] == expected

    @pytest.mark.parametrize("name,ref", COMPARE_OPS, ids=[o[0] for o in COMPARE_OPS])
    def test_one_by_one_broadcast(self, name, ref, shape, rng):
        """A 1x1 matrix broadcasts like a scalar in mask methods."""
        rows, cols = shape
        a_vals = [float(rng.randint(0, 5)) for _ in range(rows * cols)]
        a = Matrix(rows, cols, a_vals)
        scalar = 3.0
        result = getattr(a, name)(Matrix(1, 1, [scalar]))
        for i in range(rows):
            for j in range(cols):
                expected = 1.0 if ref(a_vals[i * cols + j], scalar) else 0.0
                assert result[i, j] == expected

    def test_nan_less_is_zero(self):
        """NaN comparisons follow IEEE: ordered ops yield 0, not_equal yields 1."""
        nan = float("nan")
        a = Matrix(1, 2, [nan, 1.0])
        assert a.less(0.0)[0, 0] == 0.0
        assert a.less_equal(0.0)[0, 0] == 0.0
        assert a.greater(0.0)[0, 0] == 0.0
        assert a.greater_equal(0.0)[0, 0] == 0.0
        assert a.equal(nan)[0, 0] == 0.0
        assert a.not_equal(nan)[0, 0] == 1.0

    def test_masks_are_zero_or_one(self, shape, rng):
        """Every mask element is exactly 0.0 or 1.0."""
        rows, cols = shape
        a = Matrix(rows, cols, [rng.uniform(-9, 9) for _ in range(rows * cols)])
        b = Matrix(rows, cols, [rng.uniform(-9, 9) for _ in range(rows * cols)])
        for name, _ in COMPARE_OPS:
            for v in _flat(getattr(a, name)(b)):
                assert v in (0.0, 1.0)


RICHCOMPARE_OPS = [
    ("lt", lambda a, b: a < b),
    ("le", lambda a, b: a <= b),
    ("gt", lambda a, b: a > b),
    ("ge", lambda a, b: a >= b),
    ("eq", lambda a, b: a == b),
    ("ne", lambda a, b: a != b),
]


class TestLexicographicCompare:
    """Operators (<, <=, >, >=, ==, !=) return a single lexicographic bool."""

    @pytest.mark.parametrize("name,op", RICHCOMPARE_OPS, ids=[o[0] for o in RICHCOMPARE_OPS])
    def test_matches_list_ordering(self, name, op, shape, rng):
        """Operator result matches Python list comparison of flat elements."""
        rows, cols = shape
        a_vals = [float(rng.randint(0, 3)) for _ in range(rows * cols)]
        b_vals = [float(rng.randint(0, 3)) for _ in range(rows * cols)]
        a = Matrix(rows, cols, a_vals)
        b = Matrix(rows, cols, b_vals)
        assert op(a, b) is op(a_vals, b_vals)

    def test_equal_same_values(self, shape, rng):
        """Equal-valued same-shape matrices compare equal."""
        rows, cols = shape
        vals = [rng.uniform(-9, 9) for _ in range(rows * cols)]
        a = Matrix(rows, cols, list(vals))
        b = Matrix(rows, cols, list(vals))
        assert a == b
        assert not (a != b)
        assert a <= b
        assert a >= b
        assert not (a < b)
        assert not (a > b)

    def test_first_difference_decides(self):
        """The first differing element decides, later elements ignored."""
        a = Matrix(1, 3, [1.0, 9.0, 9.0])
        b = Matrix(1, 3, [2.0, 0.0, 0.0])
        assert a < b
        assert not (a > b)

    @pytest.mark.parametrize("name,op", RICHCOMPARE_OPS, ids=[o[0] for o in RICHCOMPARE_OPS])
    def test_scalar_lexicographic(self, name, op):
        """Scalar comparison broadcasts and decides on element 0 unless tied."""
        a = Matrix(1, 3, [5.0, 1.0, 1.0])
        assert op(a, 5.0) is op([5.0, 1.0, 1.0], [5.0, 5.0, 5.0])

    def test_eq_ne_shape_mismatch_no_raise(self):
        """== / != return False / True on a shape mismatch without raising."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        c = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        assert (a == c) is False
        assert (a != c) is True

    def test_eq_same_values_different_shape_not_equal(self):
        """Same flat data but different shape is not equal."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        c = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        assert a != c

    @pytest.mark.parametrize("op", [
        lambda a, b: a < b,
        lambda a, b: a <= b,
        lambda a, b: a > b,
        lambda a, b: a >= b,
    ])
    def test_ordering_shape_mismatch_raises(self, op):
        """Ordering operators raise ValueError on a matrix shape mismatch."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        c = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(ValueError):
            op(a, c)

    def test_eq_non_matrix_is_false(self):
        """== against a non-matrix, non-scalar returns False (NotImplemented)."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        assert (a == "hello") is False
        assert (a != "hello") is True
        assert (a == object()) is False

    def test_in_list_uses_equality(self):
        """A matrix can be found in a list via value equality."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        b = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        assert a in [Matrix(2, 2, [0.0, 0.0, 0.0, 0.0]), b]

    def test_unhashable(self):
        """Defining value equality makes Matrix unhashable (mutable type)."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(TypeError):
            hash(a)
        with pytest.raises(TypeError):
            _ = {a}

    def test_one_by_one_not_scalar_in_ordering(self):
        """A 1x1 matrix only compares against another 1x1, not a larger shape."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        one = Matrix(1, 1, [2.0])
        with pytest.raises(ValueError):
            _ = a < one
        # 1x1 vs 1x1 is fine.
        assert Matrix(1, 1, [1.0]) < Matrix(1, 1, [2.0])


class TestWhere:
    """numpy-like Matrix.where(mask, a, b)."""

    def test_matrix_operands(self, shape, rng):
        """where selects a where mask is non-zero and b elsewhere."""
        rows, cols = shape
        mask_vals = [float(rng.randint(0, 1)) for _ in range(rows * cols)]
        a_vals = [rng.uniform(-9, 9) for _ in range(rows * cols)]
        b_vals = [rng.uniform(-9, 9) for _ in range(rows * cols)]
        mask = Matrix(rows, cols, mask_vals)
        a = Matrix(rows, cols, a_vals)
        b = Matrix(rows, cols, b_vals)
        result = Matrix.where(mask, a, b)
        assert result.rows == rows
        assert result.columns == cols
        for i in range(rows):
            for j in range(cols):
                k = i * cols + j
                expected = a_vals[k] if mask_vals[k] != 0.0 else b_vals[k]
                assert result[i, j] == pytest.approx(expected)

    def test_scalar_a(self, shape, rng):
        """where accepts a scalar for a, broadcast against the mask."""
        rows, cols = shape
        mask_vals = [float(rng.randint(0, 1)) for _ in range(rows * cols)]
        b_vals = [rng.uniform(-9, 9) for _ in range(rows * cols)]
        mask = Matrix(rows, cols, mask_vals)
        b = Matrix(rows, cols, b_vals)
        result = Matrix.where(mask, 99.0, b)
        for i in range(rows):
            for j in range(cols):
                k = i * cols + j
                expected = 99.0 if mask_vals[k] != 0.0 else b_vals[k]
                assert result[i, j] == pytest.approx(expected)

    def test_scalar_both(self):
        """where accepts scalars for both a and b."""
        mask = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        result = Matrix.where(mask, 1.0, 0.0)
        assert _flat(result) == [1.0, 0.0, 0.0, 1.0]

    def test_mask_from_comparison(self):
        """A mask produced by a comparison method drives the selection."""
        a = Matrix(1, 4, [1.0, 5.0, 2.0, 8.0])
        result = Matrix.where(a.greater(3.0), a, 0.0)
        assert _flat(result) == [0.0, 5.0, 0.0, 8.0]

    def test_nan_mask_selects_a(self):
        """A NaN mask element is non-zero and selects a."""
        mask = Matrix(1, 2, [float("nan"), 0.0])
        result = Matrix.where(mask, 1.0, 2.0)
        assert _flat(result) == [1.0, 2.0]

    def test_shape_mismatch_raises(self):
        """A matrix operand whose shape differs from the mask raises."""
        mask = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        wrong = Matrix(1, 4, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(ValueError):
            Matrix.where(mask, wrong, 0.0)
        with pytest.raises(ValueError):
            Matrix.where(mask, 0.0, wrong)

    def test_one_by_one_not_scalar(self):
        """A 1x1 matrix operand must match the mask shape (not treated as scalar)."""
        mask = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        with pytest.raises(ValueError):
            Matrix.where(mask, Matrix(1, 1, [5.0]), 0.0)

    def test_bad_operand_type_raises(self):
        """A non-matrix, non-scalar operand raises TypeError."""
        mask = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        with pytest.raises(TypeError):
            Matrix.where(mask, "x", 0.0)


class TestComparisonMaskSelfBroadcast:
    """Mask methods where self (the left operand) is the broadcast side.

    These pin the swap_right reflections inside Matrix_binary_op: when self is
    the smaller operand the op is reflected before dispatch, so the result must
    still read ``ref(self_elem, other_elem)``.
    """

    @pytest.mark.parametrize("name,ref", COMPARE_OPS, ids=[o[0] for o in COMPARE_OPS])
    def test_self_one_by_one(self, name, ref):
        """A 1x1 self broadcasts against a full other."""
        full = Matrix(2, 3, [1.0, 5.0, 3.0, 4.0, 2.0, 6.0])
        s = Matrix(1, 1, [3.0])
        result = getattr(s, name)(full)
        for i in range(2):
            for j in range(3):
                expected = 1.0 if ref(3.0, full[i, j]) else 0.0
                assert result[i, j] == expected

    @pytest.mark.parametrize("name,ref", COMPARE_OPS, ids=[o[0] for o in COMPARE_OPS])
    def test_self_row_vector(self, name, ref):
        """A 1xN self broadcasts down the rows of an MxN other."""
        full = Matrix(2, 3, [1.0, 5.0, 3.0, 4.0, 2.0, 6.0])
        s = Matrix(1, 3, [3.0, 3.0, 3.0])
        result = getattr(s, name)(full)
        for i in range(2):
            for j in range(3):
                expected = 1.0 if ref(s[0, j], full[i, j]) else 0.0
                assert result[i, j] == expected

    @pytest.mark.parametrize("name,ref", COMPARE_OPS, ids=[o[0] for o in COMPARE_OPS])
    def test_self_column_vector(self, name, ref):
        """An Mx1 self broadcasts across the columns of an MxN other."""
        full = Matrix(2, 3, [1.0, 5.0, 3.0, 4.0, 2.0, 6.0])
        s = Matrix(2, 1, [3.0, 4.0])
        result = getattr(s, name)(full)
        for i in range(2):
            for j in range(3):
                expected = 1.0 if ref(s[i, 0], full[i, j]) else 0.0
                assert result[i, j] == expected

    @pytest.mark.parametrize("name,ref", COMPARE_OPS, ids=[o[0] for o in COMPARE_OPS])
    def test_self_row_vs_column_outer(self, name, ref):
        """A 1xN self against an Mx1 other forms the MxN outer-product mask."""
        s = Matrix(1, 3, [1.0, 3.0, 5.0])
        other = Matrix(2, 1, [2.0, 4.0])
        result = getattr(s, name)(other)
        assert (result.rows, result.columns) == (2, 3)
        for i in range(2):
            for j in range(3):
                expected = 1.0 if ref(s[0, j], other[i, 0]) else 0.0
                assert result[i, j] == expected


class TestComparisonMaskCoercion:
    """Mask methods accept list/tuple and bool operands."""

    def test_list_operand_row_broadcast(self):
        """A list operand is taken as a row vector and broadcasts."""
        a = Matrix(2, 3, [1.0, 5.0, 3.0, 4.0, 2.0, 6.0])
        result = a.less([3.0, 3.0, 3.0])
        for i in range(2):
            for j in range(3):
                assert result[i, j] == (1.0 if a[i, j] < 3.0 else 0.0)

    def test_tuple_operand_same_shape(self):
        """A tuple operand matching the flat shape compares element-wise."""
        a = Matrix(1, 3, [1.0, 3.0, 5.0])
        result = a.equal((1.0, 0.0, 5.0))
        assert _flat(result) == [1.0, 0.0, 1.0]

    def test_bool_operand_is_scalar(self):
        """A bool operand is the scalar 1.0 (True) or 0.0 (False)."""
        a = Matrix(1, 3, [0.0, 1.0, 2.0])
        assert _flat(a.less(True)) == [1.0, 0.0, 0.0]
        assert _flat(a.greater(False)) == [0.0, 1.0, 1.0]
        assert _flat(a.equal(True)) == [0.0, 1.0, 0.0]

    def test_empty_list_operand_raises(self):
        """An empty list/tuple cannot be coerced and raises ValueError."""
        a = Matrix(1, 3, [0.0, 1.0, 2.0])
        with pytest.raises(ValueError):
            a.less([])
        with pytest.raises(ValueError):
            a.equal(())

    def test_malformed_list_operand_raises(self):
        """A list holding a non-number raises TypeError."""
        a = Matrix(1, 3, [0.0, 1.0, 2.0])
        with pytest.raises(TypeError):
            a.greater([1.0, "x", 3.0])


class TestLexicographicNaN:
    """NaN handling in the lexicographic comparison operators."""

    def test_all_nan_equals_itself(self):
        """An all-NaN matrix compares == equal to itself (NaN never decides)."""
        nan = float("nan")
        a = Matrix(1, 3, [nan, nan, nan])
        b = Matrix(1, 3, [nan, nan, nan])
        assert a == b
        assert not (a != b)
        assert not (a < b)
        assert not (a > b)
        assert a <= b
        assert a >= b

    def test_nan_skipped_first_real_difference_decides(self):
        """A leading NaN is skipped; the first real ordering decides."""
        nan = float("nan")
        a = Matrix(1, 3, [nan, 1.0, 9.0])
        b = Matrix(1, 3, [nan, 2.0, 0.0])
        assert a < b
        assert not (a > b)

    def test_nan_scalar_path(self):
        """The scalar comparison path also skips NaN elements."""
        nan = float("nan")
        a = Matrix(1, 3, [nan, nan, nan])
        assert (a == nan) is True
        assert (a != nan) is False
        assert (a < nan) is False
        assert (a > nan) is False

    def test_equal_mask_vs_eq_operator_nan_divergence(self):
        """equal() sends NaN->0.0, but the == operator treats all-NaN as equal."""
        nan = float("nan")
        a = Matrix(1, 2, [nan, nan])
        b = Matrix(1, 2, [nan, nan])
        assert _flat(a.equal(b)) == [0.0, 0.0]
        assert (a == b) is True


class TestReflectedScalarCompare:
    """Reflected scalar comparisons mirror the matrix-on-left form."""

    @pytest.mark.parametrize("scalar", [0.0, 3.0, 5.0])
    def test_reflected_equivalence(self, scalar):
        """``scalar OP a`` equals the reflected ``a OP' scalar`` per operator."""
        a = Matrix(1, 3, [1.0, 3.0, 5.0])
        assert (scalar < a) is (a > scalar)
        assert (scalar <= a) is (a >= scalar)
        assert (scalar > a) is (a < scalar)
        assert (scalar >= a) is (a <= scalar)
        assert (scalar == a) is (a == scalar)
        assert (scalar != a) is (a != scalar)


class TestRichcompareCoercion:
    """Comparison operators accept list/tuple operands and bool scalars."""

    @pytest.mark.parametrize("name,op", RICHCOMPARE_OPS, ids=[o[0] for o in RICHCOMPARE_OPS])
    def test_list_operand_matches_matrix(self, name, op):
        """A same-shape list operand compares like the equivalent matrix."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        seq = [1.0, 2.0, 4.0]
        assert op(a, seq) is op(a, Matrix(1, 3, seq))

    def test_tuple_operand(self):
        """A tuple operand of matching shape compares lexicographically."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        assert (a == (1.0, 2.0, 3.0)) is True
        assert (a < (1.0, 2.0, 4.0)) is True

    def test_list_shape_mismatch_eq_false(self):
        """A list whose shape differs makes == False / != True (no raise)."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        assert (a == [1.0, 2.0, 3.0, 4.0]) is False
        assert (a != [1.0, 2.0, 3.0, 4.0]) is True

    def test_list_shape_mismatch_ordering_raises(self):
        """An ordering operator against a mismatched list raises ValueError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(ValueError):
            _ = a < [1.0, 2.0, 3.0, 4.0]

    def test_bool_scalar(self):
        """A bool operand is the scalar 1.0 (True) or 0.0 (False)."""
        a = Matrix(1, 2, [1.0, 1.0])
        assert (a == True) is True  # noqa: E712 — bool-as-scalar is intended
        assert (a == False) is False  # noqa: E712 — bool-as-scalar is intended

    def test_empty_list_eq_is_total(self):
        """An empty list cannot be coerced, so == is False / != True (no raise)."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        assert (a == []) is False
        assert (a != []) is True
        assert (a == ()) is False
        assert (a != ()) is True

    def test_empty_list_ordering_raises(self):
        """An ordering operator against an empty list raises ValueError."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        with pytest.raises(ValueError):
            _ = a < []

    def test_malformed_list_eq_is_total(self):
        """A list holding a non-number makes == False / != True (no raise)."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        assert (a == [1.0, "x", 3.0]) is False
        assert (a != [1.0, "x", 3.0]) is True

    def test_nested_list_eq_is_total(self):
        """A nested list (non-number elements) makes == False / != True."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        assert (a == [[1.0, 2.0], [3.0, 4.0]]) is False
        assert (a != [[1.0, 2.0], [3.0, 4.0]]) is True

    def test_malformed_list_ordering_raises(self):
        """An ordering operator against a malformed list raises TypeError."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        with pytest.raises(TypeError):
            _ = a < [1.0, "x", 3.0]

    def test_nested_list_ordering_raises(self):
        """An ordering operator against a nested list raises TypeError."""
        a = Matrix(2, 2, [1.0, 2.0, 3.0, 4.0])
        with pytest.raises(TypeError):
            _ = a < [[1.0, 2.0], [3.0, 4.0]]

    def test_in_list_with_uncoercible_elements(self):
        """`matrix in container` stays exception-free past empty/malformed lists."""
        a = Matrix(1, 3, [1.0, 2.0, 3.0])
        b = Matrix(1, 3, [1.0, 2.0, 3.0])
        assert a in [[], [1.0, "x", 3.0], b]


class TestWhereExtended:
    """where() NaN propagation, bool, and sequence operands."""

    def test_nan_value_propagates(self):
        """Selecting a NaN value propagates it; value NaN is not special-cased."""
        nan = float("nan")
        mask = Matrix(1, 2, [0.0, 1.0])
        b = Matrix(1, 2, [nan, nan])
        result = Matrix.where(mask, 1.0, b)
        assert math.isnan(result[0, 0])
        assert result[0, 1] == 1.0

    def test_list_operand(self):
        """A list value operand is taken as a row vector matching the mask."""
        mask = Matrix(1, 3, [1.0, 0.0, 1.0])
        result = Matrix.where(mask, [10.0, 20.0, 30.0], 0.0)
        assert _flat(result) == [10.0, 0.0, 30.0]

    def test_tuple_operand(self):
        """A tuple value operand is coerced the same as a list."""
        mask = Matrix(1, 3, [0.0, 1.0, 0.0])
        result = Matrix.where(mask, 9.0, (10.0, 20.0, 30.0))
        assert _flat(result) == [10.0, 9.0, 30.0]

    def test_list_shape_mismatch_raises(self):
        """A list whose shape differs from the mask raises ValueError."""
        mask = Matrix(2, 2, [1.0, 0.0, 0.0, 1.0])
        with pytest.raises(ValueError):
            Matrix.where(mask, [1.0, 2.0, 3.0], 0.0)

    def test_bool_operand(self):
        """Bool value operands are scalars (True -> 1.0, False -> 0.0)."""
        mask = Matrix(1, 2, [1.0, 0.0])
        result = Matrix.where(mask, True, False)
        assert _flat(result) == [1.0, 0.0]

    def test_empty_list_operand_raises(self):
        """An empty list value operand cannot be coerced and raises ValueError."""
        mask = Matrix(1, 3, [1.0, 0.0, 1.0])
        with pytest.raises(ValueError):
            Matrix.where(mask, [], 0.0)

    def test_empty_list_b_operand_raises(self):
        """An empty list in the b (second) position also raises ValueError."""
        mask = Matrix(1, 3, [1.0, 0.0, 1.0])
        with pytest.raises(ValueError):
            Matrix.where(mask, 0.0, [])

    def test_malformed_list_operand_raises(self):
        """A list value operand holding a non-number raises TypeError."""
        mask = Matrix(1, 3, [1.0, 0.0, 1.0])
        with pytest.raises(TypeError):
            Matrix.where(mask, [1.0, "x", 3.0], 0.0)

    def test_malformed_list_b_operand_raises(self):
        """A non-number element in the b (second) position raises TypeError."""
        mask = Matrix(1, 3, [1.0, 0.0, 1.0])
        with pytest.raises(TypeError):
            Matrix.where(mask, 0.0, [1.0, "x", 3.0])


class TestFmaBroadcast:
    """fma() row- and column-vector broadcasting of b and c."""

    def test_row_vector_b(self):
        """A 1xN b broadcasts down the rows of self."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        row = Matrix(1, 3, [10.0, 20.0, 30.0])
        out = a.fma(row, 0.0)
        for i in range(2):
            for j in range(3):
                assert out[i, j] == ref_fma(a[i, j], row[0, j], 0.0)

    def test_column_vector_b(self):
        """An Mx1 b broadcasts across the columns of self."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        col = Matrix(2, 1, [10.0, 20.0])
        out = a.fma(col, 0.0)
        for i in range(2):
            for j in range(3):
                assert out[i, j] == ref_fma(a[i, j], col[i, 0], 0.0)

    def test_row_vector_c(self):
        """A 1xN c broadcasts down the rows of self."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        row = Matrix(1, 3, [10.0, 20.0, 30.0])
        out = a.fma(1.0, row)
        for i in range(2):
            for j in range(3):
                assert out[i, j] == ref_fma(a[i, j], 1.0, row[0, j])

    def test_column_vector_c(self):
        """An Mx1 c broadcasts across the columns of self."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        col = Matrix(2, 1, [10.0, 20.0])
        out = a.fma(1.0, col)
        for i in range(2):
            for j in range(3):
                assert out[i, j] == ref_fma(a[i, j], 1.0, col[i, 0])

    def test_row_b_and_column_c(self):
        """A row-vector b and a column-vector c broadcast independently."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        row = Matrix(1, 3, [10.0, 20.0, 30.0])
        col = Matrix(2, 1, [100.0, 200.0])
        out = a.fma(row, col)
        for i in range(2):
            for j in range(3):
                assert out[i, j] == ref_fma(a[i, j], row[0, j], col[i, 0])

    def test_broadcast_in_place(self):
        """A broadcast operand still supports in_place=True."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        av = _flat(a)
        row = Matrix(1, 3, [10.0, 20.0, 30.0])
        expected = [ref_fma(av[i * 3 + j], row[0, j], 0.0)
                    for i in range(2) for j in range(3)]
        out = a.fma(row, 0.0, in_place=True)
        assert out is a
        assert _flat(a) == expected

    def test_broadcast_leaves_operand_unmodified(self):
        """Materialising a broadcast operand does not mutate the source vector."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        row = Matrix(1, 3, [10.0, 20.0, 30.0])
        before = _flat(row)
        a.fma(row, 0.0)
        assert _flat(row) == before

    def test_wrong_length_row_vector_raises(self):
        """A 1xN b whose width mismatches self's columns raises ValueError."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        bad = Matrix(1, 2, [1.0, 2.0])
        with pytest.raises(ValueError):
            a.fma(bad, 0.0)

    def test_wrong_length_column_vector_raises(self):
        """An Mx1 b whose height mismatches self's rows raises ValueError."""
        a = Matrix(2, 3, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        bad = Matrix(3, 1, [1.0, 2.0, 3.0])
        with pytest.raises(ValueError):
            a.fma(bad, 0.0)
