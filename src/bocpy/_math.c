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
#include <time.h>

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

/* Runtime CPU dispatch for the fma kernel (GNU function multiversioning).
   target_clones emits a hardware-FMA body plus a portable default body and
   binds the right one at load time via an STT_GNU_IFUNC resolver. This lets
   the shipped x86-64 wheel use vfmadd on capable CPUs while the SSE2
   baseline still gets a correct (libcall) body.

   Gated on __GLIBC__ deliberately: this is the ONLY platform where the
   mechanism is safe, confirmed by compiling the kernel inside the actual
   cibuildwheel default containers (2026-06-16):
     - manylinux2014 (glibc):  IFUNC resolves, correct result.
     - musllinux_1_2 (musl):   HARD COMPILE ERROR — "the call requires
                               'ifunc', which is not supported by this
                               target". An unguarded attribute would break
                               the entire musllinux wheel leg.
   macOS (Mach-O) and Windows (PE/COFF) also lack IFUNC; __GLIBC__ excludes
   all three (musl, macOS, Windows) in a single test. On glibc the <math.h>
   include above defines __GLIBC__ via <features.h>. Canary builds opt out
   so per-helper disassembly stays a single stable body. */
#if defined(__x86_64__) && defined(__GLIBC__) &&                               \
    (defined(__GNUC__) || defined(__clang__)) &&                               \
    !defined(BOC_CANARY_NOINLINE_ON)
#define BOC_FMA_MULTIVERSION __attribute__((target_clones("fma", "default")))
#else
#define BOC_FMA_MULTIVERSION
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

  // Guard the rows * columns product itself: a wrapped size_t would
  // under-allocate `data` below (the calloc byte-product check only sees
  // the already-truncated size), leaving a matrix that advertises more
  // cells than its backing store holds -> out-of-bounds access. Every
  // constructor (zeros/ones/full/normal/uniform, repeat_interleave, ...)
  // routes through here, so this single check covers them all.
  if (columns != 0 && rows > SIZE_MAX / columns) {
    PyErr_SetString(PyExc_OverflowError, "Matrix dimensions are too large");
    return NULL;
  }

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
    matrix->rows = N;
    matrix->columns = M;
    return update_row_ptrs(matrix);
  }

  if (M == N) {
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
  RDivide = 1005,
  Less = 1006,
  LessEqual = 1007,
  Greater = 1008,
  GreaterEqual = 1009,
  Equal = 1010,
  NotEqual = 1011
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
  X(RDivide, rdivide, rhs / lhs)                                               \
  X(Less, less, (double)(lhs < rhs))                                           \
  X(LessEqual, less_equal, (double)(lhs <= rhs))                               \
  X(Greater, greater, (double)(lhs > rhs))                                     \
  X(GreaterEqual, greater_equal, (double)(lhs >= rhs))                         \
  X(Equal, equal, (double)(lhs == rhs))                                        \
  X(NotEqual, not_equal, (double)(lhs != rhs))

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

#define DEFINE_BINARY_OUTER(ENUM, STAMP, EXPR)                                 \
  BOC_CANARY_NOINLINE                                                          \
  static void impl_##STAMP##_outer(matrix_impl *colvec, matrix_impl *rowvec,   \
                                   matrix_impl *out) {                         \
    const size_t M = out->rows;                                                \
    const size_t N = out->columns;                                             \
    assert(colvec->rows == M && colvec->columns == 1);                         \
    assert(rowvec->rows == 1 && rowvec->columns == N);                         \
    const double *col_ptr = colvec->data;                                      \
    double *out_ptr = out->data;                                               \
    for (size_t r = 0; r < M; ++r, ++col_ptr) {                                \
      const double lhs = *col_ptr;                                             \
      const double *row_ptr = rowvec->data;                                    \
      for (size_t c = 0; c < N; ++c, ++row_ptr, ++out_ptr) {                   \
        const double rhs = *row_ptr;                                           \
        *out_ptr = (EXPR);                                                     \
      }                                                                        \
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

#define X(E, S, EX) DEFINE_BINARY_OUTER(E, S, EX)
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

static void dispatch_bin_outer(matrix_impl *colvec, matrix_impl *rowvec,
                               matrix_impl *out, enum BinaryOps op) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    impl_##STAMP##_outer(colvec, rowvec, out);                                 \
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
   Masked aggregate kernels (the ``where=`` path).

   Stamped from BOC_AGG_OPS so a new aggregate automatically gains masked
   support. An excluded element (mask cell == 0.0; a NaN mask cell counts as
   included, matching where()'s truthiness) is replaced by the op's INIT,
   which is the neutral element for that op's STEP and so leaves the
   accumulator unchanged, and it is not counted toward ``cnt``. When a group
   has no included element the published value is BOC_AGG_MASKED_EMPTY_<ENUM>:
   the additive ops (Sum/Magnitude/MagnitudeSquared) collapse to 0 (their
   additive identity), while Mean and min/max yield NaN -- there is no element
   to average or choose, and NaN matches NumPy's empty-slice mean.

   These run only when a caller passes ``where=`` (the slow path), so they use
   a single accumulator rather than the LANES=4 unrolling of the unmasked
   kernels -- simplicity over SIMD. Rows/columns are addressed through
   row_ptrs so the mask and matrix stay aligned cell-for-cell.
   -------------------------------------------------------------------------- */
#define BOC_AGG_MASKED_EMPTY_Sum 0.0
#define BOC_AGG_MASKED_EMPTY_Mean NAN
#define BOC_AGG_MASKED_EMPTY_Magnitude 0.0
#define BOC_AGG_MASKED_EMPTY_MagnitudeSquared 0.0
#define BOC_AGG_MASKED_EMPTY_Minimum NAN
#define BOC_AGG_MASKED_EMPTY_Maximum NAN

#define DEFINE_AGG_MASKED_EWISE(ENUM, STAMP, INIT, STEP, MERGE, FINAL, LANES)  \
  static double impl_##STAMP##_masked_ewise(const matrix_impl *m,              \
                                            const matrix_impl *mask) {         \
    double agg = (INIT);                                                       \
    size_t cnt = 0;                                                            \
    const double *sp = m->data;                                                \
    const double *kp = mask->data;                                             \
    const size_t n = m->size;                                                  \
    for (size_t i = 0; i < n; ++i) {                                           \
      const int inc = (kp[i] != 0.0);                                          \
      const double value = inc ? sp[i] : (INIT);                               \
      cnt += (size_t)inc;                                                      \
      agg = (STEP);                                                            \
    }                                                                          \
    return (cnt > 0) ? (FINAL) : (BOC_AGG_MASKED_EMPTY_##ENUM);                \
  }

#define DEFINE_AGG_MASKED_ROWWISE(ENUM, STAMP, INIT, STEP, MERGE, FINAL,       \
                                  LANES)                                       \
  static void impl_##STAMP##_masked_rowwise(                                   \
      const matrix_impl *m, const matrix_impl *mask, matrix_impl *vec) {       \
    const size_t M = m->rows;                                                  \
    const size_t N = m->columns;                                               \
    assert(vec->rows == M && vec->columns == 1);                               \
    for (size_t r = 0; r < M; ++r) {                                           \
      const double *sp = m->row_ptrs[r];                                       \
      const double *kp = mask->row_ptrs[r];                                    \
      double agg = (INIT);                                                     \
      size_t cnt = 0;                                                          \
      for (size_t c = 0; c < N; ++c) {                                         \
        const int inc = (kp[c] != 0.0);                                        \
        const double value = inc ? sp[c] : (INIT);                             \
        cnt += (size_t)inc;                                                    \
        agg = (STEP);                                                          \
      }                                                                        \
      vec->data[r] = (cnt > 0) ? (FINAL) : (BOC_AGG_MASKED_EMPTY_##ENUM);      \
    }                                                                          \
  }

#define DEFINE_AGG_MASKED_COLUMNWISE(ENUM, STAMP, INIT, STEP, MERGE, FINAL,    \
                                     LANES)                                    \
  static void impl_##STAMP##_masked_columnwise(                                \
      const matrix_impl *m, const matrix_impl *mask, matrix_impl *vec) {       \
    const size_t M = m->rows;                                                  \
    const size_t N = m->columns;                                               \
    assert(vec->rows == 1 && vec->columns == N);                               \
    for (size_t c = 0; c < N; ++c) {                                           \
      double agg = (INIT);                                                     \
      size_t cnt = 0;                                                          \
      for (size_t r = 0; r < M; ++r) {                                         \
        const int inc = (mask->row_ptrs[r][c] != 0.0);                         \
        const double value = inc ? m->row_ptrs[r][c] : (INIT);                 \
        cnt += (size_t)inc;                                                    \
        agg = (STEP);                                                          \
      }                                                                        \
      vec->data[c] = (cnt > 0) ? (FINAL) : (BOC_AGG_MASKED_EMPTY_##ENUM);      \
    }                                                                          \
  }

#define X(E, S, I, ST, MG, F, L) DEFINE_AGG_MASKED_EWISE(E, S, I, ST, MG, F, L)
BOC_AGG_OPS(X)
#undef X

#define X(E, S, I, ST, MG, F, L)                                               \
  DEFINE_AGG_MASKED_ROWWISE(E, S, I, ST, MG, F, L)
BOC_AGG_OPS(X)
#undef X

#define X(E, S, I, ST, MG, F, L)                                               \
  DEFINE_AGG_MASKED_COLUMNWISE(E, S, I, ST, MG, F, L)
BOC_AGG_OPS(X)
#undef X

static double dispatch_agg_masked_ewise(matrix_impl *m, matrix_impl *mask,
                                        enum AggregateOps op) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    return impl_##STAMP##_masked_ewise(m, mask);
    BOC_AGG_OPS(X)
#undef X
  default:
    fprintf(stderr, "Unknown aggregate op\n");
    return nan("");
  }
}

static void dispatch_agg_masked_rowwise(matrix_impl *m, matrix_impl *mask,
                                        enum AggregateOps op,
                                        matrix_impl *vec) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    impl_##STAMP##_masked_rowwise(m, mask, vec);                               \
    return;
    BOC_AGG_OPS(X)
#undef X
  default:
    fprintf(stderr, "Unknown aggregate op\n");
  }
}

static void dispatch_agg_masked_columnwise(matrix_impl *m, matrix_impl *mask,
                                           enum AggregateOps op,
                                           matrix_impl *vec) {
  switch (op) {
#define X(ENUM, STAMP, ...)                                                    \
  case ENUM:                                                                   \
    impl_##STAMP##_masked_columnwise(m, mask, vec);                            \
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
  X(Abs, abs, fabs(v))                                                         \
  X(Sqrt, sqrt, sqrt(v))                                                       \
  X(Sign, sign, (double)((v > 0.0) - (v < 0.0)))                               \
  X(Cos, cos, cos(v))                                                          \
  X(Sin, sin, sin(v))

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

  // ikj (rank-1 update) order: the inner c-loop is a contiguous AXPY over
  // row-major out and rhs rows, with no loop-carried dependency across c, so
  // -O3 autovectorises it without -ffast-math. Products are still summed in
  // ascending-k order per (r, c), so results are bitwise identical to ijk.
  memset(out->data, 0, out->size * sizeof(double));
  for (size_t r = 0; r < M0; ++r) {
    double *out_row = out->row_ptrs[r];
    const double *lhs_row = lhs->row_ptrs[r];
    for (size_t k = 0; k < N0; ++k) {
      const double a = lhs_row[k];
      const double *rhs_row = rhs->row_ptrs[k];
      for (size_t c = 0; c < N1; ++c) {
        out_row[c] += a * rhs_row[c];
      }
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
  bool scalar; ///< True iff the key was a bare integer (collapses to a float).
} range;

/// @brief This processes the arguments to __get__ to produce the actual
/// requested range in the matrix.
int range_read(range *range, PyObject *key, size_t length) {
  Py_ssize_t start, stop, step;
  if (PyLong_Check(key)) {
    start = PyLong_AsSsize_t(key);
    if (start == -1 && PyErr_Occurred()) {
      return -1;
    }
    if (start < 0) {
      start += (Py_ssize_t)length;
    }
    stop = start + 1;
    step = 1;
    range->scalar = true;
  } else if (PySlice_Check(key)) {
    if (PySlice_Unpack(key, &start, &stop, &step) < 0) {
      return -1;
    }
    range->scalar = false;
  } else {
    PyErr_SetString(PyExc_TypeError, "Key must be a long or a slice");
    return -1;
  }

  Py_ssize_t count =
      PySlice_AdjustIndices((Py_ssize_t)length, &start, &stop, step);

  range->start = start;
  range->stop = stop;
  range->step = step;
  range->count = (size_t)count;

  if (count == 0) {
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
  if (size == 0) {
    Py_DECREF(fast);
    PyErr_SetString(PyExc_ValueError,
                    "cannot coerce an empty list/tuple to a matrix");
    return NULL;
  }
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

typedef struct {
  int_least64_t interpid;
  PyTypeObject *matrix_type;
  PyTypeObject *values_iter_type;
  PyObject *matrix_unpickle;
  uint64_t prng_state; ///< Per-interpreter PRNG state (splitmix64).
} _math_module_state;

static thread_local _math_module_state *LOCAL_STATE;

#define LOCAL_STATE_SET(m)                                                     \
  do {                                                                         \
    LOCAL_STATE = (_math_module_state *)PyModule_GetState(m);                  \
  } while (0)

/// @brief Resolve this thread's @ref _math_module_state, warming the cache.
/// @details Fast path returns the thread-local set at module exec. A handful
///          of paths (Matrix pickle reduce/unpickle and the XIData
///          reconstructor) can also be reached from a *second* thread in the
///          same interpreter that never ran module init -- concretely the
///          primary interpreter's noticeboard mutator thread, which
///          reconstructs Matrix-valued noticeboard entries during
///          @c notice_update snapshots. On that cold thread @ref LOCAL_STATE
///          is NULL, so we resolve *this interpreter's* already-imported
///          module from @c sys.modules and read its state. Module state is
///          per-interpreter, so this is correct on any interpreter without a
///          published global. @c PyImport_GetModule is a plain @c sys.modules
///          lookup: it returns the module @ref _math_module_exec already
///          created and never re-runs init, so no duplicate state is built.
///
///          The lookup is deliberately *not* cached in @ref LOCAL_STATE: it
///          runs only on the cold noticeboard path, while the fast path is
///          reserved for the threads that ran module init.
///
///          Invariant relied on by the many hot paths that dereference
///          @ref LOCAL_STATE *directly* (the number-protocol ops,
///          @c unwrap_matrix, @c use_out_target, @c unwrap_mask, the values
///          iterator, ...): those run only on threads that executed module
///          init, so @ref LOCAL_STATE is warm. The one cold thread that
///          reaches @c _math (the noticeboard mutator) enters solely through
///          @ref Matrix_reduce, which routes through this helper and never
///          chains a @ref LOCAL_STATE -direct call. If a future noticeboard
///          path executes a Matrix *method* on that thread, re-audit: it must
///          go through @ref math_local_state, not a bare @ref LOCAL_STATE.
/// @return the module state, or NULL with a RuntimeError set if the module is
///         no longer present in @c sys.modules for the current interpreter
///         (does not happen in bocpy's threading model, where the noticeboard
///         thread is stopped before module teardown; guarded defensively
///         rather than crashing).
static _math_module_state *math_local_state(void) {
  if (LOCAL_STATE != NULL) {
    return LOCAL_STATE;
  }
  PyObject *name = PyUnicode_FromString("bocpy._math");
  if (name == NULL) {
    return NULL;
  }
  PyObject *module = PyImport_GetModule(name);
  Py_DECREF(name);
  if (module == NULL) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_RuntimeError,
                      "bocpy._math module state is unavailable on this thread");
    }
    return NULL;
  }
  _math_module_state *state = (_math_module_state *)PyModule_GetState(module);
  Py_DECREF(module);
  return state;
}

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
  _math_module_state *state = math_local_state();
  if (state == NULL) {
    impl_free(impl);
    return NULL;
  }
  PyObject *matrix = wrap_matrix(state->matrix_type, impl);
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

/// @brief Validate a caller-supplied ``out=`` target and alias its buffer.
/// @details Implements the numpy-style ``out=`` convention for the
///          allocation-free elementwise paths: the result is written into an
///          existing matrix the caller owns rather than a fresh allocation.
///          The target must be a Matrix owned by the current interpreter whose
///          shape exactly matches the operation's result shape; the shape is
///          validated here, before any kernel writes, so a rejected target
///          leaves every buffer untouched (the validate-then-write contract
///          shared with ``put``). The target may alias the primary
///          same-shape operand because the kernel reads and writes that
///          operand at the same index; aliasing a lower-rank broadcast
///          operand is impossible because the result-shape check rejects any
///          target whose shape differs from the result.
/// @param out_target The ``out=`` Matrix object (never NULL / Py_None here).
/// @param out_op Receives a new reference to ``out_target`` on success.
/// @param want_rows Required row count of the result.
/// @param want_cols Required column count of the result.
/// @return The target's impl (no extra C refcount taken — the live Python
///         reference owns it), or NULL with an exception set on failure.
static matrix_impl *use_out_target(PyObject *out_target, PyObject **out_op,
                                   size_t want_rows, size_t want_cols) {
  if (Py_TYPE(out_target) != LOCAL_STATE->matrix_type) {
    PyErr_SetString(PyExc_TypeError, "out must be a Matrix");
    return NULL;
  }
  matrix_impl *impl = ((MatrixObject *)out_target)->impl;
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }
  if (impl->rows != want_rows || impl->columns != want_cols) {
    PyErr_Format(PyExc_ValueError,
                 "out shape %zux%zu does not match result %zux%zu", impl->rows,
                 impl->columns, want_rows, want_cols);
    return NULL;
  }
  *out_op = Py_NewRef(out_target);
  return impl;
}

/// @brief Sets the output of an arithmetic operation.
/// @details The output of an arithmetic operation is one of three things: a
/// caller-supplied ``out=`` target (when ``out_target`` is a non-None Matrix),
/// the left-hand side itself (in-place operations), or a freshly allocated
/// matrix of the same dimensions as the left-hand side. ``out_target`` and
/// ``inplace`` are mutually exclusive (asserted here; callers reject the
/// combination before reaching this point).
///
/// The result shape and wrap type come from the already-unwrapped ``lhs``
/// impl and the interpreter's canonical Matrix type -- never by casting
/// ``lhs_op``. ``lhs_op`` may be a sequence that ``unwrap_matrix`` coerced
/// into ``lhs`` (e.g. ``[1, 2, 3] + matrix``), so it is not guaranteed to be
/// a Matrix; it is dereferenced only as the in-place return value, a path
/// reachable solely with a genuine Matrix left operand.
/// @param lhs The unwrapped impl of the left-hand operand (gives result shape)
/// @param lhs_op The PyObject of the left-hand operand (in-place return only)
/// @param out_op A pointer to the output pointer of the equation
/// @param out_target Optional ``out=`` Matrix (NULL or Py_None when unused)
/// @param inplace Whether this is an inplace operation
/// @return The matrix wrapped by out_op, or NULL in the case of an error
static matrix_impl *set_output(matrix_impl *lhs, PyObject *lhs_op,
                               PyObject **out_op, PyObject *out_target,
                               bool inplace) {
  matrix_impl *out;
  if (out_target != NULL && out_target != Py_None) {
    assert(!inplace);
    return use_out_target(out_target, out_op, lhs->rows, lhs->columns);
  }
  if (inplace) {
    *out_op = Py_NewRef(lhs_op);
    return lhs;
  }

  out = impl_new(lhs->rows, lhs->columns);
  if (out == NULL) {
    return NULL;
  }

  *out_op = wrap_matrix(LOCAL_STATE->matrix_type, out);
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
    PyErr_SetString(PyExc_ValueError, "axis must be -2, -1, 0, or 1");
    return -1;
  }
  if (ax == -1) {
    out->axis = 1;
  } else if (ax == -2) {
    out->axis = 0;
  }
  return 0;
}

