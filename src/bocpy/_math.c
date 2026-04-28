#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "compat.h"
#include "xidata.h"

#ifndef _WIN32
#include <math.h>
#endif

/// @brief Convenience method to obtain the interpreter ID
/// @return the ID of the currently running interpreter
static inline PY_INT64_T get_interpid() {
  PyThreadState *ts = PyThreadState_GET();
  PyInterpreterState *is = PyThreadState_GetInterpreter(ts);
  return PyInterpreterState_GetID(is);
}

/// @brief Underlying C-based matrix implementation
typedef struct boc_matrix_impl {
  /// @brief The raw double values of the matrix
  double *data;
  /// @brief The number of values
  size_t size;

  /// @brief The number of rows in the matrix
  size_t rows;
  /// @brief The number of columns in the matrix
  size_t columns;

  /// @brief Pointers to the beginings of each row
  double **row_ptrs;

  /// @brief The interpreter that owns the matrix. Only the owner can read/write
  /// its values.
  atomic_int_least64_t owner;
  /// @brief Atomic reference count
  atomic_int_least64_t rc;
} matrix_impl;

static void impl_free(matrix_impl *matrix) {
  if (matrix->row_ptrs != NULL) {
    PyMem_RawFree(matrix->row_ptrs);
  }

  if (matrix->data != NULL) {
    PyMem_RawFree(matrix->data);
  }

  PyMem_RawFree(matrix);
}

static inline int_least64_t impl_decref(matrix_impl *matrix) {
  int_least64_t rc = atomic_fetch_add(&matrix->rc, -1) - 1;
  return rc;
}

#define IMPL_DECREF(c)                                                         \
  if (c != NULL && impl_decref((c)) == 0) {                                    \
    impl_free((c));                                                            \
  }

static inline int_least64_t impl_incref(matrix_impl *matrix) {
  int_least64_t rc = atomic_fetch_add(&matrix->rc, 1) + 1;
  return rc;
}

#define IMPL_INCREF(c) impl_incref((c))

static int update_row_ptrs(matrix_impl *matrix) {
  if (matrix->row_ptrs != NULL) {
    PyMem_RawFree(matrix->row_ptrs);
  }

  matrix->row_ptrs = PyMem_RawCalloc(matrix->rows, sizeof(double *));
  if (matrix->row_ptrs == NULL) {
    PyErr_NoMemory();
    return -1;
  }

  double *row_ptr = matrix->data;
  for (size_t r = 0; r < matrix->rows; ++r, row_ptr += matrix->columns) {
    matrix->row_ptrs[r] = row_ptr;
  }

  return 0;
}

