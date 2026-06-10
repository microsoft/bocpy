/// @file boc_terminator.c
/// @brief Implementation of the process-global rundown counter.
///
/// All state lives in file-scope statics so that every sub-interpreter
/// in the same process shares one counter, mutex, and condvar. See
/// `boc_terminator.h` for the public API and lifecycle contract.

#include "boc_terminator.h"

#include "boc_compat.h"

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
  boc_mtx_init(&TERMINATOR_MUTEX);
  cnd_init(&TERMINATOR_COND);
}

int_least64_t terminator_inc(void) {
  if (atomic_load(&TERMINATOR_CLOSED)) {
    return -1;
  }
  int_least64_t newval = atomic_fetch_add(&TERMINATOR_COUNT, 1) + 1;
  if (atomic_load(&TERMINATOR_CLOSED)) {
    // close() raced in after our first check: undo, and broadcast on a
    // 0-transition since close()'s own wake predated our increment.
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

bool terminator_seed_inc(void) {
  int_least64_t expected = 0;
  if (atomic_compare_exchange_strong(&TERMINATOR_SEEDED, &expected, 1)) {
    atomic_fetch_add(&TERMINATOR_COUNT, 1);
    return true;
  }
  return false;
}

void terminator_reset(int_least64_t *prior_count, int_least64_t *prior_seeded) {
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

void terminator_wake_all(void) {
  mtx_lock(&TERMINATOR_MUTEX);
  cnd_broadcast(&TERMINATOR_COND);
  mtx_unlock(&TERMINATOR_MUTEX);
}

boc_terminator_wake_reason_t
terminator_wait_pumpable(double timeout_s, uint64_t (*pinned_depth_fn)(void)) {
  boc_terminator_wake_reason_t reason = BOC_TERMINATOR_WAIT_TIMED_OUT;
  double end_time = boc_now_s() + timeout_s;
  mtx_lock(&TERMINATOR_MUTEX);
  for (;;) {
    if (atomic_load(&TERMINATOR_COUNT) == 0) {
      reason = BOC_TERMINATOR_TERMINATED;
      break;
    }
    if (pinned_depth_fn != NULL && pinned_depth_fn() > 0) {
      reason = BOC_TERMINATOR_PUMP_READY;
      break;
    }
    double now = boc_now_s();
    if (now >= end_time) {
      reason = BOC_TERMINATOR_WAIT_TIMED_OUT;
      break;
    }
    cnd_timedwait_s(&TERMINATOR_COND, &TERMINATOR_MUTEX, end_time - now);
  }
  mtx_unlock(&TERMINATOR_MUTEX);
  return reason;
}