// Validate and unwrap a where= mask: it must be a Matrix of the same shape
// as the matrix being aggregated and owned by the current interpreter. A
// mask cell == 0.0 excludes that element; NaN counts as included (matching
// where()'s truthiness). Returns a borrowed impl (the caller holds the
// Python object alive for the synchronous call) or NULL with an exception.
static matrix_impl *unwrap_mask(PyObject *where_op, matrix_impl *impl) {
  if (Py_TYPE(where_op) != LOCAL_STATE->matrix_type) {
    PyErr_SetString(PyExc_TypeError, "where must be a Matrix mask");
    return NULL;
  }

  matrix_impl *mask = ((MatrixObject *)where_op)->impl;
  if (!impl_check_acquired(mask, true)) {
    return NULL;
  }

  if (mask->rows != impl->rows || mask->columns != impl->columns) {
    PyErr_Format(PyExc_ValueError,
                 "where mask shape %zux%zu does not match matrix shape "
                 "%zux%zu",
                 mask->rows, mask->columns, impl->rows, impl->columns);
    return NULL;
  }

  return mask;
}

static int Matrix_aggregate(PyObject *matrix_op, AxisArg axis,
                            PyObject *where_op, PyObject **out_op,
                            enum AggregateOps agg) {
  MatrixObject *matrix = (MatrixObject *)matrix_op;
  matrix_impl *impl = matrix->impl;

  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  matrix_impl *mask = NULL;
  if (where_op != NULL && where_op != Py_None) {
    mask = unwrap_mask(where_op, impl);
    if (mask == NULL) {
      return -1;
    }
  }

  if (!axis.has_axis) {
    double value = mask ? dispatch_agg_masked_ewise(impl, mask, agg)
                        : dispatch_agg_ewise(impl, agg);
    *out_op = PyFloat_FromDouble(value);
    return 0;
  }

  if (axis.axis == 0) {
    matrix_impl *vector = impl_new(1, impl->columns);
    if (vector == NULL) {
      return -1;
    }

    if (mask) {
      dispatch_agg_masked_columnwise(impl, mask, agg, vector);
    } else {
      dispatch_agg_columnwise(impl, agg, vector);
    }
    *out_op = wrap_matrix(Py_TYPE(matrix_op), vector);
    if (*out_op == NULL) {
      impl_free(vector);
      return -1;
    }

    return 0;
  }

  // Fall-through is axis == 1 (row-wise): parse_validate_normalise_axis
  // restricts axis to {0, 1}.
  matrix_impl *vector = impl_new(impl->rows, 1);
  if (vector == NULL) {
    return -1;
  }

  if (mask) {
    dispatch_agg_masked_rowwise(impl, mask, agg, vector);
  } else {
    dispatch_agg_rowwise(impl, agg, vector);
  }
  *out_op = wrap_matrix(Py_TYPE(matrix_op), vector);
  if (*out_op == NULL) {
    impl_free(vector);
    return -1;
  }

  return 0;
}