static matrix_impl *impl_new(size_t rows, size_t columns) {
  assert(rows > 0 && columns > 0);

  matrix_impl *matrix = (matrix_impl *)PyMem_RawMalloc(sizeof(matrix_impl));
  if (matrix == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  matrix->size = rows * columns;

  matrix->data = (double *)PyMem_RawCalloc(matrix->size, sizeof(double));
  if (matrix->data == NULL) {
    impl_free(matrix);
    PyErr_NoMemory();
    return NULL;
  }

  matrix->row_ptrs = PyMem_RawCalloc(rows, sizeof(double *));
  if (matrix->row_ptrs == NULL) {
    impl_free(matrix);
    PyErr_NoMemory();
    return NULL;
  }

  matrix->rows = rows;
  matrix->columns = columns;
  atomic_store(&matrix->owner, get_interpid());
  atomic_store(&matrix->rc, 0);

  if (update_row_ptrs(matrix) < 0) {
    impl_free(matrix);
    return NULL;
  }

  return matrix;
}

static matrix_impl *impl_transpose(matrix_impl *matrix) {
  const size_t M = matrix->rows;
  const size_t N = matrix->columns;
  matrix_impl *transpose = impl_new(N, M);
  if (transpose == NULL) {
    return NULL;
  }

  if (M == 1 || N == 1) {
    memcpy(transpose->data, matrix->data, sizeof(double) * matrix->size);
    return transpose;
  }

  double *src_col_ptr = matrix->data;
  double *dst_ptr = transpose->data;
  for (size_t r = 0; r < N; ++r, ++src_col_ptr) {
    double *src_ptr = src_col_ptr;
    for (size_t c = 0; c < M; ++c, src_ptr += N, ++dst_ptr) {
      *dst_ptr = *src_ptr;
    }
  }

  return transpose;
}

static int impl_transpose_in_place(matrix_impl *matrix) {
  const size_t M = matrix->rows;
  const size_t N = matrix->columns;

  if (M == 1 || N == 1) {
    // vector
    matrix->rows = N;
    matrix->columns = M;
    return update_row_ptrs(matrix);
  }

  if (M == N) {
    // square matrix
    for (size_t r = 0; r < matrix->rows; ++r) {
      for (size_t c = 0; c < r; ++c) {
        double temp = matrix->row_ptrs[r][c];
        matrix->row_ptrs[r][c] = matrix->row_ptrs[c][r];
        matrix->row_ptrs[c][r] = temp;
      }
    }

    return 0;
  }

  bool *visited = PyMem_RawCalloc(matrix->size, sizeof(bool));
  if (visited == NULL) {
    PyErr_NoMemory();
    return -1;
  }

  size_t size = M * N - 1;

  for (size_t i = 0; i < matrix->size; ++i) {
    visited[i] = false;
  }

  visited[0] = visited[size] = true;

  for (size_t i = 1; i < size; ++i) {
    if (visited[i]) {
      continue;
    }

    size_t current = i;
    size_t cycle_start = current;
    double value = matrix->data[current];
    do {
      size_t next = (current * M) % size;
      double temp = matrix->data[next];
      matrix->data[next] = value;
      value = temp;
      visited[current] = true;
      current = next;
    } while (current != cycle_start);
  }

  PyMem_RawFree(visited);

  matrix->rows = N;
  matrix->columns = M;

  return update_row_ptrs(matrix) < 0;
}

enum BinaryOps {
  Add = 1000,
  Subtract = 1001,
  RSubtract = 1002,
  Multiply = 1003,
  Divide = 1004,
  RDivide = 1005
};

static double binary_op(enum BinaryOps op, double lhs, double rhs) {
  switch (op) {
  case Add:
    return lhs + rhs;

  case Subtract:
    return lhs - rhs;

  case RSubtract:
    return rhs - lhs;

  case Multiply:
    return lhs * rhs;

  case Divide:
    return lhs / rhs;

  case RDivide:
    return rhs / lhs;

  default:
    fprintf(stderr, "Unknown binary op\n");
    return nan("");
  }
}

/// @brief This computes the result of a binary operation on every value in both
/// matrices
static void impl_ewise_binary(matrix_impl *lhs, matrix_impl *rhs,
                              matrix_impl *out, enum BinaryOps op) {
  assert(lhs->rows == out->rows && lhs->columns == out->columns);
  assert(lhs->rows == rhs->rows && lhs->columns == rhs->columns);

  const double *lhs_ptr = lhs->data;
  const double *rhs_ptr = rhs->data;
  double *out_ptr = out->data;
  for (size_t i = 0; i < lhs->size; ++i, ++lhs_ptr, ++rhs_ptr, ++out_ptr) {
    *out_ptr = binary_op(op, *lhs_ptr, *rhs_ptr);
  }
}

/// @brief Same as above, but broadcasts a row vector to the matrix
static void impl_rowwise_binary(matrix_impl *matrix, matrix_impl *vector,
                                matrix_impl *out, enum BinaryOps op) {
  const size_t M = matrix->rows;
  const size_t N = matrix->columns;
  assert(M == out->rows && N == out->columns);
  assert(N == vector->columns && vector->rows == 1);

  const double *lhs_ptr = matrix->data;
  double *out_ptr = out->data;
  for (size_t r = 0; r < M; ++r) {
    const double *rhs_ptr = vector->data;
    for (size_t c = 0; c < N; ++c, ++lhs_ptr, ++rhs_ptr, ++out_ptr) {
      *out_ptr = binary_op(op, *lhs_ptr, *rhs_ptr);
    }
  }
}

/// @brief Same as above, but broadcasts a column vector to the matrix
static void impl_columnwise_binary(matrix_impl *matrix, matrix_impl *vector,
                                   matrix_impl *out, enum BinaryOps op) {
  const size_t M = matrix->rows;
  const size_t N = matrix->columns;
  assert(M == out->rows && N == out->columns);
  assert(M == vector->rows && vector->columns == 1);

  const double *lhs_ptr = matrix->data;
  const double *rhs_ptr = vector->data;
  double *out_ptr = out->data;

  for (size_t r = 0; r < M; ++r, ++rhs_ptr) {
    for (size_t c = 0; c < N; ++c, ++lhs_ptr, ++out_ptr) {
      *out_ptr = binary_op(op, *lhs_ptr, *rhs_ptr);
    }
  }
}

/// @brief Same as te above, but broadcasts a scalar to the matrix
static void impl_scalar_binary(matrix_impl *matrix, double scalar,
                               matrix_impl *out, enum BinaryOps op) {
  assert(matrix->rows == out->rows && matrix->columns == out->columns);
  const double *lhs_ptr = matrix->data;
  double *out_ptr = out->data;
  for (size_t i = 0; i < matrix->size; ++i, ++lhs_ptr, ++out_ptr) {
    *out_ptr = binary_op(op, *lhs_ptr, scalar);
  }
}

enum AggregateOps {
  Sum = 2000,
  Mean = 2001,
  Magnitude = 2002,
  Maximum = 2003,
  Minimum = 2004
};

static double aggregate_op(enum AggregateOps op, double aggregate, double value,
                           size_t count) {
  switch (op) {
  case Sum:
    return aggregate + value;

  case Mean:
    return aggregate + (value - aggregate) / count;

  case Magnitude:
    return aggregate + (value * value);

  case Minimum:
    if (count == 1) {
      return value;
    }

    return aggregate < value ? aggregate : value;

  case Maximum:
    if (count == 1) {
      return value;
    }

    return aggregate > value ? aggregate : value;

  default:
    fprintf(stderr, "Unknown aggregate op\n");
    return nan("");
  }
}

static double impl_ewise_aggregate(matrix_impl *matrix, enum AggregateOps op) {
  double agg = 0;
  double *ptr = matrix->data;
  for (size_t i = 0; i < matrix->size; ++i, ++ptr) {
    agg = aggregate_op(op, agg, *ptr, i + 1);
  }

  if (op == Magnitude) {
    agg = sqrt(agg);
  }

  return agg;
}

static void impl_rowwise_aggregate(matrix_impl *matrix, enum AggregateOps op,
                                   matrix_impl *vector) {
  const size_t M = matrix->rows;
  const size_t N = matrix->columns;

  assert(vector->rows == M && vector->columns == 1);

  const double *mat_ptr = matrix->data;
  double *vec_ptr = vector->data;
  for (size_t r = 0; r < M; ++r, ++vec_ptr) {
    double agg = 0;
    for (size_t c = 0; c < N; ++c, ++mat_ptr) {
      agg = aggregate_op(op, agg, *mat_ptr, c + 1);
    }

    if (op == Magnitude) {
      agg = sqrt(agg);
    }

    *vec_ptr = agg;
  }
}

static void impl_columnwise_aggregate(matrix_impl *matrix, enum AggregateOps op,
                                      matrix_impl *vector) {
  const size_t M = matrix->rows;
  const size_t N = matrix->columns;

  assert(vector->columns == N && vector->rows == 1);

  const double *mat_ptr = matrix->data;

  for (size_t r = 0; r < M; ++r) {
    double *vec_ptr = vector->data;
    for (size_t c = 0; c < N; ++c, ++mat_ptr, ++vec_ptr) {
      *vec_ptr = aggregate_op(op, *vec_ptr, *mat_ptr, r + 1);
    }
  }

  if (op == Magnitude) {
    double *vec_ptr = vector->data;
    for (size_t c = 0; c < N; ++c, ++vec_ptr) {
      *vec_ptr = sqrt(*vec_ptr);
    }
  }
}

enum UnaryOps {
  Ceil = 3000,
  Floor = 3001,
  Round = 3002,
  Negate = 3003,
  Abs = 3004
};

static double unary_op(enum UnaryOps op, double value) {
  switch (op) {
  case Ceil:
    return ceil(value);

  case Floor:
    return floor(value);

  case Round:
    return round(value);

  case Negate:
    return -value;

  case Abs:
    return fabs(value);

  default:
    fprintf(stderr, "Unknown unary op\n");
    return nan("");
  }
}

static void impl_unary(matrix_impl *matrix, enum UnaryOps op,
                       matrix_impl *out) {
  assert(matrix->rows == out->rows && matrix->columns == out->columns);

  const double *src_ptr = matrix->data;
  double *dst_ptr = out->data;
  for (size_t i = 0; i < matrix->size; ++i, ++src_ptr, ++dst_ptr) {
    *dst_ptr = unary_op(op, *src_ptr);
  }
}

static void impl_matmul(matrix_impl *lhs, matrix_impl *rhs, matrix_impl *out) {
  const size_t M0 = lhs->rows;
  const size_t N0 = lhs->columns;
  const size_t N1 = rhs->columns;
  assert(M0 == out->rows && N1 == out->columns);
  assert(N0 == rhs->rows);

  double *out_ptr = out->data;

  for (size_t r = 0; r < M0; ++r) {
    for (size_t c = 0; c < N1; ++c, ++out_ptr) {
      const double *lhs_ptr = lhs->row_ptrs[r];
      const double *rhs_ptr = rhs->data + c;
      double sum = 0;
      for (size_t k = 0; k < N0; ++k, ++lhs_ptr, rhs_ptr += N1) {
        sum += (*lhs_ptr) * (*rhs_ptr);
      }

      *out_ptr = sum;
    }
  }
}

static bool impl_allclose(matrix_impl *lhs, matrix_impl *rhs, double rtol,
                          double atol, bool equal_nan) {
  if (lhs->rows != rhs->rows || lhs->columns != rhs->columns) {
    return false;
  }

  const double *lhs_ptr = lhs->data;
  const double *rhs_ptr = rhs->data;

  for (size_t i = 0; i < lhs->size; ++i, ++lhs_ptr, ++rhs_ptr) {
    double a = *lhs_ptr;
    double b = *rhs_ptr;
    if (isnan(a) && isnan(b) && equal_nan) {
      continue;
    }

    if (fabs(a - b) > (atol + rtol * fabs(b))) {
      return false;
    }
  }

  return true;
}

typedef struct range_s {
  Py_ssize_t start;
  Py_ssize_t stop;
  Py_ssize_t step;
  size_t count;
} range;

/// @brief This processes the arguments to __get__ to produce the actual
/// requested range in the matrix.
int range_read(range *range, PyObject *key, size_t length) {
  Py_ssize_t start, stop, step;
  if (PyLong_Check(key)) {
    start = PyLong_AsSsize_t(key);
    if (start < 0) {
      start += (Py_ssize_t)length;
    }
    stop = start + 1;
    step = 1;
  } else if (PySlice_Check(key)) {
    PySlice_Unpack(key, &start, &stop, &step);
  } else {
    PyErr_SetString(PyExc_TypeError, "Key must be a long or a slice");
    return -1;
  }

  PySlice_AdjustIndices((Py_ssize_t)length, &start, &stop, step);

  range->start = start;
  range->stop = stop;
  range->step = step;
  range->count = (size_t)((range->stop - range->start) / range->step);

  if (range->count == 0) {
    PyErr_SetNone(PyExc_IndexError);
    return -1;
  }

  return 0;
}

static void impl_get(matrix_impl *matrix, range *rows, range *columns,
                     matrix_impl *out) {
  assert(rows->count == out->rows);
  assert(columns->count == out->columns);

  double *out_ptr = out->data;
  for (size_t rr = 0, r = rows->start; rr < out->rows; ++rr, r += rows->step) {
    double *mat_ptr = matrix->row_ptrs[r] + columns->start;
    for (size_t cc = 0; cc < out->columns;
         ++cc, mat_ptr += columns->step, ++out_ptr) {
      *out_ptr = *mat_ptr;
    }
  }
}

static void impl_set_scalar(matrix_impl *matrix, range *rows, range *columns,
                            double value) {
  Py_ssize_t r = rows->start;
  for (size_t rr = 0; rr < rows->count; ++rr, r += rows->step) {
    double *mat_ptr = matrix->row_ptrs[r] + columns->start;
    for (size_t cc = 0; cc < columns->count; ++cc, mat_ptr += columns->step) {
      *mat_ptr = value;
    }
  }
}

static void impl_set_rowwise(matrix_impl *matrix, range *rows, range *columns,
                             matrix_impl *vector) {
  assert(vector->rows == 1 && vector->columns == columns->count);

  Py_ssize_t r = rows->start;
  for (size_t rr = 0; rr < rows->count; ++rr, r += rows->step) {
    double *mat_ptr = matrix->row_ptrs[r] + columns->start;
    const double *vec_ptr = vector->data;
    for (size_t cc = 0; cc < columns->count;
         ++cc, mat_ptr += columns->step, ++vec_ptr) {
      *mat_ptr = *vec_ptr;
    }
  }
}

static void impl_set_columnwise(matrix_impl *matrix, range *rows,
                                range *columns, matrix_impl *vector) {
  assert(vector->columns == 1 && vector->rows == rows->count);

  const double *vec_ptr = vector->data;
  Py_ssize_t r = rows->start;
  for (size_t rr = 0; rr < rows->count; ++rr, r += rows->step, ++vec_ptr) {
    double *mat_ptr = matrix->row_ptrs[r] + columns->start;
    for (size_t cc = 0; cc < columns->count; ++cc, mat_ptr += columns->step) {
      *mat_ptr = *vec_ptr;
    }
  }
}

static void impl_set(matrix_impl *matrix, range *rows, range *columns,
                     matrix_impl *submatrix) {
  assert(rows->count == submatrix->rows &&
         columns->count == submatrix->columns);

  const double *submat_ptr = submatrix->data;
  Py_ssize_t r = rows->start;
  for (size_t rr = 0; rr < rows->count; ++rr, r += rows->step) {
    double *mat_ptr = matrix->row_ptrs[r] + columns->start;
    for (size_t cc = 0; cc < columns->count;
         ++cc, mat_ptr += columns->step, ++submat_ptr) {
      *mat_ptr = *submat_ptr;
    }
  }
}

static bool unwrap_double(PyObject *op, double *value) {
  if (!PyNumber_Check(op)) {
    return false;
  }

  PyObject *fvalue = PyNumber_Float(op);
  if (fvalue == NULL) {
    return false;
  }

  *value = PyFloat_AsDouble(fvalue);
  Py_DECREF(fvalue);
  return true;
}

/// @brief Converts a list or tuple to a matrix
static matrix_impl *impl_new_from_sequence(PyObject *sequence, bool as_column) {
  const char *err_msg = "Expected a list/tuple of numbers";
  PyObject *fast = PySequence_Fast(sequence, err_msg);
  if (fast == NULL) {
    return NULL;
  }

  Py_ssize_t size = PySequence_Fast_GET_SIZE(fast);
  matrix_impl *impl;
  if (as_column) {
    impl = impl_new((size_t)size, 1);
  } else {
    impl = impl_new(1, (size_t)size);
  }
  if (impl == NULL) {
    Py_DECREF(fast);
    return NULL;
  }

  double *ptr = impl->data;
  for (Py_ssize_t i = 0; i < size; ++i, ++ptr) {
    PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
    if (!unwrap_double(item, ptr)) {
      PyErr_SetString(PyExc_TypeError, err_msg);
      Py_DECREF(fast);
      impl_free(impl);
      return NULL;
    }
  }

  Py_DECREF(fast);
  return impl;
}

static bool impl_check_acquired(matrix_impl *matrix, bool set_error) {
  PY_INT64_T current_id = get_interpid();
  if (current_id != atomic_load(&matrix->owner)) {
    if (set_error) {
      PyErr_SetString(PyExc_RuntimeError,
                      "The current interpreter does not own this matrix");
    }
    return false;
  }

  return true;
}

// Forward declarations
static struct PyModuleDef _math_module;

typedef struct {
  int_least64_t interpid;
  PyTypeObject *matrix_type;
} _math_module_state;

static thread_local _math_module_state *LOCAL_STATE;

#define LOCAL_STATE_SET(m)                                                     \
  do {                                                                         \
    LOCAL_STATE = (_math_module_state *)PyModule_GetState(m);                  \
  } while (0)

typedef struct matrix_object {
  PyObject_HEAD matrix_impl *impl;
} MatrixObject;

static PyObject *Matrix_new(PyTypeObject *type, PyObject *args,
                            PyObject *kwds) {
  MatrixObject *self;
  self = (MatrixObject *)type->tp_alloc(type, 0);
  if (self == NULL) {
    return NULL;
  }

  self->impl = NULL;
  return (PyObject *)self;
}

static int Matrix_init(PyObject *op, PyObject *args, PyObject *kwargs) {
  MatrixObject *self = (MatrixObject *)op;
  Py_ssize_t srows = 0;
  Py_ssize_t scolumns = 0;
  PyObject *values = NULL;

  if (!PyArg_ParseTuple(args, "nn|O", &srows, &scolumns, &values)) {
    return -1;
  }

  if (srows <= 0 || scolumns <= 0) {
    PyErr_SetString(PyExc_AssertionError, "Rows and columns must both be > 0");
    return -1;
  }

  self->impl = NULL;

  size_t rows = (size_t)srows;
  size_t columns = (size_t)scolumns;
  matrix_impl *impl = impl_new(rows, columns);
  if (impl == NULL) {
    return -1;
  }

  self->impl = impl;
  IMPL_INCREF(impl);

  if (values == NULL) {
    return 0;
  }

  range rows_range = {0, (Py_ssize_t)rows, 1, rows};
  range columns_range = {0, (Py_ssize_t)columns, 1, columns};

  if (PyLong_Check(values)) {
    impl_set_scalar(self->impl, &rows_range, &columns_range,
                    PyLong_AsDouble(values));
    return 0;
  }

  if (PyFloat_Check(values)) {
    impl_set_scalar(self->impl, &rows_range, &columns_range,
                    PyFloat_AsDouble(values));
    return 0;
  }

  const char *err_msg =
      "Values must be either a number or a list/tuple of MxN numbers";

  if (!PySequence_Check(values)) {
    PyErr_SetString(PyExc_TypeError, err_msg);
    return -1;
  }

  PyObject *values_fast = PySequence_Fast(values, err_msg);
  if (values_fast == NULL) {
    return -1;
  }

  Py_ssize_t size = PySequence_Fast_GET_SIZE(values_fast);

  if ((size_t)size != impl->size) {
    Py_DECREF(values_fast);
    PyErr_SetString(PyExc_TypeError, err_msg);
    return -1;
  }

  double *ptr = impl->data;
  for (Py_ssize_t i = 0; i < size; ++i, ++ptr) {
    PyObject *item = PySequence_Fast_GET_ITEM(values_fast, i);
    if (PyLong_Check(item)) {
      *ptr = PyLong_AsDouble(item);
    } else if (PyFloat_Check(item)) {
      *ptr = PyFloat_AsDouble(item);
    } else {
      PyErr_SetString(PyExc_TypeError, err_msg);
      Py_DECREF(values_fast);
      return -1;
    }
  }

  Py_DECREF(values_fast);
  return 0;
}

static void Matrix_dealloc(PyObject *op) {
  MatrixObject *self = (MatrixObject *)op;
  if (self->impl != NULL) {
    IMPL_DECREF(self->impl);
    self->impl = NULL;
  }

  Py_TYPE(self)->tp_free(self);
}

PyObject *wrap_matrix(PyTypeObject *type, matrix_impl *impl) {
  MatrixObject *matrix = (MatrixObject *)type->tp_alloc(type, 0);
  if (matrix == NULL) {
    return NULL;
  }

  matrix->impl = impl;
  IMPL_INCREF(impl);
  return (PyObject *)matrix;
}

static PyObject *wrap_impl_or_free(matrix_impl *impl) {
  PyObject *matrix = wrap_matrix(LOCAL_STATE->matrix_type, impl);
  if (matrix == NULL) {
    impl_free(impl);
  }

  return matrix;
}

matrix_impl *unwrap_matrix(PyObject *op, bool seq_as_column) {
  PyTypeObject *type = LOCAL_STATE->matrix_type;
  MatrixObject *matrix;
  matrix_impl *impl;
  if (Py_TYPE(op) == type) {
    matrix = (MatrixObject *)op;
    impl = matrix->impl;

    if (!impl_check_acquired(impl, true)) {
      return NULL;
    }

    IMPL_INCREF(impl);
    return impl;
  }

  impl = impl_new_from_sequence(op, seq_as_column);
  if (impl == NULL) {
    return NULL;
  }

  IMPL_INCREF(impl);
  return impl;
}

static PyObject *Matrix_transpose(PyObject *op, PyObject *Py_UNUSED(dummy)) {
  MatrixObject *matrix = (MatrixObject *)op;
  matrix_impl *impl = matrix->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  matrix_impl *transpose = impl_transpose(impl);
  if (transpose == NULL) {
    return NULL;
  }

  return wrap_impl_or_free(transpose);
}

static PyObject *Matrix_transpose_in_place(PyObject *op,
                                           PyObject *Py_UNUSED(dummy)) {
  MatrixObject *matrix = (MatrixObject *)op;
  matrix_impl *impl = matrix->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  impl_transpose_in_place(impl);
  Py_RETURN_NONE;
}

/// @brief Sets the output of an arithmetic operation.
/// @details The output of an arithmetic operation is either a new matrix of the
/// same dimensions as the left hand side of the equation, or the left-hand side
/// itself in the case of in-place operations.
/// @param lhs_op The PyObject of the left-hand side of the operation
/// @param out_op A pointer to the output pointer of the equation
/// @param inplace Whether this is an inplace operation
/// @return The matrix wrapped by out_op, or NULL in the case of an error
static matrix_impl *set_output(PyObject *lhs_op, PyObject **out_op,
                               bool inplace) {
  MatrixObject *lhs_matrix = (MatrixObject *)lhs_op;
  matrix_impl *out;
  if (inplace) {
    *out_op = Py_NewRef(lhs_op);
    return lhs_matrix->impl;
  }

  matrix_impl *lhs = lhs_matrix->impl;
  out = impl_new(lhs->rows, lhs->columns);
  if (out == NULL) {
    return NULL;
  }

  *out_op = wrap_matrix(Py_TYPE(lhs_op), out);
  if (*out_op == NULL) {
    impl_free(out);
    return NULL;
  }

  return out;
}

const int NO_AXIS = -1000;

static int Matrix_aggregate(PyObject *matrix_op, int axis, PyObject **out_op,
                            enum AggregateOps agg) {
  MatrixObject *matrix = (MatrixObject *)matrix_op;
  matrix_impl *impl = matrix->impl;

  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  if (axis == NO_AXIS) {
    *out_op = PyFloat_FromDouble(impl_ewise_aggregate(impl, agg));
    return 0;
  }

  if (axis == 0 || axis == -2) {
    matrix_impl *vector = impl_new(1, impl->columns);
    if (vector == NULL) {
      return -1;
    }

    impl_columnwise_aggregate(impl, agg, vector);
    *out_op = wrap_matrix(Py_TYPE(matrix_op), vector);
    if (*out_op == NULL) {
      impl_free(vector);
      return -1;
    }

    return 0;
  }

  if (axis == 1 || axis == -1) {
    matrix_impl *vector = impl_new(impl->rows, 1);
    if (vector == NULL) {
      return -1;
    }

    impl_rowwise_aggregate(impl, agg, vector);
    *out_op = wrap_matrix(Py_TYPE(matrix_op), vector);
    if (*out_op == NULL) {
      impl_free(vector);
      return -1;
    }

    return 0;
  }

  PyErr_SetString(PyExc_NotImplementedError, "axis must be -2, -1, 0, or 1");
  return -1;
}

// this macro provides a kind of template for all the aggregate methods to
// follow, as they are all identical with the exception of the operator

#define MATRIX_AGGREGATE(agg)                                                  \
  static PyObject *Matrix_##agg##_method(PyObject *op, PyObject *args) {       \
    PyObject *out = NULL;                                                      \
    PyObject *axis = NULL;                                                     \
    if (!PyArg_ParseTuple(args, "|O", &axis)) {                                \
      return NULL;                                                             \
    }                                                                          \
    if (axis == NULL) {                                                        \
      if (Matrix_aggregate(op, NO_AXIS, &out, agg) < 0) {                      \
        return NULL;                                                           \
      }                                                                        \
      return out;                                                              \
    }                                                                          \
    if (!PyLong_Check(axis)) {                                                 \
      PyErr_SetString(PyExc_TypeError, "axis must be a long");                 \
      return NULL;                                                             \
    }                                                                          \
    if (Matrix_aggregate(op, PyLong_AsLong(axis), &out, agg) < 0) {            \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

MATRIX_AGGREGATE(Sum)
MATRIX_AGGREGATE(Mean)
MATRIX_AGGREGATE(Magnitude)
MATRIX_AGGREGATE(Minimum)
MATRIX_AGGREGATE(Maximum)

static int Matrix_unary(PyObject *matrix_op, PyObject **out_op,
                        enum UnaryOps unary) {
  MatrixObject *self = (MatrixObject *)matrix_op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  matrix_impl *out = set_output(matrix_op, out_op, false);
  if (out == NULL) {
    return -1;
  }

  impl_unary(impl, unary, out);
  return 0;
}

#define MATRIX_UNARY_METHOD(unary)                                             \
  static PyObject *Matrix_##unary##_method(PyObject *op,                       \
                                           PyObject *Py_UNUSED(dummy)) {       \
    PyObject *out = NULL;                                                      \
    if (Matrix_unary(op, &out, unary) < 0) {                                   \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

MATRIX_UNARY_METHOD(Ceil)
MATRIX_UNARY_METHOD(Floor)
MATRIX_UNARY_METHOD(Round)
MATRIX_UNARY_METHOD(Negate)
MATRIX_UNARY_METHOD(Abs)

static PyObject *Matrix_clip(PyObject *op, PyObject *args) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  PyObject *minval_op = NULL;
  PyObject *maxval_op = NULL;

  if (!PyArg_ParseTuple(args, "O|O", &minval_op, &maxval_op)) {
    return NULL;
  }

  double minval;
  double maxval;
  if (maxval_op == NULL) {
    minval = 0;
    if (!unwrap_double(minval_op, &maxval)) {
      PyErr_SetString(PyExc_TypeError, "Expected a number");
      return NULL;
    }
  } else {
    if (!unwrap_double(minval_op, &minval)) {
      PyErr_SetString(PyExc_TypeError, "Expected a number");
      return NULL;
    }

    if (!unwrap_double(maxval_op, &maxval)) {
      PyErr_SetString(PyExc_TypeError, "Expected a number");
      return NULL;
    }
  }

  if (maxval < minval) {
    PyErr_SetString(PyExc_AssertionError, "maxval < minval");
    return NULL;
  }

  PyTypeObject *type = Py_TYPE(self);
  MatrixObject *out = (MatrixObject *)type->tp_alloc(type, 0);
  if (out == NULL) {
    return NULL;
  }

  out->impl = impl_new(impl->rows, impl->columns);
  if (out->impl == NULL) {
    Py_DECREF(out);
    return NULL;
  }

  IMPL_INCREF(out->impl);

  double *src = impl->data;
  double *dst = out->impl->data;
  for (size_t i = 0; i < impl->size; ++i, ++src, ++dst) {
    double value = *src;
    if (value < minval) {
      value = minval;
    }
    if (value > maxval) {
      value = maxval;
    }

    *dst = value;
  }

  return (PyObject *)out;
}

static PyObject *Matrix_copy(PyObject *op, PyObject *Py_UNUSED(dummy)) {
  MatrixObject *matrix = (MatrixObject *)op;
  matrix_impl *impl = matrix->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  matrix_impl *copy = impl_new(impl->rows, impl->columns);
  if (copy == NULL) {
    return NULL;
  }

  memcpy(copy->data, impl->data, impl->size * sizeof(double));

  return (PyObject *)wrap_impl_or_free(copy);
}

PyObject *Matrix_select(PyObject *op, PyObject *args) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  PyObject *indices = NULL;
  int axis = 0;

  if (!PyArg_ParseTuple(args, "O|i", &indices, &axis)) {
    return NULL;
  }

  if (axis < 0) {
    axis = 2 + axis;
  }

  if (axis >= 2) {
    PyErr_SetString(PyExc_KeyError, "Invalid axis (must be 0 or 1)");
    return NULL;
  }

  const char *err_msg =
      "Indices must be specified as a list or a tuple of ints";
  PyObject *fast = PySequence_Fast(indices, err_msg);
  if (fast == NULL) {
    return NULL;
  }

  Py_ssize_t size = PySequence_Fast_GET_SIZE(indices);
  if (size <= 0) {
    Py_DECREF(fast);
    Py_RETURN_NONE;
  }

  matrix_impl *out;
  if (axis == 0) {
    out = impl_new((size_t)size, impl->columns);
    if (out == NULL) {
      Py_DECREF(fast);
      return NULL;
    }

    double *dst = out->data;
    for (Py_ssize_t i = 0; i < size; ++i) {
      PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
      size_t r = PyLong_AsSize_t(item);
      if ((Py_ssize_t)r == -1 && PyErr_Occurred()) {
        Py_DECREF(fast);
        impl_free(out);
        return NULL;
      }

      double *src = impl->row_ptrs[r];
      for (size_t i = 0; i < impl->columns; ++i, ++src, ++dst) {
        *dst = *src;
      }
    }

    Py_DECREF(fast);
    return wrap_impl_or_free(out);
  }

  out = impl_new(impl->rows, (size_t)size);
  if (out == NULL) {
    Py_DECREF(fast);
    return NULL;
  }

  size_t dst_c = 0;
  for (Py_ssize_t i = 0; i < size; ++i, ++dst_c) {
    PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
    size_t src_c = PyLong_AsSize_t(item);
    if ((Py_ssize_t)src_c == -1 && PyErr_Occurred()) {
      Py_DECREF(fast);
      impl_free(out);
      return NULL;
    }

    for (size_t r = 0; r < impl->rows; ++r) {
      out->row_ptrs[r][dst_c] = impl->row_ptrs[r][src_c];
    }
  }

  Py_DECREF(fast);
  return wrap_impl_or_free(out);
}

static PyObject *Matrix_allclose(PyObject *cls, PyObject *args,
                                 PyObject *kwargs) {
  PyObject *lhs_op = NULL;
  PyObject *rhs_op = NULL;
  double rtol = 1e-05;
  double atol = 1e-08;
  int equal_nan = 0;

  static char *keywords[] = {"", "", "rtol", "atol", "equal_nan", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O!O!|ddp", keywords, cls,
                                   &lhs_op, cls, &rhs_op, &rtol, &atol,
                                   &equal_nan)) {
    return NULL;
  }

  MatrixObject *lhs = (MatrixObject *)lhs_op;
  MatrixObject *rhs = (MatrixObject *)rhs_op;
  if (impl_allclose(lhs->impl, rhs->impl, rtol, atol, equal_nan)) {
    Py_RETURN_TRUE;
  }

  Py_RETURN_FALSE;
}

