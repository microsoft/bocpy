/// @file compat.h
/// @brief Cross-platform portability shims for bocpy C extensions.
///
/// Centralises the platform-specific atomic, mutex, condition-variable,
/// thread-local, sleep, and monotonic-time primitives used by `_core.c`,
/// `_math.c`, and `sched.c`.
///
/// **Linkage:** all heavy-weight platform primitives are exposed as
/// `static inline` wrappers around the platform's native API, except for
/// the MSVC `atomic_*` functions on `int_least64_t` (kept as out-of-line
/// definitions in `compat.c` to preserve their original symbol shape).
///
/// Also exposes the `boc_atomic_*_explicit` typed atomics API that the
/// work-stealing scheduler depends on for ARM64-correct memory ordering
/// on Windows.
///
/// **File layout.** All platform-specific machinery is grouped behind a
/// single top-level `#ifdef _WIN32 / #elif __APPLE__ / #else` ladder:
///
///   1. Cross-platform headers and the C11 alignas/alignof shim.
///   2. Memory-order tags (`BOC_MO_*`) used by both arms of the typed
///      atomics API below.
///   3. **Windows arm** — Win32 headers, `atomic_*` polyfill on
///      `int_least64_t` / `intptr_t`, BOC mutex/cond on `SRWLOCK` and
///      `CONDITION_VARIABLE`, the typed `boc_atomic_*_explicit` API
///      with x86/x64/ARM64 dispatch, `boc_yield`, and `thread_local`.
///   4. **Apple arm** — `pthread`-based BOC mutex/cond with
///      `<stdatomic.h>` typed atomics; `nanosleep` aliased to
///      `thrd_sleep`.
///   5. **Other POSIX (Linux) arm** — C11 `<threads.h>`-based BOC
///      mutex/cond with `<stdatomic.h>` typed atomics.
///   6. Cross-platform monotonic time / sleep helpers
///      (`boc_now_s`, `boc_now_ns`, `boc_sleep_ns`).
///   7. Cross-platform timeout-validation helper.

#ifndef BOCPY_COMPAT_H
#define BOCPY_COMPAT_H

#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <stdbool.h>
#include <stdint.h>
#include <time.h>

// ---------------------------------------------------------------------------
// Cross-platform alignas / alignof shim
// ---------------------------------------------------------------------------
//
// Portable C11-style alignment macros. MSVC's `<stdalign.h>` only
// defines `alignas` / `alignof` when the compiler is invoked in C11
// mode (`/std:c11` or later); the Python build does not pass that
// flag, so we map directly to the underlying `__declspec(align(...))`
// / `__alignof` intrinsics on MSVC and fall back to `<stdalign.h>`
// elsewhere. C++ TUs always get the standard header.
#if defined(__cplusplus)
#include <stdalign.h>
#elif defined(_MSC_VER)
#if _MSC_VER >= 1900
#ifndef alignas
#define alignas(x) __declspec(align(x))
#endif
#ifndef alignof
#define alignof(x) __alignof(x)
#endif
#else
#error "MSVC >= 1900 required for alignas/alignof support"
#endif
#else
#include <stdalign.h>
#endif

// ---------------------------------------------------------------------------
// Memory-order tags
// ---------------------------------------------------------------------------
//
// Used by the typed `boc_atomic_*_explicit` API below. Defined here
// (above the platform fork) because both arms reference these tags.
// Distinct integer constants so the MSVC dispatch can `switch` on
// them; on POSIX they are mapped to `memory_order_*` by the
// `boc_mo_to_std` helper inside the POSIX arm. Skip 1 to leave room
// for `consume`.

typedef enum {
  BOC_MO_RELAXED = 0,
  BOC_MO_ACQUIRE = 2,
  BOC_MO_RELEASE = 3,
  BOC_MO_ACQ_REL = 4,
  BOC_MO_SEQ_CST = 5,
} boc_memory_order_t;

// ===========================================================================
// Platform fork: Windows / Apple / other POSIX (Linux).
// ===========================================================================

#ifdef _WIN32

// ---------------------------------------------------------------------------
// Windows: headers, thread_local, yield
// ---------------------------------------------------------------------------

#define WIN32_LEAN_AND_MEAN
#include <process.h>
#include <windows.h>

#define thread_local __declspec(thread)
#define boc_yield() SwitchToThread()

// ---------------------------------------------------------------------------
// Windows: legacy `atomic_*` polyfill on int_least64_t / intptr_t
// ---------------------------------------------------------------------------

typedef volatile int_least64_t atomic_int_least64_t;
typedef volatile intptr_t atomic_intptr_t;

