/// @file boc_terminator.c
/// @brief Per-instance completion generations and lifetime-safe pinned wake routing

#include "boc_terminator.h"
#include "boc_compat.h"

struct boc_terminator {
  atomic_int_least64_t refs;
  BOCMutex mutex;
  BOCCond cond;
  int_least64_t count;
  uint64_t zero_count;
  /// @brief Immutable role; only non-root instances reserve an enclosing runtime
  bool is_root;
  bool open;
  /// @brief Root startup claim, cleared on activation or fencing
  bool starting;
};

typedef struct registered_wait {
  BOCTerminator *term;
  struct registered_wait *next;
} RegisteredWait;

static BOCMutex ROOT_MUTEX;
static BOCTerminator *ROOT;
static BOCMutex WAITERS_MUTEX;
static RegisteredWait *WAITERS;

void terminator_init(void) {
  boc_mtx_init(&ROOT_MUTEX);
  boc_mtx_init(&WAITERS_MUTEX);
}

int_least64_t terminator_inc(void) {
  BOCTerminator *root = terminator_root_acquire();
  int_least64_t count = boc_terminator_inc(root);
  boc_terminator_decref(root);
  return count;
}

int_least64_t terminator_dec(void) {
  BOCTerminator *root = terminator_root_acquire();
  int_least64_t count = boc_terminator_dec(root);
  boc_terminator_decref(root);
  return count;
}

bool terminator_close(void) {
  BOCTerminator *root = terminator_root_acquire();
  if (root == NULL) {
    return true;
  }
  bool allowed = boc_terminator_fence(root);
  boc_terminator_decref(root);
  return allowed;
}

bool terminator_reset(void) {
  mtx_lock(&ROOT_MUTEX);
  if (ROOT != NULL) {
    mtx_unlock(&ROOT_MUTEX);
    PyErr_SetString(PyExc_RuntimeError, "the previous runtime has not been retired");
    return false;
  }
  ROOT = boc_terminator_new(true);
  if (ROOT != NULL) {
    ROOT->starting = true;
  }
  bool ok = ROOT != NULL;
  mtx_unlock(&ROOT_MUTEX);
  if (!ok) {
    PyErr_NoMemory();
  }
  return ok;
}

bool terminator_activate(void) {
  mtx_lock(&ROOT_MUTEX);
  bool allowed = false;
  if (ROOT != NULL) {
    mtx_lock(&ROOT->mutex);
    allowed = ROOT->starting && !ROOT->open && ROOT->count == 0;
    if (allowed) {
      ROOT->starting = false;
      ROOT->open = true;
    }
    mtx_unlock(&ROOT->mutex);
  }
  mtx_unlock(&ROOT_MUTEX);
  if (!allowed) {
    PyErr_SetString(PyExc_RuntimeError, "cannot activate a runtime without a startup claim");
  }
  return allowed;
}

int_least64_t terminator_count(void) {
  BOCTerminator *root = terminator_root_acquire();
  int_least64_t count = boc_terminator_count(root);
  boc_terminator_decref(root);
  return count;
}

void terminator_wake_all(void) {
  mtx_lock(&WAITERS_MUTEX);
  for (RegisteredWait *entry = WAITERS; entry != NULL; entry = entry->next) {
    mtx_lock(&entry->term->mutex);
    cnd_broadcast(&entry->term->cond);
    mtx_unlock(&entry->term->mutex);
  }
  mtx_unlock(&WAITERS_MUTEX);
}

BOCTerminator *boc_terminator_new(bool is_root) {
  BOCTerminator *term = PyMem_RawCalloc(1, sizeof(*term));
  if (term != NULL) {
    atomic_store(&term->refs, 1);
    term->is_root = is_root;
    boc_mtx_init(&term->mutex);
    cnd_init(&term->cond);
  }
  return term;
}

void boc_terminator_incref(BOCTerminator *term) {
  if (term != NULL) {
    atomic_fetch_add(&term->refs, 1);
  }
}

void boc_terminator_decref(BOCTerminator *term) {
  if (term != NULL && atomic_fetch_add(&term->refs, -1) == 1) {
    cnd_destroy(&term->cond);
    mtx_destroy(&term->mutex);
    PyMem_RawFree(term);
  }
}

bool boc_terminator_start(BOCTerminator *term, BOCTerminator **root_reservation) {
  if (!term->is_root) {
    BOCTerminator *root = terminator_reserve();
    if (root == NULL) {
      return false;
    }
    *root_reservation = root;
  }
  mtx_lock(&term->mutex);
  term->open = true;
  mtx_unlock(&term->mutex);
  return true;
}

int_least64_t boc_terminator_inc(BOCTerminator *term) {
  if (term == NULL) {
    return -1;
  }
  mtx_lock(&term->mutex);
  int_least64_t count = term->open ? ++term->count : -1;
  mtx_unlock(&term->mutex);
  return count;
}

int_least64_t boc_terminator_dec(BOCTerminator *term) {
  if (term == NULL) {
    return -1;
  }
  mtx_lock(&term->mutex);
  int_least64_t count = -1;
  if (term->count > 0) {
    count = --term->count;
    if (count == 0) {
      ++term->zero_count;
      cnd_broadcast(&term->cond);
    }
  }
  mtx_unlock(&term->mutex);
  return count;
}

