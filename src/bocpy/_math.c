#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <assert.h>
#include <float.h>
#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <bocpy/bocpy.h>

#ifndef _WIN32
#include <math.h>
#endif

/* Inlining barrier for asm-capture builds only. Default-off so the
   release wheel and the bench-baseline binary are byte-equivalent to
   today's optimisation profile. Define BOC_CANARY_NOINLINE_ON at
   compile time to capture per-helper disassembly.

   Note: a canary-noinline build shows kernel shape but NOT release
   call overhead — release ``-O3`` inlines these helpers into the
   public ``Matrix_*`` methods, eliminating call overhead and enabling
   cross-call IPO. Wall-clock measurements should always be taken
   against a wheel built without this macro; canary builds over-report
   per-call cost. */
#ifdef BOC_CANARY_NOINLINE_ON
#define BOC_CANARY_NOINLINE __attribute__((noinline))
#else
#define BOC_CANARY_NOINLINE
#endif

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
  atomic_store(&matrix->owner, bocpy_interpid());
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

/* --------------------------------------------------------------------------
   Binary family X-macro template.

   Purpose: stamp one tight per-op helper for each (op x broadcast pattern)
   pair so the inner loop is a single straight-line expression visible to
   the autovectoriser. Replaces the per-iteration switch through binary_op.

   Add an op: append one X(ENUM, STAMP, EXPR) row to BOC_BINARY_OPS and
   add the matching value to enum BinaryOps. The four dispatchers below
   are stamped from the same table.
     ENUM = matching value in enum BinaryOps.
     STAMP = lowercase identifier used in the generated symbol names.
     EXPR = right-hand-side expression assigned to the output cell each
            iteration; emitted as `*out_ptr = (EXPR);`. Reflected operators
            (RSubtract, RDivide) encode the swap in EXPR itself, so the
            per-shape stamping stays uniform across all six rows.

   Stamped symbol names: impl_<STAMP>_ewise, impl_<STAMP>_rowwise,
   impl_<STAMP>_columnwise, impl_<STAMP>_scalar.

   Names in scope inside EXPR: `lhs` (left operand element), `rhs` (right
   operand element). For the scalar shape, `rhs` is bound to the scalar
   argument; for the rowwise / columnwise broadcast shapes, `rhs` is the
   broadcast vector element.

   See `.github/skills/commenting-c-and-python/SKILL.md` for the
   "Exception: X-macro descriptor tables" convention.
   -------------------------------------------------------------------------- */
#define BOC_BINARY_OPS(X)                                                      \
  /*  enum        stamp       expr */                                          \
  X(Add, add, lhs + rhs)                                                       \
  X(Subtract, subtract, lhs - rhs)                                             \
  X(RSubtract, rsubtract, rhs - lhs)                                           \
  X(Multiply, multiply, lhs *rhs)                                              \
  X(Divide, divide, lhs / rhs)                                                 \
  X(RDivide, rdivide, rhs / lhs)

#define DEFINE_BINARY_EWISE(ENUM, STAMP, EXPR)                                 \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##STAMP##_ewise(matrix_impl *lhs_m, matrix_impl *rhs_m,     \
                                   matrix_impl *out) {                         \
    assert(lhs_m->rows == out->rows && lhs_m->columns == out->columns);        \
    assert(lhs_m->rows == rhs_m->rows && lhs_m->columns == rhs_m->columns);    \
    const double *lhs_ptr = lhs_m->data;                                       \
    const double *rhs_ptr = rhs_m->data;                                       \
    double *out_ptr = out->data;                                               \
    for (size_t i = 0; i < lhs_m->size;                                        \
         ++i, ++lhs_ptr, ++rhs_ptr, ++out_ptr) {                               \
      const double lhs = *lhs_ptr;                                             \
      const double rhs = *rhs_ptr;                                             \
      *out_ptr = (EXPR);                                                       \
    }                                                                          \
  }

#define DEFINE_BINARY_ROWWISE(ENUM, STAMP, EXPR)                               \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##STAMP##_rowwise(matrix_impl *matrix, matrix_impl *vector, \
                                     matrix_impl *out) {                       \
    const size_t M = matrix->rows;                                             \
    const size_t N = matrix->columns;                                          \
    assert(M == out->rows && N == out->columns);                               \
    assert(N == vector->columns && vector->rows == 1);                         \
    const double *lhs_ptr = matrix->data;                                      \
    double *out_ptr = out->data;                                               \
    for (size_t r = 0; r < M; ++r) {                                           \
      const double *rhs_ptr = vector->data;                                    \
      for (size_t c = 0; c < N; ++c, ++lhs_ptr, ++rhs_ptr, ++out_ptr) {        \
        const double lhs = *lhs_ptr;                                           \
        const double rhs = *rhs_ptr;                                           \
        *out_ptr = (EXPR);                                                     \
      }                                                                        \
    }                                                                          \
  }

#define DEFINE_BINARY_COLUMNWISE(ENUM, STAMP, EXPR)                            \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##STAMP##_columnwise(                                       \
      matrix_impl *matrix, matrix_impl *vector, matrix_impl *out) {            \
    const size_t M = matrix->rows;                                             \
    const size_t N = matrix->columns;                                          \
    assert(M == out->rows && N == out->columns);                               \
    assert(M == vector->rows && vector->columns == 1);                         \
    const double *lhs_ptr = matrix->data;                                      \
    const double *rhs_ptr = vector->data;                                      \
    double *out_ptr = out->data;                                               \
    for (size_t r = 0; r < M; ++r, ++rhs_ptr) {                                \
      const double rhs = *rhs_ptr;                                             \
      for (size_t c = 0; c < N; ++c, ++lhs_ptr, ++out_ptr) {                   \
        const double lhs = *lhs_ptr;                                           \
        *out_ptr = (EXPR);                                                     \
      }                                                                        \
    }                                                                          \
  }

#define DEFINE_BINARY_SCALAR(ENUM, STAMP, EXPR)                                \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##STAMP##_scalar(matrix_impl *matrix, double rhs,           \
                                    matrix_impl *out) {                        \
    assert(matrix->rows == out->rows && matrix->columns == out->columns);      \
    const double *lhs_ptr = matrix->data;                                      \
    double *out_ptr = out->data;                                               \
    for (size_t i = 0; i < matrix->size; ++i, ++lhs_ptr, ++out_ptr) {          \
      const double lhs = *lhs_ptr;                                             \
      *out_ptr = (EXPR);                                                       \
    }                                                                          \
  }

#define X(E, S, EX) DEFINE_BINARY_EWISE(E, S, EX)
BOC_BINARY_OPS(X)
#undef X

#define X(E, S, EX) DEFINE_BINARY_ROWWISE(E, S, EX)
BOC_BINARY_OPS(X)
#undef X

#define X(E, S, EX) DEFINE_BINARY_COLUMNWISE(E, S, EX)
BOC_BINARY_OPS(X)
#undef X

#define X(E, S, EX) DEFINE_BINARY_SCALAR(E, S, EX)
BOC_BINARY_OPS(X)
#undef X

static void dispatch_bin_ewise(matrix_impl *lhs, matrix_impl *rhs,
                               matrix_impl *out, enum BinaryOps op) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    impl_##STAMP##_ewise(lhs, rhs, out);                                       \
    return;
    BOC_BINARY_OPS(X)
#undef X
  default:
    fprintf(stderr, "Unknown binary op\n");
  }
}

static void dispatch_bin_rowwise(matrix_impl *matrix, matrix_impl *vector,
                                 matrix_impl *out, enum BinaryOps op) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    impl_##STAMP##_rowwise(matrix, vector, out);                               \
    return;
    BOC_BINARY_OPS(X)
#undef X
  default:
    fprintf(stderr, "Unknown binary op\n");
  }
}

static void dispatch_bin_columnwise(matrix_impl *matrix, matrix_impl *vector,
                                    matrix_impl *out, enum BinaryOps op) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    impl_##STAMP##_columnwise(matrix, vector, out);                            \
    return;
    BOC_BINARY_OPS(X)
#undef X
  default:
    fprintf(stderr, "Unknown binary op\n");
  }
}

static void dispatch_bin_scalar(matrix_impl *matrix, double scalar,
                                matrix_impl *out, enum BinaryOps op) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    impl_##STAMP##_scalar(matrix, scalar, out);                                \
    return;
    BOC_BINARY_OPS(X)
#undef X
  default:
    fprintf(stderr, "Unknown binary op\n");
  }
}

enum AggregateOps {
  Sum = 2000,
  Mean = 2001,
  Magnitude = 2002,
  Maximum = 2003,
  Minimum = 2004,
  MagnitudeSquared = 2005
};

/* --------------------------------------------------------------------------
   Aggregate family X-macro template.

   Purpose: stamp one tight per-op helper for each (op x shape) pair so the
   inner loop is a single straight-line accumulator visible to the
   autovectoriser. Replaces the per-iteration switch through aggregate_op.

   Add an op: append one X(ENUM, STAMP, INIT, STEP, MERGE, FINAL, LANES)
   row to BOC_AGG_OPS and add the matching value to enum AggregateOps.
   The dispatchers below are stamped from the same table.
     ENUM  = matching value in enum AggregateOps.
     STAMP = lowercase identifier used in the generated symbol names.
     INIT  = initial value of the accumulator (double expression).
     STEP  = right-hand-side expression assigned to the accumulator each
             iteration; emitted as `agg = (STEP);`.
     MERGE = right-hand-side expression that combines two partial
             accumulator values into one; emitted as
             `acc[0] = (MERGE);` in the LANES=4 horizontal merge.
             For ops where STEP already has the form
             `combine(agg, value)`, MERGE == STEP; for ops where STEP
             also transforms `value` (e.g. Magnitude's `agg + value*value`)
             MERGE must drop the transform (`agg + value`) because each
             lane already holds a partial reduction. Ignored when LANES=1
             and by columnwise stamping.
     FINAL = right-hand-side expression that produces the published result
             from the accumulator after the loop.
     LANES = number of parallel accumulators for ewise + rowwise stamping.
             Must be a literal `1` or `4`. LANES=4 splits the reduction
             into 4 independent dep chains so the OoO engine pipelines
             through the minsd/addsd 4-cycle latency. LANES=1 reuses the
             original single-accumulator form (bit-identical codegen to a
             single accumulator) for ops where parallel accumulation
             would change semantics. Columnwise stamping ignores LANES
             (the inner loop already walks distinct output cells).

   Stamped symbol names: impl_<STAMP>_ewise, impl_<STAMP>_rowwise,
   impl_<STAMP>_columnwise.

   Names in scope inside STEP: `agg` (the accumulator), `value` (the
   current matrix element).
   Names in scope inside FINAL: `agg`, `cnt` (the element count over the
   reduction axis: matrix size for ewise, columns for rowwise, rows for
   columnwise).

   Empty-axis contract: when cnt == 0, Mean returns 0 rather than NaN.
   The FINAL expression for Mean handles this guard inline.

   See `.github/skills/commenting-c-and-python/SKILL.md` for the
   "Exception: X-macro descriptor tables" convention.
   -------------------------------------------------------------------------- */