#define MATRIX_AGGREGATE(agg)                                                  \
  static PyObject *Matrix_##agg##_method(PyObject *op, PyObject *args,         \
                                         PyObject *kwds) {                     \
    PyObject *out = NULL;                                                      \
    PyObject *axis_obj = NULL;                                                 \
    PyObject *where_obj = NULL;                                                \
    static char *kwlist[] = {"axis", "where", NULL};                           \
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|OO", kwlist, &axis_obj,     \
                                     &where_obj)) {                            \
      return NULL;                                                             \
    }                                                                          \
    AxisArg axis;                                                              \
    if (parse_validate_normalise_axis(axis_obj, &axis) < 0) {                  \
      return NULL;                                                             \
    }                                                                          \
    if (Matrix_aggregate(op, axis, where_obj, &out, agg) < 0) {                \
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

/* --------------------------------------------------------------------------
   Arg-reduction (argmin / argmax) kernels.

   These do not fit the BOC_AGG_OPS X-macro: that table accumulates a
   single double, whereas an arg-reduction must also carry the index of
   the running extreme. Comparisons are strict so the first occurrence of
   a tied extreme wins, matching NumPy. Indices are published as doubles
   in the result matrix (the Matrix type stores only doubles).
   -------------------------------------------------------------------------- */
// Arg-reduction (argmin/argmax) kernels: kept out of the BOC_AGG_OPS X-macro
// because they must carry the running index, not just a double accumulator.
// Strict comparisons make the first tied extreme win (NumPy tie-break);
// indices are published as doubles (Matrix stores only doubles).
static Py_ssize_t argextreme_ewise(matrix_impl *m, bool want_max) {
  const double *p = m->data;
  double best = p[0];
  Py_ssize_t best_i = 0;
  for (size_t i = 1; i < m->size; ++i) {
    const double v = p[i];
    if (want_max ? (v > best) : (v < best)) {
      best = v;
      best_i = (Py_ssize_t)i;
    }
  }
  return best_i;
}

static void argextreme_columnwise(matrix_impl *m, bool want_max,
                                  matrix_impl *out) {
  const size_t M = m->rows;
  const size_t N = m->columns;
  for (size_t c = 0; c < N; ++c) {
    double best = m->data[c];
    size_t best_r = 0;
    for (size_t r = 1; r < M; ++r) {
      const double v = m->data[r * N + c];
      if (want_max ? (v > best) : (v < best)) {
        best = v;
        best_r = r;
      }
    }
    out->data[c] = (double)best_r;
  }
}

static void argextreme_rowwise(matrix_impl *m, bool want_max,
                               matrix_impl *out) {
  const size_t M = m->rows;
  const size_t N = m->columns;
  for (size_t r = 0; r < M; ++r) {
    const double *row = m->data + r * N;
    double best = row[0];
    size_t best_c = 0;
    for (size_t c = 1; c < N; ++c) {
      const double v = row[c];
      if (want_max ? (v > best) : (v < best)) {
        best = v;
        best_c = c;
      }
    }
    out->data[r] = (double)best_c;
  }
}

/* Masked arg-reduction kernels: among the included elements (mask cell !=
   0.0; NaN counts as included, matching where()'s truthiness) find the index
   of the first strict extreme. The first included element seeds the running
   extreme (best_i < 0 guard); strict comparisons then keep the first
   occurrence on a tie and -- exactly like the unmasked kernels -- a NaN never
   displaces a real running extreme, while a NaN seed pins the result to its
   position. An all-excluded group has no argument and yields
   ARGEXTREME_MASKED_EMPTY (-1), the integer analog of the masked aggregate's
   NaN. Rows/columns are addressed through row_ptrs so mask and matrix stay
   aligned cell-for-cell. */
#define ARGEXTREME_MASKED_EMPTY (-1)

static Py_ssize_t argextreme_masked_ewise(matrix_impl *m, matrix_impl *mask,
                                          bool want_max) {
  const double *p = m->data;
  const double *kp = mask->data;
  double best = 0.0;
  Py_ssize_t best_i = ARGEXTREME_MASKED_EMPTY;
  for (size_t i = 0; i < m->size; ++i) {
    if (kp[i] == 0.0) {
      continue;
    }
    const double v = p[i];
    if (best_i < 0 || (want_max ? (v > best) : (v < best))) {
      best = v;
      best_i = (Py_ssize_t)i;
    }
  }
  return best_i;
}

static void argextreme_masked_columnwise(matrix_impl *m, matrix_impl *mask,
                                         bool want_max, matrix_impl *out) {
  const size_t M = m->rows;
  const size_t N = m->columns;
  for (size_t c = 0; c < N; ++c) {
    double best = 0.0;
    Py_ssize_t best_r = ARGEXTREME_MASKED_EMPTY;
    for (size_t r = 0; r < M; ++r) {
      if (mask->row_ptrs[r][c] == 0.0) {
        continue;
      }
      const double v = m->row_ptrs[r][c];
      if (best_r < 0 || (want_max ? (v > best) : (v < best))) {
        best = v;
        best_r = (Py_ssize_t)r;
      }
    }
    out->data[c] = (double)best_r;
  }
}

static void argextreme_masked_rowwise(matrix_impl *m, matrix_impl *mask,
                                      bool want_max, matrix_impl *out) {
  const size_t M = m->rows;
  const size_t N = m->columns;
  for (size_t r = 0; r < M; ++r) {
    const double *row = m->row_ptrs[r];
    const double *krow = mask->row_ptrs[r];
    double best = 0.0;
    Py_ssize_t best_c = ARGEXTREME_MASKED_EMPTY;
    for (size_t c = 0; c < N; ++c) {
      if (krow[c] == 0.0) {
        continue;
      }
      const double v = row[c];
      if (best_c < 0 || (want_max ? (v > best) : (v < best))) {
        best = v;
        best_c = (Py_ssize_t)c;
      }
    }
    out->data[r] = (double)best_c;
  }
}

// Convert an Mx1 or 1xN vector of index positions (stored as doubles by the
// argextreme kernels) into a flat Python list[int] — directly usable in fancy
// indexing without a float-matrix conversion.
static PyObject *argextreme_indices_to_list(const matrix_impl *vector) {
  PyObject *list = PyList_New((Py_ssize_t)vector->size);
  if (list == NULL) {
    return NULL;
  }
  for (size_t i = 0; i < vector->size; ++i) {
    PyObject *idx = PyLong_FromSsize_t((Py_ssize_t)vector->data[i]);
    if (idx == NULL) {
      Py_DECREF(list);
      return NULL;
    }
    PyList_SET_ITEM(list, (Py_ssize_t)i, idx);
  }
  return list;
}

static int Matrix_argextreme(PyObject *matrix_op, AxisArg axis,
                             PyObject *where_op, PyObject **out_op,
                             bool want_max, bool as_matrix) {
  MatrixObject *matrix = (MatrixObject *)matrix_op;
  matrix_impl *impl = matrix->impl;

  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  matrix_impl *mask = NULL;
  if (where_op != NULL && where_op != Py_None) {
    mask = unwrap_mask(where_op, impl);
    if (mask == NULL) {
      return -1;
    }
  }

  // Defensive: the public constructors reject zero-size matrices, but guard
  // each axis so a future empty-capable path can't read p[0] out of bounds.
  // (The masked kernels never index p[0] unconditionally, so an all-excluded
  // group is fine -- they return the -1 "no argument" sentinel instead.)
  const char *empty_error = "arg-reduction of an empty matrix is undefined";

  if (!axis.has_axis) {
    if (impl->size == 0) {
      PyErr_SetString(PyExc_ValueError, empty_error);
      return -1;
    }
    Py_ssize_t idx = mask ? argextreme_masked_ewise(impl, mask, want_max)
                          : argextreme_ewise(impl, want_max);
    *out_op = PyLong_FromSsize_t(idx);
    return *out_op == NULL ? -1 : 0;
  }

  if (axis.axis == 0) {
    if (impl->rows == 0) {
      PyErr_SetString(PyExc_ValueError, empty_error);
      return -1;
    }
    matrix_impl *vector = impl_new(1, impl->columns);
    if (vector == NULL) {
      return -1;
    }
    if (mask) {
      argextreme_masked_columnwise(impl, mask, want_max, vector);
    } else {
      argextreme_columnwise(impl, want_max, vector);
    }
    if (as_matrix) {
      *out_op = (PyObject *)wrap_impl_or_free(vector);
    } else {
      *out_op = argextreme_indices_to_list(vector);
      impl_free(vector);
    }
    return *out_op == NULL ? -1 : 0;
  }

  if (impl->columns == 0) {
    PyErr_SetString(PyExc_ValueError, empty_error);
    return -1;
  }
  matrix_impl *vector = impl_new(impl->rows, 1);
  if (vector == NULL) {
    return -1;
  }
  if (mask) {
    argextreme_masked_rowwise(impl, mask, want_max, vector);
  } else {
    argextreme_rowwise(impl, want_max, vector);
  }
  if (as_matrix) {
    *out_op = (PyObject *)wrap_impl_or_free(vector);
  } else {
    *out_op = argextreme_indices_to_list(vector);
    impl_free(vector);
  }
  return *out_op == NULL ? -1 : 0;
}

#define MATRIX_ARGEXTREME(name, want_max_val)                                  \
  static PyObject *Matrix_##name##_method(PyObject *op, PyObject *args,        \
                                          PyObject *kwds) {                    \
    PyObject *out = NULL;                                                      \
    PyObject *axis_obj = NULL;                                                 \
    PyObject *where_obj = NULL;                                                \
    int as_matrix = 0;                                                         \
    static char *kwlist[] = {"axis", "where", "as_matrix", NULL};              \
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|OOp", kwlist, &axis_obj,    \
                                     &where_obj, &as_matrix)) {                \
      return NULL;                                                             \
    }                                                                          \
    AxisArg axis;                                                              \
    if (parse_validate_normalise_axis(axis_obj, &axis) < 0) {                  \
      return NULL;                                                             \
    }                                                                          \
    if (Matrix_argextreme(op, axis, where_obj, &out, want_max_val,             \
                          (bool)as_matrix) < 0) {                              \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

MATRIX_ARGEXTREME(argmin, false)
MATRIX_ARGEXTREME(argmax, true)

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

  if (lhs->rows == rhs->rows && lhs->columns == rhs->columns) {
    shape = BCAST_NONE;
  } else if (lhs->rows == rhs->rows &&
             (lhs->columns == 1 || rhs->columns == 1)) {
    shape = BCAST_COL;
    if (lhs->columns == 1) {
      mat_arg = rhs;
      vec_arg = lhs;
    }
  } else if (lhs->columns == rhs->columns &&
             (lhs->rows == 1 || rhs->rows == 1)) {
    shape = BCAST_ROW;
    if (lhs->rows == 1) {
      mat_arg = rhs;
      vec_arg = lhs;
    }
  } else if ((lhs->rows == 1 || lhs->columns == 1) &&
             (rhs->rows == 1 || rhs->columns == 1) && lhs->size == rhs->size) {
    shape = BCAST_NONE;
  } else {
    PyErr_Format(PyExc_ValueError,
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
    // axis == 1 (row-wise): parse_validate_normalise_axis restricts axis
    // to {0, 1}.
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

/// @brief A classified broadcast operand: scalar or same-shape matrix.
/// @details ``full`` is an INCREF'd matrix with the same shape as the
///          receiver, and the caller must IMPL_DECREF it; when ``full`` is
///          NULL the operand is the scalar in ``scalar``. Shared by ``fma``
///          and ``scaled_add`` to classify a multiplier / scale operand.
typedef struct {
  double scalar;
  matrix_impl *full;
} broadcast_operand;

/// @brief Materialise a row- or column-vector broadcast into a full buffer.
/// @details Expands a ``1xN`` row vector (``is_row``) or an ``Mx1`` column
///          vector into a fresh contiguous ``rows`` x ``columns`` matrix so
///          the contiguous fma kernel keeps its unit stride (and hardware
///          FMA). The returned impl has refcount 0; the caller INCREFs it.
static matrix_impl *impl_broadcast_vector(const matrix_impl *vec, size_t rows,
                                          size_t columns, bool is_row) {
  matrix_impl *out = impl_new(rows, columns);
  if (out == NULL) {
    return NULL;
  }
  double *dst = out->data;
  const double *v = vec->data;
  if (is_row) {
    for (size_t r = 0; r < rows; ++r) {
      for (size_t c = 0; c < columns; ++c) {
        *dst++ = v[c];
      }
    }
  } else {
    for (size_t r = 0; r < rows; ++r) {
      for (size_t c = 0; c < columns; ++c) {
        *dst++ = v[r];
      }
    }
  }
  return out;
}

/// @brief Classify a broadcast operand as a scalar or a same-shape matrix.
/// @details A Matrix whose size is 1 is treated as a scalar so a ``1x1``
///          broadcasts like a number (the in-house scalar rule used
///          elsewhere); a same-shape Matrix is INCREF'd into ``out->full``.
///          A ``1xN`` row vector (matching self's columns) or an ``Mx1``
///          column vector (matching self's rows) is materialised into a fresh
///          same-shape buffer stored in ``out->full`` so the contiguous kernel
///          keeps its unit stride; any other matrix shape raises ``ValueError``
///          naming both shapes. Any real number is accepted as a scalar.
/// @param op_name Method name used as the error-message prefix (e.g. ``fma``).
/// @param operand The operand object to classify.
/// @param self_impl The receiver's impl, used for the shape check.
/// @param what Operand name used in error messages.
/// @param out Output operand descriptor (zeroed before classification).
/// @return 0 on success; -1 with an exception set on failure.
static int classify_broadcast_operand(const char *op_name, PyObject *operand,
                                      const matrix_impl *self_impl,
                                      const char *what,
                                      broadcast_operand *out) {
  out->scalar = 0.0;
  out->full = NULL;

  if (Py_TYPE(operand) == LOCAL_STATE->matrix_type) {
    matrix_impl *impl = ((MatrixObject *)operand)->impl;
    if (!impl_check_acquired(impl, true)) {
      return -1;
    }
    if (impl->size == 1) {
      out->scalar = impl->data[0];
      return 0;
    }
    if (impl->rows == self_impl->rows && impl->columns == self_impl->columns) {
      IMPL_INCREF(impl);
      out->full = impl;
      return 0;
    }
    if (impl->rows == 1 && impl->columns == self_impl->columns) {
      matrix_impl *full = impl_broadcast_vector(impl, self_impl->rows,
                                                self_impl->columns, true);
      if (full == NULL) {
        return -1;
      }
      IMPL_INCREF(full);
      out->full = full;
      return 0;
    }
    if (impl->columns == 1 && impl->rows == self_impl->rows) {
      matrix_impl *full = impl_broadcast_vector(impl, self_impl->rows,
                                                self_impl->columns, false);
      if (full == NULL) {
        return -1;
      }
      IMPL_INCREF(full);
      out->full = full;
      return 0;
    }
    PyErr_Format(PyExc_ValueError,
                 "%s: %s shape %zux%zu incompatible with self %zux%zu", op_name,
                 what, impl->rows, impl->columns, self_impl->rows,
                 self_impl->columns);
    return -1;
  }

  if (unwrap_double(operand, &out->scalar)) {
    return 0;
  }
  if (PyErr_Occurred()) {
    return -1;
  }

  PyErr_Format(PyExc_TypeError, "%s: %s must be a Matrix or a real number",
               op_name, what);
  return -1;
}

/// @brief Fused multiply-add kernel: ``out = a*b + c`` with one rounding.
/// @details ``b_step`` / ``c_step`` are 1 for a full same-shape operand and
///          0 to repeat a scalar (the pointer then aims at a single local
///          ``double``). Each output cell reads only its own index, so the
///          in-place case (``out == a``) and ``b`` / ``c`` aliasing ``a``
///          are all safe. ``fma()`` rounds the product once, unlike
///          ``a*b + c`` which rounds twice under ``-ffp-contract=off``.
///          BOC_FMA_MULTIVERSION clones this kernel for hardware FMA on
///          glibc x86-64 (see the macro definition for why glibc-only).
BOC_CANARY_NOINLINE
BOC_FMA_MULTIVERSION
static void impl_fma(const double *a, const double *b, size_t b_step,
                     const double *c, size_t c_step, double *out, size_t n) {
  for (size_t i = 0; i < n; ++i, ++a, b += b_step, c += c_step, ++out) {
    *out = fma(*a, *b, *c);
  }
}

/// @brief Fused multiply-add: single-rounding ``self*b + c``.
/// @details ``b`` and ``c`` may each be a same-shape matrix, a ``1x1``
///          matrix, a ``1xN`` row vector or ``Mx1`` column vector that
///          broadcasts against ``self``, or a scalar; any other matrix shape
///          raises ``ValueError``. Both operands are validated before any
///          allocation, so a rejected operand leaves ``self`` untouched.
///          With ``in_place=True`` the result is written into ``self`` and
///          ``self`` is returned.
static PyObject *Matrix_fma(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  PyObject *b_op = NULL;
  PyObject *c_op = NULL;
  int in_place = 0;
  PyObject *out_op = NULL;
  broadcast_operand bop = {0.0, NULL};
  broadcast_operand cop = {0.0, NULL};

  static char *kwlist[] = {"", "", "in_place", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "OO|p", kwlist, &b_op, &c_op,
                                   &in_place)) {
    return NULL;
  }

  if (!impl_check_acquired(self->impl, true)) {
    return NULL;
  }

  if (classify_broadcast_operand("fma", b_op, self->impl, "b", &bop) < 0) {
    goto done;
  }
  if (classify_broadcast_operand("fma", c_op, self->impl, "c", &cop) < 0) {
    goto done;
  }

  matrix_impl *out = set_output(self->impl, op, &out_op, NULL, in_place);
  if (out == NULL) {
    out_op = NULL;
    goto done;
  }

  double b_scalar = bop.scalar;
  double c_scalar = cop.scalar;
  const double *b_ptr = bop.full != NULL ? bop.full->data : &b_scalar;
  const double *c_ptr = cop.full != NULL ? cop.full->data : &c_scalar;
  size_t b_step = bop.full != NULL ? 1 : 0;
  size_t c_step = cop.full != NULL ? 1 : 0;

  impl_fma(self->impl->data, b_ptr, b_step, c_ptr, c_step, out->data,
           self->impl->size);

done:
  IMPL_DECREF(bop.full);
  IMPL_DECREF(cop.full);
  return out_op;
}

/// @brief Two-rounding scaled-add kernel: ``out = y + s * x``.
/// @details Deliberately NOT fused: the product ``s[i] * x[i]`` is rounded to
///          double first, then the sum with ``y[i]`` is rounded again, so the
///          result is bit-for-bit identical to the naive ``y[i] + s * x[i]``
///          expression in IEEE-754 double (two roundings). This is the
///          two-rounding complement to ``impl_fma``'s single rounding; the
///          ``-ffp-contract=off`` build flag guarantees the two statements
///          are never contracted back into an fma. ``s_step`` is 1 for a full
///          same-shape scale buffer and 0 to repeat a broadcast scalar (the
///          pointer then aims at a single local ``double``); ``x`` and ``y``
///          are the same shape (unit stride). Each output cell reads only its
///          own index, so the in-place case (``out == y``) and ``x`` / ``s``
///          aliasing ``y`` are all safe.
BOC_CANARY_NOINLINE
static void impl_scaled_add(const double *y, const double *s, size_t s_step,
                            const double *x, double *out, size_t n) {
  for (size_t i = 0; i < n; ++i, ++y, s += s_step, ++x, ++out) {
    const double prod = (*s) * (*x);
    *out = *y + prod;
  }
}

/// @brief Scaled add: ``self + s * x`` with two roundings.
/// @details The two-rounding sibling of ``fma``: the product ``s * x`` is
///          rounded to double, then the sum with ``self`` is rounded again, so
///          the result is bit-for-bit identical to ``self + s * x`` and
///          distinct from the single-rounded ``fma``. ``s`` is the scale and
///          may be a scalar, a ``1x1`` matrix, a ``1xN`` row vector or ``Mx1``
///          column vector that broadcasts against ``self``, or a same-shape
///          matrix (the same operand rules as ``fma``'s multiplier); ``x`` is
///          a same-shape matrix. Both operands are validated before any
///          allocation, so a rejected operand leaves ``self`` untouched (the
///          validate-then-write contract shared with ``put``). With
///          ``in_place=True`` the result is written into ``self``'s existing
///          buffer (allocating nothing) and ``self`` is returned; otherwise a
///          fresh matrix is returned. ``s`` and ``x`` may alias ``self``.
static PyObject *Matrix_scaled_add(PyObject *op, PyObject *args,
                                   PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  PyObject *s_op = NULL;
  PyObject *x_op = NULL;
  int in_place = 0;
  PyObject *out_op = NULL;
  broadcast_operand sop = {0.0, NULL};
  matrix_impl *x = NULL;

  static char *kwlist[] = {"", "", "in_place", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "OO|p", kwlist, &s_op, &x_op,
                                   &in_place)) {
    return NULL;
  }

  if (!impl_check_acquired(self->impl, true)) {
    return NULL;
  }

  if (classify_broadcast_operand("scaled_add", s_op, self->impl, "s", &sop) <
      0) {
    return NULL;
  }

  x = unwrap_matrix(x_op, false);
  if (x == NULL) {
    goto done;
  }

  matrix_impl *y = self->impl;
  if (x->rows != y->rows || x->columns != y->columns) {
    PyErr_Format(PyExc_ValueError,
                 "scaled_add: x shape %zux%zu does not match self %zux%zu",
                 x->rows, x->columns, y->rows, y->columns);
    goto done;
  }

  matrix_impl *out = set_output(y, op, &out_op, NULL, in_place);
  if (out == NULL) {
    out_op = NULL;
    goto done;
  }

  double s_scalar = sop.scalar;
  const double *s_ptr = sop.full != NULL ? sop.full->data : &s_scalar;
  size_t s_step = sop.full != NULL ? 1 : 0;

  impl_scaled_add(y->data, s_ptr, s_step, x->data, out->data, y->size);

done:
  IMPL_DECREF(sop.full);
  IMPL_DECREF(x);
  return out_op;
}

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
  if (M == 2 && N == 2) {
    return (has_axis && explicit_axis == 0) ? CROSS_COLS_2D_2xN
                                            : CROSS_ROWS_2D_Nx2;
  }
  if (M == 3 && N == 3) {
    return (has_axis && explicit_axis == 0) ? CROSS_COLS_3D_3xN
                                            : CROSS_ROWS_3D_Nx3;
  }
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
        PyExc_ValueError,
        "cross requires a 2D or 3D vector or Nx2 or 2xN or Nx3 or 3xN matrix");
    goto done;
  }

  if (flavor == CROSS_SCALAR_2D_1x2 || flavor == CROSS_SCALAR_2D_2x1) {
    if (rhs->size != 2) {
      PyErr_Format(PyExc_ValueError,
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
      PyErr_Format(PyExc_ValueError,
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

  if (flavor == CROSS_ROWS_2D_Nx2) {
    const size_t N = lhs->rows;
    const bool same_shape =
        (lhs->rows == rhs->rows && lhs->columns == rhs->columns);
    const bool broadcast =
        (rhs->size == 2 && (rhs->rows == 1 || rhs->columns == 1));
    if (!same_shape && !broadcast) {
      PyErr_Format(PyExc_ValueError,
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
      PyErr_Format(PyExc_ValueError,
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
      PyErr_Format(PyExc_ValueError,
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
      PyErr_Format(PyExc_ValueError,
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

  // axis == 1 (row-wise): parse_validate_normalise_axis restricts axis
  // to {0, 1}.
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

  matrix_impl *out = set_output(self->impl, op, &out_op, NULL, in_place);
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
    PyErr_SetString(PyExc_ValueError,
                    "perpendicular requires a 2D vector or Nx2 or 2xN matrix");
    return NULL;
  }

  if (in_place) {
    impl_perpendicular_in_place(self->impl, flavor);
    return Py_NewRef(op);
  }

  matrix_impl *out = set_output(self->impl, op, &out_op, NULL, false);
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
    PyErr_SetString(PyExc_ValueError,
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

  // Fall-through is the 2xN column-batch case: classify_vec2_axis already
  // rejected every shape other than VEC2_ROWS_Nx2 and this one.
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
   false, a fresh matrix is allocated and returned. The keyword-only
   ``out`` target writes the result into a caller-supplied same-shape
   matrix (allocation-free, numpy convention) and returns it; ``out`` and
   ``in_place`` are mutually exclusive. See the BOC_UNARY_OPS top-of-family
   block comment for the full template. */
#define MATRIX_UNARY_METHOD(ENUM, STAMP)                                       \
  static PyObject *Matrix_##ENUM##_method(PyObject *op, PyObject *args,        \
                                          PyObject *kwds) {                    \
    MatrixObject *self = (MatrixObject *)op;                                   \
    matrix_impl *impl = self->impl;                                            \
    int in_place = 0;                                                          \
    PyObject *out_target = NULL;                                               \
    static char *kwlist[] = {"in_place", "out", NULL};                         \
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|$pO", kwlist, &in_place,    \
                                     &out_target)) {                           \
      return NULL;                                                             \
    }                                                                          \
    if (!impl_check_acquired(impl, true)) {                                    \
      return NULL;                                                             \
    }                                                                          \
    if (in_place && out_target != NULL && out_target != Py_None) {             \
      PyErr_SetString(PyExc_ValueError,                                        \
                      "out and in_place are mutually exclusive");              \
      return NULL;                                                             \
    }                                                                          \
    PyObject *out_op = NULL;                                                   \
    matrix_impl *out = set_output(impl, op, &out_op, out_target, in_place);    \
    if (out == NULL) {                                                         \
      return NULL;                                                             \
    }                                                                          \
    impl_##STAMP##_ewise(impl, out);                                           \
    return out_op;                                                             \
  }

#define X(E, S, EX) MATRIX_UNARY_METHOD(E, S)
BOC_UNARY_OPS(X)
#undef X

/// @brief Clamp every element to ``[min, max]``; either bound may be omitted
/// @details The first positional argument is the lower bound, the second the
///          upper. An omitted bound (``None``) is realised as
///          ``-INFINITY`` / ``+INFINITY`` so the inner loop stays branch-free;
///          NaN elements pass through unchanged. Raises ``ValueError`` if both
///          bounds are omitted and ``AssertionError`` if ``max < min``.
///          Output routing matches the unary elementwise family via
///          ``set_output``: ``in_place=True`` clamps ``self`` in place and
///          returns it, the keyword-only ``out`` writes into a caller-supplied
///          same-shape matrix, and the two are mutually exclusive.
static PyObject *Matrix_clip(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  PyObject *minval_op = Py_None;
  PyObject *maxval_op = Py_None;
  int in_place = 0;
  PyObject *out_target = NULL;
  static char *kwlist[] = {"min", "max", "in_place", "out", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwds, "|OO$pO", kwlist, &minval_op,
                                   &maxval_op, &in_place, &out_target)) {
    return NULL;
  }

  if (in_place && out_target != NULL && out_target != Py_None) {
    PyErr_SetString(PyExc_ValueError,
                    "out and in_place are mutually exclusive");
    return NULL;
  }

  bool has_min = minval_op != Py_None;
  bool has_max = maxval_op != Py_None;
  if (!has_min && !has_max) {
    PyErr_SetString(PyExc_ValueError, "clip: must provide min and/or max");
    return NULL;
  }

  double minval = -INFINITY;
  double maxval = INFINITY;
  if (has_min && !unwrap_double(minval_op, &minval)) {
    PyErr_SetString(PyExc_TypeError, "Expected a number");
    return NULL;
  }
  if (has_max && !unwrap_double(maxval_op, &maxval)) {
    PyErr_SetString(PyExc_TypeError, "Expected a number");
    return NULL;
  }

  if (has_min && has_max && maxval < minval) {
    PyErr_SetString(PyExc_AssertionError, "maxval < minval");
    return NULL;
  }

  PyObject *out_op = NULL;
  matrix_impl *out = set_output(impl, op, &out_op, out_target, in_place);
  if (out == NULL) {
    return NULL;
  }

  double *src = impl->data;
  double *dst = out->data;
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

  return out_op;
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

/// @brief Resolve one fancy-index item to an in-bounds row/column number.
/// @details Applies Python-style negative wrapping (``-1`` -> ``dim-1``)
///          then an explicit bounds check, so a list/tuple subscript can
///          never read outside the matrix. ``axis_name`` ("row"/"column")
///          and the *original* (pre-wrap) value are named in the
///          ``IndexError`` so a bad subscript is self-describing.
///          ``bool`` items are accepted as ``0``/``1`` (bool is an ``int``
///          subclass), matching the scalar-int subscript path.
/// @return 0 with ``*out`` set on success; -1 with an exception set on
///         failure: ``TypeError`` for a non-int item, ``OverflowError``
///         for a value outside ``Py_ssize_t`` (raised by
///         ``PyLong_AsSsize_t`` before the bounds check), or
///         ``IndexError`` for an out-of-range index.
static int resolve_gather_index(PyObject *item, size_t dim,
                                const char *axis_name, size_t *out) {
  Py_ssize_t value = PyLong_AsSsize_t(item);
  if (value == -1 && PyErr_Occurred()) {
    return -1;
  }

  Py_ssize_t resolved = value;
  if (resolved < 0) {
    resolved += (Py_ssize_t)dim;
  }

  if (resolved < 0 || (size_t)resolved >= dim) {
    PyErr_Format(PyExc_IndexError,
                 "%s index %zd out of range for dimension of size %zu",
                 axis_name, value, dim);
    return -1;
  }

  *out = (size_t)resolved;
  return 0;
}

/// @brief Resolve a gather's output matrix: a caller ``out=`` or a fresh one.
/// @details Shared by @ref gather_axis and @ref gather_along_axis. When
///          @p out_target is a real object it is shape-validated via
///          @ref use_out_target and rejected if it aliases @p impl (a
///          reordering gather would read cells it has already overwritten);
///          otherwise a fresh matrix of the requested shape is allocated and
///          wrapped as @p type (so a subclass is preserved). On success the
///          impl is returned and @p *result is set to the owning ``PyObject``;
///          on failure NULL is returned with an exception set (the caller
///          still owns any index buffer it must free).
static matrix_impl *gather_output(matrix_impl *impl, PyObject *out_target,
                                  PyTypeObject *type, size_t want_rows,
                                  size_t want_cols, PyObject **result) {
  if (out_target != NULL && out_target != Py_None) {
    matrix_impl *out = use_out_target(out_target, result, want_rows, want_cols);
    if (out == NULL) {
      return NULL;
    }
    if (out == impl) {
      Py_DECREF(*result);
      PyErr_SetString(PyExc_ValueError,
                      "out must not alias the matrix being taken from");
      return NULL;
    }
    return out;
  }

  matrix_impl *out = impl_new(want_rows, want_cols);
  if (out == NULL) {
    return NULL;
  }
  *result = wrap_matrix(type, out);
  if (*result == NULL) {
    impl_free(out);
    return NULL;
  }
  return out;
}

/// @brief Gather rows (``axis == 0``) or columns (``axis == 1``) named by a
///        sequence of indices into a matrix.
/// @details Shared by ``Matrix.take`` and the subscript gather path so
///          both surfaces resolve indices identically (negative-aware and
///          bounds-checked via ``resolve_gather_index``) rather than
///          keeping two copies in sync. Row gather copies each selected
///          row with ``memcpy`` (source and destination rows are both
///          contiguous); column gather copies element-wise (inherently
///          strided). An empty ``indices`` sequence raises ``IndexError``
///          and is rejected *before* any allocation — ``impl_new`` only
///          ``assert``s non-zero dimensions, so a ``0``-length axis must
///          never reach it.
///
///          Every index is resolved up front, before any element is written,
///          so a bad index can never leave a caller-supplied ``out_target``
///          partially overwritten (the validate-then-write contract shared
///          with ``put``). When ``out_target`` is NULL / ``Py_None`` the
///          result is a fresh matrix wrapped as ``type`` (so a subclass is
///          preserved); otherwise it is written into that caller-owned
///          matrix, which must have the result's shape and must not alias the
///          source (a gather reorders elements, so an in-place permutation
///          would read rows it had already clobbered).
static PyObject *gather_axis(matrix_impl *impl, PyObject *indices, int axis,
                             PyTypeObject *type, PyObject *out_target) {
  const char *err_msg =
      "Indices must be specified as a list or a tuple of ints";
  PyObject *fast = PySequence_Fast(indices, err_msg);
  if (fast == NULL) {
    return NULL;
  }

  Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
  if (count == 0) {
    Py_DECREF(fast);
    PyErr_SetString(PyExc_IndexError, "index sequence must not be empty");
    return NULL;
  }

  const size_t dim = (axis == 0) ? impl->rows : impl->columns;
  const char *axis_name = (axis == 0) ? "row" : "column";

  // Validate phase: resolve every index before any write.
  size_t *resolved = PyMem_Malloc((size_t)count * sizeof(size_t));
  if (resolved == NULL) {
    Py_DECREF(fast);
    return PyErr_NoMemory();
  }
  for (Py_ssize_t i = 0; i < count; ++i) {
    PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
    if (resolve_gather_index(item, dim, axis_name, &resolved[i]) < 0) {
      PyMem_Free(resolved);
      Py_DECREF(fast);
      return NULL;
    }
  }
  Py_DECREF(fast);

  const size_t want_rows = (axis == 0) ? (size_t)count : impl->rows;
  const size_t want_cols = (axis == 0) ? impl->columns : (size_t)count;

  PyObject *result = NULL;
  matrix_impl *out =
      gather_output(impl, out_target, type, want_rows, want_cols, &result);
  if (out == NULL) {
    PyMem_Free(resolved);
    return NULL;
  }

  // Write phase.
  if (axis == 0) {
    for (Py_ssize_t i = 0; i < count; ++i) {
      memcpy(out->row_ptrs[i], impl->row_ptrs[resolved[i]],
             impl->columns * sizeof(double));
    }
  } else {
    for (Py_ssize_t i = 0; i < count; ++i) {
      const size_t c = resolved[i];
      for (size_t r = 0; r < impl->rows; ++r) {
        out->row_ptrs[r][i] = impl->row_ptrs[r][c];
      }
    }
  }

  PyMem_Free(resolved);
  return result;
}

/// @brief Gather one element per row (``axis == 1``) or per column
///        (``axis == 0``) into a fresh matrix, the indices running *along*
///        the named axis.
/// @details This is the ``np.take_along_axis`` counterpart to ``gather_axis``
///          (which selects whole rows/columns). For ``axis == 1`` the
///          sequence holds one column index per row — its length must equal
///          ``rows`` — and the result is ``rows x 1`` with
///          ``out[r] = self[r][indices[r]]``. For ``axis == 0`` it holds one
///          row index per column — length ``columns`` — and the result is
///          ``1 x columns`` with ``out[c] = self[indices[c]][c]``. This pairs
///          with ``argmin``/``argmax`` along the same axis: the index list
///          they return feeds straight back in to gather the reduced values.
///          Each index is resolved negative-aware and bounds-checked against
///          the *gathered* axis via ``resolve_gather_index``.
///
///          Every index is resolved up front, before any element is written,
///          so a bad index can never leave a caller-supplied ``out_target``
///          partially overwritten (the validate-then-write contract shared
///          with ``take``). When ``out_target`` is NULL / ``Py_None`` the
///          result is a fresh matrix wrapped as ``type`` (so a subclass is
///          preserved); otherwise it is written into that caller-owned
///          matrix, which must have the result's shape and must not alias the
///          source.
static PyObject *gather_along_axis(matrix_impl *impl, PyObject *indices,
                                   int axis, PyTypeObject *type,
                                   PyObject *out_target) {
  const char *err_msg =
      "Indices must be specified as a list or a tuple of ints";
  PyObject *fast = PySequence_Fast(indices, err_msg);
  if (fast == NULL) {
    return NULL;
  }

  Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
  size_t expected = axis == 0 ? impl->columns : impl->rows;
  size_t bound = axis == 0 ? impl->rows : impl->columns;
  const char *bound_name = axis == 0 ? "row" : "column";
  if ((size_t)count != expected) {
    PyErr_Format(PyExc_ValueError,
                 "take_along_axis on axis %d expects %zu indices (one per %s), "
                 "got %zd",
                 axis, expected, axis == 0 ? "column" : "row", count);
    Py_DECREF(fast);
    return NULL;
  }

  // Validate phase: resolve every index before any write.
  size_t *resolved = PyMem_Malloc((size_t)count * sizeof(size_t));
  if (resolved == NULL) {
    Py_DECREF(fast);
    return PyErr_NoMemory();
  }
  for (Py_ssize_t i = 0; i < count; ++i) {
    PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
    if (resolve_gather_index(item, bound, bound_name, &resolved[i]) < 0) {
      PyMem_Free(resolved);
      Py_DECREF(fast);
      return NULL;
    }
  }
  Py_DECREF(fast);

  const size_t want_rows = axis == 0 ? 1 : impl->rows;
  const size_t want_cols = axis == 0 ? impl->columns : 1;

  PyObject *result = NULL;
  matrix_impl *out =
      gather_output(impl, out_target, type, want_rows, want_cols, &result);
  if (out == NULL) {
    PyMem_Free(resolved);
    return NULL;
  }

  // Write phase.
  for (Py_ssize_t i = 0; i < count; ++i) {
    const size_t k = resolved[i];
    if (axis == 0) {
      out->row_ptrs[0][i] = impl->row_ptrs[k][i];
    } else {
      out->row_ptrs[i][0] = impl->row_ptrs[i][k];
    }
  }

  PyMem_Free(resolved);
  return result;
}

/* Defined alongside Matrix_ass_subscript (the scatter machinery lives next
   to the write path); forward-declared here so Matrix_put can share it. */
static int scatter_axis(matrix_impl *impl, PyObject *indices, int axis,
                        PyObject *value_op, int accumulate);

/* The along-axis scatter counterpart to scatter_axis, likewise defined next
   to the write path and forward-declared so Matrix_put_along_axis can use it.
 */
static int scatter_along_axis(matrix_impl *impl, PyObject *indices, int axis,
                              PyObject *value_op, int accumulate);

PyObject *Matrix_take(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  PyObject *indices = NULL;
  int axis = 0;
  PyObject *out = NULL;
  static char *keywords[] = {"indices", "axis", "out", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwds, "O|i$O", keywords, &indices,
                                   &axis, &out)) {
    return NULL;
  }

  if (axis < 0) {
    axis = 2 + axis;
  }

  if (axis < 0 || axis >= 2) {
    PyErr_SetString(PyExc_KeyError, "Invalid axis (must be 0 or 1)");
    return NULL;
  }

  return gather_axis(impl, indices, axis, Py_TYPE(self), out);
}

PyObject *Matrix_put(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  PyObject *indices = NULL;
  PyObject *value = NULL;
  int axis = 0;
  int accumulate = 0;
  static char *keywords[] = {"indices", "value", "axis", "accumulate", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwds, "OO|ip", keywords, &indices,
                                   &value, &axis, &accumulate)) {
    return NULL;
  }

  if (axis < 0) {
    axis = 2 + axis;
  }

  if (axis < 0 || axis >= 2) {
    PyErr_SetString(PyExc_KeyError, "Invalid axis (must be 0 or 1)");
    return NULL;
  }

  if (scatter_axis(impl, indices, axis, value, accumulate) < 0) {
    return NULL;
  }

  Py_INCREF(self);
  return (PyObject *)self;
}

PyObject *Matrix_take_along_axis(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  PyObject *indices = NULL;
  int axis = 0;
  PyObject *out = NULL;
  static char *keywords[] = {"indices", "axis", "out", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwds, "O|i$O", keywords, &indices,
                                   &axis, &out)) {
    return NULL;
  }

  if (axis < 0) {
    axis = 2 + axis;
  }

  if (axis < 0 || axis >= 2) {
    PyErr_SetString(PyExc_KeyError, "Invalid axis (must be 0 or 1)");
    return NULL;
  }

  return gather_along_axis(impl, indices, axis, Py_TYPE(self), out);
}

PyObject *Matrix_put_along_axis(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  PyObject *indices = NULL;
  PyObject *value = NULL;
  int axis = 0;
  int accumulate = 0;
  static char *keywords[] = {"indices", "value", "axis", "accumulate", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwds, "OO|ip", keywords, &indices,
                                   &value, &axis, &accumulate)) {
    return NULL;
  }

  if (axis < 0) {
    axis = 2 + axis;
  }

  if (axis < 0 || axis >= 2) {
    PyErr_SetString(PyExc_KeyError, "Invalid axis (must be 0 or 1)");
    return NULL;
  }

  if (scatter_along_axis(impl, indices, axis, value, accumulate) < 0) {
    return NULL;
  }

  Py_INCREF(self);
  return (PyObject *)self;
}

/// @brief Repeat each element/row/column ``repeats`` times consecutively.
/// @details The ``np.repeat`` / ``torch.repeat_interleave`` shape: copies run
///          *interleaved*, not tiled — ``[a, b]`` with ``repeats == 2`` becomes
///          ``[a, a, b, b]`` (contrast tiling, which gives ``[a, b, a, b]``).
///          ``axis == 0`` repeats whole rows into a ``(rows*repeats) x
///          columns`` result (each source row ``memcpy``'d ``repeats`` times);
///          ``axis == 1`` repeats each column into ``rows x
///          (columns*repeats)``;
///          ``flatten`` (the no-axis path) walks the row-major buffer and emits
///          a ``1 x (size*repeats)`` row vector. The *total* output element
///          count (``size * repeats``) is validated for ``size_t`` overflow
///          before allocation — and ``impl_new`` re-checks the final
///          ``rows * columns`` product — so a huge ``repeats`` can never
///          under-allocate and write out of bounds.
static matrix_impl *impl_repeat_interleave(matrix_impl *m, size_t repeats,
                                           int axis, bool flatten) {
  // Bound the total element count, not just the repeated dimension: for
  // axis 0/1 the result is rows*columns*repeats == size*repeats, and a
  // per-dimension guard (columns*repeats) would let the other dimension
  // wrap the product. impl_new re-checks rows*columns as a backstop.
  if (repeats != 0 && m->size > SIZE_MAX / repeats) {
    PyErr_SetString(PyExc_OverflowError, "repeat_interleave result too large");
    return NULL;
  }

  if (flatten) {
    matrix_impl *out = impl_new(1, m->size * repeats);
    if (out == NULL) {
      return NULL;
    }
    double *op = out->data;
    for (size_t i = 0; i < m->size; ++i) {
      const double v = m->data[i];
      for (size_t t = 0; t < repeats; ++t, ++op) {
        *op = v;
      }
    }
    return out;
  }

  if (axis == 0) {
    matrix_impl *out = impl_new(m->rows * repeats, m->columns);
    if (out == NULL) {
      return NULL;
    }
    for (size_t r = 0; r < m->rows; ++r) {
      const double *src = m->row_ptrs[r];
      for (size_t t = 0; t < repeats; ++t) {
        memcpy(out->row_ptrs[r * repeats + t], src,
               m->columns * sizeof(double));
      }
    }
    return out;
  }

  matrix_impl *out = impl_new(m->rows, m->columns * repeats);
  if (out == NULL) {
    return NULL;
  }
  for (size_t r = 0; r < m->rows; ++r) {
    const double *src = m->row_ptrs[r];
    double *dst = out->row_ptrs[r];
    for (size_t c = 0; c < m->columns; ++c) {
      const double v = src[c];
      for (size_t t = 0; t < repeats; ++t, ++dst) {
        *dst = v;
      }
    }
  }
  return out;
}

PyObject *Matrix_repeat_interleave(PyObject *op, PyObject *args,
                                   PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  Py_ssize_t repeats = 0;
  PyObject *axis_obj = NULL;
  static char *keywords[] = {"repeats", "axis", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "n|O", keywords, &repeats,
                                   &axis_obj)) {
    return NULL;
  }

  if (repeats < 1) {
    PyErr_SetString(PyExc_ValueError, "repeats must be a positive integer");
    return NULL;
  }

  AxisArg axis;
  if (parse_validate_normalise_axis(axis_obj, &axis) < 0) {
    return NULL;
  }

  matrix_impl *out = impl_repeat_interleave(
      impl, (size_t)repeats, axis.has_axis ? axis.axis : 0, !axis.has_axis);
  if (out == NULL) {
    return NULL;
  }

  return wrap_impl_or_free(out);
}

/* --------------------------------------------------------------------------
   topk: the k extreme elements per reduction group, in sorted order.

   A group is gathered into a scratch array of (value, original-index) pairs
   and sorted by qsort with one of two comparators. Both put any NaN last
   (a NaN is never a "top" value, matching np.sort) and break ties between
   equal values by the smaller original index, so qsort's lack of stability
   does not matter: the comparator is a strict total order and the first
   occurrence of a tied value always wins (NumPy tie-break).

   With a where= mask only the included cells (mask cell != 0.0; NaN counts
   as included) are gathered. A group with fewer than k included elements
   fills its leading slots with the available extremes and pads the rest with
   NaN values and -1 indices -- the same "no element" sentinels used by the
   masked aggregates (NaN) and masked argmin/argmax (-1).

   The indices come back in one of two shapes. By default (as_matrix=False)
   they are Python ints -- a flat list for axis=None, or a list of per-group
   lists for axis=0/1 -- directly usable in fancy indexing. With
   as_matrix=True they are instead a Matrix of the same shape as the values
   matrix, each cell the (double-valued) source index aligned with the value
   beside it; the -1 pad becomes -1.0.
   -------------------------------------------------------------------------- */
typedef struct {
  double value;
  Py_ssize_t index;
} topk_entry;

static int topk_cmp_desc(const void *pa, const void *pb) {
  const topk_entry *a = (const topk_entry *)pa;
  const topk_entry *b = (const topk_entry *)pb;
  const int na = isnan(a->value);
  const int nb = isnan(b->value);
  if (na || nb) {
    if (na && nb) {
      return (a->index < b->index) ? -1 : 1;
    }
    return na ? 1 : -1; /* NaN sorts last */
  }
  if (a->value > b->value) {
    return -1;
  }
  if (a->value < b->value) {
    return 1;
  }
  return (a->index < b->index) ? -1 : 1; /* first occurrence wins ties */
}

static int topk_cmp_asc(const void *pa, const void *pb) {
  const topk_entry *a = (const topk_entry *)pa;
  const topk_entry *b = (const topk_entry *)pb;
  const int na = isnan(a->value);
  const int nb = isnan(b->value);
  if (na || nb) {
    if (na && nb) {
      return (a->index < b->index) ? -1 : 1;
    }
    return na ? 1 : -1; /* NaN sorts last */
  }
  if (a->value < b->value) {
    return -1;
  }
  if (a->value > b->value) {
    return 1;
  }
  return (a->index < b->index) ? -1 : 1; /* first occurrence wins ties */
}

// Sort the first `m` gathered entries and emit the top `k`: the j-th slot
// takes entry j when j < m, else the (NaN, -1) pad. out_vals/out_idx are
// contiguous length-k scratch buffers the caller scatters into its result.
static void topk_sort_pad(topk_entry *scratch, size_t m, size_t k, bool largest,
                          double *out_vals, Py_ssize_t *out_idx) {
  qsort(scratch, m, sizeof(topk_entry), largest ? topk_cmp_desc : topk_cmp_asc);
  for (size_t j = 0; j < k; ++j) {
    if (j < m) {
      out_vals[j] = scratch[j].value;
      out_idx[j] = scratch[j].index;
    } else {
      out_vals[j] = NAN;
      out_idx[j] = -1;
    }
  }
}

static PyObject *topk_build_int_list(const Py_ssize_t *idx, size_t k) {
  PyObject *list = PyList_New((Py_ssize_t)k);
  if (list == NULL) {
    return NULL;
  }
  for (size_t j = 0; j < k; ++j) {
    PyObject *o = PyLong_FromSsize_t(idx[j]);
    if (o == NULL) {
      Py_DECREF(list);
      return NULL;
    }
    PyList_SET_ITEM(list, (Py_ssize_t)j, o);
  }
  return list;
}

/// @brief Gather the included candidate cells of one topk reduction group.
/// @details Group @p g is the whole buffer for axis=None, column @p g for
///          axis=0, or row @p g for axis=1. A masked-out cell (mask == 0.0;
///          NaN counts as included) is skipped. Each kept cell is written to
///          @p scratch as (value, original-index-along-the-reduced-axis).
/// @return the number of included cells (the group's effective length).
static size_t topk_gather_group(const matrix_impl *impl,
                                const matrix_impl *mask, AxisArg axis, size_t g,
                                topk_entry *scratch) {
  size_t m = 0;
  if (!axis.has_axis) {
    for (size_t i = 0; i < impl->size; ++i) {
      if (mask != NULL && mask->data[i] == 0.0) {
        continue;
      }
      scratch[m].value = impl->data[i];
      scratch[m].index = (Py_ssize_t)i;
      ++m;
    }
  } else if (axis.axis == 0) {
    for (size_t r = 0; r < impl->rows; ++r) {
      if (mask != NULL && mask->row_ptrs[r][g] == 0.0) {
        continue;
      }
      scratch[m].value = impl->row_ptrs[r][g];
      scratch[m].index = (Py_ssize_t)r;
      ++m;
    }
  } else {
    for (size_t c = 0; c < impl->columns; ++c) {
      if (mask != NULL && mask->row_ptrs[g][c] == 0.0) {
        continue;
      }
      scratch[m].value = impl->row_ptrs[g][c];
      scratch[m].index = (Py_ssize_t)c;
      ++m;
    }
  }
  return m;
}

/// @brief Scatter one group's k sorted (value, index) pairs into the outputs.
/// @details Mirrors @ref topk_gather_group's group layout. The value matrix
///          always receives the k values; @p idx_impl (the as_matrix index
///          matrix) receives the k indices as doubles when non-NULL — it is
///          NULL in the default (list) path, where indices are emitted as
///          Python lists by the caller instead.
static void topk_write_group(matrix_impl *values, matrix_impl *idx_impl,
                             AxisArg axis, size_t g, size_t kk,
                             const double *tmp_vals,
                             const Py_ssize_t *tmp_idx) {
  for (size_t j = 0; j < kk; ++j) {
    const double iv = (double)tmp_idx[j];
    if (!axis.has_axis) {
      values->data[j] = tmp_vals[j];
      if (idx_impl != NULL) {
        idx_impl->data[j] = iv;
      }
    } else if (axis.axis == 0) {
      values->row_ptrs[j][g] = tmp_vals[j];
      if (idx_impl != NULL) {
        idx_impl->row_ptrs[j][g] = iv;
      }
    } else {
      values->row_ptrs[g][j] = tmp_vals[j];
      if (idx_impl != NULL) {
        idx_impl->row_ptrs[g][j] = iv;
      }
    }
  }
}

/// @brief Pack the (values, indices) result tuple, consuming both operands.
/// @details Wraps @p values into a Matrix and packs it with the already-built
///          @p index_obj. Balances references on every path: on any failure
///          the half-built pieces are released and NULL is returned. @p values
///          is consumed (wrapped or freed); @p index_obj's reference is stolen
///          into the tuple (or released on failure).
static PyObject *topk_pack_result(matrix_impl *values, PyObject *index_obj) {
  if (index_obj == NULL) {
    impl_free(values);
    return NULL;
  }
  PyObject *values_obj = wrap_impl_or_free(values);
  if (values_obj == NULL) {
    Py_DECREF(index_obj);
    return NULL;
  }
  PyObject *tuple = PyTuple_Pack(2, values_obj, index_obj);
  Py_DECREF(values_obj);
  Py_DECREF(index_obj);
  return tuple;
}

PyObject *Matrix_topk(PyObject *op, PyObject *args, PyObject *kwds) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  Py_ssize_t k = 0;
  PyObject *axis_obj = NULL;
  int largest = 1;
  PyObject *where_obj = NULL;
  int as_matrix = 0;
  static char *keywords[] = {"k",     "axis",      "largest",
                             "where", "as_matrix", NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, "n|OpOp", keywords, &k,
                                   &axis_obj, &largest, &where_obj,
                                   &as_matrix)) {
    return NULL;
  }

  if (k < 1) {
    PyErr_SetString(PyExc_ValueError, "k must be a positive integer");
    return NULL;
  }

  AxisArg axis;
  if (parse_validate_normalise_axis(axis_obj, &axis) < 0) {
    return NULL;
  }

  matrix_impl *mask = NULL;
  if (where_obj != NULL && where_obj != Py_None) {
    mask = unwrap_mask(where_obj, impl);
    if (mask == NULL) {
      return NULL;
    }
  }

  const size_t kk = (size_t)k;
  const size_t rows = impl->rows;
  const size_t cols = impl->columns;

  // The reduced axis must be at least k long (counting every cell, masked or
  // not): flattened size for axis=None, rows for axis=0, columns for axis=1.
  const size_t axis_len =
      !axis.has_axis ? impl->size : (axis.axis == 0 ? rows : cols);
  if (kk > axis_len) {
    PyErr_Format(PyExc_ValueError,
                 "k (%zu) cannot exceed the length of the reduced axis (%zu)",
                 kk, axis_len);
    return NULL;
  }

  // One scratch (value, index) buffer sized to the largest group, plus two
  // contiguous length-k staging buffers reused across groups.
  const size_t group_max =
      !axis.has_axis ? impl->size : (axis.axis == 0 ? rows : cols);
  topk_entry *scratch = PyMem_Malloc(group_max * sizeof(topk_entry));
  double *tmp_vals = PyMem_Malloc(kk * sizeof(double));
  Py_ssize_t *tmp_idx = PyMem_Malloc(kk * sizeof(Py_ssize_t));
  if (scratch == NULL || tmp_vals == NULL || tmp_idx == NULL) {
    PyMem_Free(scratch);
    PyMem_Free(tmp_vals);
    PyMem_Free(tmp_idx);
    return PyErr_NoMemory();
  }

  PyObject *result = NULL;

  // Unified over all three axis layouts: `groups` reduction groups, each
  // producing k (value, index) pairs scattered into a values/index matrix of
  // shape (vrows x vcols). axis=None is the single flat group -> (1 x k);
  // axis=0 reduces down the rows -> (k x cols); axis=1 across the columns ->
  // (rows x k).
  size_t groups, vrows, vcols;
  if (!axis.has_axis) {
    groups = 1;
    vrows = 1;
    vcols = kk;
  } else if (axis.axis == 0) {
    groups = cols;
    vrows = kk;
    vcols = cols;
  } else {
    groups = rows;
    vrows = rows;
    vcols = kk;
  }

  matrix_impl *values = impl_new(vrows, vcols);
  if (values == NULL) {
    goto done;
  }

  // Default index form is a list (a same-length list[int] for axis=None, or a
  // list of `groups` per-group int lists for an axis). Pass as_matrix=True for
  // a same-shape Matrix of float index positions (idx_impl) instead.
  matrix_impl *idx_impl = NULL;
  PyObject *index_lists = NULL;
  if (as_matrix) {
    idx_impl = impl_new(vrows, vcols);
    if (idx_impl == NULL) {
      impl_free(values);
      goto done;
    }
  } else if (axis.has_axis) {
    index_lists = PyList_New((Py_ssize_t)groups);
    if (index_lists == NULL) {
      impl_free(values);
      goto done;
    }
  }

  for (size_t g = 0; g < groups; ++g) {
    size_t m = topk_gather_group(impl, mask, axis, g, scratch);
    topk_sort_pad(scratch, m, kk, largest, tmp_vals, tmp_idx);
    topk_write_group(values, idx_impl, axis, g, kk, tmp_vals, tmp_idx);
    if (!as_matrix && axis.has_axis) {
      PyObject *sub = topk_build_int_list(tmp_idx, kk);
      if (sub == NULL) {
        Py_DECREF(index_lists);
        impl_free(values);
        goto done;
      }
      PyList_SET_ITEM(index_lists, (Py_ssize_t)g, sub);
    }
  }

  PyObject *index_obj;
  if (as_matrix) {
    index_obj = wrap_impl_or_free(idx_impl);
  } else if (axis.has_axis) {
    index_obj = index_lists;
  } else {
    // Flat list form: groups == 1, so tmp_idx still holds the single group's
    // sorted indices.
    index_obj = topk_build_int_list(tmp_idx, kk);
  }
  result = topk_pack_result(values, index_obj);

done:
  PyMem_Free(scratch);
  PyMem_Free(tmp_vals);
  PyMem_Free(tmp_idx);
  return result;
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
  if (!impl_check_acquired(lhs->impl, true) ||
      !impl_check_acquired(rhs->impl, true)) {
    return NULL;
  }

  if (impl_allclose(lhs->impl, rhs->impl, rtol, atol, equal_nan)) {
    Py_RETURN_TRUE;
  }

  Py_RETURN_FALSE;
}

/// @brief Resolve a where() value operand to a scalar or a same-shape matrix.
/// @details ``op`` may be a Python scalar (bound into ``*scalar`` with
///          ``*mat`` left NULL), a Matrix, or a list/tuple of numbers (taken
///          as a ``1xN`` row vector). A matrix or coerced sequence must match
///          ``rows`` x ``columns`` and is returned in ``*mat`` with a fresh
///          reference the caller must release. Any other type sets a
///          ``TypeError`` and a shape mismatch sets a ``ValueError``; both
///          return -1.
static int where_resolve_operand(PyObject *op, size_t rows, size_t columns,
                                 double *scalar, matrix_impl **mat) {
  *mat = NULL;
  if (unwrap_double(op, scalar)) {
    return 0;
  }
  if (Py_TYPE(op) != LOCAL_STATE->matrix_type && !PyList_Check(op) &&
      !PyTuple_Check(op)) {
    PyErr_SetString(PyExc_TypeError,
                    "where() operands must each be a Matrix, a scalar, or a "
                    "list/tuple of numbers");
    return -1;
  }
  matrix_impl *impl = unwrap_matrix(op, false);
  if (impl == NULL) {
    return -1;
  }
  if (impl->rows != rows || impl->columns != columns) {
    IMPL_DECREF(impl);
    PyErr_SetString(PyExc_ValueError,
                    "where() matrix or list/tuple operands must match the "
                    "mask shape");
    return -1;
  }
  *mat = impl;
  return 0;
}

/// @brief Select between two operands element-wise on a truthy mask.
/// @details ``where(mask, a, b)`` returns a fresh matrix taking ``a`` where the
///          corresponding ``mask`` element is non-zero and ``b`` elsewhere.
///          ``a`` and ``b`` may each be a scalar, a matrix matching the mask's
///          shape, or a list/tuple of numbers (taken as a ``1xN`` row vector
///          that must then match the mask shape); other shapes raise
///          ``ValueError``. A 1x1 matrix is treated as a matrix (not a scalar)
///          and so must match the mask shape. NaN mask elements are non-zero
///          and select ``a``.
static PyObject *Matrix_where(PyObject *cls, PyObject *args, PyObject *kwds) {
  PyObject *mask_op = NULL;
  PyObject *a_op = NULL;
  PyObject *b_op = NULL;
  PyObject *out_target = NULL;
  static char *kwlist[] = {"", "", "", "out", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwds, "O!OO|$O", kwlist, cls, &mask_op,
                                   &a_op, &b_op, &out_target)) {
    return NULL;
  }

  matrix_impl *mask = ((MatrixObject *)mask_op)->impl;
  if (!impl_check_acquired(mask, true)) {
    return NULL;
  }
  const size_t rows = mask->rows;
  const size_t columns = mask->columns;

  double a_scalar = 0.0;
  double b_scalar = 0.0;
  matrix_impl *a_mat = NULL;
  matrix_impl *b_mat = NULL;
  matrix_impl *out = NULL;
  PyObject *result = NULL;

  if (where_resolve_operand(a_op, rows, columns, &a_scalar, &a_mat) < 0) {
    goto done;
  }
  if (where_resolve_operand(b_op, rows, columns, &b_scalar, &b_mat) < 0) {
    goto done;
  }

  const bool fresh = (out_target == NULL || out_target == Py_None);
  if (fresh) {
    out = impl_new(rows, columns);
  } else {
    out = use_out_target(out_target, &result, rows, columns);
  }
  if (out == NULL) {
    goto done;
  }

  const double *mp = mask->data;
  double *outp = out->data;
  for (size_t i = 0; i < out->size; ++i, ++mp, ++outp) {
    const double av = a_mat != NULL ? a_mat->data[i] : a_scalar;
    const double bv = b_mat != NULL ? b_mat->data[i] : b_scalar;
    *outp = (*mp != 0.0) ? av : bv;
  }

  if (fresh) {
    result = wrap_impl_or_free(out);
  }

done:
  IMPL_DECREF(a_mat);
  IMPL_DECREF(b_mat);
  return result;
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

static PyObject *Matrix_full(PyObject *cls, PyObject *args) {
  PyObject *size = NULL;
  double value;

  if (!PyArg_ParseTuple(args, "Od", &size, &value)) {
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
    *ptr = value;
  }

  return wrap_impl_or_free(impl);
}

/// @brief splitmix64: draw the next 64-bit value and advance the state.
/// @details A single-word PRNG kept per interpreter in the module state, so
///          parallel worker sub-interpreters draw from independent streams
///          and never share mutable RNG state (the old ``rand()``/``srand()``
///          were process-global -- non-reproducible across workers and a data
///          race off glibc / on free-threaded builds). splitmix64 is chosen
///          for its tiny footprint and good distribution, and mixes even
///          low-entropy seeds well so ``seed(1)`` and ``seed(2)`` diverge.
static inline uint64_t prng_next_u64(uint64_t *state) {
  uint64_t z = (*state += 0x9E3779B97F4A7C15ULL);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
  return z ^ (z >> 31);
}

/// @brief A uniform double in [0, 1) using the top 53 bits (full mantissa).
static inline double prng_next_unit(uint64_t *state) {
  return (double)(prng_next_u64(state) >> 11) * (1.0 / 9007199254740992.0);
}

static double sample_uniform(uint64_t *rng, double min, double max) {
  double val = prng_next_unit(rng);
  return (val * (max - min)) + min;
}

static void sample_normal(uint64_t *rng, double *values, size_t n, double mean,
                          double stddev) {
  size_t i = 0;
  while (i < n) {
    double u = sample_uniform(rng, -1, 1);
    double v = sample_uniform(rng, -1, 1);
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

  _math_module_state *state = math_local_state();
  if (state == NULL) {
    return NULL;
  }

  if (Py_IsNone(size)) {
    double value = 0;
    sample_normal(&state->prng_state, &value, 1, mean, stddev);
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

  sample_normal(&state->prng_state, impl->data, impl->size, mean, stddev);

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

  _math_module_state *state = math_local_state();
  if (state == NULL) {
    return NULL;
  }

  if (Py_IsNone(size)) {
    return PyFloat_FromDouble(
        sample_uniform(&state->prng_state, minval, maxval));
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
    *ptr = sample_uniform(&state->prng_state, minval, maxval);
  }

  return wrap_impl_or_free(impl);
}

static PyObject *Matrix_seed(PyObject *cls, PyObject *args) {
  unsigned long value;

  if (!PyArg_ParseTuple(args, "k", &value)) {
    return NULL;
  }

  _math_module_state *state = math_local_state();
  if (state == NULL) {
    return NULL;
  }

  // Seed this interpreter's stream only. splitmix64 mixes the raw seed
  // internally, so distinct seeds diverge immediately.
  state->prng_state = (uint64_t)value;

  Py_RETURN_NONE;
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

static PyObject *Matrix_concat(PyObject *cls, PyObject *args,
                               PyObject *kwargs) {
  PyObject *matrices = NULL;
  int axis = 0;

  static char *kwlist[] = {"values", "axis", NULL};

  if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|i", kwlist, &matrices,
                                   &axis)) {
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

/// @brief Pickle support: reduce a matrix to its raw double buffer.
/// @details Returns ``(_matrix_unpickle, (rows, columns, payload))`` where
///          ``payload`` is the native-endian byte image of the contiguous
///          row-major ``double`` data. Reconstruction is a single ``memcpy``,
///          so pickling cost is linear in the element count with no per-element
///          Python object churn. ``copy.copy`` and ``copy.deepcopy`` route
///          through the same path. The current interpreter must own the matrix.
static PyObject *Matrix_reduce(PyObject *op, PyObject *Py_UNUSED(dummy)) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (impl == NULL) {
    PyErr_SetString(PyExc_ValueError, "Cannot pickle an uninitialized matrix");
    return NULL;
  }

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  _math_module_state *state = math_local_state();
  if (state == NULL) {
    return NULL;
  }
  PyObject *rebuild = state->matrix_unpickle;

  PyObject *payload = PyBytes_FromStringAndSize(
      (const char *)impl->data, (Py_ssize_t)(impl->size * sizeof(double)));
  if (payload == NULL) {
    return NULL;
  }

  return Py_BuildValue("(O(nnN))", rebuild, (Py_ssize_t)impl->rows,
                       (Py_ssize_t)impl->columns, payload);
}

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

static inline enum BinaryOps swap_right(enum BinaryOps op) {
  switch (op) {
  case Subtract:
    return RSubtract;

  case Divide:
    return RDivide;

  case Less:
    return Greater;

  case Greater:
    return Less;

  case LessEqual:
    return GreaterEqual;

  case GreaterEqual:
    return LessEqual;

  default:
    return op;
  }
}

static int Matrix_binary_op(PyObject *lhs_op, PyObject *rhs_op,
                            PyObject **out_op, PyObject *out_target,
                            enum BinaryOps op, bool inplace) {
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
    lhs = unwrap_matrix(mat_op, false);
    if (lhs == NULL) {
      goto error;
    }

    matrix_impl *out = set_output(lhs, mat_op, out_op, out_target, inplace);
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

  // A 1x1 matrix is a scalar: route it through the scalar kernels so it
  // broadcasts against any shape, exactly like a Python float operand.
  // Check rhs first so 1x1 op= 1x1 stays in-place-safe.
  if (rhs->size == 1) {
    matrix_impl *out = set_output(lhs, lhs_op, out_op, out_target, inplace);
    if (out == NULL) {
      goto error;
    }
    dispatch_bin_scalar(lhs, rhs->data[0], out, op);
    goto exit;
  }
  if (lhs->size == 1) {
    if (inplace) {
      PyErr_SetString(PyExc_ValueError,
                      "in-place scalar broadcast would change operand shape");
      goto error;
    }
    matrix_impl *out = set_output(rhs, rhs_op, out_op, out_target, inplace);
    if (out == NULL) {
      goto error;
    }
    dispatch_bin_scalar(rhs, lhs->data[0], out, swap_right(op));
    goto exit;
  }

  const char *mismatch_error = "Dimension mismatch between operands";

  if (lhs->rows != rhs->rows) {
    if (lhs->columns != rhs->columns) {
      matrix_impl *colvec;
      matrix_impl *rowvec;
      if (lhs->columns == 1 && rhs->rows == 1) {
        colvec = lhs;
        rowvec = rhs;
      } else if (lhs->rows == 1 && rhs->columns == 1) {
        colvec = rhs;
        rowvec = lhs;
        op = swap_right(op);
      } else {
        PyErr_SetString(PyExc_ValueError, mismatch_error);
        goto error;
      }

      if (inplace) {
        PyErr_SetString(PyExc_ValueError,
                        "in-place outer broadcast would change operand shape");
        goto error;
      }

      matrix_impl *out;
      if (out_target != NULL && out_target != Py_None) {
        out = use_out_target(out_target, out_op, colvec->rows, rowvec->columns);
        if (out == NULL) {
          goto error;
        }
      } else {
        out = impl_new(colvec->rows, rowvec->columns);
        if (out == NULL) {
          goto error;
        }
        // Wrap as the interpreter's canonical Matrix type. Deriving the type
        // from lhs_op would be unsafe: in the reflected outer case lhs_op is a
        // sequence unwrap_matrix coerced into a matrix, not a Matrix object.
        *out_op = wrap_matrix(LOCAL_STATE->matrix_type, out);
        if (*out_op == NULL) {
          impl_free(out);
          goto error;
        }
      }

      dispatch_bin_outer(colvec, rowvec, out, op);
      goto exit;
    }

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
      PyErr_SetString(PyExc_ValueError, mismatch_error);
      goto error;
    }

    matrix_impl *out = set_output(matrix, mat_op, out_op, out_target, inplace);
    if (out == NULL) {
      goto error;
    }

    dispatch_bin_rowwise(matrix, vector, out, op);
    goto exit;
  }

  if (lhs->columns != rhs->columns) {
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
      PyErr_SetString(PyExc_ValueError, mismatch_error);
      goto error;
    }

    matrix_impl *out = set_output(matrix, mat_op, out_op, out_target, inplace);
    if (out == NULL) {
      goto error;
    }

    dispatch_bin_columnwise(matrix, vector, out, op);
    goto exit;
  }

  matrix_impl *out = set_output(lhs, lhs_op, out_op, out_target, inplace);
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
    if (Matrix_binary_op(lhs, rhs, &out, NULL, binary, false) < 0) {           \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

#define MATRIX_INPLACE_BINARY_OP(binary)                                       \
  static PyObject *Matrix_inplace_##binary##_op(PyObject *lhs,                 \
                                                PyObject *rhs) {               \
    PyObject *out = NULL;                                                      \
    if (Matrix_binary_op(lhs, rhs, &out, NULL, binary, true) < 0) {            \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

/* MATRIX_COMPARE_METHOD stamps a named element-wise comparison method that
   reuses Matrix_binary_op for the full broadcast routing (same-shape, scalar,
   1x1, row-vector, column-vector). Always invoked as self.<name>(other), so
   self is the left operand and other the right. Returns a fresh 0/1 mask
   matrix; the comparison kernels compile branch-free to cmppd + andpd. A
   keyword-only ``out=`` target writes the mask into a caller-supplied
   same-shape matrix (allocation-free) and returns it; out may alias self. */
#define MATRIX_COMPARE_METHOD(ENUM)                                            \
  static PyObject *Matrix_##ENUM##_compare(PyObject *self, PyObject *args,     \
                                           PyObject *kwds) {                   \
    PyObject *other = NULL;                                                    \
    PyObject *out_target = NULL;                                               \
    static char *kwlist[] = {"", "out", NULL};                                 \
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "O|$O", kwlist, &other,       \
                                     &out_target)) {                           \
      return NULL;                                                             \
    }                                                                          \
    PyObject *out = NULL;                                                      \
    if (Matrix_binary_op(self, other, &out, out_target, ENUM, false) < 0) {    \
      return NULL;                                                             \
    }                                                                          \
    return out;                                                                \
  }

/* MATRIX_NAMED_BINARY_METHOD stamps a named element-wise arithmetic method
   (add / subtract / multiply / divide) that reuses Matrix_binary_op for the
   full broadcast routing, exactly like the ``+`` / ``-`` / ``*`` / ``/``
   operators, but
   additionally accepts a keyword-only ``out=`` target. With ``out`` the
   result is written into a caller-supplied matrix (allocation-free, numpy
   ufunc convention) instead of a fresh allocation, and that matrix is
   returned; ``out`` may alias an input. Invoked as self.<name>(other), so
   self is the left operand. The result is bit-for-bit identical to the
   operator form because the same per-op kernels run either way. */
#define MATRIX_NAMED_BINARY_METHOD(name, ENUM)                                 \
  static PyObject *Matrix_##name##_method(PyObject *self, PyObject *args,      \
                                          PyObject *kwds) {                    \
    PyObject *other = NULL;                                                    \
    PyObject *out_target = NULL;                                               \
    static char *kwlist[] = {"", "out", NULL};                                 \
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "O|$O", kwlist, &other,       \
                                     &out_target)) {                           \
      return NULL;                                                             \
    }                                                                          \
    PyObject *out = NULL;                                                      \
    if (Matrix_binary_op(self, other, &out, out_target, ENUM, false) < 0) {    \
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
    matrix_impl *out = set_output(impl, op, &out_op, NULL, false);             \
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

MATRIX_COMPARE_METHOD(Less)
MATRIX_COMPARE_METHOD(LessEqual)
MATRIX_COMPARE_METHOD(Greater)
MATRIX_COMPARE_METHOD(GreaterEqual)
MATRIX_COMPARE_METHOD(Equal)
MATRIX_COMPARE_METHOD(NotEqual)

MATRIX_NAMED_BINARY_METHOD(add, Add)
MATRIX_NAMED_BINARY_METHOD(subtract, Subtract)
MATRIX_NAMED_BINARY_METHOD(multiply, Multiply)
MATRIX_NAMED_BINARY_METHOD(divide, Divide)

/// @brief Lexicographic three-way compare of two equal-length buffers.
/// @details Walks both buffers in row-major order and returns at the first
///          element where a strict ``<`` or ``>`` holds: ``-1`` if ``lhs`` is
///          smaller there, ``+1`` if larger. Elements that are equal — or
///          where neither ordering holds, as with NaN — are skipped, so a
///          run of NaNs does not decide the result. Returns ``0`` when no
///          element decides (the buffers compare equal element-wise).
static int impl_lexcompare(const double *lhs, const double *rhs, size_t n) {
  for (size_t i = 0; i < n; ++i) {
    if (lhs[i] < rhs[i]) {
      return -1;
    }
    if (lhs[i] > rhs[i]) {
      return 1;
    }
  }
  return 0;
}

/// @brief Map a three-way compare result and a Py_RichCompare opid to a bool.
static PyObject *richcompare_from_sign(int sign, int opid) {
  int result;
  switch (opid) {
  case Py_LT:
    result = sign < 0;
    break;
  case Py_LE:
    result = sign <= 0;
    break;
  case Py_GT:
    result = sign > 0;
    break;
  case Py_GE:
    result = sign >= 0;
    break;
  case Py_EQ:
    result = sign == 0;
    break;
  case Py_NE:
    result = sign != 0;
    break;
  default:
    Py_RETURN_NOTIMPLEMENTED;
  }
  return PyBool_FromLong(result);
}

/// @brief Lexicographic compare of two matrices, total for ``==`` / ``!=``.
/// @details A shape mismatch makes ``==`` ``False`` and ``!=`` ``True``; the
///          ordering operators raise ``ValueError`` instead. Equal-shape
///          buffers compare lexicographically in row-major order.
static PyObject *richcompare_matrix(const matrix_impl *lhs,
                                    const matrix_impl *rhs, int opid) {
  if (lhs->rows != rhs->rows || lhs->columns != rhs->columns) {
    if (opid == Py_EQ) {
      Py_RETURN_FALSE;
    }
    if (opid == Py_NE) {
      Py_RETURN_TRUE;
    }
    PyErr_SetString(PyExc_ValueError,
                    "ordering comparison requires a matrix, list, or tuple "
                    "operand of the same shape (or a scalar operand)");
    return NULL;
  }
  int sign = impl_lexcompare(lhs->data, rhs->data, lhs->size);
  return richcompare_from_sign(sign, opid);
}

/// @brief Lexicographic ``bool`` comparison operators for Matrix.
/// @details Returns a single Python ``bool``, comparing element by element in
///          row-major order like a list or tuple: the first element where a
///          strict ordering holds decides, and all-equal compares equal. The
///          right operand may be a same-shape matrix, a Python scalar (which
///          broadcasts to ``self``'s shape), or a list/tuple of numbers (taken
///          as a ``1xN`` row vector that must then match ``self``'s shape).
///          ``==`` / ``!=`` are total: a shape mismatch makes them
///          ``False`` / ``True``, an operand that is not a matrix, scalar, or
///          list/tuple yields ``NotImplemented`` so Python falls back to
///          identity, and a list/tuple that cannot be coerced (empty, or
///          containing a non-number) likewise makes ``==`` ``False`` /
///          ``!=`` ``True`` instead of raising — so ``matrix in some_list``
///          still works. They are not total over ownership: comparing a matrix
///          not owned by the current interpreter raises ``RuntimeError``.
///          Defining value equality makes Matrix
///          unhashable (CPython disables the inherited ``__hash__``), which is
///          correct for a mutable type. The ordering operators
///          (``<`` ``<=`` ``>`` ``>=``) raise ``ValueError`` on a shape
///          mismatch (and propagate the coercion error — ``ValueError`` for an
///          empty list, ``TypeError`` for a non-number element). NaN never
///          decides an ordering (neither ``<`` nor ``>``
///          holds, so the element is skipped); an all-NaN matrix therefore
///          compares ``==`` equal to itself — unlike the ``equal`` mask, where
///          ``NaN == x`` is ``0.0``. A 1x1 matrix is NOT treated as a scalar
///          here (unlike the arithmetic and mask paths); it only compares
///          against another 1x1.
static PyObject *Matrix_richcompare(PyObject *self_op, PyObject *other_op,
                                    int opid) {
  MatrixObject *self = (MatrixObject *)self_op;
  matrix_impl *lhs = self->impl;
  if (!impl_check_acquired(lhs, true)) {
    return NULL;
  }

  double scalar;
  if (unwrap_double(other_op, &scalar)) {
    int sign = 0;
    for (size_t i = 0; i < lhs->size; ++i) {
      if (lhs->data[i] < scalar) {
        sign = -1;
        break;
      }
      if (lhs->data[i] > scalar) {
        sign = 1;
        break;
      }
    }
    return richcompare_from_sign(sign, opid);
  }

  if (Py_TYPE(other_op) == LOCAL_STATE->matrix_type) {
    matrix_impl *rhs = ((MatrixObject *)other_op)->impl;
    if (!impl_check_acquired(rhs, true)) {
      return NULL;
    }
    return richcompare_matrix(lhs, rhs, opid);
  }

  if (PyList_Check(other_op) || PyTuple_Check(other_op)) {
    matrix_impl *coerced = impl_new_from_sequence(other_op, false);
    if (coerced == NULL) {
      // == / != stay total over coercion failure: an empty or non-numeric
      // list/tuple counts as "not equal" rather than raising, so
      // ``matrix in some_list`` works. Only ValueError/TypeError are swallowed;
      // a MemoryError still propagates so an alloc failure is never read as
      // equal.
      if ((opid == Py_EQ || opid == Py_NE) &&
          (PyErr_ExceptionMatches(PyExc_ValueError) ||
           PyErr_ExceptionMatches(PyExc_TypeError))) {
        PyErr_Clear();
        if (opid == Py_EQ) {
          Py_RETURN_FALSE;
        }
        Py_RETURN_TRUE;
      }
      return NULL;
    }
    PyObject *result = richcompare_matrix(lhs, coerced, opid);
    impl_free(coerced);
    return result;
  }

  Py_RETURN_NOTIMPLEMENTED;
}

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
    PyErr_SetString(PyExc_ValueError, "M0xN0 @ M1xN1  N0 != M1");
    goto error;
  }

  out = impl_new(lhs->rows, rhs->columns);
  if (out == NULL) {
    goto error;
  }

  impl_matmul(lhs, rhs, out);
  // Wrap as the interpreter's canonical Matrix type, never Py_TYPE(lhs_op):
  // lhs_op may be a sequence unwrap_matrix coerced into lhs (e.g.
  // ``[1, 2, 3] @ matrix``), in which case it is not a Matrix object.
  out_op = wrap_matrix(LOCAL_STATE->matrix_type, out);
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
  rows->scalar = true;
  columns->start = 0;
  columns->stop = (Py_ssize_t)impl->columns;
  columns->step = 1;
  columns->count = impl->columns;
  columns->scalar = true;
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

/// @brief True iff ``obj`` is the bare ``slice(None, None, None)`` (``:``)
/// @details Only the bare ``:`` qualifies as a full selector alongside a list
///          key; an explicit range such as ``0:R`` does not.
static int is_full_slice(PyObject *obj) {
  if (!PySlice_Check(obj)) {
    return 0;
  }

  PySliceObject *slice = (PySliceObject *)obj;
  return slice->start == Py_None && slice->stop == Py_None &&
         slice->step == Py_None;
}

/// @brief How a subscript key maps onto list-based fancy indexing.
typedef enum {
  FANCY_KEY_NONE = 0,   ///< Not a fancy key; fall through to range parsing.
  FANCY_KEY_AXIS0,      ///< A list selects whole rows.
  FANCY_KEY_AXIS1,      ///< A list selects whole columns.
  FANCY_KEY_UNSUPPORTED ///< A list-bearing key in an unsupported shape.
} fancy_key_kind;

/// @brief Classify a subscript key for list-based fancy indexing
/// @details Shared by the read and write paths so both accept the same
///          shapes: ``m[[rows]]``, ``m[[rows], :]``, ``m[:, [cols]]``. The
///          int/slice hot path returns ``FANCY_KEY_NONE``. On
///          ``FANCY_KEY_UNSUPPORTED`` no exception is set — the caller raises.
/// @param key The subscript key.
/// @param list_out On an axis match, set to the borrowed index-list object.
/// @return The fancy-key classification.
static fancy_key_kind classify_fancy_key(PyObject *key, PyObject **list_out) {
  if (PyList_Check(key)) {
    *list_out = key;
    return FANCY_KEY_AXIS0;
  }

  if (PyTuple_Check(key) && PyTuple_GET_SIZE(key) == 2) {
    PyObject *k0 = PyTuple_GET_ITEM(key, 0);
    PyObject *k1 = PyTuple_GET_ITEM(key, 1);
    int list0 = PyList_Check(k0);
    int list1 = PyList_Check(k1);
    if (list0 || list1) {
      if (list0 && !list1 && is_full_slice(k1)) {
        *list_out = k0;
        return FANCY_KEY_AXIS0;
      }
      if (list1 && !list0 && is_full_slice(k0)) {
        *list_out = k1;
        return FANCY_KEY_AXIS1;
      }
      return FANCY_KEY_UNSUPPORTED;
    }
  }

  return FANCY_KEY_NONE;
}

PyObject *Matrix_subscript(PyObject *op, PyObject *key) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }

  // Dispatch fancy indexing before read_key_ranges; non-list keys classify
  // as FANCY_KEY_NONE and fall through unchanged.
  PyObject *index_list;
  switch (classify_fancy_key(key, &index_list)) {
  case FANCY_KEY_AXIS0:
    return gather_axis(impl, index_list, 0, Py_TYPE(op), NULL);
  case FANCY_KEY_AXIS1:
    return gather_axis(impl, index_list, 1, Py_TYPE(op), NULL);
  case FANCY_KEY_UNSUPPORTED:
    PyErr_SetString(PyExc_IndexError,
                    "fancy indexing supports only m[[rows]], m[[rows], :], or "
                    "m[:, [cols]]; use take() for other selections");
    return NULL;
  case FANCY_KEY_NONE:
    break;
  }

  range rows;
  range columns;
  if (read_key_ranges(key, &rows, &columns, impl) < 0) {
    return NULL;
  }

  // Collapse to a Python float only for an all-integer key that selects a
  // single cell. A slice anywhere in the key (even a length-1 one) keeps the
  // result a Matrix, so m[0:1, 0:1] stays 1x1 while m[i, j] stays a scalar.
  if (rows.scalar && columns.scalar && rows.count == 1 && columns.count == 1) {
    return PyFloat_FromDouble(impl->row_ptrs[rows.start][columns.start]);
  }

  PyTypeObject *type = Py_TYPE(op);
  MatrixObject *out = (MatrixObject *)type->tp_alloc(type, 0);
  if (out == NULL) {
    return NULL;
  }

  out->impl = impl_new(rows.count, columns.count);
  if (out->impl == NULL) {
    Py_DECREF(out);
    return NULL;
  }
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
  if (out->impl == NULL) {
    Py_DECREF(out);
    return NULL;
  }
  IMPL_INCREF(out->impl);
  impl_get(impl, &rows, &columns, out->impl);
  return (PyObject *)out;
}