int_least64_t boc_terminator_count(BOCTerminator *term) {
  if (term == NULL) {
    return 0;
  }
  mtx_lock(&term->mutex);
  int_least64_t count = term->count;
  mtx_unlock(&term->mutex);
  return count;
}

uint64_t boc_terminator_zero_count(BOCTerminator *term) {
  if (term == NULL) {
    return 0;
  }
  mtx_lock(&term->mutex);
  uint64_t count = term->zero_count;
  mtx_unlock(&term->mutex);
  return count;
}

bool boc_terminator_fence(BOCTerminator *term) {
  mtx_lock(&term->mutex);
  bool done = term->count == 0;
  if (done) {
    term->open = false;
    term->starting = false;
  }
  mtx_unlock(&term->mutex);
  return done;
}

void boc_terminator_waiter_init(BOCTerminatorWaiter *waiter,
                              BOCTerminator *term, bool drain) {
  boc_terminator_incref(term);
  waiter->term = term;
  waiter->drain = drain;
  waiter->generation = 0;
  waiter->completed = term == NULL;
  if (term != NULL) {
    mtx_lock(&term->mutex);
    waiter->generation = term->zero_count;
    waiter->completed = !drain && term->count == 0;
    mtx_unlock(&term->mutex);
  }
}

void boc_terminator_waiter_clear(BOCTerminatorWaiter *waiter) {
  boc_terminator_decref(waiter->term);
  waiter->term = NULL;
}

/// @brief Test a sticky event or fence a strict drain under the instance mutex
static bool waiter_complete_locked(BOCTerminatorWaiter *waiter) {
  BOCTerminator *term = waiter->term;
  if (!waiter->completed) {
    if (waiter->drain) {
      if (term->count == 0) {
        term->open = false;
        term->starting = false;
        waiter->completed = true;
      }
    } else if (term->zero_count != waiter->generation) {
      waiter->completed = true;
    }
  }
  return waiter->completed;
}

boc_terminator_wake_reason_t boc_terminator_waiter_wait(
    BOCTerminatorWaiter *waiter, double timeout, bool forever,
    uint64_t (*pinned_depth_fn)(void)) {
  BOCTerminator *term = waiter->term;
  if (term == NULL) {
    return BOC_TERMINATOR_TERMINATED;
  }
  RegisteredWait registration = {.term = term};
  if (pinned_depth_fn != NULL) {
    mtx_lock(&WAITERS_MUTEX);
    registration.next = WAITERS;
    WAITERS = &registration;
    mtx_unlock(&WAITERS_MUTEX);
  }
  double end = forever ? 0 : boc_now_s() + timeout;
  boc_terminator_wake_reason_t reason;
  mtx_lock(&term->mutex);
  for (;;) {
    if (waiter_complete_locked(waiter)) {
      reason = BOC_TERMINATOR_TERMINATED;
      break;
    }
    // Registration precedes this predicate, closing the enqueue-before-sleep race.
    if (pinned_depth_fn != NULL && pinned_depth_fn() > 0) {
      reason = BOC_TERMINATOR_PUMP_READY;
      break;
    }
    double remaining = forever ? 0 : end - boc_now_s();
    if (!forever && remaining <= 0) {
      reason = BOC_TERMINATOR_WAIT_TIMED_OUT;
      break;
    }
    if (forever) {
      cnd_wait(&term->cond, &term->mutex);
    } else {
      cnd_timedwait_s(&term->cond, &term->mutex, remaining);
    }
  }
  mtx_unlock(&term->mutex);
  if (pinned_depth_fn != NULL) {
    // Never acquire the registry while holding an instance mutex: notifiers take the opposite order.
    mtx_lock(&WAITERS_MUTEX);
    RegisteredWait **link = &WAITERS;
    while (*link != &registration) {
      link = &(*link)->next;
    }
    *link = registration.next;
    mtx_unlock(&WAITERS_MUTEX);
  }
  return reason;
}

BOCTerminator *terminator_root_acquire(void) {
  mtx_lock(&ROOT_MUTEX);
  BOCTerminator *root = ROOT;
  boc_terminator_incref(root);
  mtx_unlock(&ROOT_MUTEX);
  return root;
}

BOCTerminator *terminator_reserve(void) {
  BOCTerminator *root = terminator_root_acquire();
  if (boc_terminator_inc(root) < 0) {
    boc_terminator_decref(root);
    return NULL;
  }
  return root;
}

bool terminator_retire(void) {
  mtx_lock(&ROOT_MUTEX);
  BOCTerminator *root = ROOT;
  bool ok = true;
  if (root != NULL) {
    mtx_lock(&root->mutex);
    ok = !root->open && root->count == 0;
    if (ok) {
      root->starting = false;
      ROOT = NULL;
    }
    mtx_unlock(&root->mutex);
  }
  mtx_unlock(&ROOT_MUTEX);
  if (ok) {
    boc_terminator_decref(root);
  }
  return ok;
}
