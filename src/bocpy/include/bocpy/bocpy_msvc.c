/* MSVC out-of-line bodies for the four atomic ops declared in
 * bocpy.h. This file is package data — compiled by downstream
 * extensions via bocpy.get_sources(), NOT linked into any bocpy
 * extension itself (boc_compat.c provides identical bodies for the
 * bocpy build).
 *
 * Add this to a downstream setuptools.Extension's sources= list
 * only on Windows. The file is a no-op on non-MSVC compilers. */
#include "bocpy.h"
#if defined(_MSC_VER)
/* `windows.h` provides the `Interlocked*64` intrinsics used in the
 * marker region below. The marker region itself must stay byte-
 * identical to `boc_compat.c`, so the include lives outside it. */
#include <windows.h>
/* The bytes between @atomic-bodies-begin and @atomic-bodies-end must
 * be byte-identical to the marker region in src/bocpy/boc_compat.c
 * (enforced by test_msvc_bodies_in_lockstep). */
/* @atomic-bodies-begin */
int_least64_t atomic_fetch_add(atomic_int_least64_t *ptr, int_least64_t value) {
#if defined(_M_IX86)
  int_least64_t old = *ptr;
  for (;;) {
    int_least64_t prev = InterlockedCompareExchange64(ptr, old + value, old);
    if (prev == old)
      return old;
    old = prev;
  }
#else
  return InterlockedExchangeAdd64(ptr, value);
#endif
}

bool atomic_compare_exchange_strong(atomic_int_least64_t *ptr,
                                    atomic_int_least64_t *expected,
                                    int_least64_t desired) {
  int_least64_t prev;
  prev = InterlockedCompareExchange64(ptr, desired, *expected);
  if (prev == *expected) {
    return true;
  }

  *expected = prev;
  return false;
}

int_least64_t atomic_load(atomic_int_least64_t *ptr) {
#if defined(_M_IX86)
  return InterlockedCompareExchange64(ptr, 0, 0);
#else
  /* Seq-cst load. Plain `*ptr` is acquire/release at best on x64
   * and gives no ordering on ARM64; InterlockedOr64(ptr, 0) is a
   * full barrier on every supported MSVC target. */
  return InterlockedOr64(ptr, 0);
#endif
}

void atomic_store(atomic_int_least64_t *ptr, int_least64_t value) {
#if defined(_M_IX86)
  int_least64_t old = *ptr;
  for (;;) {
    int_least64_t prev = InterlockedCompareExchange64(ptr, value, old);
    if (prev == old)
      return;
    old = prev;
  }
#else
  /* Seq-cst store. Plain `*ptr = value` does not forbid StoreLoad
   * reordering on x64/ARM64; InterlockedExchange64 is a full barrier. */
  (void)InterlockedExchange64(ptr, value);
#endif
}
/* @atomic-bodies-end */
#endif