int_least64_t atomic_fetch_add(atomic_int_least64_t *ptr, int_least64_t value);
int_least64_t atomic_fetch_sub(atomic_int_least64_t *ptr, int_least64_t value);
bool atomic_compare_exchange_strong(atomic_int_least64_t *ptr,
                                    atomic_int_least64_t *expected,
                                    int_least64_t desired);
int_least64_t atomic_load(atomic_int_least64_t *ptr);
int_least64_t atomic_exchange(atomic_int_least64_t *ptr, int_least64_t value);
void atomic_store(atomic_int_least64_t *ptr, int_least64_t value);

// ----- atomic_intptr_t siblings ---------------------------------------------
// The MSVC polyfill defines `atomic_intptr_t` and `atomic_int_least64_t` as
// distinct typedefs; the plain `atomic_load` / `atomic_store` / etc. above
// only accept `atomic_int_least64_t *`. Without these siblings, code that
// touches an `atomic_intptr_t` field (e.g. BOCRequest::next, BOCCown::last,
// BOCRecycleQueue::head, BOCQueue::tag, NB_NOTICEBOARD_TID) would silently
// pass a mistyped pointer to the int64 polyfill on Windows. On POSIX C11 the
// same names are aliased to the generic atomic_* macros (which already
// dispatch on type via _Generic), so user code below is platform-uniform.
//
// All Interlocked*Pointer intrinsics on x86/x64 are full barriers; the
// pointer-width matches `intptr_t` on both Win32 and Win64 (CPython itself
// requires a sane intptr_t == void* relationship).
static inline intptr_t atomic_load_intptr(atomic_intptr_t *ptr) { return *ptr; }

static inline void atomic_store_intptr(atomic_intptr_t *ptr, intptr_t value) {
  *ptr = value;
}

static inline intptr_t atomic_exchange_intptr(atomic_intptr_t *ptr,
                                              intptr_t value) {
  return (intptr_t)InterlockedExchangePointer((PVOID volatile *)ptr,
                                              (PVOID)value);
}

static inline bool atomic_compare_exchange_strong_intptr(atomic_intptr_t *ptr,
                                                         intptr_t *expected,
                                                         intptr_t desired) {
  intptr_t prev = (intptr_t)InterlockedCompareExchangePointer(
      (PVOID volatile *)ptr, (PVOID)desired, (PVOID)*expected);
  if (prev == *expected) {
    return true;
  }
  *expected = prev;
  return false;
}

// All Interlocked* intrinsics on x86/x64 are full barriers, so the
// memory_order argument is accepted but ignored.
// Note: atomic_load_explicit is a plain volatile read. On x86/x64 this
// provides acquire semantics due to TSO. Correctness of the parking
// protocol relies on the mutex-protected re-check, not on seq_cst ordering.
#define atomic_load_explicit(ptr, order) (*(ptr))
#define atomic_fetch_add_explicit(ptr, val, order)                             \
  InterlockedExchangeAdd64((ptr), (val))
#define atomic_fetch_sub_explicit(ptr, val, order)                             \
  InterlockedExchangeAdd64((ptr), -(val))
#define memory_order_seq_cst 0

// ---------------------------------------------------------------------------
// Windows: BOCMutex / BOCCond on SRWLOCK + CONDITION_VARIABLE
// ---------------------------------------------------------------------------

typedef SRWLOCK BOCMutex;
typedef CONDITION_VARIABLE BOCCond;

static inline void boc_mtx_init(BOCMutex *m) { InitializeSRWLock(m); }

static inline void mtx_destroy(BOCMutex *m) { (void)m; }

static inline void mtx_lock(BOCMutex *m) { AcquireSRWLockExclusive(m); }

static inline void mtx_unlock(BOCMutex *m) { ReleaseSRWLockExclusive(m); }

static inline void cnd_init(BOCCond *c) { InitializeConditionVariable(c); }

static inline void cnd_destroy(BOCCond *c) { (void)c; }

static inline void cnd_signal(BOCCond *c) { WakeConditionVariable(c); }

static inline void cnd_broadcast(BOCCond *c) { WakeAllConditionVariable(c); }

static inline void cnd_wait(BOCCond *c, BOCMutex *m) {
  SleepConditionVariableSRW(c, m, INFINITE, 0);
}

/// @brief Wait on a condition variable for at most @p seconds.
/// @param c The condition variable
/// @param m The mutex (must be held by caller)
/// @return true if signalled (or spurious wake), false if the timeout expired
static inline bool cnd_timedwait_s(BOCCond *c, BOCMutex *m, double seconds) {
  // Negated form catches NaN (every comparison with NaN is false),
  // which a bare `seconds < 0` test does not. Defence in depth
  // for the public boundary helper `boc_validate_finite_timeout`.
  if (!(seconds >= 0.0))
    seconds = 0.0;
  DWORD ms = (DWORD)(seconds * 1000.0);
  BOOL ok = SleepConditionVariableSRW(c, m, ms, 0);
  if (!ok && GetLastError() == ERROR_TIMEOUT) {
    return false;
  }
  return true;
}

