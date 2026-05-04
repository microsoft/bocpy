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

int_least64_t atomic_fetch_sub(atomic_int_least64_t *ptr, int_least64_t value) {
#if defined(_M_IX86)
  return atomic_fetch_add(ptr, -value);
#else
  return InterlockedExchangeAdd64(ptr, -value);
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
  return *ptr;
#endif
}

int_least64_t atomic_exchange(atomic_int_least64_t *ptr, int_least64_t value) {
#if defined(_M_IX86)
  int_least64_t old = *ptr;
  for (;;) {
    int_least64_t prev = InterlockedCompareExchange64(ptr, value, old);
    if (prev == old)
      return old;
    old = prev;
  }
#else
  return InterlockedExchange64(ptr, value);
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
  *ptr = value;
#endif
}

void thrd_sleep(const struct timespec *duration, struct timespec *remaining) {
  const DWORD MS_PER_NS = 1000000;
  DWORD ms = (DWORD)duration->tv_sec * 1000;
  ms += (DWORD)duration->tv_nsec / MS_PER_NS;
  Sleep(ms);
}

// ---------------------------------------------------------------------------
// Physical CPU detection (Windows arm). See compat.h for contract.
// ---------------------------------------------------------------------------

int boc_physical_cpu_count(void) {
  DWORD len = 0;
  // First call: query required buffer size.
  GetLogicalProcessorInformationEx(RelationProcessorCore, NULL, &len);
  if (len == 0) {
    return 0;
  }
  BYTE *buf = (BYTE *)malloc((size_t)len);
  if (buf == NULL) {
    return 0;
  }
  if (!GetLogicalProcessorInformationEx(
          RelationProcessorCore, (SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX *)buf,
          &len)) {
    free(buf);
    return 0;
  }
  // One record per physical core; count records.
  int count = 0;
  DWORD off = 0;
  while (off < len) {
    SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX *info =
        (SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX *)(buf + off);
    if (info->Relationship == RelationProcessorCore) {
      ++count;
    }
    off += info->Size;
  }
  free(buf);
  return count;
}

#elif defined(__APPLE__)

// ---------------------------------------------------------------------------
// Physical CPU detection (macOS arm). See compat.h for contract.
// ---------------------------------------------------------------------------

#include <sys/sysctl.h>

int boc_physical_cpu_count(void) {
  int value = 0;
  size_t len = sizeof(value);
  // hw.physicalcpu_max reports physical cores in the machine; fall back
  // to hw.physicalcpu (cores currently available) if unavailable.
  if (sysctlbyname("hw.physicalcpu_max", &value, &len, NULL, 0) == 0 &&
      value > 0) {
    return value;
  }
  len = sizeof(value);
  if (sysctlbyname("hw.physicalcpu", &value, &len, NULL, 0) == 0 && value > 0) {
    return value;
  }
  return 0;
}

#else // assume Linux / glibc-compatible

// ---------------------------------------------------------------------------
// Physical CPU detection (Linux arm). See compat.h for contract.
// ---------------------------------------------------------------------------

#include <ctype.h>
#include <dirent.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/// @brief Read the first comma/range-separated CPU id from a
/// thread_siblings_list file.
/// @details Sibling lists look like "0,28" or "0-1" or "0". The first
/// id is the canonical leader of the sibling set; counting distinct
/// leaders across all CPUs yields the physical-core count.
/// @return The leader CPU id, or -1 on parse failure.
static int boc_read_first_sibling(const char *path) {
  FILE *f = fopen(path, "r");
  if (f == NULL) {
    return -1;
  }
  int id = -1;
  if (fscanf(f, "%d", &id) != 1) {
    id = -1;
  }
  fclose(f);
  return id;
}

int boc_physical_cpu_count(void) {
  // Snapshot the affinity mask first so cgroup / taskset restrictions
  // are honoured. Without this the count would be host-physical even
  // inside a container with --cpuset-cpus=0-3.
  cpu_set_t affinity;
  CPU_ZERO(&affinity);
  bool have_affinity = (sched_getaffinity(0, sizeof(affinity), &affinity) == 0);

  // Per-CPU sibling-leader array. Sized to the kernel's maximum
  // reasonable CPU id; CPU_SETSIZE is 1024 on glibc, which exceeds
  // any current hardware.
  enum { MAX_CPU = 4096 };
  int leaders[MAX_CPU];
  int leader_count = 0;

  DIR *d = opendir("/sys/devices/system/cpu");
  if (d == NULL) {
    return 0;
  }
  struct dirent *ent;
  while ((ent = readdir(d)) != NULL) {
    // Match "cpu<digits>" entries only. "cpuidle", "cpufreq", etc.
    // share the prefix but are not per-CPU dirs.
    if (strncmp(ent->d_name, "cpu", 3) != 0) {
      continue;
    }
    const char *suffix = ent->d_name + 3;
    if (*suffix == '\0' || !isdigit((unsigned char)*suffix)) {
      continue;
    }
    char *endp;
    long cpu_id = strtol(suffix, &endp, 10);
    if (*endp != '\0' || cpu_id < 0 || cpu_id >= MAX_CPU) {
      continue;
    }

    // Skip CPUs outside our affinity mask: a worker scheduled on
    // them could not run, so they don't contribute usable physical
    // cores from this process's point of view.
    if (have_affinity && !CPU_ISSET((int)cpu_id, &affinity)) {
      continue;
    }

    char path[256];
    snprintf(path, sizeof(path),
             "/sys/devices/system/cpu/cpu%ld/topology/thread_siblings_list",
             cpu_id);
    int leader = boc_read_first_sibling(path);
    if (leader < 0) {
      // Topology unreadable (very old kernel, sysfs not mounted, etc.).
      // Bail out so the caller falls back to the logical count.
      closedir(d);
      return 0;
    }

    // Linear dedup: physical-core counts on real hardware are small
    // (< 256 typical, < 1024 on the largest current servers), so
    // O(n^2) over leaders is fine.
    bool seen = false;
    for (int i = 0; i < leader_count; ++i) {
      if (leaders[i] == leader) {
        seen = true;
        break;
      }
    }
    if (!seen) {
      if (leader_count >= MAX_CPU) {
        closedir(d);
        return 0;
      }
      leaders[leader_count++] = leader;
    }
  }
  closedir(d);
  return leader_count;
}

#endif // _WIN32 / __APPLE__ / other

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
