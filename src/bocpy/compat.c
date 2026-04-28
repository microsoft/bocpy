/// @file compat.c
/// @brief Out-of-line definitions for the cross-platform shims declared in
///        `compat.h`.
///
/// On POSIX the C11 `<stdatomic.h>` machinery is fully header-only, so this
/// translation unit is essentially empty there. On MSVC the `atomic_*`
/// functions on `int_least64_t` are kept as out-of-line definitions
/// (linked into `_core.o` and `_math.o` from `compat.o`).

#include "compat.h"

#ifdef _WIN32

int_least64_t atomic_fetch_add(atomic_int_least64_t *ptr, int_least64_t value) {
  return InterlockedExchangeAdd64(ptr, value);
}

int_least64_t atomic_fetch_sub(atomic_int_least64_t *ptr, int_least64_t value) {
  return InterlockedExchangeAdd64(ptr, -value);
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

int_least64_t atomic_load(atomic_int_least64_t *ptr) { return *ptr; }

int_least64_t atomic_exchange(atomic_int_least64_t *ptr, int_least64_t value) {
  return InterlockedExchange64(ptr, value);
}

void atomic_store(atomic_int_least64_t *ptr, int_least64_t value) {
  *ptr = value;
}

void thrd_sleep(const struct timespec *duration, struct timespec *remaining) {
  const DWORD MS_PER_NS = 1000000;
  DWORD ms = (DWORD)duration->tv_sec * 1000;
  ms += (DWORD)duration->tv_nsec / MS_PER_NS;
  Sleep(ms);
}

#endif // _WIN32

double boc_now_s(void) {
  const double S_PER_NS = 1.0e-9;
  struct timespec ts;
  // Prefer clock_gettime on POSIX: timespec_get requires macOS 10.15+ while
  // Python's default macOS deployment target is older, producing an
  // -Wunguarded-availability-new warning. clock_gettime has been available on
  // macOS since 10.12. Windows UCRT provides timespec_get but not
  // clock_gettime, so fall back there.
#ifdef _WIN32
  timespec_get(&ts, TIME_UTC);
#else
  clock_gettime(CLOCK_REALTIME, &ts);
#endif
  double time = (double)ts.tv_sec;
  time += ts.tv_nsec * S_PER_NS;
  return time;
}

uint64_t boc_now_ns(void) {
#ifdef _WIN32
  // QueryPerformanceCounter is monotonic and high-resolution on every
  // Windows version we target; the frequency is queried once and
  // cached because it is constant for the lifetime of the system.
  static LARGE_INTEGER freq = {0};
  if (freq.QuadPart == 0) {
    QueryPerformanceFrequency(&freq);
  }
  LARGE_INTEGER counter;
  QueryPerformanceCounter(&counter);
  // Convert ticks -> ns without overflow on a 64-bit counter for any
  // realistic frequency (<= 10 GHz): split into seconds + remainder.
  uint64_t sec = (uint64_t)counter.QuadPart / (uint64_t)freq.QuadPart;
  uint64_t rem = (uint64_t)counter.QuadPart % (uint64_t)freq.QuadPart;
  return sec * 1000000000ULL + (rem * 1000000000ULL) / (uint64_t)freq.QuadPart;
#else
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
#endif
}

void boc_sleep_ns(uint64_t ns) {
  if (ns == 0) {
    return;
  }
  struct timespec duration;
  duration.tv_sec = (time_t)(ns / 1000000000ULL);
  duration.tv_nsec = (long)(ns % 1000000000ULL);
  thrd_sleep(&duration, NULL);
}