void thrd_sleep(const struct timespec *duration, struct timespec *remaining);

// ---------------------------------------------------------------------------
// Windows: typed `boc_atomic_*_explicit` storage typedefs
// ---------------------------------------------------------------------------
//
// `volatile T` storage with distinct typedefs per width so the
// dispatch picks the right Interlocked* family. Note these are ordinary
// `volatile`, NOT C11 `_Atomic` — MSVC's `_Atomic` is gated behind
// `<stdatomic.h>` (VS 2022 17.5+) which is above bocpy's VS 2019 floor.

typedef volatile uint64_t boc_atomic_u64_t;
typedef volatile uint32_t boc_atomic_u32_t;
typedef volatile uint8_t boc_atomic_bool_t; // sizeof(bool) == 1
typedef void *volatile boc_atomic_ptr_t;

// ---------------------------------------------------------------------------
// Windows: typed `boc_atomic_*_explicit` implementations
// ---------------------------------------------------------------------------
//
// Switch on order, dispatch to Interlocked*. On x86/x64 every
// Interlocked* intrinsic is a full barrier, so all orderings collapse
// to the unsuffixed form (which is correct for any requested
// ordering). On ARM64 we pick the matching `_acq`/`_rel`/`_nf`
// variant. `BOC_MO_ACQ_REL` and `BOC_MO_SEQ_CST` use the unsuffixed
// (full barrier) form on every target.

#if defined(_M_ARM64)
#define BOC_IL_LOAD64_ACQ(p)                                                   \
  ((uint64_t)__ldar64((unsigned __int64 const volatile *)(p)))
#define BOC_IL_LOAD32_ACQ(p)                                                   \
  ((uint32_t)__ldar32((unsigned __int32 const volatile *)(p)))
#define BOC_IL_LOAD8_ACQ(p)                                                    \
  ((uint8_t)__ldar8((unsigned __int8 const volatile *)(p)))
#define BOC_IL_STORE64_REL(p, v)                                               \
  __stlr64((unsigned __int64 volatile *)(p), (unsigned __int64)(v))
#define BOC_IL_STORE32_REL(p, v)                                               \
  __stlr32((unsigned __int32 volatile *)(p), (unsigned __int32)(v))
#define BOC_IL_STORE8_REL(p, v)                                                \
  __stlr8((unsigned __int8 volatile *)(p), (unsigned __int8)(v))
#endif

// ---- u64 -------------------------------------------------------------------

static inline uint64_t boc_atomic_load_u64_explicit(boc_atomic_u64_t *p,
                                                    boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    return *p;
  case BOC_MO_ACQUIRE:
    return BOC_IL_LOAD64_ACQ(p);
  default:
    return BOC_IL_LOAD64_ACQ(p);
  }
#else
  (void)order;
  return *p;
#endif
}

static inline void boc_atomic_store_u64_explicit(boc_atomic_u64_t *p,
                                                 uint64_t v,
                                                 boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    *p = v;
    return;
  case BOC_MO_RELEASE:
    BOC_IL_STORE64_REL(p, v);
    return;
  default:
    (void)_InterlockedExchange64((volatile __int64 *)p, (__int64)v);
    return;
  }
#else
  (void)order;
  *p = v;
#endif
}

static inline uint64_t
boc_atomic_exchange_u64_explicit(boc_atomic_u64_t *p, uint64_t v,
                                 boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    return (uint64_t)_InterlockedExchange64_nf((volatile __int64 *)p,
                                               (__int64)v);
  case BOC_MO_ACQUIRE:
    return (uint64_t)_InterlockedExchange64_acq((volatile __int64 *)p,
                                                (__int64)v);
  case BOC_MO_RELEASE:
    return (uint64_t)_InterlockedExchange64_rel((volatile __int64 *)p,
                                                (__int64)v);
  default:
    return (uint64_t)_InterlockedExchange64((volatile __int64 *)p, (__int64)v);
  }
#else
  (void)order;
  return (uint64_t)_InterlockedExchange64((volatile __int64 *)p, (__int64)v);
#endif
}