#define BOC_AGG_OPS(X)                                                         \
  X(Sum, sum, 0.0, agg + value, agg + value, agg, 4)                           \
  X(Mean, mean, 0.0, agg + value, agg + value,                                 \
    (cnt > 0 ? agg / (double)cnt : agg), 4)                                    \
  X(Magnitude, magnitude, 0.0, agg + value * value, agg + value, sqrt(agg), 4) \
  X(MagnitudeSquared, magnitude_squared, 0.0, agg + value * value,             \
    agg + value, agg, 4)                                                       \
  X(Minimum, minimum, DBL_MAX, (agg < value ? agg : value),                    \
    (agg < value ? agg : value), agg, 4)                                       \
  X(Maximum, maximum, -DBL_MAX, (agg > value ? agg : value),                   \
    (agg > value ? agg : value), agg, 4)

/* Codegen guard: every row in BOC_AGG_OPS must keep LANES=4. A typo
   walking it back to 1 would silently regress the parallel-accumulator
   unroll speedup, and the bench is manual-only (not wired into CI). */
#define X(E, S, I, ST, MG, F, L)                                               \
  static_assert((L) == 4,                                                      \
                "BOC_AGG_OPS row " #E " must use LANES=4 to keep parallel-"    \
                "accumulator unrolling");
BOC_AGG_OPS(X)
#undef X

/* Parallel-accumulator helpers used by LANES=4 stamping. Each AGG_LANE_STEP
   invocation evaluates STEP against a single fixed lane K with private
   `agg` and `value` locals, so the four lanes inside AGG_UNROLL_4 are four
   independent dep chains the OoO engine can pipeline. The do-while-0 gives
   each lane a fresh scope so name shadowing works without name leaks. */
#define AGG_LANE_STEP(STEP, acc, value_expr, K)                                \
  do {                                                                         \
    const double value = (value_expr);                                         \
    double agg = (acc)[K];                                                     \
    (acc)[K] = (STEP);                                                         \
  } while (0)

#define AGG_UNROLL_4(STEP, acc, sp, i)                                         \
  do {                                                                         \
    AGG_LANE_STEP(STEP, acc, (sp)[(i) + 0], 0);                                \
    AGG_LANE_STEP(STEP, acc, (sp)[(i) + 1], 1);                                \
    AGG_LANE_STEP(STEP, acc, (sp)[(i) + 2], 2);                                \
    AGG_LANE_STEP(STEP, acc, (sp)[(i) + 3], 3);                                \
  } while (0)

#define DEFINE_AGG_EWISE_1(ENUM, STAMP, INIT, STEP, MERGE, FINAL)              \
  BOC_CANARY_NOINLINE                                                          \
  static double impl_##STAMP##_ewise(const matrix_impl *m) {                   \
    double agg = (INIT);                                                       \
    const double *sp = m->data;                                                \
    const size_t cnt = m->size;                                                \
    for (size_t i = 0; i < cnt; ++i, ++sp) {                                   \
      const double value = *sp;                                                \
      agg = (STEP);                                                            \
    }                                                                          \
    return (FINAL);                                                            \
  }

#define DEFINE_AGG_EWISE_4(ENUM, STAMP, INIT, STEP, MERGE, FINAL)              \
  BOC_CANARY_NOINLINE                                                          \
  static double impl_##STAMP##_ewise(const matrix_impl *m) {                   \
    double acc[4] = {(INIT), (INIT), (INIT), (INIT)};                          \
    const double *sp = m->data;                                                \
    const size_t cnt = m->size;                                                \
    const size_t main_end = cnt - (cnt & 3u);                                  \
    for (size_t i = 0; i < main_end; i += 4) {                                 \
      AGG_UNROLL_4(STEP, acc, sp, i);                                          \
    }                                                                          \
    for (size_t i = main_end; i < cnt; ++i) {                                  \
      AGG_LANE_STEP(STEP, acc, sp[i], 0);                                      \
    }                                                                          \
    if (main_end > 0) {                                                        \
      for (size_t k = 1; k < 4; ++k) {                                         \
        const double value = acc[k];                                           \
        double agg = acc[0];                                                   \
        acc[0] = (MERGE);                                                      \
      }                                                                        \
    }                                                                          \
    double agg = acc[0];                                                       \
    return (FINAL);                                                            \
  }

#define DEFINE_AGG_EWISE(ENUM, STAMP, INIT, STEP, MERGE, FINAL, LANES)         \
  DEFINE_AGG_EWISE_##LANES(ENUM, STAMP, INIT, STEP, MERGE, FINAL)

#define DEFINE_AGG_ROWWISE_1(ENUM, STAMP, INIT, STEP, MERGE, FINAL)            \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##STAMP##_rowwise(const matrix_impl *m, matrix_impl *vec) { \
    const size_t M = m->rows;                                                  \
    const size_t cnt = m->columns;                                             \
    assert(vec->rows == M && vec->columns == 1);                               \
    const double *mp = m->data;                                                \
    double *vp = vec->data;                                                    \
    for (size_t r = 0; r < M; ++r, ++vp) {                                     \
      double agg = (INIT);                                                     \
      for (size_t i = 0; i < cnt; ++i, ++mp) {                                 \
        const double value = *mp;                                              \
        agg = (STEP);                                                          \
      }                                                                        \
      *vp = (FINAL);                                                           \
    }                                                                          \
  }

#define DEFINE_AGG_ROWWISE_4(ENUM, STAMP, INIT, STEP, MERGE, FINAL)            \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##STAMP##_rowwise(const matrix_impl *m, matrix_impl *vec) { \
    const size_t M = m->rows;                                                  \
    const size_t cnt = m->columns;                                             \
    assert(vec->rows == M && vec->columns == 1);                               \
    const double *mp = m->data;                                                \
    double *vp = vec->data;                                                    \
    const size_t main_end = cnt - (cnt & 3u);                                  \
    for (size_t r = 0; r < M; ++r, ++vp, mp += cnt) {                          \
      double acc[4] = {(INIT), (INIT), (INIT), (INIT)};                        \
      for (size_t i = 0; i < main_end; i += 4) {                               \
        AGG_UNROLL_4(STEP, acc, mp, i);                                        \
      }                                                                        \
      for (size_t i = main_end; i < cnt; ++i) {                                \
        AGG_LANE_STEP(STEP, acc, mp[i], 0);                                    \
      }                                                                        \
      if (main_end > 0) {                                                      \
        for (size_t k = 1; k < 4; ++k) {                                       \
          const double value = acc[k];                                         \
          double agg = acc[0];                                                 \
          acc[0] = (MERGE);                                                    \
        }                                                                      \
      }                                                                        \
      double agg = acc[0];                                                     \
      *vp = (FINAL);                                                           \
    }                                                                          \
  }

#define DEFINE_AGG_ROWWISE(ENUM, STAMP, INIT, STEP, MERGE, FINAL, LANES)       \
  DEFINE_AGG_ROWWISE_##LANES(ENUM, STAMP, INIT, STEP, MERGE, FINAL)

/* Columnwise: the inner accumulator is the output vector slot itself.
   INIT is applied with one explicit pass over the output vector before
   the main loop, so Min/Max (which need +/-DBL_MAX, not 0) work
   correctly. FINAL is applied in a separate second-pass loop over the
   output vector after all rows have been consumed (mirrors today's sqrt
   pass for Magnitude). MERGE and LANES are ignored: the inner loop
   already walks distinct output cells so each column has its own
   independent dep chain across rows. */
#define DEFINE_AGG_COLUMNWISE(ENUM, STAMP, INIT, STEP, MERGE, FINAL, LANES)    \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##STAMP##_columnwise(const matrix_impl *m,                  \
                                        matrix_impl *vec) {                    \
    const size_t cnt = m->rows;                                                \
    const size_t N = m->columns;                                               \
    assert(vec->columns == N && vec->rows == 1);                               \
    const double *mp = m->data;                                                \
    {                                                                          \
      double *vp = vec->data;                                                  \
      for (size_t c = 0; c < N; ++c, ++vp) {                                   \
        *vp = (INIT);                                                          \
      }                                                                        \
    }                                                                          \
    for (size_t r = 0; r < cnt; ++r) {                                         \
      double *vp = vec->data;                                                  \
      for (size_t c = 0; c < N; ++c, ++mp, ++vp) {                             \
        const double value = *mp;                                              \
        double agg = *vp;                                                      \
        *vp = (STEP);                                                          \
      }                                                                        \
    }                                                                          \
    double *vp = vec->data;                                                    \
    for (size_t c = 0; c < N; ++c, ++vp) {                                     \
      double agg = *vp;                                                        \
      *vp = (FINAL);                                                           \
    }                                                                          \
  }

#define X(E, S, I, ST, MG, F, L) DEFINE_AGG_EWISE(E, S, I, ST, MG, F, L)
BOC_AGG_OPS(X)
#undef X