static int parse_dims(PyObject *shape, size_t *rows, size_t *columns) {
  Py_ssize_t srows;
  Py_ssize_t scolumns;

  if (!PyArg_ParseTuple(shape, "nn", &srows, &scolumns)) {
    return -1;
  }

  if (srows <= 0 || scolumns <= 0) {
    PyErr_SetString(PyExc_AssertionError, "rows and columns must both be >= 1");
    return -1;
  }

  *rows = (size_t)srows;
  *columns = (size_t)scolumns;
  return 0;
}

static PyObject *Matrix_zeros(PyObject *cls, PyObject *args) {
  PyObject *size = NULL;

  if (!PyArg_ParseTuple(args, "O", &size)) {
    return NULL;
  }

  size_t rows;
  size_t columns;

  if (parse_dims(size, &rows, &columns) < 0) {
    return NULL;
  }

  matrix_impl *impl = impl_new(rows, columns);
  if (impl == NULL) {
    return NULL;
  }

  double *ptr = impl->data;
  for (size_t i = 0; i < impl->size; ++i, ++ptr) {
    *ptr = 0.0;
  }

  return wrap_impl_or_free(impl);
}

static PyObject *Matrix_ones(PyObject *cls, PyObject *args) {
  PyObject *size = NULL;

  if (!PyArg_ParseTuple(args, "O", &size)) {
    return NULL;
  }

  size_t rows;
  size_t columns;

  if (parse_dims(size, &rows, &columns) < 0) {
    return NULL;
  }

  matrix_impl *impl = impl_new(rows, columns);
  if (impl == NULL) {
    return NULL;
  }

  double *ptr = impl->data;
  for (size_t i = 0; i < impl->size; ++i, ++ptr) {
    *ptr = 1.0;
  }

  return wrap_impl_or_free(impl);
}