static inline bool boc_atomic_compare_exchange_strong_u64_explicit(
    boc_atomic_u64_t *p, uint64_t *expected, uint64_t desired,
    boc_memory_order_t succ, boc_memory_order_t fail) {
  (void)fail;
  uint64_t exp = *expected;
  uint64_t prev;
#if defined(_M_ARM64)
  switch (succ) {
  case BOC_MO_RELAXED:
    prev = (uint64_t)_InterlockedCompareExchange64_nf(
        (volatile __int64 *)p, (__int64)desired, (__int64)exp);
    break;
  case BOC_MO_ACQUIRE:
    prev = (uint64_t)_InterlockedCompareExchange64_acq(
        (volatile __int64 *)p, (__int64)desired, (__int64)exp);
    break;
  case BOC_MO_RELEASE:
    prev = (uint64_t)_InterlockedCompareExchange64_rel(
        (volatile __int64 *)p, (__int64)desired, (__int64)exp);
    break;
  default:
    prev = (uint64_t)_InterlockedCompareExchange64(
        (volatile __int64 *)p, (__int64)desired, (__int64)exp);
    break;
  }
#else
  (void)succ;
  prev = (uint64_t)_InterlockedCompareExchange64(
      (volatile __int64 *)p, (__int64)desired, (__int64)exp);
#endif
  if (prev == exp)
    return true;
  *expected = prev;
  return false;
}

static inline uint64_t
boc_atomic_fetch_add_u64_explicit(boc_atomic_u64_t *p, uint64_t v,
                                  boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    return (uint64_t)_InterlockedExchangeAdd64_nf((volatile __int64 *)p,
                                                  (__int64)v);
  case BOC_MO_ACQUIRE:
    return (uint64_t)_InterlockedExchangeAdd64_acq((volatile __int64 *)p,
                                                   (__int64)v);
  case BOC_MO_RELEASE:
    return (uint64_t)_InterlockedExchangeAdd64_rel((volatile __int64 *)p,
                                                   (__int64)v);
  default:
    return (uint64_t)_InterlockedExchangeAdd64((volatile __int64 *)p,
                                               (__int64)v);
  }
#else
  (void)order;
  return (uint64_t)_InterlockedExchangeAdd64((volatile __int64 *)p, (__int64)v);
#endif
}

static inline uint64_t
boc_atomic_fetch_sub_u64_explicit(boc_atomic_u64_t *p, uint64_t v,
                                  boc_memory_order_t order) {
  return boc_atomic_fetch_add_u64_explicit(p, (uint64_t)(-(int64_t)v), order);
}

// ---- u32 -------------------------------------------------------------------

static inline uint32_t boc_atomic_load_u32_explicit(boc_atomic_u32_t *p,
                                                    boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    return *p;
  case BOC_MO_ACQUIRE:
    return BOC_IL_LOAD32_ACQ(p);
  default:
    return BOC_IL_LOAD32_ACQ(p);
  }
#else
  (void)order;
  return *p;
#endif
}

static inline void boc_atomic_store_u32_explicit(boc_atomic_u32_t *p,
                                                 uint32_t v,
                                                 boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    *p = v;
    return;
  case BOC_MO_RELEASE:
    BOC_IL_STORE32_REL(p, v);
    return;
  default:
    (void)_InterlockedExchange((volatile long *)p, (long)v);
    return;
  }
#else
  (void)order;
  *p = v;
#endif
}

static inline uint32_t
boc_atomic_exchange_u32_explicit(boc_atomic_u32_t *p, uint32_t v,
                                 boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    return (uint32_t)_InterlockedExchange_nf((volatile long *)p, (long)v);
  case BOC_MO_ACQUIRE:
    return (uint32_t)_InterlockedExchange_acq((volatile long *)p, (long)v);
  case BOC_MO_RELEASE:
    return (uint32_t)_InterlockedExchange_rel((volatile long *)p, (long)v);
  default:
    return (uint32_t)_InterlockedExchange((volatile long *)p, (long)v);
  }
#else
  (void)order;
  return (uint32_t)_InterlockedExchange((volatile long *)p, (long)v);
#endif
}

static inline bool boc_atomic_compare_exchange_strong_u32_explicit(
    boc_atomic_u32_t *p, uint32_t *expected, uint32_t desired,
    boc_memory_order_t succ, boc_memory_order_t fail) {
  (void)fail;
  uint32_t exp = *expected;
  uint32_t prev;
#if defined(_M_ARM64)
  switch (succ) {
  case BOC_MO_RELAXED:
    prev = (uint32_t)_InterlockedCompareExchange_nf((volatile long *)p,
                                                    (long)desired, (long)exp);
    break;
  case BOC_MO_ACQUIRE:
    prev = (uint32_t)_InterlockedCompareExchange_acq((volatile long *)p,
                                                     (long)desired, (long)exp);
    break;
  case BOC_MO_RELEASE:
    prev = (uint32_t)_InterlockedCompareExchange_rel((volatile long *)p,
                                                     (long)desired, (long)exp);
    break;
  default:
    prev = (uint32_t)_InterlockedCompareExchange((volatile long *)p,
                                                 (long)desired, (long)exp);
    break;
  }
#else
  (void)succ;
  prev = (uint32_t)_InterlockedCompareExchange((volatile long *)p,
                                               (long)desired, (long)exp);
#endif
  if (prev == exp)
    return true;
  *expected = prev;
  return false;
}