static PyObject *Matrix_iter(PyObject *op) { return PySeqIter_New(op); }

// Lazy iterator returned by Matrix.values(): a strong ref to the source matrix
// plus a row-major cursor. One float is boxed per step; the backing store is
// already row-major so the cursor is a flat data[0..size) walk.
typedef struct {
  PyObject_HEAD MatrixObject *matrix;
  size_t cursor;
} MatrixValuesIterObject;

static void MatrixValuesIter_dealloc(PyObject *op) {
  MatrixValuesIterObject *it = (MatrixValuesIterObject *)op;
  PyTypeObject *type = Py_TYPE(op);
  Py_XDECREF(it->matrix);
  type->tp_free(op);
  Py_DECREF(type);
}

static PyObject *MatrixValuesIter_next(PyObject *op) {
  MatrixValuesIterObject *it = (MatrixValuesIterObject *)op;
  matrix_impl *impl = it->matrix->impl;

  // Re-check ownership every step: the matrix may be released between yields
  // (e.g. a values() iterator outliving the @when body that produced it).
  if (!impl_check_acquired(impl, true)) {
    return NULL;
  }
  if (it->cursor >= impl->size) {
    return NULL; // StopIteration
  }
  return PyFloat_FromDouble(impl->data[it->cursor++]);
}