const double RAND_MAX_D = (double)RAND_MAX;

static double sample_uniform(double min, double max) {
  double val = (double)rand() / RAND_MAX_D;
  return (val * (max - min)) + min;
}

static void sample_normal(double *values, size_t n, double mean,
                          double stddev) {
  size_t i = 0;
  while (i < n) {
    double u = sample_uniform(-1, 1);
    double v = sample_uniform(-1, 1);
    double s = (u * u) + (v * v);
    if (s > 0 && s < 1) {
      double factor = sqrt(-2 * log(s) / s);
      values[i] = mean + stddev * u * factor;
      i += 1;
      if (i < n) {
        values[i] = mean + stddev * v * factor;
        i += 1;
      }
    }
  }
}

static PyObject *Matrix_normal(PyObject *cls, PyObject *args,
                               PyObject *kwargs) {
  double mean = 0.0;
  double stddev = 1.0;
  PyObject *size = Py_None;

  static char *kwlist[] = {"mean", "stddev", "size", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|ddO", kwlist, &mean, &stddev,
                                   &size)) {
    return NULL;
  }

  if (Py_IsNone(size)) {
    double value = 0;
    sample_normal(&value, 1, mean, stddev);
    return PyFloat_FromDouble(value);
  }

  size_t rows, columns;
  if (parse_dims(size, &rows, &columns) < 0) {
    return NULL;
  }

  matrix_impl *impl = impl_new(rows, columns);
  if (impl == NULL) {
    return NULL;
  }

  sample_normal(impl->data, impl->size, mean, stddev);

  return wrap_impl_or_free(impl);
}