static inline uint32_t
boc_atomic_fetch_add_u32_explicit(boc_atomic_u32_t *p, uint32_t v,
                                  boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    return (uint32_t)_InterlockedExchangeAdd_nf((volatile long *)p, (long)v);
  case BOC_MO_ACQUIRE:
    return (uint32_t)_InterlockedExchangeAdd_acq((volatile long *)p, (long)v);
  case BOC_MO_RELEASE:
    return (uint32_t)_InterlockedExchangeAdd_rel((volatile long *)p, (long)v);
  default:
    return (uint32_t)_InterlockedExchangeAdd((volatile long *)p, (long)v);
  }
#else
  (void)order;
  return (uint32_t)_InterlockedExchangeAdd((volatile long *)p, (long)v);
#endif
}

static inline uint32_t
boc_atomic_fetch_sub_u32_explicit(boc_atomic_u32_t *p, uint32_t v,
                                  boc_memory_order_t order) {
  return boc_atomic_fetch_add_u32_explicit(p, (uint32_t)(-(int32_t)v), order);
}

// ---- bool (uint8_t storage) ------------------------------------------------
// MSVC has no Interlocked*8 with order suffixes pre-VS-2022; we use the
// unsuffixed Interlocked*8 (full barrier) for exchange/cas, which satisfies
// any requested ordering. Plain volatile load/store on a 1-byte slot is
// atomic on every supported MSVC target (ARM64 included; the architecture
// guarantees aligned single-byte access atomicity).

static inline bool boc_atomic_load_bool_explicit(boc_atomic_bool_t *p,
                                                 boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    return (bool)*p;
  case BOC_MO_ACQUIRE:
    return (bool)BOC_IL_LOAD8_ACQ(p);
  default:
    return (bool)BOC_IL_LOAD8_ACQ(p);
  }
#else
  (void)order;
  return (bool)*p;
#endif
}

static inline void boc_atomic_store_bool_explicit(boc_atomic_bool_t *p, bool v,
                                                  boc_memory_order_t order) {
#if defined(_M_ARM64)
  switch (order) {
  case BOC_MO_RELAXED:
    *p = (uint8_t)v;
    return;
  case BOC_MO_RELEASE:
    BOC_IL_STORE8_REL(p, (uint8_t)v);
    return;
  default:
    (void)_InterlockedExchange8((volatile char *)p, (char)v);
    return;
  }
#else
  (void)order;
  *p = (uint8_t)v;
#endif
}

static inline bool boc_atomic_exchange_bool_explicit(boc_atomic_bool_t *p,
                                                     bool v,
                                                     boc_memory_order_t order) {
  (void)order;
  return (bool)_InterlockedExchange8((volatile char *)p, (char)v);
}

static inline bool boc_atomic_compare_exchange_strong_bool_explicit(
    boc_atomic_bool_t *p, bool *expected, bool desired, boc_memory_order_t succ,
    boc_memory_order_t fail) {
  (void)succ;
  (void)fail;
  char exp = (char)*expected;
  char prev =
      _InterlockedCompareExchange8((volatile char *)p, (char)desired, exp);
  if (prev == exp)
    return true;
  *expected = (bool)prev;
  return false;
}

// ---- ptr -------------------------------------------------------------------

static inline void *boc_atomic_load_ptr_explicit(boc_atomic_ptr_t *p,
                                                 boc_memory_order_t order) {
  // InterlockedCompareExchangePointerNoFence is the cleanest way to express
  // a relaxed atomic pointer load, but a plain volatile read suffices on
  // every supported target (pointer width matches the natural word size).
  (void)order;
  return (void *)*p;
}

static inline void boc_atomic_store_ptr_explicit(boc_atomic_ptr_t *p, void *v,
                                                 boc_memory_order_t order) {
#if defined(_M_ARM64)
  if (order == BOC_MO_RELAXED) {
    *p = v;
    return;
  }
  (void)InterlockedExchangePointer((PVOID volatile *)p, (PVOID)v);
#else
  (void)order;
  *p = v;
#endif
}

static inline void *boc_atomic_exchange_ptr_explicit(boc_atomic_ptr_t *p,
                                                     void *v,
                                                     boc_memory_order_t order) {
  (void)order;
  return (void *)InterlockedExchangePointer((PVOID volatile *)p, (PVOID)v);
}