static PyObject *MatrixValuesIter_self(PyObject *op) {
  Py_INCREF(op);
  return op;
}

static PyType_Slot MatrixValuesIter_slots[] = {
    {Py_tp_dealloc, MatrixValuesIter_dealloc},
    {Py_tp_iter, MatrixValuesIter_self},
    {Py_tp_iternext, MatrixValuesIter_next},
    {0, NULL},
};

static PyType_Spec MatrixValuesIter_Spec = {
    .name = "bocpy._math.MatrixValuesIter",
    .basicsize = sizeof(MatrixValuesIterObject),
    .itemsize = 0,
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_IMMUTABLETYPE,
    .slots = MatrixValuesIter_slots};

static PyObject *Matrix_values(PyObject *op, PyObject *Py_UNUSED(ignored)) {
  if (!impl_check_acquired(((MatrixObject *)op)->impl, true)) {
    return NULL;
  }
  PyTypeObject *type = LOCAL_STATE->values_iter_type;
  MatrixValuesIterObject *it =
      (MatrixValuesIterObject *)type->tp_alloc(type, 0);
  if (it == NULL) {
    return NULL;
  }
  Py_INCREF(op);
  it->matrix = (MatrixObject *)op;
  it->cursor = 0;
  return (PyObject *)it;
}