#define X(E, S, I, ST, MG, F, L) DEFINE_AGG_ROWWISE(E, S, I, ST, MG, F, L)
BOC_AGG_OPS(X)
#undef X

#define X(E, S, I, ST, MG, F, L) DEFINE_AGG_COLUMNWISE(E, S, I, ST, MG, F, L)
BOC_AGG_OPS(X)
#undef X

static double dispatch_agg_ewise(matrix_impl *m, enum AggregateOps op) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    return impl_##STAMP##_ewise(m);
    BOC_AGG_OPS(X)
#undef X
  default:
    fprintf(stderr, "Unknown aggregate op\n");
    return nan("");
  }
}

static void dispatch_agg_rowwise(matrix_impl *m, enum AggregateOps op,
                                 matrix_impl *vec) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    impl_##STAMP##_rowwise(m, vec);                                            \
    return;
    BOC_AGG_OPS(X)
#undef X
  default:
    fprintf(stderr, "Unknown aggregate op\n");
  }
}

static void dispatch_agg_columnwise(matrix_impl *m, enum AggregateOps op,
                                    matrix_impl *vec) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    impl_##STAMP##_columnwise(m, vec);                                         \
    return;
    BOC_AGG_OPS(X)
#undef X
  default:
    fprintf(stderr, "Unknown aggregate op\n");
  }
}

/* --------------------------------------------------------------------------
   Unary family X-macro template.

   BOC_UNARY_OPS is the single source of truth for the unary op set. It is
   stamped in three places:
     1. impl_<STAMP>_ewise        (here)        — tight per-op kernel; the
                                                  inner loop sees the per-row
                                                  EXPR as a compile-time
                                                  constant, so the
                                                  autovectoriser succeeds.
     2. Matrix_<ENUM>_method      (later)       — Python METH_NOARGS wrapper.
     3. Matrix_<ENUM>_op          (number-protocol slots) — only Abs / Negate
                                                  have a slot, stamped
                                                  explicitly at those two
                                                  call sites.

   Unlike binary/aggregate, there is no runtime dispatcher: every Python
   entry point is statically bound to exactly one stamped kernel, because
   unary has no operand-shape routing to decide at call time.

   Add an op: append one X(ENUM, STAMP, EXPR) row to BOC_UNARY_OPS. The
   impl kernel and Matrix_<ENUM>_method wrapper are stamped automatically;
   if the op also needs a number-protocol slot, add one
   MATRIX_UNARY_OP(ENUM, STAMP) line near the bottom of the file.
     ENUM  = capitalised identifier used in symbol names
             (impl_<stamp_lowered>_ewise vs Matrix_<ENUM>_method).
     STAMP = lowercase identifier used in the impl symbol name.
     EXPR  = per-element expression in terms of `v` (the current source
             value) that yields the destination value.

   Names in scope inside EXPR: `v` (double, the current source value).

   Round uses `nearbyint` (round-half-to-even, IEEE 754 default — banker's
   rounding). Compiles to a single vectorisable `roundsd $0x04` on SSE4.1+;
   libm's `round()` (half away from zero) is scalar and ~5x slower.

   See `.github/skills/commenting-c-and-python/SKILL.md` for the
   "Exception: X-macro descriptor tables" convention.
   -------------------------------------------------------------------------- */
#define BOC_UNARY_OPS(X)                                                       \
  /*  enum    stamp   expr */                                                  \
  X(Ceil, ceil, ceil(v))                                                       \
  X(Floor, floor, floor(v))                                                    \
  X(Round, round, nearbyint(v))                                                \
  X(Negate, negate, -v)                                                        \
  X(Abs, abs, fabs(v))

#define DEFINE_UNARY(ENUM, STAMP, EXPR)                                        \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##STAMP##_ewise(const matrix_impl *m, matrix_impl *out) {   \
    assert(m->rows == out->rows && m->columns == out->columns);                \
    const double *sp = m->data;                                                \
    double *dp = out->data;                                                    \
    for (size_t i = 0; i < m->size; ++i, ++sp, ++dp) {                         \
      const double v = *sp;                                                    \
      *dp = (EXPR);                                                            \
    }                                                                          \
  }

#define X(E, S, EX) DEFINE_UNARY(E, S, EX)
BOC_UNARY_OPS(X)
#undef X

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
  PY_INT64_T current_id = bocpy_interpid();
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

static PyObject *Matrix_transpose(PyObject *op, PyObject *args,
                                  PyObject *kwds) {
  MatrixObject *matrix = (MatrixObject *)op;
  matrix_impl *impl = matrix->impl;

  int in_place = 0;
  static char *kwlist[] = {"in_place", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "|p", kwlist, &in_place)) {
    return NULL;
  }

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  if (in_place) {
    if (impl_transpose_in_place(impl) < 0) {
      return NULL;
    }
    return Py_NewRef(op);
  }

  matrix_impl *transpose = impl_transpose(impl);
  if (transpose == NULL) {
    return NULL;
  }

  return wrap_impl_or_free(transpose);
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

/// @brief Tri-state representation of an optional ``axis`` kwarg.
/// @details ``has_axis`` is true iff the caller passed a non-None axis;
///          ``axis`` is the validated int value (and is undefined when
///          ``has_axis`` is false). Replaces the historical NO_AXIS=-1000
///          sentinel which collided with the integer -1000.
typedef struct {
  bool has_axis;
  int axis;
} AxisArg;

/// @brief Decode an optional ``axis`` keyword argument into an AxisArg.
/// @details Accepts ``NULL`` or ``Py_None`` (no axis), an ``int`` in
///          ``INT_MIN..INT_MAX``. Rejects ``bool`` (subclass of int) and
///          overflows. Returns 0 on success and writes through ``*out``;
///          returns -1 with TypeError / OverflowError set on failure.
static int decode_axis_kwarg(PyObject *axis_obj, AxisArg *out) {
  if (axis_obj == NULL || axis_obj == Py_None) {
    out->has_axis = false;
    out->axis = 0;
    return 0;
  }
  if (PyBool_Check(axis_obj)) {
    PyErr_SetString(PyExc_TypeError, "axis must be an int or None, not bool");
    return -1;
  }
  if (!PyLong_Check(axis_obj)) {
    PyErr_SetString(PyExc_TypeError, "axis must be an int or None");
    return -1;
  }
  long ax = PyLong_AsLong(axis_obj);
  if (ax == -1 && PyErr_Occurred()) {
    return -1;
  }
  if (ax < INT_MIN || ax > INT_MAX) {
    PyErr_Format(PyExc_OverflowError, "axis %ld out of int range", ax);
    return -1;
  }
  out->has_axis = true;
  out->axis = (int)ax;
  return 0;
}

/// @brief Decode, validate (-2/-1/0/1) and normalise (-1->1, -2->0) axis.
/// @details Single entry point for the cross / perpendicular / angle /
///          normalize / aggregate methods that accept only the four
///          standard axis values. After this call, ``out->axis`` is
///          either 0 (column-wise) or 1 (row-wise) when ``has_axis`` is
///          true. Returns -1 with the appropriate exception set on any
///          decode or range error.
static int parse_validate_normalise_axis(PyObject *axis_obj, AxisArg *out) {
  if (decode_axis_kwarg(axis_obj, out) < 0) {
    return -1;
  }
  if (!out->has_axis) {
    return 0;
  }
  int ax = out->axis;
  if (ax != 0 && ax != 1 && ax != -1 && ax != -2) {
    PyErr_SetString(PyExc_NotImplementedError, "axis must be -2, -1, 0, or 1");
    return -1;
  }
  if (ax == -1) {
    out->axis = 1;
  } else if (ax == -2) {
    out->axis = 0;
  }
  return 0;
}

static int Matrix_aggregate(PyObject *matrix_op, AxisArg axis,
                            PyObject **out_op, enum AggregateOps agg) {
  MatrixObject *matrix = (MatrixObject *)matrix_op;
  matrix_impl *impl = matrix->impl;

  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  if (!axis.has_axis) {
    *out_op = PyFloat_FromDouble(dispatch_agg_ewise(impl, agg));
    return 0;
  }

  if (axis.axis == 0) {
    matrix_impl *vector = impl_new(1, impl->columns);
    if (vector == NULL) {
      return -1;
    }

    dispatch_agg_columnwise(impl, agg, vector);
    *out_op = wrap_matrix(Py_TYPE(matrix_op), vector);
    if (*out_op == NULL) {
      impl_free(vector);
      return -1;
    }

    return 0;
  }

  /* axis.axis == 1 (row-wise). parse_validate_normalise_axis already
     rejected anything else. */
  matrix_impl *vector = impl_new(impl->rows, 1);
  if (vector == NULL) {
    return -1;
  }

  dispatch_agg_rowwise(impl, agg, vector);
  *out_op = wrap_matrix(Py_TYPE(matrix_op), vector);
  if (*out_op == NULL) {
    impl_free(vector);
    return -1;
  }

  return 0;
}

// this macro provides a kind of template for all the aggregate methods to
// follow, as they are all identical with the exception of the operator