static PyObject *Matrix_uniform(PyObject *cls, PyObject *args,
                                PyObject *kwargs) {
  double minval = 0.0;
  double maxval = 1.0;
  PyObject *size = Py_None;

  static char *kwlist[] = {"minval", "maxval", "size", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|ddO", kwlist, &minval,
                                   &maxval, &size)) {
    return NULL;
  }

  if (Py_IsNone(size)) {
    return PyFloat_FromDouble(sample_uniform(minval, maxval));
  }

  size_t rows, columns;
  if (parse_dims(size, &rows, &columns) < 0) {
    return NULL;
  }

  matrix_impl *impl = impl_new(rows, columns);
  if (impl == NULL) {
    return NULL;
  }

  double *ptr = impl->data;
  for (size_t i = 0; i < impl->size; ++i, ptr++) {
    *ptr = sample_uniform(minval, maxval);
  }

  return wrap_impl_or_free(impl);
}

static PyObject *Matrix_vector(PyObject *cls, PyObject *args) {
  PyObject *sequence = NULL;
  int as_column = 0;

  if (!PyArg_ParseTuple(args, "O|b", &sequence, &as_column)) {
    return NULL;
  }

  matrix_impl *impl = impl_new_from_sequence(sequence, as_column);
  if (impl == NULL) {
    return NULL;
  }

  return wrap_impl_or_free(impl);
}

typedef struct shape_s {
  size_t rows;
  size_t columns;
} shape;

static int unwrap_and_get_shape(PyObject *object, shape *shape,
                                bool seq_to_column) {
  if (Py_TYPE(object) == LOCAL_STATE->matrix_type) {
    MatrixObject *matrix = (MatrixObject *)object;
    matrix_impl *impl = matrix->impl;
    if (!impl_check_acquired(impl, true)) {
      return -1;
    }

    shape->rows = impl->rows;
    shape->columns = impl->columns;
    return 0;
  }

  PyObject *fast =
      PySequence_Fast(object, "object must be a Matrix, List, or Tuple");
  if (fast == NULL) {
    return -1;
  }

  if (seq_to_column) {
    shape->rows = PySequence_Fast_GET_SIZE(fast);
    shape->columns = 1;
  } else {
    shape->rows = 1;
    shape->columns = PySequence_Fast_GET_SIZE(fast);
  }
  Py_DECREF(fast);
  return 0;
}

static PyObject *Matrix_concat(PyObject *cls, PyObject *args) {
  PyObject *matrices = NULL;
  int axis = 0;

  if (!PyArg_ParseTuple(args, "O|i", &matrices, &axis)) {
    return NULL;
  }

  if (axis < 0) {
    axis = 2 + axis;
  }

  if (axis >= 2) {
    PyErr_SetString(PyExc_KeyError, "Invalid axis (must be 0 or 1)");
    return NULL;
  }

  const char *err_msg =
      "Matrices must be specified as a list or a tuple of Matrix";
  PyObject *fast = PySequence_Fast(matrices, err_msg);
  if (fast == NULL) {
    return NULL;
  }

  Py_ssize_t size = PySequence_Fast_GET_SIZE(fast);
  if (size <= 0) {
    Py_DECREF(fast);
    Py_RETURN_NONE;
  }

  shape shape;
  size_t rows = 0;
  size_t columns = 0;
  range rows_range;
  range columns_range;
  matrix_impl *out;
  if (axis == 0) {
    for (Py_ssize_t i = 0; i < size; ++i) {
      PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
      if (unwrap_and_get_shape(item, &shape, false) < 0) {
        Py_DECREF(fast);
        return NULL;
      }

      if (i == 0) {
        columns = shape.columns;
      } else if (shape.columns != columns) {
        PyErr_SetString(
            PyExc_AssertionError,
            "all sub-matrices must have the same number of columns");
        Py_DECREF(fast);
        return NULL;
      }

      rows += shape.rows;
    }

    out = impl_new(rows, columns);
    if (out == NULL) {
      Py_DECREF(fast);
      return NULL;
    }

    rows_range.start = 0;
    rows_range.step = 1;
    columns_range.start = 0;
    columns_range.stop = (Py_ssize_t)columns;
    columns_range.count = columns;
    columns_range.step = 1;
    for (Py_ssize_t i = 0; i < size; ++i) {
      PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
      matrix_impl *impl = unwrap_matrix(item, false);
      if (impl == NULL) {
        Py_DECREF(fast);
        impl_free(out);
        return NULL;
      }

      rows_range.count = impl->rows;
      rows_range.stop = rows_range.start + (Py_ssize_t)impl->rows;
      impl_set(out, &rows_range, &columns_range, impl);
      IMPL_DECREF(impl);
      rows_range.start = rows_range.stop;
    }

    Py_DECREF(fast);
    return wrap_impl_or_free(out);
  }

  for (Py_ssize_t i = 0; i < size; ++i) {
    PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
    if (unwrap_and_get_shape(item, &shape, true) < 0) {
      Py_DECREF(fast);
      return NULL;
    }

    if (i == 0) {
      rows = shape.rows;
    } else if (shape.rows != rows) {
      PyErr_SetString(PyExc_AssertionError,
                      "all sub-matrices must have the same number of rows");
      Py_DECREF(fast);
      return NULL;
    }

    columns += shape.columns;
  }

  out = impl_new(rows, columns);
  if (out == NULL) {
    Py_DECREF(fast);
    return NULL;
  }

  columns_range.start = 0;
  columns_range.step = 1;
  rows_range.start = 0;
  rows_range.stop = (Py_ssize_t)rows;
  rows_range.count = rows;
  rows_range.step = 1;
  for (Py_ssize_t i = 0; i < size; ++i) {
    PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
    matrix_impl *impl = unwrap_matrix(item, true);
    if (impl == NULL) {
      Py_DECREF(fast);
      impl_free(out);
      return NULL;
    }

    columns_range.count = impl->columns;
    columns_range.stop = columns_range.start + (Py_ssize_t)impl->columns;
    impl_set(out, &rows_range, &columns_range, impl);
    IMPL_DECREF(impl);
    columns_range.start = columns_range.stop;
  }

  Py_DECREF(fast);
  return wrap_impl_or_free(out);
}

