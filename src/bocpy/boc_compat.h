/// @file boc_compat.h
/// @brief Cross-platform portability shims for bocpy C extensions.
///
/// Centralises the platform-specific atomic, mutex, condition-variable,
/// thread-local, sleep, and monotonic-time primitives used by `_core.c`,
/// `_math.c`, and `boc_sched.c`.
///
/// **Linkage:** all heavy-weight platform primitives are exposed as
/// `static inline` wrappers around the platform's native API, except for
/// the MSVC `atomic_*` functions on `int_least64_t` (kept as out-of-line
/// definitions in `boc_compat.c` to preserve their original symbol shape).
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

/// @brief Portable stand-in for C11's @c max_align_t.
/// @details Avoids C11-mode @c max_align_t which MSVC only exposes under @c
/// /std:c11 (not set by the CPython build).
typedef union boc_max_align {
  long long _ll;
  long double _ld;
  void *_p;
  void (*_fp)(void);
} boc_max_align_t;

typedef enum {
  BOC_MO_RELAXED = 0,
  BOC_MO_ACQUIRE = 2,
  BOC_MO_RELEASE = 3,
  BOC_MO_ACQ_REL = 4,
  BOC_MO_SEQ_CST = 5,
} boc_memory_order_t;

#ifdef _WIN32

#define WIN32_LEAN_AND_MEAN
#include <process.h>
#include <windows.h>

#define thread_local __declspec(thread)
#define boc_yield() SwitchToThread()

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

#if defined(_M_IX86)
#define atomic_load_explicit(ptr, order)                                       \
  ((int_least64_t)InterlockedCompareExchange64((ptr), 0, 0))
#define atomic_fetch_add_explicit(ptr, val, order)                             \
  atomic_fetch_add((ptr), (val))
#define atomic_fetch_sub_explicit(ptr, val, order)                             \
  atomic_fetch_sub((ptr), (val))
#else
#define atomic_load_explicit(ptr, order) (*(ptr))
#define atomic_fetch_add_explicit(ptr, val, order)                             \
  InterlockedExchangeAdd64((ptr), (val))
#define atomic_fetch_sub_explicit(ptr, val, order)                             \
  InterlockedExchangeAdd64((ptr), -(val))
#endif
#define memory_order_seq_cst 0

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

typedef volatile uint64_t boc_atomic_u64_t;
typedef volatile uint32_t boc_atomic_u32_t;
typedef volatile uint8_t boc_atomic_bool_t;
typedef void *volatile boc_atomic_ptr_t;

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
#elif defined(_M_IX86)
  (void)order;
  return (uint64_t)_InterlockedCompareExchange64((volatile __int64 *)p, 0, 0);
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
#elif defined(_M_IX86)
  (void)order;
  __int64 old = *((volatile __int64 *)p);
  for (;;) {
    __int64 prev =
        _InterlockedCompareExchange64((volatile __int64 *)p, (__int64)v, old);
    if (prev == old)
      return;
    old = prev;
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
#elif defined(_M_IX86)
  (void)order;
  __int64 old = *((volatile __int64 *)p);
  for (;;) {
    __int64 prev =
        _InterlockedCompareExchange64((volatile __int64 *)p, (__int64)v, old);
    if (prev == old)
      return (uint64_t)old;
    old = prev;
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
#elif defined(_M_IX86)
  (void)order;
  __int64 old = *((volatile __int64 *)p);
  for (;;) {
    __int64 desired = old + (__int64)v;
    __int64 prev =
        _InterlockedCompareExchange64((volatile __int64 *)p, desired, old);
    if (prev == old)
      return (uint64_t)old;
    old = prev;
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

static inline void *boc_atomic_load_ptr_explicit(boc_atomic_ptr_t *p,
                                                 boc_memory_order_t order) {
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

static inline void boc_atomic_thread_fence_explicit(boc_memory_order_t o) {
  (void)o;
  MemoryBarrier();
}

#else // _WIN32

#include <errno.h>
#include <sched.h>
#include <stdatomic.h>
#include <unistd.h>

#define thread_local _Thread_local
#define boc_yield() sched_yield()

#define atomic_load_intptr(ptr) atomic_load(ptr)
#define atomic_store_intptr(ptr, val) atomic_store((ptr), (val))
#define atomic_exchange_intptr(ptr, val) atomic_exchange((ptr), (val))
#define atomic_compare_exchange_strong_intptr(ptr, expected, desired)          \
  atomic_compare_exchange_strong((ptr), (expected), (desired))

#ifdef __APPLE__

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

#include <threads.h>

typedef mtx_t BOCMutex;
typedef cnd_t BOCCond;

static inline void boc_mtx_init(BOCMutex *m) { mtx_init(m, mtx_plain); }

/// @brief Wait on a condition variable for at most @p seconds.
/// @param c The condition variable
/// @param m The mutex (must be held by caller)
/// @return true if signalled (or spurious wake), false if the timeout expired
static inline bool cnd_timedwait_s(BOCCond *c, BOCMutex *m, double seconds) {
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

static inline void boc_atomic_thread_fence_explicit(boc_memory_order_t o) {
  atomic_thread_fence(boc_mo_to_std(o));
}

#endif // _WIN32

/// @brief Returns the current time as double-precision seconds.
/// @return the current time
double boc_now_s(void);

/// @brief Best-effort count of physical CPU cores available to this process.
/// @details Unlike @c sysconf(_SC_NPROCESSORS_ONLN) /
/// @c os.cpu_count(), this excludes hyperthread / SMT siblings so it
/// matches the count of independent execution units. Used to size the
/// default worker pool: oversubscribing CPU-bound Python workloads on
/// HT siblings causes the two siblings on a physical core to fight for
/// the same execution resources, often halving throughput vs. one
/// worker per physical core.
///
/// **Per-platform behaviour.**
/// - **Linux**: walks @c
/// /sys/devices/system/cpu/cpu*/topology/thread_siblings_list,
///   counts distinct sibling sets, and intersects with
///   @c sched_getaffinity(0) so cgroup / container CPU restrictions
///   are honoured.
/// - **macOS**: @c sysctlbyname("hw.physicalcpu_max", ...) (falling
///   back to @c "hw.physicalcpu").
/// - **Windows**: @c GetLogicalProcessorInformationEx with
///   @c RelationProcessorCore.
///
/// On any platform where detection fails (sysfs unreadable, sysctl /
/// API failure), returns 0; callers should fall back to the logical
/// CPU count in that case.
/// @return Number of physical cores available to the process, or 0
///         on failure.
int boc_physical_cpu_count(void);

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

static inline int boc_validate_finite_timeout(double seconds,
                                              double *out_seconds,
                                              bool *out_wait_forever) {
  if (seconds != seconds) {
    PyErr_SetString(PyExc_ValueError, "timeout must not be NaN");
    return -1;
  }
  if (seconds > 1e9) {
    *out_seconds = 0.0;
    *out_wait_forever = true;
    return 0;
  }
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
