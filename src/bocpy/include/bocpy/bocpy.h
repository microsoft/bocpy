/// @file bocpy.h
/// @brief Public C ABI umbrella header for the bocpy package.
///
/// This header is the single supported include for downstream C
/// extensions that want to interoperate with bocpy at the C level. It
/// re-exports the cross-interpreter data macros from `xidata.h`, a
/// minimal sequentially-consistent atomic surface compatible with
/// CPython's MSVC builds, and the `BOCPY_NO_OWNER` / `bocpy_interpid()`
/// pair used to flip per-resource ownership during XIData handoffs.
///
/// **C-only and order-insensitive.** This header may be included before
/// or after `<Python.h>`. Including it from C++ translation units is
/// not supported in this release; downstream C++ consumers must wrap
/// the bocpy ABI in a thin C translation unit. See :ref:`c-abi` for
/// the full usage contract.

#ifndef BOCPY_H
#define BOCPY_H

/// Public C ABI revision. Bumped on any incompatible change to this
/// header or `xidata.h`.
#define BOCPY_ABI 1

#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <stdbool.h>
#include <stdint.h>

#include "xidata.h"

#if defined(_MSC_VER)

#ifndef thread_local
#define thread_local __declspec(thread)
#endif

typedef volatile int_least64_t atomic_int_least64_t;

/// @brief Sequentially-consistent fetch-and-add: `*ptr += value`.
/// @return The previous value of `*ptr` (before the add).
int_least64_t atomic_fetch_add(atomic_int_least64_t *ptr, int_least64_t value);

/// @brief Sequentially-consistent compare-and-swap.
/// @return ``true`` if the swap happened. On failure, writes the
///         observed value through ``expected``.
bool atomic_compare_exchange_strong(atomic_int_least64_t *ptr,
                                    atomic_int_least64_t *expected,
                                    int_least64_t desired);

/// @brief Sequentially-consistent load of `*ptr`.
int_least64_t atomic_load(atomic_int_least64_t *ptr);

/// @brief Sequentially-consistent store of `value` into `*ptr`.
void atomic_store(atomic_int_least64_t *ptr, int_least64_t value);

#else

#include <stdatomic.h>

#ifndef thread_local
#define thread_local _Thread_local
#endif

#endif

/// @brief Sentinel owner value meaning "no interpreter currently owns this
///        cross-interpreter resource".
///
/// Use it as the initial value of any per-resource owner field that downstream
/// code flips with `atomic_compare_exchange_strong` during the producer-side
/// `XIDATA_GETDATA_FUNC` callback (this-interpreter -> `BOCPY_NO_OWNER`) and
/// the consumer-side `new_object` callback (`BOCPY_NO_OWNER` ->
/// this-interpreter). Chosen to be negative so it never collides with a real
/// `PyInterpreterState_GetID()` return value (which is non-negative).
#define BOCPY_NO_OWNER (-2)

/// @brief Return the running interpreter's ID as `int_least64_t`.
///
/// Convenience wrapper over
/// `PyInterpreterState_GetID(PyInterpreterState_Get())`, pre-typed for the
/// `atomic_int_least64_t` owner-field pattern paired with `BOCPY_NO_OWNER`.
/// Must be called with the GIL held (or while attached to an interpreter, on
/// free-threaded builds) — same contract as the underlying CPython API.
static inline int_least64_t bocpy_interpid(void) {
  return (int_least64_t)PyInterpreterState_GetID(PyInterpreterState_Get());
}

#endif // BOCPY_H