#define MATRIX_AGGREGATE(agg)                                                  \
  static PyObject *Matrix_##agg##_method(PyObject *op, PyObject *args,         \
                                         PyObject *kwds) {                     \
    PyObject *out = NULL;                                                      \
    PyObject *axis_obj = NULL;                                                 \
    static char *kwlist[] = {"axis", NULL};                                    \
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|O", kwlist, &axis_obj)) {   \
      return NULL;                                                             \
    }                                                                          \
    AxisArg axis;                                                              \
    if (parse_validate_normalise_axis(axis_obj, &axis) < 0) {                  \
      return NULL;                                                             \
    }                                                                          \
    if (Matrix_aggregate(op, axis, &out, agg) < 0) {                           \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

MATRIX_AGGREGATE(Sum)
MATRIX_AGGREGATE(Mean)
MATRIX_AGGREGATE(Magnitude)
MATRIX_AGGREGATE(Minimum)
MATRIX_AGGREGATE(Maximum)
MATRIX_AGGREGATE(MagnitudeSquared)

enum BroadcastShape { BCAST_NONE = 0, BCAST_ROW, BCAST_COL };

/* --------------------------------------------------------------------------
   Two-operand aggregate family X-macro template.

   BOC_2AGG_OPS is the single source of truth for the two-operand aggregate
   op set. It is stamped in three places:
     1. impl_<NAME>_total       — flat traversal returning a scalar.
     2. impl_<NAME>_rowwise     — per-row reduction writing Mx1 output.
     3. impl_<NAME>_columnwise  — per-column accumulation writing 1xN output.
                                  REQUIRES caller-zeroed output buffer
                                  (dispatcher uses impl_new -> PyMem_RawCalloc).

   Each walker carries the per-shape switch at the TOP of the body, with
   three specialised inner loops (NONE / ROW / COL). Moving the switch
   inside the inner loop would defeat contraction; the per-shape pointer
   arithmetic and broadcast behaviour are intentionally different.
   The dispatcher (Matrix_vecdot) canonicalises operands so the matrix is
   always the LHS and the vector is always the RHS before calling the
   helpers.

   Names in scope inside STEP:
     lhs (double)   — current left-hand-side value
     rhs (double)   — current right-hand-side value (in BCAST_COL: the
                       per-row scalar, hoisted out of the inner loop)
     agg (double)   — accumulator (for total/rowwise it is a local;
                       for columnwise the per-iteration cell of the output
                       vector is loaded into `agg`, STEP runs, then the
                       cell is written back)

   Add an op: append one X(NAME, INIT, STEP) row to BOC_2AGG_OPS and the
   three impl_<NAME>_* kernels are stamped automatically. Wire it into a
   Python entry point separately (mirrors Matrix_vecdot).

   See `.github/skills/commenting-c-and-python/SKILL.md` for the
   "Exception: X-macro descriptor tables" convention.
   -------------------------------------------------------------------------- */
#define BOC_2AGG_OPS(X)                                                        \
  /*  name      init   step (lhs, rhs in scope; agg accumulator) */            \
  X(vecdot, 0.0, agg += lhs * rhs)

#define DEFINE_2AGG_TOTAL(NAME, INIT, STEP)                                    \
  BOC_CANARY_NOINLINE                                                          \
  static double impl_##NAME##_total(const matrix_impl *lm,                     \
                                    const matrix_impl *rm,                     \
                                    enum BroadcastShape shape) {               \
    double agg = (INIT);                                                       \
    switch (shape) {                                                           \
    case BCAST_NONE: {                                                         \
      const double *lp = lm->data;                                             \
      const double *rp = rm->data;                                             \
      for (size_t i = 0; i < lm->size; ++i, ++lp, ++rp) {                      \
        const double lhs = *lp;                                                \
        const double rhs = *rp;                                                \
        STEP;                                                                  \
      }                                                                        \
      break;                                                                   \
    }                                                                          \
    case BCAST_ROW: {                                                          \
      const double *lp = lm->data;                                             \
      const size_t M = lm->rows;                                               \
      const size_t N = lm->columns;                                            \
      for (size_t r = 0; r < M; ++r) {                                         \
        const double *rp = rm->data;                                           \
        for (size_t c = 0; c < N; ++c, ++lp, ++rp) {                           \
          const double lhs = *lp;                                              \
          const double rhs = *rp;                                              \
          STEP;                                                                \
        }                                                                      \
      }                                                                        \
      break;                                                                   \
    }                                                                          \
    case BCAST_COL: {                                                          \
      const double *lp = lm->data;                                             \
      const double *rp = rm->data;                                             \
      const size_t M = lm->rows;                                               \
      const size_t N = lm->columns;                                            \
      for (size_t r = 0; r < M; ++r) {                                         \
        const double rhs = *rp++;                                              \
        for (size_t c = 0; c < N; ++c, ++lp) {                                 \
          const double lhs = *lp;                                              \
          STEP;                                                                \
        }                                                                      \
      }                                                                        \
      break;                                                                   \
    }                                                                          \
    }                                                                          \
    return agg;                                                                \
  }

#define DEFINE_2AGG_ROWWISE(NAME, INIT, STEP)                                  \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##NAME##_rowwise(                                           \
      const matrix_impl *lm, const matrix_impl *rm, matrix_impl *out_Mx1,      \
      enum BroadcastShape shape) {                                             \
    const size_t M = lm->rows;                                                 \
    const size_t N = lm->columns;                                              \
    double *out_ptr = out_Mx1->data;                                           \
    switch (shape) {                                                           \
    case BCAST_NONE: {                                                         \
      const double *lp = lm->data;                                             \
      const double *rp = rm->data;                                             \
      for (size_t r = 0; r < M; ++r, ++out_ptr) {                              \
        double agg = (INIT);                                                   \
        for (size_t c = 0; c < N; ++c, ++lp, ++rp) {                           \
          const double lhs = *lp;                                              \
          const double rhs = *rp;                                              \
          STEP;                                                                \
        }                                                                      \
        *out_ptr = agg;                                                        \
      }                                                                        \
      break;                                                                   \
    }                                                                          \
    case BCAST_ROW: {                                                          \
      const double *lp = lm->data;                                             \
      for (size_t r = 0; r < M; ++r, ++out_ptr) {                              \
        const double *rp = rm->data;                                           \
        double agg = (INIT);                                                   \
        for (size_t c = 0; c < N; ++c, ++lp, ++rp) {                           \
          const double lhs = *lp;                                              \
          const double rhs = *rp;                                              \
          STEP;                                                                \
        }                                                                      \
        *out_ptr = agg;                                                        \
      }                                                                        \
      break;                                                                   \
    }                                                                          \
    case BCAST_COL: {                                                          \
      const double *lp = lm->data;                                             \
      const double *rp = rm->data;                                             \
      for (size_t r = 0; r < M; ++r, ++out_ptr) {                              \
        const double rhs = *rp++;                                              \
        double agg = (INIT);                                                   \
        for (size_t c = 0; c < N; ++c, ++lp) {                                 \
          const double lhs = *lp;                                              \
          STEP;                                                                \
        }                                                                      \
        *out_ptr = agg;                                                        \
      }                                                                        \
      break;                                                                   \
    }                                                                          \
    }                                                                          \
  }

/* Columnwise: the per-output-cell accumulator IS the output vector slot
   itself. Loading `*out_ptr` into a local `agg`, running STEP, and
   writing back keeps STEP uniform across all three walkers; the compiler
   collapses the load/store pair to a single += against memory. The
   caller must hand in a zero-initialised buffer (impl_new -> calloc);
   a recycled non-zero buffer would silently produce wrong sums because
   STEP accumulates with `+=`. */
#define DEFINE_2AGG_COLUMNWISE(NAME, INIT, STEP)                               \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##NAME##_columnwise(                                        \
      const matrix_impl *lm, const matrix_impl *rm, matrix_impl *out_1xN,      \
      enum BroadcastShape shape) {                                             \
    const size_t M = lm->rows;                                                 \
    const size_t N = lm->columns;                                              \
    (void)(INIT);                                                              \
    switch (shape) {                                                           \
    case BCAST_NONE: {                                                         \
      const double *lp = lm->data;                                             \
      const double *rp = rm->data;                                             \
      for (size_t r = 0; r < M; ++r) {                                         \
        double *out_ptr = out_1xN->data;                                       \
        for (size_t c = 0; c < N; ++c, ++lp, ++rp, ++out_ptr) {                \
          const double lhs = *lp;                                              \
          const double rhs = *rp;                                              \
          double agg = *out_ptr;                                               \
          STEP;                                                                \
          *out_ptr = agg;                                                      \
        }                                                                      \
      }                                                                        \
      break;                                                                   \
    }                                                                          \
    case BCAST_ROW: {                                                          \
      const double *lp = lm->data;                                             \
      for (size_t r = 0; r < M; ++r) {                                         \
        const double *rp = rm->data;                                           \
        double *out_ptr = out_1xN->data;                                       \
        for (size_t c = 0; c < N; ++c, ++lp, ++rp, ++out_ptr) {                \
          const double lhs = *lp;                                              \
          const double rhs = *rp;                                              \
          double agg = *out_ptr;                                               \
          STEP;                                                                \
          *out_ptr = agg;                                                      \
        }                                                                      \
      }                                                                        \
      break;                                                                   \
    }                                                                          \
    case BCAST_COL: {                                                          \
      const double *lp = lm->data;                                             \
      const double *rp = rm->data;                                             \
      for (size_t r = 0; r < M; ++r) {                                         \
        const double rhs = *rp++;                                              \
        double *out_ptr = out_1xN->data;                                       \
        for (size_t c = 0; c < N; ++c, ++lp, ++out_ptr) {                      \
          const double lhs = *lp;                                              \
          double agg = *out_ptr;                                               \
          STEP;                                                                \
          *out_ptr = agg;                                                      \
        }                                                                      \
      }                                                                        \
      break;                                                                   \
    }                                                                          \
    }                                                                          \
  }

#define X(N, I, S) DEFINE_2AGG_TOTAL(N, I, S)
BOC_2AGG_OPS(X)
#undef X

#define X(N, I, S) DEFINE_2AGG_ROWWISE(N, I, S)
BOC_2AGG_OPS(X)
#undef X

#define X(N, I, S) DEFINE_2AGG_COLUMNWISE(N, I, S)
BOC_2AGG_OPS(X)
#undef X