static PyMethodDef Matrix_methods[] = {
    {"transpose", Matrix_transpose, METH_NOARGS,
     "transpose($self, /)\n--\n\nReturn a transposed copy."},
    {"transpose_in_place", Matrix_transpose_in_place, METH_NOARGS,
     "transpose_in_place($self, /)\n--\n\nTranspose in place."},
    {"sum", Matrix_Sum_method, METH_VARARGS,
     "sum($self, /, axis=None)\n--\n\nSum of elements."},
    {"mean", Matrix_Mean_method, METH_VARARGS,
     "mean($self, /, axis=None)\n--\n\nMean of elements."},
    {"magnitude", Matrix_Magnitude_method, METH_VARARGS,
     "magnitude($self, /, axis=None)\n--\n\nEuclidean magnitude."},
    {"min", Matrix_Minimum_method, METH_VARARGS,
     "min($self, /, axis=None)\n--\n\nMinimum of elements."},
    {"max", Matrix_Maximum_method, METH_VARARGS,
     "max($self, /, axis=None)\n--\n\nMaximum of elements."},
    {"ceil", Matrix_Ceil_method, METH_NOARGS,
     "ceil($self, /)\n--\n\nElement-wise ceiling."},
    {"floor", Matrix_Floor_method, METH_NOARGS,
     "floor($self, /)\n--\n\nElement-wise floor."},
    {"round", Matrix_Round_method, METH_NOARGS,
     "round($self, /)\n--\n\nElement-wise rounding."},
    {"negate", Matrix_Negate_method, METH_NOARGS,
     "negate($self, /)\n--\n\nElement-wise negation."},
    {"abs", Matrix_Abs_method, METH_NOARGS,
     "abs($self, /)\n--\n\nElement-wise absolute value."},
    {"clip", Matrix_clip, METH_VARARGS,
     "clip($self, min_or_maxval, /, maxval=None)\n--\n\n"
     "Clip elements to a range."},
    {"copy", Matrix_copy, METH_NOARGS,
     "copy($self, /)\n--\n\nReturn a deep copy."},
    {"select", Matrix_select, METH_VARARGS,
     "select($self, indices, /, axis=0)\n--\n\n"
     "Select rows or columns by index."},
    {"allclose", (PyCFunction)Matrix_allclose,
     METH_VARARGS | METH_KEYWORDS | METH_CLASS,
     "allclose($type, lhs, rhs, /, rtol=1e-05, atol=1e-08, "
     "equal_nan=False)\n--\n\n"
     "Check element-wise equality within tolerance."},
    {"zeros", Matrix_zeros, METH_VARARGS | METH_CLASS,
     "zeros($type, size, /)\n--\n\nCreate a zero-filled matrix."},
    {"ones", Matrix_ones, METH_VARARGS | METH_CLASS,
     "ones($type, size, /)\n--\n\nCreate a matrix of ones."},
    {"normal", (PyCFunction)Matrix_normal,
     METH_VARARGS | METH_KEYWORDS | METH_CLASS,
     "normal($type, mean=0.0, stddev=1.0, /, size=None)\n--\n\n"
     "Sample from a normal distribution."},
    {"uniform", (PyCFunction)Matrix_uniform,
     METH_VARARGS | METH_KEYWORDS | METH_CLASS,
     "uniform($type, minval=0.0, maxval=1.0, /, size=None)\n--\n\n"
     "Sample from a uniform distribution."},
    {"vector", Matrix_vector, METH_VARARGS | METH_CLASS,
     "vector($type, values, /, as_column=False)\n--\n\n"
     "Create a vector from a sequence."},
    {"concat", Matrix_concat, METH_VARARGS | METH_CLASS,
     "concat($type, values, /, axis=0)\n--\n\n"
     "Concatenate matrices along an axis."},
    {NULL} /* Sentinel */
};

static PyObject *Matrix_get_rows(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }
  return PyLong_FromSize_t(impl->rows);
}

static PyObject *Matrix_get_columns(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }
  return PyLong_FromSize_t(impl->columns);
}

static PyObject *Matrix_get_T(PyObject *op, void *Py_UNUSED(dummy)) {
  return Matrix_transpose(op, NULL);
}

static PyObject *Matrix_get_x(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }
  return PyFloat_FromDouble(impl->data[0]);
}

static int Matrix_set_x(PyObject *op, PyObject *arg, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  if (!unwrap_double(arg, impl->data)) {
    return -1;
  }

  return 0;
}

static PyObject *Matrix_get_y(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  if (impl->size < 2) {
    PyErr_SetNone(PyExc_IndexError);
    return NULL;
  }

  return PyFloat_FromDouble(impl->data[1]);
}

static int Matrix_set_y(PyObject *op, PyObject *arg, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  if (impl->size < 2) {
    PyErr_SetNone(PyExc_IndexError);
    return -1;
  }

  if (!unwrap_double(arg, impl->data + 1)) {
    return -1;
  }

  return 0;
}

static PyObject *Matrix_get_z(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  if (impl->size < 3) {
    PyErr_SetNone(PyExc_IndexError);
    return NULL;
  }

  return PyFloat_FromDouble(impl->data[2]);
}

static int Matrix_set_z(PyObject *op, PyObject *arg, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  if (impl->size < 3) {
    PyErr_SetNone(PyExc_IndexError);
    return -1;
  }

  if (!unwrap_double(arg, impl->data + 2)) {
    return -1;
  }

  return 0;
}

static PyObject *Matrix_get_w(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  if (impl->size < 4) {
    PyErr_SetNone(PyExc_IndexError);
    return NULL;
  }

  return PyFloat_FromDouble(impl->data[3]);
}

static int Matrix_set_w(PyObject *op, PyObject *arg, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  if (impl->size < 4) {
    PyErr_SetNone(PyExc_IndexError);
    return -1;
  }

  if (!unwrap_double(arg, impl->data + 3)) {
    return -1;
  }

  return 0;
}

static PyObject *Matrix_get_acquired(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (impl_check_acquired(impl, false)) {
    Py_RETURN_TRUE;
  }

  Py_RETURN_FALSE;
}

static PyObject *Matrix_get_shape(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  return Py_BuildValue("(nn)", (Py_ssize_t)impl->rows,
                       (Py_ssize_t)impl->columns);
}

static PyGetSetDef Matrix_getset[] = {
    {"acquired", (getter)Matrix_get_acquired, NULL, NULL, NULL},
    {"rows", (getter)Matrix_get_rows, NULL, NULL, NULL},
    {"columns", (getter)Matrix_get_columns, NULL, NULL, NULL},
    {"T", (getter)Matrix_get_T, NULL, NULL, NULL},
    {"x", (getter)Matrix_get_x, (setter)Matrix_set_x, NULL, NULL},
    {"y", (getter)Matrix_get_y, (setter)Matrix_set_y, NULL, NULL},
    {"z", (getter)Matrix_get_z, (setter)Matrix_set_z, NULL, NULL},
    {"w", (getter)Matrix_get_w, (setter)Matrix_set_w, NULL, NULL},
    {"shape", (getter)Matrix_get_shape, NULL, NULL, NULL},
    {NULL} /* Sentinel */
};

inline enum BinaryOps swap_right(enum BinaryOps op) {
  switch (op) {
  case Subtract:
    return RSubtract;

  case Divide:
    return RDivide;

  default:
    return op;
  }
}

static int Matrix_binary_op(PyObject *lhs_op, PyObject *rhs_op,
                            PyObject **out_op, enum BinaryOps op,
                            bool inplace) {
  double scalar;
  PyObject *mat_op = NULL;
  int result = 0;
  matrix_impl *lhs = NULL;
  matrix_impl *rhs = NULL;
  if (unwrap_double(lhs_op, &scalar)) {
    mat_op = rhs_op;
    op = swap_right(op);
  } else if (unwrap_double(rhs_op, &scalar)) {
    mat_op = lhs_op;
  }

  if (mat_op != NULL) {
    // scalar operation
    lhs = unwrap_matrix(mat_op, false);
    if (lhs == NULL) {
      goto error;
    }

    matrix_impl *out = set_output(mat_op, out_op, inplace);
    if (out == NULL) {
      goto error;
    }

    impl_scalar_binary(lhs, scalar, out, op);
    goto exit;
  }

  lhs = unwrap_matrix(lhs_op, false);
  if (lhs == NULL) {
    goto error;
  }

  rhs = unwrap_matrix(rhs_op, false);
  if (rhs == NULL) {
    goto error;
  }

  const char *mismatch_error = "Dimension mismatch between operands";

  if (lhs->rows != rhs->rows) {
    if (lhs->columns != rhs->columns) {
      PyErr_SetString(PyExc_NotImplementedError, mismatch_error);
      goto error;
    }

    // row-wise
    matrix_impl *matrix;
    matrix_impl *vector;
    if (lhs->rows == 1) {
      op = swap_right(op);
      mat_op = rhs_op;
      matrix = rhs;
      vector = lhs;
    } else if (rhs->rows == 1) {
      mat_op = lhs_op;
      matrix = lhs;
      vector = rhs;
    } else {
      PyErr_SetString(PyExc_NotImplementedError, mismatch_error);
      goto error;
    }

    matrix_impl *out = set_output(mat_op, out_op, inplace);
    if (out == NULL) {
      goto error;
    }

    impl_rowwise_binary(matrix, vector, out, op);
    goto exit;
  }

  if (lhs->columns != rhs->columns) {
    // column-wise
    matrix_impl *matrix;
    matrix_impl *vector;
    if (lhs->columns == 1) {
      op = swap_right(op);
      mat_op = rhs_op;
      matrix = rhs;
      vector = lhs;
    } else if (rhs->columns == 1) {
      mat_op = lhs_op;
      matrix = lhs;
      vector = rhs;
    } else {
      PyErr_SetString(PyExc_NotImplementedError, mismatch_error);
      goto error;
    }

    matrix_impl *out = set_output(mat_op, out_op, inplace);
    if (out == NULL) {
      goto error;
    }

    impl_columnwise_binary(matrix, vector, out, op);
    goto exit;
  }

  // element-wise
  matrix_impl *out = set_output(lhs_op, out_op, inplace);
  if (out == NULL) {
    goto error;
  }

  impl_ewise_binary(lhs, rhs, out, op);
  goto exit;

error:
  result = -1;

exit:
  IMPL_DECREF(lhs);
  IMPL_DECREF(rhs);
  return result;
}