static inline bool boc_atomic_compare_exchange_strong_ptr_explicit(
    boc_atomic_ptr_t *p, void **expected, void *desired,
    boc_memory_order_t succ, boc_memory_order_t fail) {
  (void)succ;
  (void)fail;
  void *exp = *expected;
  void *prev = InterlockedCompareExchangePointer((PVOID volatile *)p,
                                                 (PVOID)desired, (PVOID)exp);
  if (prev == exp)
    return true;
  *expected = prev;
  return false;
}

// Standalone memory fence. `MemoryBarrier()` is a full hardware
// barrier on every supported MSVC target (x86, x64, ARM64) and
// matches the strongest standalone fence we ever need from this
// helper. Mapping every `BOC_MO_*` to a full barrier is correct
// (over-strong is safe; under-strong is not) and keeps the
// implementation a one-liner.
static inline void boc_atomic_thread_fence_explicit(boc_memory_order_t o) {
  (void)o;
  MemoryBarrier();
}

#else // _WIN32

// ---------------------------------------------------------------------------
// POSIX (Apple + Linux): shared headers, thread_local, yield, intptr aliases
// ---------------------------------------------------------------------------

#include <errno.h>
#include <sched.h>
#include <stdatomic.h>
#include <unistd.h>

#define thread_local _Thread_local
#define boc_yield() sched_yield()

// On POSIX the C11 atomic_* macros dispatch on type via _Generic, so the
// `atomic_load(&intptr_var)` form Just Works. The `_intptr` siblings are
// aliased to the generic forms purely so the source reads the same on
// every platform; on Windows they expand to dedicated InterlockedXxxPointer
// shims (see polyfill block above).
#define atomic_load_intptr(ptr) atomic_load(ptr)
#define atomic_store_intptr(ptr, val) atomic_store((ptr), (val))
#define atomic_exchange_intptr(ptr, val) atomic_exchange((ptr), (val))
#define atomic_compare_exchange_strong_intptr(ptr, expected, desired)          \
  atomic_compare_exchange_strong((ptr), (expected), (desired))

#ifdef __APPLE__

// ---------------------------------------------------------------------------
// Apple: pthread-based BOCMutex / BOCCond
// ---------------------------------------------------------------------------

#include <pthread.h>
#define thrd_sleep nanosleep

typedef pthread_mutex_t BOCMutex;
typedef pthread_cond_t BOCCond;

static inline void boc_mtx_init(BOCMutex *m) { pthread_mutex_init(m, NULL); }

static inline void mtx_destroy(BOCMutex *m) { pthread_mutex_destroy(m); }

static inline void mtx_lock(BOCMutex *m) { pthread_mutex_lock(m); }

static inline void mtx_unlock(BOCMutex *m) { pthread_mutex_unlock(m); }

static inline void cnd_init(BOCCond *c) { pthread_cond_init(c, NULL); }

static inline void cnd_destroy(BOCCond *c) { pthread_cond_destroy(c); }

static inline void cnd_signal(BOCCond *c) { pthread_cond_signal(c); }

static inline void cnd_broadcast(BOCCond *c) { pthread_cond_broadcast(c); }

static inline void cnd_wait(BOCCond *c, BOCMutex *m) {
  pthread_cond_wait(c, m);
}