/// @brief Axis-aware inner product: sum of element-wise products.
/// @details The canonicalisation swap rearranges the dispatch-argument
///          pointers ``mat_arg`` / ``vec_arg`` so the helpers always see
///          ``(matrix, vector)``. The refcounted ``rhs`` from
///          ``unwrap_matrix`` is preserved unchanged so the IMPL_DECREF at
///          ``done`` always matches the single INCREF. ``self->impl`` is
///          NOT refcount-paired here (mirrors Matrix_transpose).
static PyObject *Matrix_vecdot(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  PyObject *other = NULL;
  PyObject *axis = NULL;
  PyObject *result = NULL;
  matrix_impl *rhs = NULL;

  /* ``other`` is positional-only; ``axis`` accepts both forms. */
  static char *kwlist[] = {"", "axis", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "O|O", kwlist, &other, &axis)) {
    return NULL;
  }
  if (!impl_check_acquired(self->impl, true)) {
    return NULL;
  }

  rhs = unwrap_matrix(other, false);
  if (rhs == NULL) {
    goto done;
  }

  matrix_impl *lhs = self->impl;
  matrix_impl *mat_arg = lhs;
  matrix_impl *vec_arg = rhs;
  enum BroadcastShape shape;

  /* Shape classification — mirrors Matrix_binary_op's broadcast switch.
     Two call sites is not enough duplication to warrant a shared helper. */
  if (lhs->rows == rhs->rows && lhs->columns == rhs->columns) {
    shape = BCAST_NONE;
  } else if (lhs->rows == rhs->rows &&
             (lhs->columns == 1 || rhs->columns == 1)) {
    /* Column-vector broadcast. Canonicalise so the helper sees
       (matrix, vector). vecdot is commutative — no swap needed. */
    shape = BCAST_COL;
    if (lhs->columns == 1) {
      mat_arg = rhs;
      vec_arg = lhs;
    }
  } else if (lhs->columns == rhs->columns &&
             (lhs->rows == 1 || rhs->rows == 1)) {
    /* Row-vector broadcast — same canonicalisation as above. */
    shape = BCAST_ROW;
    if (lhs->rows == 1) {
      mat_arg = rhs;
      vec_arg = lhs;
    }
  } else if ((lhs->rows == 1 || lhs->columns == 1) &&
             (rhs->rows == 1 || rhs->columns == 1) && lhs->size == rhs->size) {
    /* Both vectors, possibly mixed orientation (1xN vs Nx1): walk the
       flat buffers in lockstep. No matrix/vector roles, so no swap. */
    shape = BCAST_NONE;
  } else {
    PyErr_Format(PyExc_NotImplementedError,
                 "vecdot: lhs %zux%zu incompatible with rhs %zux%zu", lhs->rows,
                 lhs->columns, rhs->rows, rhs->columns);
    goto done;
  }

  AxisArg axis_arg;
  if (parse_validate_normalise_axis(axis, &axis_arg) < 0) {
    goto done;
  }

  if (!axis_arg.has_axis) {
    result = PyFloat_FromDouble(impl_vecdot_total(mat_arg, vec_arg, shape));
  } else if (axis_arg.axis == 0) {
    matrix_impl *out = impl_new(1, mat_arg->columns);
    if (out != NULL) {
      impl_vecdot_columnwise(mat_arg, vec_arg, out, shape);
      result = wrap_impl_or_free(out);
    }
  } else {
    /* axis_arg.axis == 1 (row-wise). parse_validate_normalise_axis
       already rejected anything else. */
    matrix_impl *out = impl_new(mat_arg->rows, 1);
    if (out != NULL) {
      impl_vecdot_rowwise(mat_arg, vec_arg, out, shape);
      result = wrap_impl_or_free(out);
    }
  }

done:
  IMPL_DECREF(rhs);
  return result;
}

/// @brief Classify a matrix as a 2D/3D cross-product operand or batch.
/// @details ``has_axis`` / ``explicit_axis`` carry an optional caller-
///          supplied axis (already normalised to 0 or 1 by
///          ``parse_validate_normalise_axis``). For the doubly-valid
///          ``2x2`` / ``3x3`` shapes ``explicit_axis`` picks the
///          orientation (``0`` -> columns, default -> rows). For all
///          other shapes only one orientation is valid; supplying an
///          ``explicit_axis`` that contradicts that orientation returns
///          ``CROSS_INVALID`` so the caller raises rather than running
///          the wrong kernel. Returns ``CROSS_INVALID`` for any shape
///          that has no valid cross-product interpretation.
enum CrossAxis {
  CROSS_SCALAR_2D_1x2,
  CROSS_SCALAR_2D_2x1,
  CROSS_ROWS_2D_Nx2,
  CROSS_COLS_2D_2xN,
  CROSS_SCALAR_3D_1x3,
  CROSS_SCALAR_3D_3x1,
  CROSS_ROWS_3D_Nx3,
  CROSS_COLS_3D_3xN,
  CROSS_INVALID
};

static enum CrossAxis classify_cross_axis(const matrix_impl *impl,
                                          bool has_axis, int explicit_axis) {
  const size_t M = impl->rows;
  const size_t N = impl->columns;
  /* Ambiguous square shapes: axis picks orientation, default is rows. */
  if (M == 2 && N == 2) {
    return (has_axis && explicit_axis == 0) ? CROSS_COLS_2D_2xN
                                            : CROSS_ROWS_2D_Nx2;
  }
  if (M == 3 && N == 3) {
    return (has_axis && explicit_axis == 0) ? CROSS_COLS_3D_3xN
                                            : CROSS_ROWS_3D_Nx3;
  }
  /* Inherently row-oriented scalars: only axis=1 (or no axis) is valid. */
  if (M == 1 && N == 2) {
    if (has_axis && explicit_axis == 0) {
      return CROSS_INVALID;
    }
    return CROSS_SCALAR_2D_1x2;
  }
  if (M == 1 && N == 3) {
    if (has_axis && explicit_axis == 0) {
      return CROSS_INVALID;
    }
    return CROSS_SCALAR_3D_1x3;
  }
  /* Inherently column-oriented scalars: only axis=0 (or no axis) is valid. */
  if (M == 2 && N == 1) {
    if (has_axis && explicit_axis == 1) {
      return CROSS_INVALID;
    }
    return CROSS_SCALAR_2D_2x1;
  }
  if (M == 3 && N == 1) {
    if (has_axis && explicit_axis == 1) {
      return CROSS_INVALID;
    }
    return CROSS_SCALAR_3D_3x1;
  }
  /* Batch shapes with a unique orientation: explicit axis must match.
     (2x3 and 3x2 remain doubly-valid and fall through to the legacy
     default selection.) */
  if (N == 2 && M != 3) {
    if (has_axis && explicit_axis == 0) {
      return CROSS_INVALID;
    }
    return CROSS_ROWS_2D_Nx2;
  }
  if (M == 2 && N != 3) {
    if (has_axis && explicit_axis == 1) {
      return CROSS_INVALID;
    }
    return CROSS_COLS_2D_2xN;
  }
  if (N == 3 && M != 2) {
    if (has_axis && explicit_axis == 0) {
      return CROSS_INVALID;
    }
    return CROSS_ROWS_3D_Nx3;
  }
  if (M == 3 && N != 2) {
    if (has_axis && explicit_axis == 1) {
      return CROSS_INVALID;
    }
    return CROSS_COLS_3D_3xN;
  }
  /* Doubly-valid 2x3 / 3x2: legacy default (Nx2 / 2xN wins). */
  if (N == 2) {
    return CROSS_ROWS_2D_Nx2;
  }
  if (M == 2) {
    return CROSS_COLS_2D_2xN;
  }
  if (N == 3) {
    return CROSS_ROWS_3D_Nx3;
  }
  if (M == 3) {
    return CROSS_COLS_3D_3xN;
  }
  return CROSS_INVALID;
}

