/// @file terminator.c
/// @brief Implementation of the process-global rundown counter.
///
/// All state lives in file-scope statics so that every sub-interpreter
/// in the same process shares one counter, mutex, and condvar. See
/// `terminator.h` for the public API and lifecycle contract.

#include "terminator.h"

#include "compat.h"

/// @brief Active behavior count + the Pyrona seed.
static atomic_int_least64_t TERMINATOR_COUNT = 0;

/// @brief Set to 1 by terminator_close() to refuse further increments.
static atomic_int_least64_t TERMINATOR_CLOSED = 0;

/// @brief One-shot guard for the Pyrona seed: 1 = seed still present.
static atomic_int_least64_t TERMINATOR_SEEDED = 0;

/// @brief Mutex protecting TERMINATOR_COND.
static BOCMutex TERMINATOR_MUTEX;

/// @brief Condition variable signalled when TERMINATOR_COUNT reaches 0.
static BOCCond TERMINATOR_COND;

void terminator_init(void) {
  // The Pyrona seed (count=1, seeded=1) is set by terminator_reset()
  // when the runtime starts; here we only initialize the kernel
  // objects.
  boc_mtx_init(&TERMINATOR_MUTEX);
  cnd_init(&TERMINATOR_COND);
}

int_least64_t terminator_inc(void) {
  if (atomic_load(&TERMINATOR_CLOSED)) {
    return -1;
  }
  int_least64_t newval = atomic_fetch_add(&TERMINATOR_COUNT, 1) + 1;
  if (atomic_load(&TERMINATOR_CLOSED)) {
    int_least64_t after = atomic_fetch_add(&TERMINATOR_COUNT, -1) - 1;
    if (after == 0) {
      mtx_lock(&TERMINATOR_MUTEX);
      cnd_broadcast(&TERMINATOR_COND);
      mtx_unlock(&TERMINATOR_MUTEX);
    }
    return -1;
  }
  return newval;
}

int_least64_t terminator_dec(void) {
  int_least64_t newval = atomic_fetch_add(&TERMINATOR_COUNT, -1) - 1;
  if (newval == 0) {
    mtx_lock(&TERMINATOR_MUTEX);
    cnd_broadcast(&TERMINATOR_COND);
    mtx_unlock(&TERMINATOR_MUTEX);
  }
  return newval;
}

void terminator_close(void) { atomic_store(&TERMINATOR_CLOSED, 1); }

bool terminator_wait(double timeout, bool wait_forever) {
  bool ok = true;
  double end_time = wait_forever ? 0.0 : boc_now_s() + timeout;
  mtx_lock(&TERMINATOR_MUTEX);
  while (atomic_load(&TERMINATOR_COUNT) != 0) {
    if (!wait_forever) {
      double now = boc_now_s();
      if (now >= end_time) {
        ok = false;
        break;
      }
      cnd_timedwait_s(&TERMINATOR_COND, &TERMINATOR_MUTEX, end_time - now);
    } else {
      cnd_wait(&TERMINATOR_COND, &TERMINATOR_MUTEX);
    }
  }
  mtx_unlock(&TERMINATOR_MUTEX);
  return ok;
}

bool terminator_seed_dec(void) {
  int_least64_t prev = atomic_exchange(&TERMINATOR_SEEDED, 0);
  if (prev == 1) {
    int_least64_t newval = atomic_fetch_add(&TERMINATOR_COUNT, -1) - 1;
    if (newval == 0) {
      mtx_lock(&TERMINATOR_MUTEX);
      cnd_broadcast(&TERMINATOR_COND);
      mtx_unlock(&TERMINATOR_MUTEX);
    }
    return true;
  }
  return false;
}

void terminator_reset(int_least64_t *prior_count, int_least64_t *prior_seeded) {
  // Fence: raise the closed bit before we touch anything else so any
  // stray thread still holding a reference to the previous runtime
  // (e.g. a late whencall call) is refused by terminator_inc rather
  // than slipping a new behavior past the reset boundary. We clear
  // the bit again at the end, once the new COUNT/SEEDED values have
  // been published, so a fresh start() sees closed=0.
  atomic_store(&TERMINATOR_CLOSED, 1);
  mtx_lock(&TERMINATOR_MUTEX);
  *prior_count = atomic_load(&TERMINATOR_COUNT);
  *prior_seeded = atomic_load(&TERMINATOR_SEEDED);
  atomic_store(&TERMINATOR_COUNT, 1);
  atomic_store(&TERMINATOR_SEEDED, 1);
  atomic_store(&TERMINATOR_CLOSED, 0);
  cnd_broadcast(&TERMINATOR_COND);
  mtx_unlock(&TERMINATOR_MUTEX);
}

int_least64_t terminator_seeded(void) {
  return atomic_load(&TERMINATOR_SEEDED);
}

int_least64_t terminator_count(void) { return atomic_load(&TERMINATOR_COUNT); }