/// @brief Wait on a condition variable for at most @p seconds.
/// @param c The condition variable
/// @param m The mutex (must be held by caller)
/// @return true if signalled (or spurious wake), false if the timeout expired
static inline bool cnd_timedwait_s(BOCCond *c, BOCMutex *m, double seconds) {
  // Negated form catches NaN (every comparison with NaN is false),
  // which a bare `seconds < 0` test does not. Defence in depth
  // for the public boundary helper `boc_validate_finite_timeout`.
  if (!(seconds >= 0.0))
    seconds = 0.0;
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  double total = (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9 + seconds;
  ts.tv_sec = (time_t)total;
  ts.tv_nsec = (long)((total - (double)ts.tv_sec) * 1e9);
  if (ts.tv_nsec >= 1000000000L) {
    ts.tv_sec += 1;
    ts.tv_nsec -= 1000000000L;
  }
  int rc = pthread_cond_timedwait(c, m, &ts);
  return rc != ETIMEDOUT;
}

#else // __APPLE__

// ---------------------------------------------------------------------------
// Linux (and other non-Apple POSIX): C11 <threads.h>-based BOCMutex / BOCCond
// ---------------------------------------------------------------------------

#include <threads.h>

typedef mtx_t BOCMutex;
typedef cnd_t BOCCond;

static inline void boc_mtx_init(BOCMutex *m) { mtx_init(m, mtx_plain); }

/// @brief Wait on a condition variable for at most @p seconds.
/// @param c The condition variable
/// @param m The mutex (must be held by caller)
/// @return true if signalled (or spurious wake), false if the timeout expired
static inline bool cnd_timedwait_s(BOCCond *c, BOCMutex *m, double seconds) {
  // Negated form catches NaN (every comparison with NaN is false),
  // which a bare `seconds < 0` test does not. Defence in depth
  // for the public boundary helper `boc_validate_finite_timeout`.
  if (!(seconds >= 0.0))
    seconds = 0.0;
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  double total = (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9 + seconds;
  ts.tv_sec = (time_t)total;
  ts.tv_nsec = (long)((total - (double)ts.tv_sec) * 1e9);
  if (ts.tv_nsec >= 1000000000L) {
    ts.tv_sec += 1;
    ts.tv_nsec -= 1000000000L;
  }
  int rc = cnd_timedwait(c, m, &ts);
  return rc != thrd_timedout;
}

#endif // __APPLE__

// ---------------------------------------------------------------------------
// POSIX: typed `boc_atomic_*_explicit` API on top of <stdatomic.h>
// ---------------------------------------------------------------------------
//
// The compiler folds these wrappers away. Legacy `atomic_*` callers
// are unaffected; the new API is purely additive.

typedef _Atomic uint64_t boc_atomic_u64_t;
typedef _Atomic uint32_t boc_atomic_u32_t;
typedef _Atomic bool boc_atomic_bool_t;
typedef _Atomic(void *) boc_atomic_ptr_t;

static inline memory_order boc_mo_to_std(boc_memory_order_t order) {
  switch (order) {
  case BOC_MO_RELAXED:
    return memory_order_relaxed;
  case BOC_MO_ACQUIRE:
    return memory_order_acquire;
  case BOC_MO_RELEASE:
    return memory_order_release;
  case BOC_MO_ACQ_REL:
    return memory_order_acq_rel;
  case BOC_MO_SEQ_CST:
  default:
    return memory_order_seq_cst;
  }
}

#define BOC_ATOMIC_OPS_(SUF, T, AT)                                            \
  static inline T boc_atomic_load_##SUF##_explicit(AT *p,                      \
                                                   boc_memory_order_t o) {     \
    return atomic_load_explicit(p, boc_mo_to_std(o));                          \
  }                                                                            \
  static inline void boc_atomic_store_##SUF##_explicit(AT *p, T v,             \
                                                       boc_memory_order_t o) { \
    atomic_store_explicit(p, v, boc_mo_to_std(o));                             \
  }                                                                            \
  static inline T boc_atomic_exchange_##SUF##_explicit(AT *p, T v,             \
                                                       boc_memory_order_t o) { \
    return atomic_exchange_explicit(p, v, boc_mo_to_std(o));                   \
  }                                                                            \
  static inline bool boc_atomic_compare_exchange_strong_##SUF##_explicit(      \
      AT *p, T *expected, T desired, boc_memory_order_t succ,                  \
      boc_memory_order_t fail) {                                               \
    return atomic_compare_exchange_strong_explicit(                            \
        p, expected, desired, boc_mo_to_std(succ), boc_mo_to_std(fail));       \
  }

BOC_ATOMIC_OPS_(u64, uint64_t, boc_atomic_u64_t)
BOC_ATOMIC_OPS_(u32, uint32_t, boc_atomic_u32_t)
BOC_ATOMIC_OPS_(bool, bool, boc_atomic_bool_t)

// `ptr` carries `void *` payload but the underlying storage is
// `_Atomic(void *)`; cast at the API edge to keep call sites clean.
static inline void *boc_atomic_load_ptr_explicit(boc_atomic_ptr_t *p,
                                                 boc_memory_order_t o) {
  return atomic_load_explicit(p, boc_mo_to_std(o));
}
static inline void boc_atomic_store_ptr_explicit(boc_atomic_ptr_t *p, void *v,
                                                 boc_memory_order_t o) {
  atomic_store_explicit(p, v, boc_mo_to_std(o));
}
static inline void *boc_atomic_exchange_ptr_explicit(boc_atomic_ptr_t *p,
                                                     void *v,
                                                     boc_memory_order_t o) {
  return atomic_exchange_explicit(p, v, boc_mo_to_std(o));
}
static inline bool boc_atomic_compare_exchange_strong_ptr_explicit(
    boc_atomic_ptr_t *p, void **expected, void *desired,
    boc_memory_order_t succ, boc_memory_order_t fail) {
  return atomic_compare_exchange_strong_explicit(
      p, expected, desired, boc_mo_to_std(succ), boc_mo_to_std(fail));
}

#define BOC_ATOMIC_FETCH_OPS_(SUF, T, AT)                                      \
  static inline T boc_atomic_fetch_add_##SUF##_explicit(                       \
      AT *p, T v, boc_memory_order_t o) {                                      \
    return atomic_fetch_add_explicit(p, v, boc_mo_to_std(o));                  \
  }                                                                            \
  static inline T boc_atomic_fetch_sub_##SUF##_explicit(                       \
      AT *p, T v, boc_memory_order_t o) {                                      \
    return atomic_fetch_sub_explicit(p, v, boc_mo_to_std(o));                  \
  }

