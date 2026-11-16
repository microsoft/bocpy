/// @file boc_terminator.c
/// @brief Implementation of the process-global rundown counter.
///
/// All state lives in file-scope statics so that every sub-interpreter
/// in the same process shares one counter, mutex, and condvar. See
/// `boc_terminator.h` for the public API and lifecycle contract.

#include "boc_terminator.h"

#include "boc_compat.h"

/// @brief Active behavior count
static int_least64_t TERMINATOR_COUNT = 0;

/// @brief Startup claims keep admission closed until resources are ready
static enum {
  TERMINATOR_CLOSED,
  TERMINATOR_STARTING,
  TERMINATOR_OPEN
} TERMINATOR_STATE = TERMINATOR_CLOSED;

/// @brief Zero transitions across all runtime starts
static uint64_t TERMINATOR_ZERO_COUNT = 0;

/// @brief Mutex protecting the counter, admission, and completion observations
static BOCMutex TERMINATOR_MUTEX;

/// @brief Condition variable signalled when TERMINATOR_COUNT reaches 0.
static BOCCond TERMINATOR_COND;

void terminator_init(void) {
  boc_mtx_init(&TERMINATOR_MUTEX);
  cnd_init(&TERMINATOR_COND);
}

int_least64_t terminator_inc(void) {
  mtx_lock(&TERMINATOR_MUTEX);
  int_least64_t count = TERMINATOR_STATE == TERMINATOR_OPEN ? ++TERMINATOR_COUNT : -1;
  mtx_unlock(&TERMINATOR_MUTEX);
  return count;
}

int_least64_t terminator_dec(void) {
  mtx_lock(&TERMINATOR_MUTEX);
  int_least64_t count = -1;
  if (TERMINATOR_COUNT > 0) {
    count = --TERMINATOR_COUNT;
    if (count == 0) {
      ++TERMINATOR_ZERO_COUNT;
      cnd_broadcast(&TERMINATOR_COND);
    }
  }
  mtx_unlock(&TERMINATOR_MUTEX);
  return count;
}

bool terminator_close(void) {
  mtx_lock(&TERMINATOR_MUTEX);
  bool done = TERMINATOR_COUNT == 0;
  if (done) {
    TERMINATOR_STATE = TERMINATOR_CLOSED;
  }
  mtx_unlock(&TERMINATOR_MUTEX);
  return done;
}

bool terminator_reset(void) {
  mtx_lock(&TERMINATOR_MUTEX);
  bool allowed = TERMINATOR_STATE == TERMINATOR_CLOSED && TERMINATOR_COUNT == 0;
  if (allowed) {
    TERMINATOR_STATE = TERMINATOR_STARTING;
  }
  mtx_unlock(&TERMINATOR_MUTEX);
  if (!allowed) {
    PyErr_SetString(PyExc_RuntimeError, "cannot reset a starting, open or busy runtime");
  }
  return allowed;
}

bool terminator_activate(void) {
  mtx_lock(&TERMINATOR_MUTEX);
  bool allowed = TERMINATOR_STATE == TERMINATOR_STARTING && TERMINATOR_COUNT == 0;
  if (allowed) {
    TERMINATOR_STATE = TERMINATOR_OPEN;
  }
  mtx_unlock(&TERMINATOR_MUTEX);
  if (!allowed) {
    PyErr_SetString(PyExc_RuntimeError, "cannot activate a runtime without a startup claim");
  }
  return allowed;
}

int_least64_t terminator_count(void) {
  mtx_lock(&TERMINATOR_MUTEX);
  int_least64_t count = TERMINATOR_COUNT;
  mtx_unlock(&TERMINATOR_MUTEX);
  return count;
}

void terminator_wake_all(void) {
  mtx_lock(&TERMINATOR_MUTEX);
  cnd_broadcast(&TERMINATOR_COND);
  mtx_unlock(&TERMINATOR_MUTEX);
}

uint64_t terminator_zero_count(void) {
  mtx_lock(&TERMINATOR_MUTEX);
  uint64_t count = TERMINATOR_ZERO_COUNT;
  mtx_unlock(&TERMINATOR_MUTEX);
  return count;
}

void terminator_waiter_init(BOCTerminatorWaiter *waiter, bool drain) {
  mtx_lock(&TERMINATOR_MUTEX);
  waiter->generation = TERMINATOR_ZERO_COUNT;
  waiter->completed = !drain && TERMINATOR_COUNT == 0;
  waiter->drain = drain;
  mtx_unlock(&TERMINATOR_MUTEX);
}

/// @brief Test a sticky event or close a strict drain under the terminator mutex
static bool waiter_complete_locked(BOCTerminatorWaiter *waiter) {
  if (!waiter->completed) {
    if (waiter->drain) {
      if (TERMINATOR_COUNT == 0) {
        TERMINATOR_STATE = TERMINATOR_CLOSED;
        waiter->completed = true;
      }
    } else if (TERMINATOR_ZERO_COUNT != waiter->generation) {
      waiter->completed = true;
    }
  }
  return waiter->completed;
}

boc_terminator_wake_reason_t terminator_waiter_wait(
    BOCTerminatorWaiter *waiter, double timeout, bool wait_forever,
    uint64_t (*pinned_depth_fn)(void)) {
  double end_time = wait_forever ? 0.0 : boc_now_s() + timeout;
  boc_terminator_wake_reason_t reason;
  mtx_lock(&TERMINATOR_MUTEX);
  for (;;) {
    if (waiter_complete_locked(waiter)) {
      reason = BOC_TERMINATOR_TERMINATED;
      break;
    }
    // The notifier takes this mutex, so enqueue cannot race between the depth check and sleep.
    if (pinned_depth_fn != NULL && pinned_depth_fn() > 0) {
      reason = BOC_TERMINATOR_PUMP_READY;
      break;
    }
    double now = boc_now_s();
    if (!wait_forever && now >= end_time) {
      reason = BOC_TERMINATOR_WAIT_TIMED_OUT;
      break;
    }
    if (wait_forever) {
      cnd_wait(&TERMINATOR_COND, &TERMINATOR_MUTEX);
    } else {
      cnd_timedwait_s(&TERMINATOR_COND, &TERMINATOR_MUTEX, end_time - now);
    }
  }
  mtx_unlock(&TERMINATOR_MUTEX);
  return reason;
}