/// @brief Resolve an entire index list into a validated ``size_t`` array.
/// @details The scatter (write) counterpart of ``gather_axis``'s per-element
///          resolution. Every index is validated (negative-aware,
///          bounds-checked) *before* the caller writes any element, so the
///          write phase is infallible — the property that licenses the
///          ``memcpy`` row kernel with no mid-loop error exit. Indices are
///          written into ``stack`` when ``count <= stack_size`` (zero heap
///          allocation for the common small scatter) and into a fresh heap
///          buffer otherwise; on success ``*out_idx`` points at whichever
///          buffer was used, so the caller frees it only when it differs
///          from ``stack``. An empty sequence raises ``IndexError``
///          (matching gather). On any failure the heap buffer, if one was
///          allocated, is freed here before returning.
/// @param indices The list/sequence of index objects.
/// @param dim Size of the axis being indexed, for bounds checking.
/// @param axis_name ``"row"`` or ``"column"`` for error messages.
/// @param stack Caller-provided fixed buffer used when small enough.
/// @param stack_size Number of slots in ``stack``.
/// @param out_idx Receives the resolved index array (``stack`` or heap).
/// @param out_count Receives the number of resolved indices.
/// @return 0 on success; -1 with an exception set on failure.
static int resolve_index_list(PyObject *indices, size_t dim,
                              const char *axis_name, size_t *stack,
                              size_t stack_size, size_t **out_idx,
                              size_t *out_count) {
  const char *err_msg =
      "Indices must be specified as a list or a tuple of ints";
  PyObject *fast = PySequence_Fast(indices, err_msg);
  if (fast == NULL) {
    return -1;
  }

  Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
  if (count == 0) {
    Py_DECREF(fast);
    PyErr_SetString(PyExc_IndexError, "index sequence must not be empty");
    return -1;
  }

  size_t *idx = stack;
  if ((size_t)count > stack_size) {
    idx = PyMem_Malloc((size_t)count * sizeof(size_t));
    if (idx == NULL) {
      Py_DECREF(fast);
      PyErr_NoMemory();
      return -1;
    }
  }

  for (Py_ssize_t i = 0; i < count; ++i) {
    PyObject *item = PySequence_Fast_GET_ITEM(fast, i);
    if (resolve_gather_index(item, dim, axis_name, &idx[i]) < 0) {
      if (idx != stack) {
        PyMem_Free(idx);
      }
      Py_DECREF(fast);
      return -1;
    }
  }

  Py_DECREF(fast);
  *out_idx = idx;
  *out_count = (size_t)count;
  return 0;
}

/// @brief Classify a scatter RHS as a scalar or an exact-shape matrix.
/// @details Mirrors ``classify_fma_operand``: a real number or a ``1x1``
///          matrix is a broadcast scalar (the in-house scalar rule), and a
///          matrix matching the selection shape exactly is INCREF'd into
///          ``*full``. Any other matrix shape raises ``ValueError`` naming
///          the offered shape and the selection shape. ``want_rows`` /
///          ``want_cols`` are the shape the selection writes (``count`` by
///          the receiver's column count for a row scatter, the receiver's
///          row count by ``count`` for a column scatter).
/// @param value_op The assigned value object.
/// @param want_rows Expected RHS row count for a full-matrix RHS.
/// @param want_cols Expected RHS column count for a full-matrix RHS.
/// @param scalar Receives the broadcast value when the RHS is a scalar.
/// @param full Receives an INCREF'd impl when the RHS is a full matrix,
///        otherwise ``NULL``.
/// @return 0 on success; -1 with an exception set on failure.
static int classify_scatter_rhs(PyObject *value_op, size_t want_rows,
                                size_t want_cols, double *scalar,
                                matrix_impl **full) {
  *scalar = 0.0;
  *full = NULL;

  if (Py_TYPE(value_op) == LOCAL_STATE->matrix_type) {
    matrix_impl *impl = ((MatrixObject *)value_op)->impl;
    if (!impl_check_acquired(impl, true)) {
      return -1;
    }
    if (impl->size == 1) {
      *scalar = impl->data[0];
      return 0;
    }
    if (impl->rows == want_rows && impl->columns == want_cols) {
      IMPL_INCREF(impl);
      *full = impl;
      return 0;
    }
    PyErr_Format(PyExc_ValueError,
                 "cannot assign %zux%zu matrix to a selection of shape %zux%zu",
                 impl->rows, impl->columns, want_rows, want_cols);
    return -1;
  }

  if (unwrap_double(value_op, scalar)) {
    return 0;
  }
  if (PyErr_Occurred()) {
    return -1;
  }

  PyErr_SetString(PyExc_TypeError, "value must be a Matrix or a real number");
  return -1;
}

/// @brief Scatter a same-shape matrix into the selected rows
/// @details ``value`` is ``count x columns``. When ``accumulate`` each
///          source row is added into its destination; otherwise it overwrites.
static void impl_scatter_rows(matrix_impl *matrix, const size_t *idx,
                              size_t count, matrix_impl *value,
                              int accumulate) {
  for (size_t i = 0; i < count; ++i) {
    double *dst = matrix->row_ptrs[idx[i]];
    const double *src = value->row_ptrs[i];
    if (accumulate) {
      for (size_t c = 0; c < matrix->columns; ++c) {
        dst[c] += src[c];
      }
    } else {
      memcpy(dst, src, matrix->columns * sizeof(double));
    }
  }
}

/// @brief Broadcast a scalar into the selected rows
static void impl_scatter_rows_scalar(matrix_impl *matrix, const size_t *idx,
                                     size_t count, double value,
                                     int accumulate) {
  for (size_t i = 0; i < count; ++i) {
    double *dst = matrix->row_ptrs[idx[i]];
    for (size_t c = 0; c < matrix->columns; ++c) {
      if (accumulate) {
        dst[c] += value;
      } else {
        dst[c] = value;
      }
    }
  }
}

/// @brief Scatter a same-shape matrix into the selected columns (strided)
/// @details ``value`` is ``rows x count``. When ``accumulate`` each source
///          element is added into its destination; otherwise it overwrites.
static void impl_scatter_cols(matrix_impl *matrix, const size_t *idx,
                              size_t count, matrix_impl *value,
                              int accumulate) {
  for (size_t r = 0; r < matrix->rows; ++r) {
    double *dst = matrix->row_ptrs[r];
    const double *src = value->row_ptrs[r];
    for (size_t i = 0; i < count; ++i) {
      if (accumulate) {
        dst[idx[i]] += src[i];
      } else {
        dst[idx[i]] = src[i];
      }
    }
  }
}

/// @brief Broadcast a scalar into the selected columns (strided)
static void impl_scatter_cols_scalar(matrix_impl *matrix, const size_t *idx,
                                     size_t count, double value,
                                     int accumulate) {
  for (size_t r = 0; r < matrix->rows; ++r) {
    double *dst = matrix->row_ptrs[r];
    for (size_t i = 0; i < count; ++i) {
      if (accumulate) {
        dst[idx[i]] += value;
      } else {
        dst[idx[i]] = value;
      }
    }
  }
}