/// @brief 2D / 3D cross product against another vector or batch.
/// @details Five paths share one dispatcher. For 1x2 / 2x1 inputs the
///          result is the scalar z-component
///          ``self.x * other.y - self.y * other.x`` as a Python float;
///          for 1x3 / 3x1 the result is a same-shape Matrix preserving
///          ``self``'s orientation. For Nx2 / 2xN batches the result is
///          a per-vector scalar collected in a Mx1 (rows) or 1xN (cols)
///          Matrix; for Nx3 / 3xN batches the result is a same-shape
///          Matrix of per-vector cross products. The ``axis`` keyword
///          disambiguates 2x2 / 3x3 squares (default: rows; ``axis=0``
///          forces columns). For scalar inputs ``other``'s orientation
///          is irrelevant; for batch inputs ``other`` must have the same
///          shape as ``self``.
static PyObject *Matrix_cross(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  PyObject *other_op = NULL;
  PyObject *axis_obj = NULL;
  PyObject *result = NULL;
  matrix_impl *rhs = NULL;

  static char *kwlist[] = {"", "axis", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "O|O", kwlist, &other_op,
                                   &axis_obj)) {
    return NULL;
  }
  if (!impl_check_acquired(self->impl, true)) {
    return NULL;
  }

  AxisArg axis;
  if (parse_validate_normalise_axis(axis_obj, &axis) < 0) {
    return NULL;
  }

  rhs = unwrap_matrix(other_op, false);
  if (rhs == NULL) {
    goto done;
  }

  matrix_impl *lhs = self->impl;
  enum CrossAxis flavor = classify_cross_axis(lhs, axis.has_axis, axis.axis);
  if (flavor == CROSS_INVALID) {
    PyErr_SetString(
        PyExc_NotImplementedError,
        "cross requires a 2D or 3D vector or Nx2 or 2xN or Nx3 or 3xN matrix");
    goto done;
  }

  // Scalar inputs: other's orientation is irrelevant, only the flat
  // element count must match.
  if (flavor == CROSS_SCALAR_2D_1x2 || flavor == CROSS_SCALAR_2D_2x1) {
    if (rhs->size != 2) {
      PyErr_Format(PyExc_NotImplementedError,
                   "cross: 2D vector lhs %zux%zu incompatible with rhs %zux%zu",
                   lhs->rows, lhs->columns, rhs->rows, rhs->columns);
      goto done;
    }
    const double *a = lhs->data;
    const double *b = rhs->data;
    result = PyFloat_FromDouble(a[0] * b[1] - a[1] * b[0]);
    goto done;
  }
  if (flavor == CROSS_SCALAR_3D_1x3 || flavor == CROSS_SCALAR_3D_3x1) {
    if (rhs->size != 3) {
      PyErr_Format(PyExc_NotImplementedError,
                   "cross: 3D vector lhs %zux%zu incompatible with rhs %zux%zu",
                   lhs->rows, lhs->columns, rhs->rows, rhs->columns);
      goto done;
    }
    matrix_impl *out = impl_new(lhs->rows, lhs->columns);
    if (out == NULL) {
      goto done;
    }
    const double *a = lhs->data;
    const double *b = rhs->data;
    out->data[0] = a[1] * b[2] - a[2] * b[1];
    out->data[1] = a[2] * b[0] - a[0] * b[2];
    out->data[2] = a[0] * b[1] - a[1] * b[0];
    result = wrap_impl_or_free(out);
    goto done;
  }

  // Batch inputs accept either a same-shape batch or a single 2D/3D
  // vector (1xK / Kx1) broadcast against every per-vector slot. Cross is
  // anticommutative, so we deliberately do NOT silently swap operands;
  // ``self`` must be the batch. Per-branch validation below decides which
  // mode applies and reports the canonical error if neither fits.

  if (flavor == CROSS_ROWS_2D_Nx2) {
    const size_t N = lhs->rows;
    const bool same_shape =
        (lhs->rows == rhs->rows && lhs->columns == rhs->columns);
    const bool broadcast =
        (rhs->size == 2 && (rhs->rows == 1 || rhs->columns == 1));
    if (!same_shape && !broadcast) {
      PyErr_Format(PyExc_NotImplementedError,
                   "cross: Nx2 batch lhs %zux%zu incompatible with rhs %zux%zu",
                   lhs->rows, lhs->columns, rhs->rows, rhs->columns);
      goto done;
    }
    matrix_impl *out = impl_new(N, 1);
    if (out == NULL) {
      goto done;
    }
    const double *a = lhs->data;
    double *dst = out->data;
    if (same_shape) {
      const double *b = rhs->data;
      for (size_t i = 0; i < N; ++i) {
        double ax = *a++;
        double ay = *a++;
        double bx = *b++;
        double by = *b++;
        *dst++ = ax * by - ay * bx;
      }
    } else {
      const double bx = rhs->data[0];
      const double by = rhs->data[1];
      for (size_t i = 0; i < N; ++i) {
        double ax = *a++;
        double ay = *a++;
        *dst++ = ax * by - ay * bx;
      }
    }
    result = wrap_impl_or_free(out);
    goto done;
  }

  if (flavor == CROSS_COLS_2D_2xN) {
    const size_t N = lhs->columns;
    const bool same_shape =
        (lhs->rows == rhs->rows && lhs->columns == rhs->columns);
    const bool broadcast =
        (rhs->size == 2 && (rhs->rows == 1 || rhs->columns == 1));
    if (!same_shape && !broadcast) {
      PyErr_Format(PyExc_NotImplementedError,
                   "cross: 2xN batch lhs %zux%zu incompatible with rhs %zux%zu",
                   lhs->rows, lhs->columns, rhs->rows, rhs->columns);
      goto done;
    }
    matrix_impl *out = impl_new(1, N);
    if (out == NULL) {
      goto done;
    }
    const double *ax_row = lhs->data;
    const double *ay_row = lhs->data + N;
    double *dst = out->data;
    if (same_shape) {
      const double *bx_row = rhs->data;
      const double *by_row = rhs->data + N;
      for (size_t j = 0; j < N; ++j) {
        *dst++ = ax_row[j] * by_row[j] - ay_row[j] * bx_row[j];
      }
    } else {
      const double bx = rhs->data[0];
      const double by = rhs->data[1];
      for (size_t j = 0; j < N; ++j) {
        *dst++ = ax_row[j] * by - ay_row[j] * bx;
      }
    }
    result = wrap_impl_or_free(out);
    goto done;
  }

  if (flavor == CROSS_ROWS_3D_Nx3) {
    const size_t N = lhs->rows;
    const bool same_shape =
        (lhs->rows == rhs->rows && lhs->columns == rhs->columns);
    const bool broadcast =
        (rhs->size == 3 && (rhs->rows == 1 || rhs->columns == 1));
    if (!same_shape && !broadcast) {
      PyErr_Format(PyExc_NotImplementedError,
                   "cross: Nx3 batch lhs %zux%zu incompatible with rhs %zux%zu",
                   lhs->rows, lhs->columns, rhs->rows, rhs->columns);
      goto done;
    }
    matrix_impl *out = impl_new(N, 3);
    if (out == NULL) {
      goto done;
    }
    const double *a = lhs->data;
    double *dst = out->data;
    if (same_shape) {
      const double *b = rhs->data;
      for (size_t i = 0; i < N; ++i) {
        double ax = a[0], ay = a[1], az = a[2];
        double bx = b[0], by = b[1], bz = b[2];
        dst[0] = ay * bz - az * by;
        dst[1] = az * bx - ax * bz;
        dst[2] = ax * by - ay * bx;
        a += 3;
        b += 3;
        dst += 3;
      }
    } else {
      const double bx = rhs->data[0];
      const double by = rhs->data[1];
      const double bz = rhs->data[2];
      for (size_t i = 0; i < N; ++i) {
        double ax = a[0], ay = a[1], az = a[2];
        dst[0] = ay * bz - az * by;
        dst[1] = az * bx - ax * bz;
        dst[2] = ax * by - ay * bx;
        a += 3;
        dst += 3;
      }
    }
    result = wrap_impl_or_free(out);
    goto done;
  }

  if (flavor == CROSS_COLS_3D_3xN) {
    const size_t N = lhs->columns;
    const bool same_shape =
        (lhs->rows == rhs->rows && lhs->columns == rhs->columns);
    const bool broadcast =
        (rhs->size == 3 && (rhs->rows == 1 || rhs->columns == 1));
    if (!same_shape && !broadcast) {
      PyErr_Format(PyExc_NotImplementedError,
                   "cross: 3xN batch lhs %zux%zu incompatible with rhs %zux%zu",
                   lhs->rows, lhs->columns, rhs->rows, rhs->columns);
      goto done;
    }
    matrix_impl *out = impl_new(3, N);
    if (out == NULL) {
      goto done;
    }
    const double *ax_row = lhs->data;
    const double *ay_row = lhs->data + N;
    const double *az_row = lhs->data + 2 * N;
    double *dx_row = out->data;
    double *dy_row = out->data + N;
    double *dz_row = out->data + 2 * N;
    if (same_shape) {
      const double *bx_row = rhs->data;
      const double *by_row = rhs->data + N;
      const double *bz_row = rhs->data + 2 * N;
      for (size_t j = 0; j < N; ++j) {
        double ax = ax_row[j], ay = ay_row[j], az = az_row[j];
        double bx = bx_row[j], by = by_row[j], bz = bz_row[j];
        dx_row[j] = ay * bz - az * by;
        dy_row[j] = az * bx - ax * bz;
        dz_row[j] = ax * by - ay * bx;
      }
    } else {
      const double bx = rhs->data[0];
      const double by = rhs->data[1];
      const double bz = rhs->data[2];
      for (size_t j = 0; j < N; ++j) {
        double ax = ax_row[j], ay = ay_row[j], az = az_row[j];
        dx_row[j] = ay * bz - az * by;
        dy_row[j] = az * bx - ax * bz;
        dz_row[j] = ax * by - ay * bx;
      }
    }
    result = wrap_impl_or_free(out);
    goto done;
  }

  // Unreachable: every CrossAxis value is handled above.
  PyErr_SetString(PyExc_RuntimeError,
                  "internal: unhandled CrossAxis in Matrix_cross");

done:
  IMPL_DECREF(rhs);
  return result;
}

/// @brief Replace every zero entry in @p vector with 1.0.
/// @note Contract: @p vector must be a freshly-computed magnitude vector
///       produced by `Magnitude` aggregation. A zero entry therefore
///       implies the corresponding row or column of the dividend is
///       all-zeros, so the subsequent divide yields 0.0 / 1.0 = 0.0 —
///       i.e. the all-zero row/column is preserved instead of producing
///       NaN. Do not call this helper on user-supplied data.
static void sanitize_divisor(matrix_impl *vector) {
  double *ptr = vector->data;
  for (size_t i = 0; i < vector->size; ++i, ++ptr) {
    if (*ptr == 0.0) {
      *ptr = 1.0;
    }
  }
}

/// @brief Normalize @p impl into @p out along the given axis.
/// @details ``axis.has_axis == false`` divides every element by the matrix's
///          total magnitude; ``axis.axis == 0`` divides each column by its
///          own magnitude; ``axis.axis == 1`` divides each row by its own
///          magnitude. The all-zero input case is preserved (see
///          ``sanitize_divisor``). Self-aliasing is supported — pass
///          ``out == impl`` for in-place operation.
static int do_normalize(matrix_impl *impl, AxisArg axis, matrix_impl *out) {
  if (!axis.has_axis) {
    double m = dispatch_agg_ewise(impl, Magnitude);
    if (m == 0.0) {
      if (out != impl) {
        memcpy(out->data, impl->data, impl->size * sizeof(double));
      }
      return 0;
    }
    dispatch_bin_scalar(impl, m, out, Divide);
    return 0;
  }

  if (axis.axis == 0) {
    matrix_impl *divisor = impl_new(1, impl->columns);
    if (divisor == NULL) {
      return -1;
    }
    dispatch_agg_columnwise(impl, Magnitude, divisor);
    sanitize_divisor(divisor);
    dispatch_bin_rowwise(impl, divisor, out, Divide);
    impl_free(divisor);
    return 0;
  }

  /* axis.axis == 1 (row-wise). parse_validate_normalise_axis already
     rejected anything else. */
  matrix_impl *divisor = impl_new(impl->rows, 1);
  if (divisor == NULL) {
    return -1;
  }
  dispatch_agg_rowwise(impl, Magnitude, divisor);
  sanitize_divisor(divisor);
  dispatch_bin_columnwise(impl, divisor, out, Divide);
  impl_free(divisor);
  return 0;
}