BOC_ATOMIC_FETCH_OPS_(u64, uint64_t, boc_atomic_u64_t)
BOC_ATOMIC_FETCH_OPS_(u32, uint32_t, boc_atomic_u32_t)

#undef BOC_ATOMIC_OPS_
#undef BOC_ATOMIC_FETCH_OPS_

// Standalone memory fence. POSIX delegates to `atomic_thread_fence`
// from `<stdatomic.h>`; the helper exists so MSVC can express the
// same operation via `MemoryBarrier()` without C11 atomics.
static inline void boc_atomic_thread_fence_explicit(boc_memory_order_t o) {
  atomic_thread_fence(boc_mo_to_std(o));
}

#endif // _WIN32

// ===========================================================================
// Cross-platform monotonic time / sleep helpers
// ===========================================================================

/// @brief Returns the current time as double-precision seconds.
/// @return the current time
double boc_now_s(void);

/// @brief Returns a monotonic timestamp in nanoseconds.
/// @details Uses @c CLOCK_MONOTONIC on POSIX and
/// @c QueryPerformanceCounter on Windows. Unlike @ref boc_now_s the
/// returned value is guaranteed monotonic non-decreasing within a
/// single process: it is suitable for measuring elapsed durations
/// (e.g. the work-stealing quiescence timeout) but not for wall-clock
/// reporting. Wraps after ~584 years on a 64-bit unsigned counter; we
/// only ever subtract two readings taken seconds apart, so wraparound
/// is a non-issue.
/// @return Monotonic time in nanoseconds since an unspecified epoch.
uint64_t boc_now_ns(void);

/// @brief Sleep the calling thread for at least @p ns nanoseconds.
/// @details Thin wrapper around @ref thrd_sleep that hides the
/// @c struct timespec construction so callers never need to include
/// @c <time.h> just to back off. Splits @p ns into seconds plus a
/// sub-second remainder so values larger than one second are
/// representable.
/// @param ns Nanoseconds to sleep. Zero is a no-op return.
void boc_sleep_ns(uint64_t ns);

// ===========================================================================
// Cross-platform timeout-validation helper
// ===========================================================================
//
// Public boundary helper for the @c terminator_wait / @c notice_sync_wait
// entry points. Centralising the NaN/Inf/negative classification here
// keeps the policy in one place: NaN is a programmer error and surfaces
// as @c ValueError; +Inf is "wait forever"; negative is clamped to 0
// (no-wait, returns immediately). Without this, NaN passed straight
// to @c cnd_timedwait_s would compute @c DWORD ms via @c (DWORD)(NaN *
// 1000.0) — undefined behaviour on Windows and a wedged-forever wait
// on POSIX.
//
// Returns 0 on success (with @p *wait_forever set); -1 on failure with
// a Python exception set.

static inline int boc_validate_finite_timeout(double seconds,
                                              double *out_seconds,
                                              bool *out_wait_forever) {
  // NaN: a comparison with NaN is always false, so `seconds == seconds`
  // is the canonical portable NaN check (no math.h dependency).
  if (seconds != seconds) {
    PyErr_SetString(PyExc_ValueError, "timeout must not be NaN");
    return -1;
  }
  // +Inf or any value that the cnd_timedwait clamp would treat as
  // "wait forever" maps to wait_forever=true. Use a finite sentinel
  // (DBL_MAX) rather than HUGE_VAL to keep the helper free of math.h
  // — the operational meaning is identical.
  //
  // We clamp at 1e9 seconds (~31.7 years) rather than DBL_MAX so
  // any caller-supplied value that would overflow `time_t` (signed
  // 32-bit on some platforms: ~68 years) or the `DWORD` millisecond
  // arg to Win32 `SleepConditionVariableSRW` (max ~49 days) also
  // routes through the wait-forever path. Operationally a 31-year
  // wait is indistinguishable from "wait forever" for any realistic
  // bocpy caller, and the clamp is the only safe way to avoid
  // platform-dependent overflow into a sub-second wait or UB.
  if (seconds > 1e9) {
    *out_seconds = 0.0;
    *out_wait_forever = true;
    return 0;
  }
  // Negative: caller asked for "no wait". Clamp to 0 and return; the
  // wait helpers will short-circuit with a timeout immediately.
  if (seconds < 0.0) {
    *out_seconds = 0.0;
    *out_wait_forever = false;
    return 0;
  }
  *out_seconds = seconds;
  *out_wait_forever = false;
  return 0;
}

#endif // BOCPY_COMPAT_H