/// @brief Three-phase scatter-store for a list-keyed assignment
/// @details Resolve all indices, then classify/shape-check the RHS, then
///          write — so a rejected scatter leaves the receiver unmodified.
///          When ``accumulate`` the RHS is added in (else it overwrites). A
///          self-aliased matrix RHS is snapshotted before the write.
/// @pre Callers must ``impl_check_acquired(impl)`` before calling; the RHS is
///      gated internally by ``classify_scatter_rhs``.
/// @param impl The receiver matrix impl (written in place).
/// @param indices The list of row (``axis == 0``) or column (``axis == 1``)
///        indices to assign.
/// @param axis 0 to scatter rows, 1 to scatter columns.
/// @param value_op The assigned scalar or matrix.
/// @param accumulate When non-zero, add into the selection instead of
///        overwriting.
/// @return 0 on success; -1 with an exception set on failure.
static int scatter_axis(matrix_impl *impl, PyObject *indices, int axis,
                        PyObject *value_op, int accumulate) {
  size_t stack[64];
  size_t *idx;
  size_t count;
  size_t dim = axis == 0 ? impl->rows : impl->columns;
  const char *axis_name = axis == 0 ? "row" : "column";
  if (resolve_index_list(indices, dim, axis_name, stack,
                         sizeof(stack) / sizeof(stack[0]), &idx, &count) < 0) {
    return -1;
  }

  size_t want_rows = axis == 0 ? count : impl->rows;
  size_t want_cols = axis == 0 ? impl->columns : count;
  double scalar;
  matrix_impl *value;
  if (classify_scatter_rhs(value_op, want_rows, want_cols, &scalar, &value) <
      0) {
    if (idx != stack) {
      PyMem_Free(idx);
    }
    return -1;
  }

  if (value == NULL) {
    if (axis == 0) {
      impl_scatter_rows_scalar(impl, idx, count, scalar, accumulate);
    } else {
      impl_scatter_cols_scalar(impl, idx, count, scalar, accumulate);
    }
  } else {
    // Snapshot a self-aliased RHS first: an in-place write would otherwise
    // clobber cells still to be read, corrupting a permutation and aliasing
    // memcpy on a fixed point.
    matrix_impl *src = value;
    matrix_impl *snapshot = NULL;
    if (value == impl) {
      snapshot = impl_new(value->rows, value->columns);
      if (snapshot == NULL) {
        IMPL_DECREF(value);
        if (idx != stack) {
          PyMem_Free(idx);
        }
        return -1;
      }
      memcpy(snapshot->data, value->data, value->size * sizeof(double));
      src = snapshot;
    }
    if (axis == 0) {
      impl_scatter_rows(impl, idx, count, src, accumulate);
    } else {
      impl_scatter_cols(impl, idx, count, src, accumulate);
    }
    if (snapshot != NULL) {
      impl_free(snapshot);
    }
    IMPL_DECREF(value);
  }

  if (idx != stack) {
    PyMem_Free(idx);
  }
  return 0;
}

/// @brief Three-phase scatter-store for ``put_along_axis``.
/// @details The write counterpart to ``gather_along_axis``: ``indices`` runs
///          *along* ``axis``, one per row (``axis == 1``, length ``rows``) or
///          one per column (``axis == 0``, length ``columns``). Resolve all
///          indices, classify/shape-check the RHS, then write — so a rejected
///          scatter leaves the receiver unmodified. The RHS is a scalar, a
///          ``1x1`` matrix, or a vector matching the selection shape
///          (``rows x 1`` for ``axis == 1``, ``1 x columns`` for
///          ``axis == 0``); element ``i`` lands in ``self[i][indices[i]]``
///          (``axis == 1``) or ``self[indices[i]][i]`` (``axis == 0``). When
///          ``accumulate`` the RHS is added in (else it overwrites). A
///          self-aliased matrix RHS is snapshotted before the write.
/// @pre Callers must ``impl_check_acquired(impl)`` before calling; the RHS is
///      gated internally by ``classify_scatter_rhs``.
/// @param impl The receiver matrix impl (written in place).
/// @param indices The along-axis index sequence.
/// @param axis 0 to index rows per column, 1 to index columns per row.
/// @param value_op The assigned scalar or matrix.
/// @param accumulate When non-zero, add into the selection instead of
///        overwriting.
/// @return 0 on success; -1 with an exception set on failure.
static int scatter_along_axis(matrix_impl *impl, PyObject *indices, int axis,
                              PyObject *value_op, int accumulate) {
  size_t stack[64];
  size_t *idx;
  size_t count;
  size_t bound = axis == 0 ? impl->rows : impl->columns;
  const char *bound_name = axis == 0 ? "row" : "column";
  if (resolve_index_list(indices, bound, bound_name, stack,
                         sizeof(stack) / sizeof(stack[0]), &idx, &count) < 0) {
    return -1;
  }

  size_t expected = axis == 0 ? impl->columns : impl->rows;
  if (count != expected) {
    PyErr_Format(PyExc_ValueError,
                 "put_along_axis on axis %d expects %zu indices (one per %s), "
                 "got %zu",
                 axis, expected, axis == 0 ? "column" : "row", count);
    if (idx != stack) {
      PyMem_Free(idx);
    }
    return -1;
  }

  size_t want_rows = axis == 0 ? 1 : impl->rows;
  size_t want_cols = axis == 0 ? impl->columns : 1;
  double scalar;
  matrix_impl *value;
  if (classify_scatter_rhs(value_op, want_rows, want_cols, &scalar, &value) <
      0) {
    if (idx != stack) {
      PyMem_Free(idx);
    }
    return -1;
  }

  // Snapshot a self-aliased RHS first: each element is read once and written
  // to a (possibly) different cell, so an in-place write could clobber a cell
  // still to be read.
  matrix_impl *src = value;
  matrix_impl *snapshot = NULL;
  if (value != NULL && value == impl) {
    snapshot = impl_new(value->rows, value->columns);
    if (snapshot == NULL) {
      IMPL_DECREF(value);
      if (idx != stack) {
        PyMem_Free(idx);
      }
      return -1;
    }
    memcpy(snapshot->data, value->data, value->size * sizeof(double));
    src = snapshot;
  }

  for (size_t i = 0; i < count; ++i) {
    double v = value != NULL
                   ? (axis == 0 ? src->row_ptrs[0][i] : src->row_ptrs[i][0])
                   : scalar;
    double *cell =
        axis == 0 ? &impl->row_ptrs[idx[i]][i] : &impl->row_ptrs[i][idx[i]];
    if (accumulate) {
      *cell += v;
    } else {
      *cell = v;
    }
  }

  if (snapshot != NULL) {
    impl_free(snapshot);
  }
  if (value != NULL) {
    IMPL_DECREF(value);
  }
  if (idx != stack) {
    PyMem_Free(idx);
  }
  return 0;
}