static PyObject *Matrix_normalize(PyObject *op, PyObject *args,
                                  PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  PyObject *axis_obj = NULL;
  int in_place = 0;
  PyObject *out_op = NULL;

  static char *kwlist[] = {"axis", "in_place", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "|Op", kwlist, &axis_obj,
                                   &in_place)) {
    return NULL;
  }
  if (!impl_check_acquired(self->impl, true)) {
    return NULL;
  }

  AxisArg axis;
  if (parse_validate_normalise_axis(axis_obj, &axis) < 0) {
    return NULL;
  }

  matrix_impl *out = set_output(op, &out_op, in_place);
  if (out == NULL) {
    return NULL;
  }
  if (do_normalize(self->impl, axis, out) < 0) {
    Py_DECREF(out_op);
    return NULL;
  }
  return out_op;
}

enum Vec2Axis {
  VEC2_SCALAR_1x2,
  VEC2_SCALAR_2x1,
  VEC2_ROWS_Nx2,
  VEC2_COLS_2xN,
  VEC2_INVALID
};

/// @brief Classify a matrix as a 2D vector or batch of 2D vectors.
/// @details ``has_axis`` / ``explicit_axis`` carry an optional caller-
///          supplied axis (already normalised to 0 or 1). The ``2x2``
///          shape is doubly-valid and ``explicit_axis`` picks the
///          orientation (``0`` -> columns, default -> rows). For all
///          other shapes only one orientation is valid; supplying an
///          ``explicit_axis`` that contradicts that orientation returns
///          ``VEC2_INVALID``. Returns ``VEC2_INVALID`` for any shape
///          that is not a 2D vector or Nx2 / 2xN batch.
static enum Vec2Axis classify_vec2_axis(const matrix_impl *impl, bool has_axis,
                                        int explicit_axis) {
  const size_t M = impl->rows;
  const size_t N = impl->columns;
  if (M == 1 && N == 2) {
    if (has_axis && explicit_axis == 0) {
      return VEC2_INVALID;
    }
    return VEC2_SCALAR_1x2;
  }
  if (M == 2 && N == 1) {
    if (has_axis && explicit_axis == 1) {
      return VEC2_INVALID;
    }
    return VEC2_SCALAR_2x1;
  }
  if (M == 2 && N == 2) {
    return (has_axis && explicit_axis == 0) ? VEC2_COLS_2xN : VEC2_ROWS_Nx2;
  }
  if (N == 2) {
    if (has_axis && explicit_axis == 0) {
      return VEC2_INVALID;
    }
    return VEC2_ROWS_Nx2;
  }
  if (M == 2) {
    if (has_axis && explicit_axis == 1) {
      return VEC2_INVALID;
    }
    return VEC2_COLS_2xN;
  }
  return VEC2_INVALID;
}

/// @brief Fill @p out with the 2D perpendicular of every vector in @p impl.
/// @details Row-batch and ``1x2`` scalar share one pointer walk; column-
///          batch and ``2x1`` scalar share another. Self-aliasing is NOT
///          supported here \u2014 callers needing in-place must use the
///          dedicated in-place helper.
static void impl_perpendicular_out_of_place(const matrix_impl *impl,
                                            matrix_impl *out,
                                            enum Vec2Axis flavor) {
  const size_t M = impl->rows;
  const size_t N = impl->columns;
  if (flavor == VEC2_SCALAR_1x2 || flavor == VEC2_ROWS_Nx2) {
    const double *src = impl->data;
    double *dst = out->data;
    for (size_t r = 0; r < M; ++r) {
      const double sx = *src++;
      const double sy = *src++;
      *dst++ = -sy;
      *dst++ = sx;
    }
    return;
  }
  /* VEC2_SCALAR_2x1 or VEC2_COLS_2xN. */
  const double *src_x = impl->data;
  const double *src_y = impl->data + N;
  double *dst_x = out->data;
  double *dst_y = out->data + N;
  for (size_t c = 0; c < N; ++c, ++src_x, ++src_y, ++dst_x, ++dst_y) {
    *dst_x = -*src_y;
    *dst_y = *src_x;
  }
}

/// @brief In-place 2D perpendicular: swap each (x, y) pair to (-y, x).
static void impl_perpendicular_in_place(matrix_impl *impl,
                                        enum Vec2Axis flavor) {
  const size_t M = impl->rows;
  const size_t N = impl->columns;
  if (flavor == VEC2_SCALAR_1x2 || flavor == VEC2_ROWS_Nx2) {
    double *p = impl->data;
    for (size_t r = 0; r < M; ++r, p += 2) {
      const double temp = p[0];
      p[0] = -p[1];
      p[1] = temp;
    }
    return;
  }
  /* VEC2_SCALAR_2x1 or VEC2_COLS_2xN. */
  double *p = impl->data;
  for (size_t c = 0; c < N; ++c, ++p) {
    const double temp = p[0];
    p[0] = -p[N];
    p[N] = temp;
  }
}

static PyObject *Matrix_perpendicular(PyObject *op, PyObject *args,
                                      PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  PyObject *axis_obj = NULL;
  int in_place = 0;
  PyObject *out_op = NULL;

  static char *kwlist[] = {"axis", "in_place", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "|Op", kwlist, &axis_obj,
                                   &in_place)) {
    return NULL;
  }
  if (!impl_check_acquired(self->impl, true)) {
    return NULL;
  }

  AxisArg axis;
  if (parse_validate_normalise_axis(axis_obj, &axis) < 0) {
    return NULL;
  }

  enum Vec2Axis flavor =
      classify_vec2_axis(self->impl, axis.has_axis, axis.axis);
  if (flavor == VEC2_INVALID) {
    PyErr_SetString(PyExc_NotImplementedError,
                    "perpendicular requires a 2D vector or Nx2 or 2xN matrix");
    return NULL;
  }

  if (in_place) {
    impl_perpendicular_in_place(self->impl, flavor);
    return Py_NewRef(op);
  }

  matrix_impl *out = set_output(op, &out_op, false);
  if (out == NULL) {
    return NULL;
  }
  impl_perpendicular_out_of_place(self->impl, out, flavor);
  return out_op;
}

/// @brief Angle of every 2D vector in @p impl, computed via ``atan2``.
/// @details Returns a Python float for a single vector input, an ``M\xc3\x971``
///          column matrix for an ``Nx2`` row batch, or a ``1\xc3\x97N`` row
///          matrix for a ``2xN`` column batch. The ``2x2`` ambiguous shape
///          defaults to per-row; pass ``axis=0`` to force per-column.
static PyObject *Matrix_angle(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  PyObject *axis_obj = NULL;

  static char *kwlist[] = {"axis", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "|O", kwlist, &axis_obj)) {
    return NULL;
  }
  if (!impl_check_acquired(self->impl, true)) {
    return NULL;
  }

  AxisArg axis;
  if (parse_validate_normalise_axis(axis_obj, &axis) < 0) {
    return NULL;
  }

  enum Vec2Axis flavor =
      classify_vec2_axis(self->impl, axis.has_axis, axis.axis);
  if (flavor == VEC2_INVALID) {
    PyErr_SetString(PyExc_NotImplementedError,
                    "angle requires a 2D vector or Nx2 or 2xN matrix");
    return NULL;
  }

  matrix_impl *impl = self->impl;
  if (flavor == VEC2_SCALAR_1x2 || flavor == VEC2_SCALAR_2x1) {
    return PyFloat_FromDouble(atan2(impl->data[1], impl->data[0]));
  }

  if (flavor == VEC2_ROWS_Nx2) {
    const size_t M = impl->rows;
    matrix_impl *out = impl_new(M, 1);
    if (out == NULL) {
      return NULL;
    }
    const double *src = impl->data;
    double *dst = out->data;
    for (size_t r = 0; r < M; ++r) {
      *dst++ = atan2(src[1], src[0]);
      src += 2;
    }
    return wrap_impl_or_free(out);
  }

  /* VEC2_COLS_2xN. */
  const size_t N = impl->columns;
  matrix_impl *out = impl_new(1, N);
  if (out == NULL) {
    return NULL;
  }
  const double *xp = impl->data;
  const double *yp = impl->data + N;
  double *dst = out->data;
  for (size_t c = 0; c < N; ++c, ++xp, ++yp, ++dst) {
    *dst = atan2(*yp, *xp);
  }
  return wrap_impl_or_free(out);
}

/* MATRIX_UNARY_METHOD stamps a Python METH_VARARGS|METH_KEYWORDS wrapper
   that calls the per-op kernel impl_<STAMP>_ewise directly — no runtime
   dispatch. The ``in_place`` kwarg routes the output through
   ``set_output``: when true, the kernel aliases its input and output
   buffers and the method returns ``self`` (refcount-incremented); when
   false, a fresh matrix is allocated and returned. See the
   BOC_UNARY_OPS top-of-family block comment for the full template. */