#define MATRIX_BINARY_OP(binary)                                               \
  static PyObject *Matrix_##binary##_op(PyObject *lhs, PyObject *rhs) {        \
    PyObject *out = NULL;                                                      \
    if (Matrix_binary_op(lhs, rhs, &out, binary, false) < 0) {                 \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

#define MATRIX_INPLACE_BINARY_OP(binary)                                       \
  static PyObject *Matrix_inplace_##binary##_op(PyObject *lhs,                 \
                                                PyObject *rhs) {               \
    PyObject *out = NULL;                                                      \
    if (Matrix_binary_op(lhs, rhs, &out, binary, true) < 0) {                  \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

#define MATRIX_UNARY_OP(unary)                                                 \
  static PyObject *Matrix_##unary##_op(PyObject *op) {                         \
    PyObject *out = NULL;                                                      \
    if (Matrix_unary(op, &out, unary) < 0) {                                   \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

MATRIX_BINARY_OP(Add)
MATRIX_BINARY_OP(Subtract)
MATRIX_BINARY_OP(Multiply)
MATRIX_BINARY_OP(Divide)
MATRIX_INPLACE_BINARY_OP(Add)
MATRIX_INPLACE_BINARY_OP(Subtract)
MATRIX_INPLACE_BINARY_OP(Multiply)
MATRIX_INPLACE_BINARY_OP(Divide)
MATRIX_UNARY_OP(Abs)
MATRIX_UNARY_OP(Negate)

static PyObject *Matrix_matmul(PyObject *lhs_op, PyObject *rhs_op) {
  matrix_impl *lhs = NULL;
  matrix_impl *rhs = NULL;
  matrix_impl *out = NULL;
  PyObject *out_op = NULL;

  lhs = unwrap_matrix(lhs_op, false);
  if (lhs == NULL) {
    goto error;
  }

  rhs = unwrap_matrix(rhs_op, false);
  if (rhs == NULL) {
    goto error;
  }

  if (lhs->columns != rhs->rows) {
    PyErr_SetString(PyExc_NotImplementedError, "M0xN0 @ M1xN1  N0 != M1");
    goto error;
  }

  out = impl_new(lhs->rows, rhs->columns);
  if (out == NULL) {
    goto error;
  }

  impl_matmul(lhs, rhs, out);
  out_op = wrap_matrix(Py_TYPE(lhs_op), out);
  if (out_op == NULL) {
    goto error;
  }

  goto exit;

error:
  if (out != NULL) {
    impl_free(out);
  }

exit:
  IMPL_DECREF(lhs);
  IMPL_DECREF(rhs);
  return out_op;
}

Py_ssize_t Matrix_length(PyObject *op) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  return (Py_ssize_t)self->impl->rows;
}

static int read_key_ranges(PyObject *key, range *rows, range *columns,
                           matrix_impl *impl) {
  rows->start = 0;
  rows->stop = (Py_ssize_t)impl->rows;
  rows->step = 1;
  rows->count = impl->rows;
  columns->start = 0;
  columns->stop = (Py_ssize_t)impl->columns;
  columns->step = 1;
  columns->count = impl->columns;
  if (PyTuple_Check(key)) {
    if (PyTuple_GET_SIZE(key) != 2) {
      PyErr_SetString(
          PyExc_KeyError,
          "Index must be one dimension (row) or two dimensions (row, column)");
      return -1;
    }

    if (range_read(rows, PyTuple_GET_ITEM(key, 0), rows->stop) < 0) {
      return -1;
    }

    if (range_read(columns, PyTuple_GET_ITEM(key, 1), columns->stop) < 0) {
      return -1;
    }
  } else if (range_read(rows, key, rows->stop) < 0) {
    return -1;
  }

  return 0;
}

PyObject *Matrix_subscript(PyObject *op, PyObject *key) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  range rows;
  range columns;
  if (read_key_ranges(key, &rows, &columns, impl) < 0) {
    return NULL;
  }

  if (rows.count == 1 && columns.count == 1) {
    return PyFloat_FromDouble(impl->row_ptrs[rows.start][columns.start]);
  }

  PyTypeObject *type = Py_TYPE(op);
  MatrixObject *out = (MatrixObject *)type->tp_alloc(type, 0);
  if (out == NULL) {
    return NULL;
  }

  out->impl = impl_new(rows.count, columns.count);
  IMPL_INCREF(out->impl);
  impl_get(impl, &rows, &columns, out->impl);
  return (PyObject *)out;
}

static PyObject *Matrix_item(PyObject *op, Py_ssize_t index) {
  MatrixObject *matrix = (MatrixObject *)op;
  matrix_impl *impl = matrix->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  if (index < 0 || (size_t)index >= impl->rows) {
    PyErr_SetNone(PyExc_IndexError);
    return NULL;
  }

  if (impl->columns == 1) {
    return PyFloat_FromDouble(impl->data[index]);
  }

  range rows = {index, index + 1, 1, 1};
  range columns = {0, (Py_ssize_t)impl->columns, 1, impl->columns};

  PyTypeObject *type = Py_TYPE(op);
  MatrixObject *out = (MatrixObject *)type->tp_alloc(type, 0);
  if (out == NULL) {
    return NULL;
  }

  out->impl = impl_new(rows.count, columns.count);
  IMPL_INCREF(out->impl);
  impl_get(impl, &rows, &columns, out->impl);
  return (PyObject *)out;
}

static PyObject *Matrix_iter(PyObject *op) { return PySeqIter_New(op); }

int Matrix_ass_subscript(PyObject *op, PyObject *key, PyObject *value_op) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  range rows;
  range columns;
  if (read_key_ranges(key, &rows, &columns, impl) < 0) {
    return -1;
  }

  double scalar = 0;
  matrix_impl *value = NULL;
  if (PyLong_Check(value_op)) {
    scalar = PyLong_AsDouble(value_op);
  } else if (PyFloat_Check(value_op)) {
    scalar = PyFloat_AsDouble(value_op);
  } else {
    value = unwrap_matrix(value_op, false);
    if (value == NULL) {
      PyErr_SetString(PyExc_TypeError, "Invalid value");
      return -1;
    }
  }

  if (value == NULL) {
    impl_set_scalar(impl, &rows, &columns, scalar);
    return 0;
  }

  if (rows.count > 1 && value->rows == 1) {
    impl_set_rowwise(impl, &rows, &columns, value);
    IMPL_DECREF(value);
    return 0;
  }

  if (columns.count > 1 && value->columns == 1) {
    impl_set_columnwise(impl, &rows, &columns, value);
    IMPL_DECREF(value);
    return 0;
  }

  impl_set(impl, &rows, &columns, value);
  IMPL_DECREF(value);
  return 0;
}

#define VALUE_BUFFER_SIZE 32

static PyObject *Matrix_str(PyObject *op) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, false)) {
    return PyUnicode_FromString("<not acquired>");
  }

  char buffer[VALUE_BUFFER_SIZE];
  char **values = (char **)PyMem_Calloc(impl->size, sizeof(char *));
  double *ptr = impl->data;

  size_t max_length = 0;
  for (size_t i = 0; i < impl->size; ++i, ++ptr) {
    snprintf(buffer, VALUE_BUFFER_SIZE, "%g", *ptr);
    size_t length = strlen(buffer);
    values[i] = PyMem_Malloc(length + 1);
    memcpy(values[i], buffer, length);
    values[i][length] = 0;
    if (length > max_length) {
      max_length = length;
    }
  }

  size_t scan = impl->columns * (max_length + 1) - 1;
  size_t padding = 4;
  size_t cstr_size = impl->rows * (scan + padding);
  char *cstr = (char *)PyMem_Malloc(cstr_size + 1);

  memset(cstr, (int)' ', cstr_size);
  cstr[cstr_size] = 0;

  char **values_ptr = values;
  char *cstr_ptr = cstr;

  for (size_t r = 0; r < impl->rows; ++r, cstr_ptr += (scan + padding)) {
    if (r == 0) {
      cstr_ptr[0] = '[';
    } else {
      cstr_ptr[0] = ' ';
    }
    cstr_ptr[1] = '[';

    for (size_t c = 0, start = 2; c < impl->columns;
         ++c, ++values_ptr, start += max_length + 1) {
      char *value = *values_ptr;
      size_t length = strlen(value);
      size_t val_start = start + max_length - length;
      memcpy(cstr_ptr + val_start, value, length);
      PyMem_Free(value);
    }

    cstr_ptr[scan + 2] = ']';
    if (r == impl->rows - 1) {
      cstr_ptr[scan + 3] = ']';
    } else {
      cstr_ptr[scan + 3] = '\n';
    }
  }

  PyMem_Free(values);
  PyObject *str = PyUnicode_FromStringAndSize(cstr, cstr_size);
  PyMem_Free(cstr);
  return str;
}