int Matrix_ass_subscript(PyObject *op, PyObject *key, PyObject *value_op) {
  MatrixObject *self = (MatrixObject *)op;
  matrix_impl *impl = self->impl;

  if (!impl_check_acquired(impl, true)) {
    return -1;
  }

  // Mirror the read prologue: fancy assignment shares classify_fancy_key, so
  // a FANCY_KEY_NONE key falls through to the plain assignment path below.
  PyObject *index_list;
  switch (classify_fancy_key(key, &index_list)) {
  case FANCY_KEY_AXIS0:
    return scatter_axis(impl, index_list, 0, value_op, 0);
  case FANCY_KEY_AXIS1:
    return scatter_axis(impl, index_list, 1, value_op, 0);
  case FANCY_KEY_UNSUPPORTED:
    PyErr_SetString(
        PyExc_IndexError,
        "fancy assignment supports only m[[rows]] = v, m[[rows], :] = v, "
        "or m[:, [cols]] = v; use put() for other selections");
    return -1;
  case FANCY_KEY_NONE:
    break;
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
  length += strlen(buffer) + 2;

  snprintf(buffer, VALUE_BUFFER_SIZE, "%zu", impl->columns);
  length += strlen(buffer) + 3;

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

static PyMethodDef Matrix_methods[] = {
    {"transpose", (PyCFunction)Matrix_transpose, METH_VARARGS | METH_KEYWORDS,
     "transpose($self, /, in_place=False)\n--\n\n"
     "Return a transposed copy, or transpose ``self`` in place when "
     "``in_place=True`` (in which case ``self`` is returned)."},
    {"sum", (PyCFunction)Matrix_Sum_method, METH_VARARGS | METH_KEYWORDS,
     "sum($self, /, axis=None, where=None)\n--\n\nSum of elements.\n\n"
     "``where`` is an optional same-shape Matrix mask: an element is included\n"
     "only where its mask cell is non-zero (NaN counts as included). An\n"
     "all-excluded group sums to 0."},
    {"mean", (PyCFunction)Matrix_Mean_method, METH_VARARGS | METH_KEYWORDS,
     "mean($self, /, axis=None, where=None)\n--\n\nMean of elements.\n\n"
     "``where`` is an optional same-shape Matrix mask: the mean is taken over\n"
     "only the elements whose mask cell is non-zero (NaN counts as included).\n"
     "An all-excluded group yields NaN (matching NumPy's empty-slice mean)."},
    {"magnitude", (PyCFunction)Matrix_Magnitude_method,
     METH_VARARGS | METH_KEYWORDS,
     "magnitude($self, /, axis=None, where=None)\n--\n\nEuclidean "
     "magnitude.\n\n"
     "``where`` is an optional same-shape Matrix mask: only elements whose\n"
     "mask cell is non-zero contribute (NaN counts as included). An\n"
     "all-excluded group yields 0."},
    {"magnitude_squared", (PyCFunction)Matrix_MagnitudeSquared_method,
     METH_VARARGS | METH_KEYWORDS,
     "magnitude_squared($self, /, axis=None, where=None)\n--\n\n"
     "Sum of squared elements (Euclidean magnitude without the sqrt).\n\n"
     "``where`` is an optional same-shape Matrix mask: only elements whose\n"
     "mask cell is non-zero contribute (NaN counts as included). An\n"
     "all-excluded group yields 0."},
    {"vecdot", (PyCFunction)Matrix_vecdot, METH_VARARGS | METH_KEYWORDS,
     "vecdot($self, other, /, axis=None)\n--\n\n"
     "Axis-aware inner product: sum of element-wise products. "
     "Equivalent to numpy.linalg.vecdot for 1-D inputs with axis=None; "
     "**not** equivalent to numpy.dot."},
    {"fma", (PyCFunction)Matrix_fma, METH_VARARGS | METH_KEYWORDS,
     "fma($self, b, c, /, in_place=False)\n--\n\n"
     "Fused multiply-add: single-rounding ``self*b + c`` (libc fma()). "
     "b and c may each be a same-shape matrix, a 1x1 matrix, a row or column "
     "vector that broadcasts, or a scalar; other shapes raise ValueError. "
     "The single rounding differs from ``self*b + c`` (which rounds twice) by "
     "up to half a ULP, so compare results with allclose(), not ==."},
    {"scaled_add", (PyCFunction)Matrix_scaled_add, METH_VARARGS | METH_KEYWORDS,
     "scaled_add($self, s, x, /, in_place=False)\n--\n\n"
     "Scaled add ``self + s * x`` with two roundings; the two-rounding "
     "sibling of fma().\n\n"
     "s is the scale -- a scalar, a 1x1 matrix, a row or column vector that "
     "broadcasts, or a same-shape matrix (the same operand rules as fma's "
     "multiplier) -- and x is a same-shape matrix. The arithmetic rounds "
     "twice (``round(round(s*x) + self)``), so the result is bit-for-bit "
     "identical to ``self + s * x`` and -- unlike fma() -- never fuses to a "
     "single rounding. With in_place=True the result is written into self's "
     "buffer (allocating nothing) and self is returned; otherwise a new "
     "matrix is returned. s and x may alias self. A shape mismatch raises "
     "ValueError before any write."},
    {"add", (PyCFunction)Matrix_add_method, METH_VARARGS | METH_KEYWORDS,
     "add($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self + other`` with the same broadcasting as ``+`` "
     "(other may be a scalar). With ``out`` (a same-shape matrix) the result "
     "is written there and returned instead of allocating a new matrix; out "
     "may alias an input. Bit-for-bit identical to ``self + other``. A shape "
     "mismatch between out and the result raises ValueError before any write."},
    {"subtract", (PyCFunction)Matrix_subtract_method,
     METH_VARARGS | METH_KEYWORDS,
     "subtract($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self - other`` with the same broadcasting as ``-`` "
     "(other may be a scalar). With ``out`` (a same-shape matrix) the result "
     "is written there and returned instead of allocating a new matrix; out "
     "may alias an input. Bit-for-bit identical to ``self - other``. A shape "
     "mismatch between out and the result raises ValueError before any write."},
    {"multiply", (PyCFunction)Matrix_multiply_method,
     METH_VARARGS | METH_KEYWORDS,
     "multiply($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self * other`` with the same broadcasting as ``*`` "
     "(other may be a scalar). With ``out`` (a same-shape matrix) the result "
     "is written there and returned instead of allocating a new matrix; out "
     "may alias an input. Bit-for-bit identical to ``self * other``. A shape "
     "mismatch between out and the result raises ValueError before any write."},
    {"divide", (PyCFunction)Matrix_divide_method, METH_VARARGS | METH_KEYWORDS,
     "divide($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self / other`` with the same broadcasting as ``/`` "
     "(other may be a scalar). With ``out`` (a same-shape matrix) the result "
     "is written there and returned instead of allocating a new matrix; out "
     "may alias an input. Bit-for-bit identical to ``self / other``. A shape "
     "mismatch between out and the result raises ValueError before any write."},
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
     "min($self, /, axis=None, where=None)\n--\n\nMinimum of elements.\n\n"
     "``where`` is an optional same-shape Matrix mask: only elements whose\n"
     "mask cell is non-zero are considered (NaN counts as included). An\n"
     "all-excluded group yields NaN."},
    {"max", (PyCFunction)Matrix_Maximum_method, METH_VARARGS | METH_KEYWORDS,
     "max($self, /, axis=None, where=None)\n--\n\nMaximum of elements.\n\n"
     "``where`` is an optional same-shape Matrix mask: only elements whose\n"
     "mask cell is non-zero are considered (NaN counts as included). An\n"
     "all-excluded group yields NaN."},
    {"argmin", (PyCFunction)Matrix_argmin_method, METH_VARARGS | METH_KEYWORDS,
     "argmin($self, /, axis=None, where=None, as_matrix=False)\n--\n\n"
     "Index of the minimum element (first occurrence on ties).\n\n"
     "With axis=None returns a single int. With axis=0/1 returns a list of\n"
     "ints (directly usable in fancy indexing); pass as_matrix=True to get a\n"
     "Matrix vector of index positions instead. NaN elements are skipped\n"
     "unless the first included element along the reduced axis is NaN, which\n"
     "pins the result to that position. This differs from NumPy, which\n"
     "propagates NaN.\n\n"
     "``where`` is an optional same-shape Matrix mask: a cell == 0.0\n"
     "excludes that element (NaN counts as included). A group with no\n"
     "included element yields -1, the integer analog of a masked min/max\n"
     "returning NaN."},
    {"argmax", (PyCFunction)Matrix_argmax_method, METH_VARARGS | METH_KEYWORDS,
     "argmax($self, /, axis=None, where=None, as_matrix=False)\n--\n\n"
     "Index of the maximum element (first occurrence on ties).\n\n"
     "With axis=None returns a single int. With axis=0/1 returns a list of\n"
     "ints (directly usable in fancy indexing); pass as_matrix=True to get a\n"
     "Matrix vector of index positions instead. NaN elements are skipped\n"
     "unless the first included element along the reduced axis is NaN, which\n"
     "pins the result to that position. This differs from NumPy, which\n"
     "propagates NaN.\n\n"
     "``where`` is an optional same-shape Matrix mask: a cell == 0.0\n"
     "excludes that element (NaN counts as included). A group with no\n"
     "included element yields -1, the integer analog of a masked min/max\n"
     "returning NaN."},
    {"ceil", (PyCFunction)Matrix_Ceil_method, METH_VARARGS | METH_KEYWORDS,
     "ceil($self, /, *, in_place=False, out=None)\n--\n\n"
     "Element-wise ceiling. With ``out`` (a same-shape matrix) the result "
     "is written there and returned; ``out`` and ``in_place`` are mutually "
     "exclusive."},
    {"floor", (PyCFunction)Matrix_Floor_method, METH_VARARGS | METH_KEYWORDS,
     "floor($self, /, *, in_place=False, out=None)\n--\n\n"
     "Element-wise floor. With ``out`` (a same-shape matrix) the result "
     "is written there and returned; ``out`` and ``in_place`` are mutually "
     "exclusive."},
    {"round", (PyCFunction)Matrix_Round_method, METH_VARARGS | METH_KEYWORDS,
     "round($self, /, *, in_place=False, out=None)\n--\n\n"
     "Element-wise rounding (banker's; IEEE round-half-to-even). With ``out`` "
     "(a same-shape matrix) the result is written there and returned; "
     "``out`` and ``in_place`` are mutually exclusive."},
    {"negate", (PyCFunction)Matrix_Negate_method, METH_VARARGS | METH_KEYWORDS,
     "negate($self, /, *, in_place=False, out=None)\n--\n\n"
     "Element-wise negation. With ``out`` (a same-shape matrix) the result "
     "is written there and returned; ``out`` and ``in_place`` are mutually "
     "exclusive."},
    {"abs", (PyCFunction)Matrix_Abs_method, METH_VARARGS | METH_KEYWORDS,
     "abs($self, /, *, in_place=False, out=None)\n--\n\n"
     "Element-wise absolute value. With ``out`` (a same-shape matrix) the "
     "result is written there and returned; ``out`` and ``in_place`` are "
     "mutually exclusive."},
    {"sqrt", (PyCFunction)Matrix_Sqrt_method, METH_VARARGS | METH_KEYWORDS,
     "sqrt($self, /, *, in_place=False, out=None)\n--\n\n"
     "Element-wise square root. Negative elements yield NaN. With ``out`` "
     "(a same-shape matrix) the result is written there and returned; "
     "``out`` and ``in_place`` are mutually exclusive."},
    {"sign", (PyCFunction)Matrix_Sign_method, METH_VARARGS | METH_KEYWORDS,
     "sign($self, /, *, in_place=False, out=None)\n--\n\n"
     "Element-wise sign: -1, 0, or 1 (NaN maps to 0). With ``out`` "
     "(a same-shape matrix) the result is written there and returned; "
     "``out`` and ``in_place`` are mutually exclusive."},
    {"cos", (PyCFunction)Matrix_Cos_method, METH_VARARGS | METH_KEYWORDS,
     "cos($self, /, *, in_place=False, out=None)\n--\n\n"
     "Element-wise cosine (radians). With ``out`` (a same-shape matrix) the "
     "result is written there and returned; ``out`` and ``in_place`` are "
     "mutually exclusive."},
    {"sin", (PyCFunction)Matrix_Sin_method, METH_VARARGS | METH_KEYWORDS,
     "sin($self, /, *, in_place=False, out=None)\n--\n\n"
     "Element-wise sine (radians). With ``out`` (a same-shape matrix) the "
     "result is written there and returned; ``out`` and ``in_place`` are "
     "mutually exclusive."},
    {"less", (PyCFunction)Matrix_Less_compare, METH_VARARGS | METH_KEYWORDS,
     "less($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self < other`` as a 0/1 mask matrix. ``other`` may be a "
     "same-shape matrix, a scalar (including bool), a 1x1 matrix, a "
     "row/column vector that broadcasts, or a list/tuple of numbers. NaN "
     "comparisons yield 0. With ``out`` (a same-shape matrix) the mask is "
     "written there and returned. Distinct from the ``<`` operator, which "
     "returns a single bool."},
    {"less_equal", (PyCFunction)Matrix_LessEqual_compare,
     METH_VARARGS | METH_KEYWORDS,
     "less_equal($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self <= other`` as a 0/1 mask matrix. ``other`` may be a "
     "same-shape matrix, a scalar (including bool), a 1x1 matrix, a "
     "row/column vector that broadcasts, or a list/tuple of numbers. NaN "
     "comparisons yield 0. With ``out`` (a same-shape matrix) the mask is "
     "written there and returned. Distinct from the ``<=`` operator, which "
     "returns a single bool."},
    {"greater", (PyCFunction)Matrix_Greater_compare,
     METH_VARARGS | METH_KEYWORDS,
     "greater($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self > other`` as a 0/1 mask matrix. ``other`` may be a "
     "same-shape matrix, a scalar (including bool), a 1x1 matrix, a "
     "row/column vector that broadcasts, or a list/tuple of numbers. NaN "
     "comparisons yield 0. With ``out`` (a same-shape matrix) the mask is "
     "written there and returned. Distinct from the ``>`` operator, which "
     "returns a single bool."},
    {"greater_equal", (PyCFunction)Matrix_GreaterEqual_compare,
     METH_VARARGS | METH_KEYWORDS,
     "greater_equal($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self >= other`` as a 0/1 mask matrix. ``other`` may be a "
     "same-shape matrix, a scalar (including bool), a 1x1 matrix, a "
     "row/column vector that broadcasts, or a list/tuple of numbers. NaN "
     "comparisons yield 0. With ``out`` (a same-shape matrix) the mask is "
     "written there and returned. Distinct from the ``>=`` operator, which "
     "returns a single bool."},
    {"equal", (PyCFunction)Matrix_Equal_compare, METH_VARARGS | METH_KEYWORDS,
     "equal($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self == other`` as a 0/1 mask matrix. ``other`` may be a "
     "same-shape matrix, a scalar (including bool), a 1x1 matrix, a "
     "row/column vector that broadcasts, or a list/tuple of numbers. NaN "
     "comparisons yield 0. With ``out`` (a same-shape matrix) the mask is "
     "written there and returned. Distinct from the ``==`` operator, which "
     "returns a single bool."},
    {"not_equal", (PyCFunction)Matrix_NotEqual_compare,
     METH_VARARGS | METH_KEYWORDS,
     "not_equal($self, other, /, *, out=None)\n--\n\n"
     "Element-wise ``self != other`` as a 0/1 mask matrix. ``other`` may be a "
     "same-shape matrix, a scalar (including bool), a 1x1 matrix, a "
     "row/column vector that broadcasts, or a list/tuple of numbers. NaN "
     "comparisons yield 1. With ``out`` (a same-shape matrix) the mask is "
     "written there and returned. Distinct from the ``!=`` operator, which "
     "returns a single bool."},
    {"clip", (PyCFunction)Matrix_clip, METH_VARARGS | METH_KEYWORDS,
     "clip($self, min=None, max=None, *, in_place=False, out=None)\n--\n\n"
     "Clamp elements to [min, max]; either bound may be omitted.\n\n"
     "The first argument is the lower bound and the second the upper\n"
     "bound. Pass None (or omit) to leave a side unbounded, so\n"
     "clip(min=0) clamps only below and clip(max=255) only above. Raises\n"
     "ValueError if both bounds are omitted.\n\n"
     "With in_place=True the matrix is clamped in place and returned; with\n"
     "out (a pre-allocated same-shape matrix) the result is written there\n"
     "and returned instead of allocating a new matrix. in_place and out\n"
     "are mutually exclusive."},
    {"copy", Matrix_copy, METH_NOARGS,
     "copy($self, /)\n--\n\nReturn a deep copy."},
    {"values", Matrix_values, METH_NOARGS,
     "values($self, /)\n--\n\n"
     "Yield every element as a float in row-major (row/column) order. "
     "Lazy: one float is boxed per step, so streaming a large matrix never "
     "materialises a list."},
    {"__reduce__", Matrix_reduce, METH_NOARGS,
     "__reduce__($self, /)\n--\n\n"
     "Pickle helper: serialize the matrix to its raw double buffer."},
    {"take", (PyCFunction)Matrix_take, METH_VARARGS | METH_KEYWORDS,
     "take($self, indices, axis=0, *, out=None)\n--\n\n"
     "Take rows or columns by index into a new matrix.\n\n"
     "indices is a 1-D list or tuple of ints selecting whole rows\n"
     "(axis=0) or columns (axis=1). Negative indices count from the\n"
     "end; duplicates repeat the row or column; an out-of-range index\n"
     "raises IndexError, and an empty index sequence raises IndexError.\n"
     "With out (a pre-allocated matrix of the selection shape -- "
     "len(indices) x columns for axis=0, rows x len(indices) for axis=1) "
     "the result is written there and returned instead of allocating a new "
     "matrix. All indices are validated before any write, so a rejected "
     "call leaves out untouched; out must not alias self."},
    {"put", (PyCFunction)Matrix_put, METH_VARARGS | METH_KEYWORDS,
     "put($self, indices, value, axis=0, accumulate=False)\n--\n\n"
     "Assign value into the selected rows or columns in place.\n\n"
     "The write-side counterpart of take(): value may be a scalar, a\n"
     "1x1 matrix, or a matrix matching the selection shape. All indices\n"
     "and the value shape are validated before any write, so a rejected\n"
     "call leaves the matrix unchanged. Negative indices count from the\n"
     "end; an out-of-range or empty index raises IndexError. With\n"
     "accumulate=False (the default) duplicate indices follow\n"
     "last-write-wins; with accumulate=True the values are added into the\n"
     "selection, so duplicate indices fold additively (scatter-add).\n"
     "Returns self."},
    {"take_along_axis", (PyCFunction)Matrix_take_along_axis,
     METH_VARARGS | METH_KEYWORDS,
     "take_along_axis($self, indices, axis=0, *, out=None)\n--\n\n"
     "Gather one element per row or column along an axis.\n\n"
     "The np.take_along_axis counterpart of take() (which selects whole\n"
     "rows or columns). With axis=1 the indices give one column index per\n"
     "row (so len(indices) must equal the row count) and the result is an\n"
     "rows x 1 column vector with out[r] = self[r][indices[r]]. With\n"
     "axis=0 they give one row index per column (len(indices) must equal\n"
     "the column count) and the result is a 1 x columns row vector with\n"
     "out[c] = self[indices[c]][c]. This pairs directly with argmin/argmax\n"
     "along the same axis: feed their returned index list straight back in\n"
     "to gather the reduced values. Negative indices count from the end;\n"
     "an out-of-range index raises IndexError, and a wrong index count\n"
     "raises ValueError. With out (a pre-allocated matrix of the result\n"
     "shape -- 1 x columns for axis=0, rows x 1 for axis=1) the result is\n"
     "written there and returned instead of allocating a new matrix. All\n"
     "indices are validated before any write, so a rejected call leaves\n"
     "out untouched; out must not alias self."},
    {"put_along_axis", (PyCFunction)Matrix_put_along_axis,
     METH_VARARGS | METH_KEYWORDS,
     "put_along_axis($self, indices, value, axis=0, accumulate=False)\n--\n\n"
     "Assign one element per row or column along an axis in place.\n\n"
     "The write-side counterpart of take_along_axis(). With axis=1 element\n"
     "i lands in self[i][indices[i]] (len(indices) equals the row count);\n"
     "with axis=0 it lands in self[indices[i]][i] (len(indices) equals the\n"
     "column count). value may be a scalar, a 1x1 matrix, or a vector\n"
     "matching the selection shape (rows x 1 for axis=1, 1 x columns for\n"
     "axis=0). All indices and the value shape are validated before any\n"
     "write, so a rejected call leaves the matrix unchanged. Negative\n"
     "indices count from the end; an out-of-range index raises IndexError,\n"
     "and a wrong index count raises ValueError. With accumulate=False (the\n"
     "default) duplicate indices follow last-write-wins; with\n"
     "accumulate=True the values are added in (scatter-add). Returns self."},
    {"repeat_interleave", (PyCFunction)Matrix_repeat_interleave,
     METH_VARARGS | METH_KEYWORDS,
     "repeat_interleave($self, repeats, axis=None)\n--\n\n"
     "Repeat each element, row, or column consecutively into a new matrix.\n\n"
     "Interleaved (np.repeat / torch.repeat_interleave), not tiled: [a, b]\n"
     "with repeats=2 gives [a, a, b, b]. With axis=0 each row is repeated\n"
     "into a (rows*repeats) x columns result; with axis=1 each column is\n"
     "repeated into rows x (columns*repeats); with axis=None (the default)\n"
     "the row-major buffer is flattened to a 1 x (size*repeats) row vector.\n"
     "repeats must be a positive integer. Returns a new matrix."},
    {"topk", (PyCFunction)Matrix_topk, METH_VARARGS | METH_KEYWORDS,
     "topk($self, k, axis=None, largest=True, where=None, "
     "as_matrix=False)\n--\n\n"
     "The k extreme elements per reduction group, in sorted order.\n\n"
     "Returns a (values, indices) tuple. ``largest=True`` (the default)\n"
     "selects the k greatest in descending order; ``largest=False`` selects\n"
     "the k smallest in ascending order. Ties keep the first occurrence\n"
     "(NumPy tie-break) and any NaN sorts last.\n\n"
     "With axis=None the whole row-major buffer is one group and values is\n"
     "a 1 x k row vector. With axis=0 each column is reduced down the rows\n"
     "and values is k x cols. With axis=1 each row is reduced across the\n"
     "columns and values is rows x k.\n\n"
     "By default (as_matrix=False) indices is Python ints: a flat list[int]\n"
     "for axis=None, or a list of `cols`/`rows` lists (each k indices) for\n"
     "axis=0/axis=1. With as_matrix=True indices is instead a Matrix of the\n"
     "same shape as values, each cell the source index (as a float) of the\n"
     "value beside it; the -1 pad becomes -1.0.\n\n"
     "k must be a positive integer no larger than the reduced axis length\n"
     "(every cell counts, masked or not), else ValueError. ``where`` is an\n"
     "optional same-shape Matrix mask: a cell == 0.0 excludes that element\n"
     "(NaN counts as included). A group with fewer than k included elements\n"
     "fills its leading slots and pads the rest with NaN values and -1\n"
     "indices."},
    {"allclose", (PyCFunction)Matrix_allclose,
     METH_VARARGS | METH_KEYWORDS | METH_CLASS,
     "allclose($type, lhs, rhs, /, rtol=1e-05, atol=1e-08, "
     "equal_nan=False)\n--\n\n"
     "Check element-wise equality within tolerance."},
    {"where", (PyCFunction)Matrix_where,
     METH_VARARGS | METH_KEYWORDS | METH_CLASS,
     "where($type, mask, a, b, /, *, out=None)\n--\n\n"
     "Select element-wise from a or b on a truthy mask.\n\n"
     "Returns a fresh matrix taking a where the corresponding mask\n"
     "element is non-zero and b elsewhere. a and b may each be a scalar,\n"
     "a matrix matching the mask's shape, or a list/tuple of numbers\n"
     "(taken as a 1xN row vector that must then match the mask shape);\n"
     "other shapes raise ValueError. A 1x1 matrix is treated as a matrix\n"
     "(not a scalar) and so must match the mask shape. NaN mask elements\n"
     "are non-zero and select a. With out (a same-shape matrix) the result\n"
     "is written there and returned; out may alias mask, a, or b."},
    {"zeros", Matrix_zeros, METH_VARARGS | METH_CLASS,
     "zeros($type, size, /)\n--\n\nCreate a zero-filled matrix."},
    {"ones", Matrix_ones, METH_VARARGS | METH_CLASS,
     "ones($type, size, /)\n--\n\nCreate a matrix of ones."},
    {"full", Matrix_full, METH_VARARGS | METH_CLASS,
     "full($type, size, value, /)\n--\n\n"
     "Create a matrix filled with a constant value."},
    {"normal", (PyCFunction)Matrix_normal,
     METH_VARARGS | METH_KEYWORDS | METH_CLASS,
     "normal($type, mean=0.0, stddev=1.0, /, size=None)\n--\n\n"
     "Sample from a normal distribution."},
    {"uniform", (PyCFunction)Matrix_uniform,
     METH_VARARGS | METH_KEYWORDS | METH_CLASS,
     "uniform($type, minval=0.0, maxval=1.0, /, size=None)\n--\n\n"
     "Sample from a uniform distribution."},
    {"seed", Matrix_seed, METH_VARARGS | METH_CLASS,
     "seed($type, value, /)\n--\n\n"
     "Seed the random generator used by normal() and uniform().\n\n"
     "Each interpreter owns an independent splitmix64 stream, so a seed\n"
     "makes that interpreter's subsequent draws reproducible; parallel\n"
     "workers seed their own streams independently. The sequence is not\n"
     "portable across platforms."},
    {"vector", Matrix_vector, METH_VARARGS | METH_CLASS,
     "vector($type, values, /, as_column=False)\n--\n\n"
     "Create a vector from a sequence."},
    {"concat", (PyCFunction)Matrix_concat,
     METH_VARARGS | METH_KEYWORDS | METH_CLASS,
     "concat($type, values, /, axis=0)\n--\n\n"
     "Concatenate matrices along an axis."},
    {NULL} /* Sentinel */
};

static PyType_Slot Matrix_slots[] = {
    {Py_tp_doc, "Matrix(rows, columns, values=None)\n--\n\n"
                "A dense 2-D matrix of double-precision floats."},
    {Py_tp_new, Matrix_new},
    {Py_tp_init, Matrix_init},
    {Py_tp_dealloc, Matrix_dealloc},
    {Py_tp_str, Matrix_str},
    {Py_tp_repr, Matrix_repr},
    {Py_tp_iter, Matrix_iter},
    {Py_tp_richcompare, Matrix_richcompare},
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

  _math_module_state *state = math_local_state();
  if (state == NULL) {
    return NULL;
  }

  int_least64_t expected = BOCPY_NO_OWNER;
  int_least64_t desired = bocpy_interpid();
  if (!atomic_compare_exchange_strong(&impl->owner, &expected, desired)) {
    PyErr_Format(PyExc_RuntimeError,
                 "%" PRIdLEAST64
                 " cannot acquire cown (already acquired by %" PRIdLEAST64 ")",
                 desired, expected);
    return NULL;
  }

  PyTypeObject *type = state->matrix_type;
  MatrixObject *matrix = (MatrixObject *)type->tp_alloc(type, 0);
  if (matrix == NULL) {
    int_least64_t rollback_expected = desired;
    desired = BOCPY_NO_OWNER;
    atomic_compare_exchange_strong(&impl->owner, &rollback_expected, desired);
    return NULL;
  }

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

  int_least64_t expected = bocpy_interpid();
  int_least64_t desired = BOCPY_NO_OWNER;
  if (!atomic_compare_exchange_strong(&impl->owner, &expected, desired)) {
    PyErr_Format(PyExc_RuntimeError,
                 "%" PRIdLEAST64
                 " cannot release matrix (acquired by %" PRIdLEAST64 ")",
                 bocpy_interpid(), expected);
    return -1;
  }

  XIDATA_INIT(xidata, tstate->interp, impl, obj, _new_matrix_object);
  return 0;
}

/// @brief Reconstruct a Matrix from its pickled raw double buffer.
/// @details Inverse of ``Matrix.__reduce__``: validates the dimensions and the
///          payload length, then copies the native-endian ``double`` image into
///          a freshly allocated matrix owned by the current interpreter.
/// @param args ``(rows, columns, payload)`` where ``payload`` exposes the
///        buffer protocol.
/// @return a new MatrixObject reference, or NULL on error
static PyObject *_matrix_unpickle(PyObject *Py_UNUSED(module), PyObject *args) {
  Py_ssize_t srows = 0;
  Py_ssize_t scolumns = 0;
  Py_buffer payload;

  if (!PyArg_ParseTuple(args, "nny*", &srows, &scolumns, &payload)) {
    return NULL;
  }

  if (srows <= 0 || scolumns <= 0) {
    PyBuffer_Release(&payload);
    PyErr_SetString(PyExc_ValueError, "Rows and columns must both be > 0");
    return NULL;
  }

  size_t rows = (size_t)srows;
  size_t columns = (size_t)scolumns;

  if (rows > SIZE_MAX / columns) {
    PyBuffer_Release(&payload);
    PyErr_SetString(PyExc_ValueError, "Matrix dimensions are too large");
    return NULL;
  }

  size_t size = rows * columns;
  if (size > SIZE_MAX / sizeof(double) ||
      (size_t)payload.len != size * sizeof(double)) {
    PyBuffer_Release(&payload);
    PyErr_SetString(PyExc_ValueError,
                    "Pickled matrix payload has the wrong length");
    return NULL;
  }

  matrix_impl *impl = impl_new(rows, columns);
  if (impl == NULL) {
    PyBuffer_Release(&payload);
    return NULL;
  }

  memcpy(impl->data, payload.buf, (size_t)payload.len);
  PyBuffer_Release(&payload);

  return (PyObject *)wrap_impl_or_free(impl);
}

static PyMethodDef _math_module_methods[] = {
    {"_matrix_unpickle", _matrix_unpickle, METH_VARARGS,
     "_matrix_unpickle(rows, columns, payload, /)\n--\n\n"
     "Internal pickle helper: rebuild a Matrix from its raw byte buffer."},
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

  state->values_iter_type = (PyTypeObject *)PyType_FromModuleAndSpec(
      module, &MatrixValuesIter_Spec, NULL);
  if (state->values_iter_type == NULL) {
    return -1;
  }

  if (XIDATA_REGISTERCLASS(state->matrix_type, _matrix_shared)) {
    Py_FatalError(
        "could not register MatrixObject for cross-interpreter sharing");
    return -1;
  }

  state->matrix_unpickle = PyObject_GetAttrString(module, "_matrix_unpickle");
  if (state->matrix_unpickle == NULL) {
    return -1;
  }

  // Seed this interpreter's PRNG with a value distinct per interpreter (and
  // per run): mixing the interpreter id and the state pointer ensures parallel
  // workers created within the same clock tick still draw independent streams.
  // Matrix.seed() overrides this for reproducibility within an interpreter.
  state->prng_state = ((uint64_t)time(NULL) << 20) ^
                      ((uint64_t)bocpy_interpid() * 0x9E3779B97F4A7C15ULL) ^
                      (uint64_t)(uintptr_t)state;

  assert(LOCAL_STATE == NULL);
  LOCAL_STATE = state;

  return 0;
}

static int _math_module_clear(PyObject *module) {
  _math_module_state *state = (_math_module_state *)PyModule_GetState(module);
  Py_CLEAR(state->matrix_type);
  Py_CLEAR(state->values_iter_type);
  Py_CLEAR(state->matrix_unpickle);
  return 0;
}

static void _math_module_free(void *module) {
  _math_module_clear((PyObject *)module);
}

static int _math_module_traverse(PyObject *module, visitproc visit, void *arg) {
  _math_module_state *state = (_math_module_state *)PyModule_GetState(module);
  Py_VISIT(state->matrix_type);
  Py_VISIT(state->values_iter_type);
  Py_VISIT(state->matrix_unpickle);
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