#define MATRIX_UNARY_METHOD(ENUM, STAMP)                                       \
  static PyObject *Matrix_##ENUM##_method(PyObject *op, PyObject *args,        \
                                          PyObject *kwds) {                    \
    MatrixObject *self = (MatrixObject *)op;                                   \
    matrix_impl *impl = self->impl;                                            \
    int in_place = 0;                                                          \
    static char *kwlist[] = {"in_place", NULL};                                \
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|p", kwlist, &in_place)) {   \
      return NULL;                                                             \
    }                                                                          \
    if (!impl_check_acquired(impl, true)) {                                    \
      return NULL;                                                             \
    }                                                                          \
    PyObject *out_op = NULL;                                                   \
    matrix_impl *out = set_output(op, &out_op, in_place);                      \
    if (out == NULL) {                                                         \
      return NULL;                                                             \
    }                                                                          \
    impl_##STAMP##_ewise(impl, out);                                           \
    return out_op;                                                             \
  }

#define X(E, S, EX) MATRIX_UNARY_METHOD(E, S)
BOC_UNARY_OPS(X)
#undef X

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
    {"transpose", (PyCFunction)Matrix_transpose, METH_VARARGS | METH_KEYWORDS,
     "transpose($self, /, in_place=False)\n--\n\n"
     "Return a transposed copy, or transpose ``self`` in place when "
     "``in_place=True`` (in which case ``self`` is returned)."},
    {"sum", (PyCFunction)Matrix_Sum_method, METH_VARARGS | METH_KEYWORDS,
     "sum($self, /, axis=None)\n--\n\nSum of elements."},
    {"mean", (PyCFunction)Matrix_Mean_method, METH_VARARGS | METH_KEYWORDS,
     "mean($self, /, axis=None)\n--\n\nMean of elements."},
    {"magnitude", (PyCFunction)Matrix_Magnitude_method,
     METH_VARARGS | METH_KEYWORDS,
     "magnitude($self, /, axis=None)\n--\n\nEuclidean magnitude."},
    {"magnitude_squared", (PyCFunction)Matrix_MagnitudeSquared_method,
     METH_VARARGS | METH_KEYWORDS,
     "magnitude_squared($self, /, axis=None)\n--\n\n"
     "Sum of squared elements (Euclidean magnitude without the sqrt)."},
    {"vecdot", (PyCFunction)Matrix_vecdot, METH_VARARGS | METH_KEYWORDS,
     "vecdot($self, other, /, axis=None)\n--\n\n"
     "Axis-aware inner product: sum of element-wise products. "
     "Equivalent to numpy.linalg.vecdot for 1-D inputs with axis=None; "
     "**not** equivalent to numpy.dot."},
    {"cross", (PyCFunction)Matrix_cross, METH_VARARGS | METH_KEYWORDS,
     "cross($self, other, /, axis=None)\n--\n\n"
     "2D (scalar z-component) or 3D cross product against another "
     "vector or batch. 1x2 / 2x1 inputs return a float; 1x3 / 3x1 return "
     "a Matrix preserving self's orientation. Nx2 / 2xN row/column "
     "batches return per-vector scalars (Mx1 / 1xN); Nx3 / 3xN return "
     "same-shape batches. Batch operands accept either a same-shape "
     "other or a single 2D/3D vector (1xK / Kx1) broadcast against "
     "every per-vector slot \u2014 ``self`` must be the batch (cross is "
     "anticommutative). ``axis`` disambiguates the 2x2 / 3x3 squares "
     "(default rows, ``axis=0`` for columns)."},
    {"normalize", (PyCFunction)Matrix_normalize, METH_VARARGS | METH_KEYWORDS,
     "normalize($self, /, axis=None, in_place=False)\n--\n\n"
     "Divide elements by their magnitude. ``axis=None`` divides by the "
     "matrix's total magnitude; ``axis=0`` divides each column by its own "
     "magnitude; ``axis=1`` divides each row by its own magnitude. Rows or "
     "columns whose magnitude is zero are left as the all-zero vector. "
     "Sub-normal magnitudes may overflow during division; threshold with "
     "magnitude_squared() if safety matters. When ``in_place=True``, mutates "
     "``self`` and returns it."},
    {"perpendicular", (PyCFunction)Matrix_perpendicular,
     METH_VARARGS | METH_KEYWORDS,
     "perpendicular($self, /, axis=None, in_place=False)\n--\n\n"
     "Rotate every 2D vector 90 degrees counter-clockwise: ``(x, y) -> "
     "(-y, x)``. Accepts a single 2D vector (``1x2`` or ``2x1``), a row "
     "batch (``Nx2``), or a column batch (``2xN``). On the ambiguous "
     "``2x2`` shape the default is per-row; pass ``axis=0`` to force "
     "per-column. When ``in_place=True``, mutates ``self`` and returns it."},
    {"angle", (PyCFunction)Matrix_angle, METH_VARARGS | METH_KEYWORDS,
     "angle($self, /, axis=None)\n--\n\n"
     "Polar angle (``atan2(y, x)``) of every 2D vector. Returns a float "
     "for a single 2D vector, an ``Mx1`` column matrix for an ``Nx2`` row "
     "batch, or a ``1xN`` row matrix for a ``2xN`` column batch. On the "
     "ambiguous ``2x2`` shape the default is per-row; pass ``axis=0`` to "
     "force per-column."},
    {"min", (PyCFunction)Matrix_Minimum_method, METH_VARARGS | METH_KEYWORDS,
     "min($self, /, axis=None)\n--\n\nMinimum of elements."},
    {"max", (PyCFunction)Matrix_Maximum_method, METH_VARARGS | METH_KEYWORDS,
     "max($self, /, axis=None)\n--\n\nMaximum of elements."},
    {"ceil", (PyCFunction)Matrix_Ceil_method, METH_VARARGS | METH_KEYWORDS,
     "ceil($self, /, in_place=False)\n--\n\n"
     "Element-wise ceiling."},
    {"floor", (PyCFunction)Matrix_Floor_method, METH_VARARGS | METH_KEYWORDS,
     "floor($self, /, in_place=False)\n--\n\n"
     "Element-wise floor."},
    {"round", (PyCFunction)Matrix_Round_method, METH_VARARGS | METH_KEYWORDS,
     "round($self, /, in_place=False)\n--\n\n"
     "Element-wise rounding (banker's; IEEE round-half-to-even)."},
    {"negate", (PyCFunction)Matrix_Negate_method, METH_VARARGS | METH_KEYWORDS,
     "negate($self, /, in_place=False)\n--\n\n"
     "Element-wise negation."},
    {"abs", (PyCFunction)Matrix_Abs_method, METH_VARARGS | METH_KEYWORDS,
     "abs($self, /, in_place=False)\n--\n\n"
     "Element-wise absolute value."},
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

static PyObject *Matrix_get_size(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }
  return PyLong_FromSize_t(impl->size);
}

static PyObject *Matrix_get_T(PyObject *op, void *Py_UNUSED(dummy)) {
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

/// @brief Total Frobenius magnitude (read-only property).
/// @details Shortcut for ``magnitude()`` with no axis argument; same
///          underlying ``dispatch_agg_ewise(impl, Magnitude)`` call.
static PyObject *Matrix_get_length(PyObject *op, void *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }
  return PyFloat_FromDouble(dispatch_agg_ewise(impl, Magnitude));
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
    {"size", (getter)Matrix_get_size, NULL, NULL, NULL},
    {"T", (getter)Matrix_get_T, NULL, NULL, NULL},
    {"length", (getter)Matrix_get_length, NULL, NULL, NULL},
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

    dispatch_bin_scalar(lhs, scalar, out, op);
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

    dispatch_bin_rowwise(matrix, vector, out, op);
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

    dispatch_bin_columnwise(matrix, vector, out, op);
    goto exit;
  }

  // element-wise
  matrix_impl *out = set_output(lhs_op, out_op, inplace);
  if (out == NULL) {
    goto error;
  }

  dispatch_bin_ewise(lhs, rhs, out, op);
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

/* MATRIX_UNARY_OP stamps a Python number-protocol slot wrapper that calls
   the per-op kernel impl_<STAMP>_ewise directly. Only stamped for ops with
   a number-protocol slot (Py_nb_absolute, Py_nb_negative). */
#define MATRIX_UNARY_OP(ENUM, STAMP)                                           \
  static PyObject *Matrix_##ENUM##_op(PyObject *op) {                          \
    MatrixObject *self = (MatrixObject *)op;                                   \
    matrix_impl *impl = self->impl;                                            \
    if (!impl_check_acquired(impl, true)) {                                    \
      return NULL;                                                             \
    }                                                                          \
    PyObject *out_op = NULL;                                                   \
    matrix_impl *out = set_output(op, &out_op, false);                         \
    if (out == NULL) {                                                         \
      return NULL;                                                             \
    }                                                                          \
    impl_##STAMP##_ewise(impl, out);                                           \
    return out_op;                                                             \
  }

MATRIX_BINARY_OP(Add)
MATRIX_BINARY_OP(Subtract)
MATRIX_BINARY_OP(Multiply)
MATRIX_BINARY_OP(Divide)
MATRIX_INPLACE_BINARY_OP(Add)
MATRIX_INPLACE_BINARY_OP(Subtract)
MATRIX_INPLACE_BINARY_OP(Multiply)
MATRIX_INPLACE_BINARY_OP(Divide)
MATRIX_UNARY_OP(Abs, abs)
MATRIX_UNARY_OP(Negate, negate)

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

/// @brief Wraps a matrix sent from another interpreter.
/// @details The underlying C matrix, when it arrives at another interpreter, is
/// wrapped by this method in a MatrixObject so that it can be used from that
/// code running in that interpreter.
/// @param xidata The xidata containing the C matrix
/// @return a new MatrixObject reference, or NULL on error
static PyObject *_new_matrix_object(XIDATA_T *xidata) {
  matrix_impl *impl = (matrix_impl *)xidata->data;

  // take ownership of the C matrix
  int_least64_t expected = BOCPY_NO_OWNER;
  int_least64_t desired = bocpy_interpid();
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
    desired = BOCPY_NO_OWNER;
    atomic_compare_exchange_strong(&impl->owner, &rollback_expected, desired);
    return NULL;
  }

  // wrap the C matrix
  matrix->impl = impl;
  IMPL_INCREF(impl);

  return (PyObject *)matrix;
}

/// @brief Prepare the underlying C matrix for sharing with another interpreter.
/// @param tstate The thread state of the current interpreter
/// @param obj The MatrixObject instance
/// @param xidata An empty xidata package
/// @return 0 if successful, < 0 on error
XIDATA_GETDATA_FUNC(_matrix_shared) {
  MatrixObject *matrix = (MatrixObject *)obj;
  matrix_impl *impl = matrix->impl;

  // put the underlying C matrix in an ownerless state during transport
  int_least64_t expected = bocpy_interpid();
  int_least64_t desired = BOCPY_NO_OWNER;
  if (!atomic_compare_exchange_strong(&impl->owner, &expected, desired)) {
    PyErr_Format(PyExc_RuntimeError,
                 "%" PRIdLEAST64
                 " cannot release matrix (acquired by %" PRIdLEAST64 ")",
                 bocpy_interpid(), expected);
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