static PyObject *Matrix_repr(PyObject *op) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, false)) {
    return PyUnicode_FromString("Matrix(<not acquired>)");
  }

  char buffer[VALUE_BUFFER_SIZE];
  double *ptr = impl->data;

  const char *prefix = "Matrix(";
  size_t length = strlen(prefix);

  snprintf(buffer, VALUE_BUFFER_SIZE, "%zu", impl->rows);
  length += strlen(buffer) + 2; // ", "

  snprintf(buffer, VALUE_BUFFER_SIZE, "%zu", impl->columns);
  length += strlen(buffer) + 3; // ", ["

  for (size_t i = 0; i < impl->size; ++i, ++ptr) {
    snprintf(buffer, VALUE_BUFFER_SIZE, "%g", *ptr);
    length += strlen(buffer) + 2;
  }

  char *cstr = (char *)PyMem_Malloc(length + 1);
  memset(cstr, (int)' ', length);
  cstr[length] = 0;
  char *cstr_ptr = cstr;

  length = strlen(prefix);
  memcpy(cstr_ptr, prefix, length);
  cstr_ptr += length;

  snprintf(buffer, VALUE_BUFFER_SIZE, "%zu", impl->rows);
  length = strlen(buffer);
  memcpy(cstr_ptr, buffer, length);
  cstr_ptr += length;
  cstr_ptr[0] = ',';
  cstr_ptr[1] = ' ';
  cstr_ptr += 2;

  snprintf(buffer, VALUE_BUFFER_SIZE, "%zu", impl->columns);
  length = strlen(buffer);
  memcpy(cstr_ptr, buffer, length);
  cstr_ptr += length;
  cstr_ptr[0] = ',';
  cstr_ptr[1] = ' ';
  cstr_ptr[2] = '[';
  cstr_ptr += 3;

  ptr = impl->data;
  for (size_t i = 0; i < impl->size; ++i, ++ptr) {
    snprintf(buffer, VALUE_BUFFER_SIZE, "%g", *ptr);
    length = strlen(buffer);
    memcpy(cstr_ptr, buffer, length);
    cstr_ptr += length;
    if (i == impl->size - 1) {
      cstr_ptr[0] = ']';
      cstr_ptr[1] = ')';
    } else {
      cstr_ptr[0] = ',';
      cstr_ptr[1] = ' ';
    }

    cstr_ptr += 2;
  }

  PyObject *str = PyUnicode_FromString(cstr);
  PyMem_Free(cstr);
  if (str == NULL) {
    return NULL;
  }

  return str;
}

static PyType_Slot Matrix_slots[] = {
    {Py_tp_doc, "Matrix(rows, columns, values=None)\n--\n\n"
                "A dense 2-D matrix of double-precision floats."},
    {Py_tp_new, Matrix_new},
    {Py_tp_init, Matrix_init},
    {Py_tp_dealloc, Matrix_dealloc},
    {Py_tp_str, Matrix_str},
    {Py_tp_repr, Matrix_repr},
    {Py_tp_iter, Matrix_iter},
    {Py_nb_add, Matrix_Add_op},
    {Py_nb_inplace_add, Matrix_inplace_Add_op},
    {Py_nb_subtract, Matrix_Subtract_op},
    {Py_nb_inplace_subtract, Matrix_inplace_Subtract_op},
    {Py_nb_multiply, Matrix_Multiply_op},
    {Py_nb_inplace_multiply, Matrix_inplace_Multiply_op},
    {Py_nb_true_divide, Matrix_Divide_op},
    {Py_nb_inplace_true_divide, Matrix_inplace_Divide_op},
    {Py_nb_matrix_multiply, Matrix_matmul},
    {Py_nb_absolute, Matrix_Abs_op},
    {Py_nb_negative, Matrix_Negate_op},
    {Py_mp_length, Matrix_length},
    {Py_mp_ass_subscript, Matrix_ass_subscript},
    {Py_mp_subscript, Matrix_subscript},
    {Py_sq_length, Matrix_length},
    {Py_sq_item, Matrix_item},
    {Py_tp_methods, Matrix_methods},
    {Py_tp_getset, Matrix_getset},
    {0, NULL} /* Sentinel */
};

static PyType_Spec Matrix_Spec = {.name = "bocpy._math.Matrix",
                                  .basicsize = sizeof(MatrixObject),
                                  .itemsize = 0,
                                  .flags = Py_TPFLAGS_DEFAULT |
                                           Py_TPFLAGS_IMMUTABLETYPE,
                                  .slots = Matrix_slots};

const PY_INT64_T NO_OWNER = -2;

/// @brief Wraps a matrix sent from another interpreter.
/// @details The underlying C matrix, when it arrives at another interpreter, is
/// wrapped by this method in a MatrixObject so that it can be used from that
/// code running in that interpreter.
/// @param xidata The xidata containing the C matrix
/// @return a new MatrixObject reference, or NULL on error
static PyObject *_new_matrix_object(XIDATA_T *xidata) {
  matrix_impl *impl = (matrix_impl *)xidata->data;

  // take ownership of the C matrix
  int_least64_t expected = NO_OWNER;
  int_least64_t desired = get_interpid();
  if (!atomic_compare_exchange_strong(&impl->owner, &expected, desired)) {
    PyErr_Format(PyExc_RuntimeError,
                 "%" PRIdLEAST64
                 " cannot acquire cown (already acquired by %" PRIdLEAST64 ")",
                 desired, expected);
    return NULL;
  }

  // Create an instance of MatrixObject using this interpreter's copy of the
  // type
  PyTypeObject *type = LOCAL_STATE->matrix_type;
  MatrixObject *matrix = (MatrixObject *)type->tp_alloc(type, 0);
  if (matrix == NULL) {
    // attempt to roll back the ownership change
    int_least64_t rollback_expected = desired;
    desired = NO_OWNER;
    atomic_compare_exchange_strong(&impl->owner, &rollback_expected, desired);
    return NULL;
  }

  // wrap the C matrix
  matrix->impl = impl;
  IMPL_INCREF(impl);

  return (PyObject *)matrix;
}

/// @brief Prepare the underlying C matrix for sharing with another interpreter.
/// @param tstate The thread state of the current interpreter (> 3.11)
/// @param obj The MatrixObject instance
/// @param xidata An empty xidata package
/// @return 0 if successful, < o on error
static int _matrix_shared(
#ifndef BOC_NO_MULTIGIL
    PyThreadState *tstate,
#endif
    PyObject *obj, XIDATA_T *xidata) {
#ifdef BOC_NO_MULTIGIL
  PyThreadState *tstate = PyThreadState_GET();
#endif

  MatrixObject *matrix = (MatrixObject *)obj;
  matrix_impl *impl = matrix->impl;

  // put the underlying C matrix in an ownerless state during transport
  int_least64_t expected = get_interpid();
  int_least64_t desired = NO_OWNER;
  if (!atomic_compare_exchange_strong(&impl->owner, &expected, desired)) {
    PyErr_Format(PyExc_RuntimeError,
                 "%" PRIdLEAST64
                 " cannot release matrix (acquired by %" PRIdLEAST64 ")",
                 get_interpid(), expected);
    return -1;
  }

  // initialize the xidata
  XIDATA_INIT(xidata, tstate->interp, impl, obj, _new_matrix_object);
  return 0;
}

static PyMethodDef _math_module_methods[] = {
    {NULL} /* Sentinel */
};

static int _math_module_exec(PyObject *module) {
  _math_module_state *state = (_math_module_state *)PyModule_GetState(module);

  state->matrix_type =
      (PyTypeObject *)PyType_FromModuleAndSpec(module, &Matrix_Spec, NULL);
  if (state->matrix_type == NULL) {
    return -1;
  }

  if (PyModule_AddType(module, state->matrix_type) < 0) {
    return -1;
  }

  // let the XIData system know that the matrix type can be shared
  if (XIDATA_REGISTERCLASS(state->matrix_type, _matrix_shared)) {
    Py_FatalError(
        "could not register MatrixObject for cross-interpreter sharing");
    return -1;
  }

  assert(LOCAL_STATE == NULL);
  LOCAL_STATE = state;

  return 0;
}

static int _math_module_clear(PyObject *module) {
  _math_module_state *state = (_math_module_state *)PyModule_GetState(module);
  Py_CLEAR(state->matrix_type);
  return 0;
}

static void _math_module_free(void *module) {
  _math_module_clear((PyObject *)module);
}

static int _math_module_traverse(PyObject *module, visitproc visit, void *arg) {
  _math_module_state *state = (_math_module_state *)PyModule_GetState(module);
  Py_VISIT(state->matrix_type);
  return 0;
}

#ifdef Py_mod_exec
static PyModuleDef_Slot _math_module_slots[] = {
    {Py_mod_exec, (void *)_math_module_exec},
#if PY_VERSION_HEX >= 0x030C0000
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
#endif
#if PY_VERSION_HEX >= 0x030D0000
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
#endif
    {0, NULL},
};
#endif

static PyModuleDef _math_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_math",
    .m_doc = "Provides BOC-enabled linear algebra functions and storage",
    .m_methods = _math_module_methods,
    .m_free = (freefunc)_math_module_free,
    .m_traverse = _math_module_traverse,
    .m_clear = _math_module_clear,
#ifdef Py_mod_exec
    .m_slots = _math_module_slots,
#endif
    .m_size = sizeof(_math_module_state)};

PyMODINIT_FUNC PyInit__math(void) {
#ifdef Py_mod_exec
  return PyModuleDef_Init(&_math_module);
#else
  PyObject *module;
  module = PyModule_Create(&_math_module);
  if (module == NULL)
    return NULL;

  if (_math_exec(module) != 0) {
    Py_DECREF(module);
    return NULL;
  }

  return module;
#endif
}