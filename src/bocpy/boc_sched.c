
#include "boc_sched.h"

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <Python.h>

void boc_bq_init(boc_bq_t *q) {
  boc_atomic_store_ptr_explicit(&q->front, NULL, BOC_MO_RELAXED);
  boc_atomic_store_ptr_explicit(&q->back, (void *)&q->front, BOC_MO_RELAXED);
}

void boc_bq_destroy_assert_empty(boc_bq_t *q) {
  assert(boc_bq_is_empty(q));
  (void)q;
}

boc_bq_node_t *boc_bq_acquire_front(boc_bq_t *q) {
  BOC_SCHED_YIELD();

  // Relaxed probe is a fast-path skip; the ACQUIRE exchange is the real fence
  // that claims exclusive ownership of the front chain (mpmcq.h).
  if (boc_atomic_load_ptr_explicit(&q->front, BOC_MO_RELAXED) == NULL) {
    return NULL;
  }

  BOC_SCHED_YIELD();

  return (boc_bq_node_t *)boc_atomic_exchange_ptr_explicit(&q->front, NULL,
                                                           BOC_MO_ACQUIRE);
}

void boc_bq_enqueue_segment(boc_bq_t *q, boc_bq_segment_t s) {
  BOC_SCHED_YIELD();

  boc_atomic_store_ptr_explicit(s.end, NULL, BOC_MO_RELAXED);

  BOC_SCHED_YIELD();

  boc_atomic_ptr_t *b = (boc_atomic_ptr_t *)boc_atomic_exchange_ptr_explicit(
      &q->back, (void *)s.end, BOC_MO_ACQ_REL);

  BOC_SCHED_YIELD();

  assert(boc_atomic_load_ptr_explicit(b, BOC_MO_RELAXED) == NULL);
  boc_atomic_store_ptr_explicit(b, s.start, BOC_MO_RELEASE);
}

void boc_bq_enqueue(boc_bq_t *q, boc_bq_node_t *n) {
  boc_bq_segment_t s = {n, &n->next_in_queue};
  boc_bq_enqueue_segment(q, s);
}

void boc_bq_enqueue_front(boc_bq_t *q, boc_bq_node_t *n) {
  boc_bq_node_t *old_front = boc_bq_acquire_front(q);
  if (old_front == NULL) {
    boc_bq_enqueue(q, n);
    return;
  }

  boc_atomic_store_ptr_explicit(&n->next_in_queue, old_front, BOC_MO_RELAXED);
  boc_atomic_store_ptr_explicit(&q->front, n, BOC_MO_RELEASE);
}

boc_bq_node_t *boc_bq_dequeue(boc_bq_t *q) {
  boc_bq_node_t *old_front = boc_bq_acquire_front(q);

  BOC_SCHED_YIELD();

  if (old_front == NULL) {
    return NULL;
  }

  boc_bq_node_t *new_front = (boc_bq_node_t *)boc_atomic_load_ptr_explicit(
      &old_front->next_in_queue, BOC_MO_ACQUIRE);

  BOC_SCHED_YIELD();

  if (new_front != NULL) {
    boc_atomic_store_ptr_explicit(&q->front, new_front, BOC_MO_RELEASE);
    return old_front;
  }

  BOC_SCHED_YIELD();

  void *expected = (void *)&old_front->next_in_queue;
  if (boc_atomic_compare_exchange_strong_ptr_explicit(
          &q->back, &expected, (void *)&q->front, BOC_MO_ACQ_REL,
          BOC_MO_RELAXED)) {
    return old_front;
  }

  BOC_SCHED_YIELD();

  // Lost the back-CAS race: a concurrent enqueue is mid-publish, so restore
  // front and report empty rather than return a node whose link is unstable.
  boc_atomic_store_ptr_explicit(&q->front, old_front, BOC_MO_RELEASE);
  return NULL;
}

boc_bq_segment_t boc_bq_dequeue_all(boc_bq_t *q) {
  boc_bq_node_t *old_front = boc_bq_acquire_front(q);

  if (old_front == NULL) {
    boc_bq_segment_t empty = {NULL, NULL};
    return empty;
  }

  BOC_SCHED_YIELD();

  boc_atomic_ptr_t *old_back =
      (boc_atomic_ptr_t *)boc_atomic_exchange_ptr_explicit(
          &q->back, (void *)&q->front, BOC_MO_ACQ_REL);

  BOC_SCHED_YIELD();

  boc_bq_segment_t out = {old_front, old_back};
  return out;
}

boc_bq_node_t *boc_bq_segment_take_one(boc_bq_segment_t *s) {
  boc_bq_node_t *n = s->start;
  if (n == NULL) {
    return NULL;
  }

  BOC_SCHED_YIELD();

  boc_bq_node_t *next = (boc_bq_node_t *)boc_atomic_load_ptr_explicit(
      &n->next_in_queue, BOC_MO_ACQUIRE);
  if (next == NULL) {
    return NULL;
  }

  s->start = next;
  return n;
}

bool boc_bq_is_empty(boc_bq_t *q) {
  BOC_SCHED_YIELD();
  return boc_atomic_load_ptr_explicit(&q->back, BOC_MO_RELAXED) == &q->front;
}

/// @brief Per-worker array, length @ref WORKER_COUNT. NULL when the
///        scheduler module is in the down state.
static boc_sched_worker_t *WORKERS = NULL;

/// @brief Length of @ref WORKERS. Zero when in the down state.
///
/// Atomic so off-worker producers in @c boc_sched_dispatch can
/// acquire-load it and observe the runtime-down sentinel (0)
/// before they could observe the freed @ref WORKERS array. The
/// shutdown side release-stores 0 here to publish that ordering;
/// the dispatch side acquire-loads to consume it. Worker-internal
/// reads (loop bounds, registration overflow) use relaxed loads
/// because the worker-shutdown handshake serialises them against
/// the @ref boc_sched_shutdown store. The underlying value is
/// non-negative; we use @c u64 for type-uniformity with the rest
/// of the atomic block.
static boc_atomic_u64_t WORKER_COUNT = 0;

/// @brief Per-start incarnation counter. Atomic for the same reason
/// as @ref WORKER_COUNT: off-worker producers acquire-load it to
/// detect a start/stop/start cycle and self-invalidate their
/// @c rr_nonlocal TLS. The shutdown side release-stores the bumped
/// value (paired with @ref WORKER_COUNT = 0) so a producer that
/// reads the new incarnation cannot observe a freed @ref WORKERS
/// slot. Initialisation reads/writes are relaxed because they
/// happen with no concurrent producers.
static boc_atomic_u64_t INCARNATION = 0;

/// @brief This thread's worker handle, or NULL if the thread has not
///        called @ref boc_sched_worker_register.
/// @details Read by the producer-locality fast path of
/// @c boc_sched_dispatch and by `boc_sched_worker_pop_*`. NULL on
/// threads that schedule from outside the worker pool (the main
/// thread); those callers take the round-robin arm.
static thread_local boc_sched_worker_t *current_worker = NULL;

/// @brief Pending fast-slot for the producer-locality dispatch path.
/// @details Stores a @c boc_bq_node_t pointer rather than a
/// @c BOCBehavior pointer to keep this TU decoupled from the
/// @c BOCBehavior struct layout. The consumer in @c _core.c converts
/// the node back to its owning behaviour via the
/// @c BEHAVIOR_FROM_BQ_NODE container_of macro.
static thread_local boc_bq_node_t *pending = NULL;

/// @brief Consumer-side batch countdown.
/// @details Verona `schedulerthread.h:122-138`. The @c pending fast
/// path (Verona `next_work`) is taken at most @ref BOC_BQ_BATCH_SIZE
/// times in a row; once @c batch reaches 0 the next pop forces a
/// @ref boc_bq_dequeue so a long producer-local chain cannot starve
/// queued cross-worker (or cross-arm) work indefinitely. Reset to
/// @ref BOC_BQ_BATCH_SIZE every time the queue path returns work.
/// Seeded to @ref BOC_BQ_BATCH_SIZE inside
/// @ref boc_sched_worker_register so the first pop on a freshly
/// registered thread treats @c pending as fully eligible.
static thread_local size_t batch = 0;

/// @brief Round-robin cursor for off-worker producers; the re-seed is
///        gated on the incarnation snapshot below.
static thread_local boc_sched_worker_t *rr_nonlocal = NULL;

/// @brief Snapshot of @ref INCARNATION at the time @c rr_nonlocal was
///        last seeded. A mismatch on the next dispatch forces a
///        re-seed (survives `start()`/`wait()`/`start()` cycles).
static thread_local size_t rr_incarnation = 0;

/// @brief Per-worker work-stealing victim cursor.
/// @details Verona equivalent: `SchedulerThread::victim`
/// (`schedulerthread.h:60`). Walks the worker ring independently of
/// @c rr_nonlocal so a worker's victim choice does not depend on
/// off-worker dispatch ordering. Seeded to @c self->next_in_ring on
/// the first @ref boc_sched_try_steal call (lazy init keeps the
/// register path zero-cost). NULL on threads that have not
/// registered as workers — they never call @c try_steal.
static thread_local boc_sched_worker_t *steal_victim = NULL;

static boc_atomic_u32_t REGISTERED_COUNT = 0;

/// @brief Park-epoch handshake. A worker about to park bumps @ref PAUSE_EPOCH
/// (seq-cst) then re-scans for work; a dispatcher CAS-advances
/// @ref UNPAUSE_EPOCH to match and the single CAS winner wakes every parked
/// worker via @ref boc_sched_unpause_all. The seq-cst bump is the lost-wakeup
/// fence: it totally-orders against the dispatcher's PAUSE_EPOCH acquire-load.
static boc_atomic_u64_t PAUSE_EPOCH = 0;
static boc_atomic_u64_t UNPAUSE_EPOCH = 0;
static boc_atomic_u32_t PARKED_COUNT = 0;

int boc_sched_init(Py_ssize_t worker_count) {
  if (WORKERS != NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "boc_sched_init called without prior shutdown");
    return -1;
  }

  if (worker_count < 0) {
    PyErr_SetString(PyExc_ValueError,
                    "boc_sched_init: worker_count must be non-negative");
    return -1;
  }

  if (worker_count > 0) {
    WORKERS = (boc_sched_worker_t *)PyMem_RawCalloc((size_t)worker_count,
                                                    sizeof(boc_sched_worker_t));
    if (WORKERS == NULL) {
      PyErr_NoMemory();
      return -1;
    }

    for (Py_ssize_t i = 0; i < worker_count; ++i) {
      boc_sched_worker_t *w = &WORKERS[i];
      for (size_t j = 0; j < (size_t)BOC_WSQ_N; ++j) {
        boc_bq_init(&w->q[j]);
      }
      w->enqueue_index.idx = 0;
      w->dequeue_index.idx = 0;
      w->steal_index.idx = 0;
      boc_mtx_init(&w->cv_mu);
      cnd_init(&w->cv);
      w->owner_interp_id = -1;
      w->next_in_ring = &WORKERS[(i + 1) % worker_count];
      boc_atomic_store_bool_explicit(&w->should_steal_for_fairness, true,
                                     BOC_MO_RELEASE);
    }
  }

  boc_atomic_store_u64_explicit(&WORKER_COUNT, (uint64_t)worker_count,
                                BOC_MO_RELEASE);
  boc_atomic_store_u64_explicit(
      &INCARNATION,
      boc_atomic_load_u64_explicit(&INCARNATION, BOC_MO_RELAXED) + 1,
      BOC_MO_RELEASE);
  boc_atomic_store_u32_explicit(&REGISTERED_COUNT, 0, BOC_MO_RELAXED);
  boc_atomic_store_u64_explicit(&PAUSE_EPOCH, 0, BOC_MO_RELAXED);
  boc_atomic_store_u64_explicit(&UNPAUSE_EPOCH, 0, BOC_MO_RELAXED);
  boc_atomic_store_u32_explicit(&PARKED_COUNT, 0, BOC_MO_RELAXED);
  return 0;
}

void boc_sched_shutdown(void) {
  Py_ssize_t old_count =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  // RELEASE store of 0 pairs with the dispatch-side ACQUIRE load: a producer
  // that observes wc==0 cannot then dereference a freed WORKERS slot.
  boc_atomic_store_u64_explicit(&WORKER_COUNT, 0, BOC_MO_RELEASE);
  boc_atomic_store_u64_explicit(
      &INCARNATION,
      boc_atomic_load_u64_explicit(&INCARNATION, BOC_MO_RELAXED) + 1,
      BOC_MO_RELEASE);
  if (WORKERS != NULL) {
    for (Py_ssize_t i = old_count - 1; i >= 0; --i) {
      boc_sched_worker_t *w = &WORKERS[i];
      for (size_t j = 0; j < (size_t)BOC_WSQ_N; ++j) {
        boc_bq_destroy_assert_empty(&w->q[j]);
      }
      cnd_destroy(&w->cv);
      mtx_destroy(&w->cv_mu);
    }
    PyMem_RawFree(WORKERS);
    WORKERS = NULL;
  }
  boc_atomic_store_u32_explicit(&REGISTERED_COUNT, 0, BOC_MO_RELAXED);
}

Py_ssize_t boc_sched_worker_count(void) {
  return (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT,
                                                  BOC_MO_RELAXED);
}

boc_sched_worker_t *boc_sched_worker_at(Py_ssize_t worker_index) {
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  if (worker_index < 0 || worker_index >= wc) {
    return NULL;
  }
  return &WORKERS[worker_index];
}

int boc_sched_stats_snapshot(Py_ssize_t worker_index, boc_sched_stats_t *out) {
  if (out == NULL) {
    return -1;
  }
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  if (worker_index < 0 || worker_index >= wc) {
    return -1;
  }
  const boc_sched_stats_atomic_t *src = &WORKERS[worker_index].stats;
  out->pushed_local = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->pushed_local, BOC_MO_RELAXED);
  out->dispatched_to_pending = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->dispatched_to_pending, BOC_MO_RELAXED);
  out->pushed_remote = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->pushed_remote, BOC_MO_RELAXED);
  out->popped_local = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->popped_local, BOC_MO_RELAXED);
  out->popped_via_steal = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->popped_via_steal, BOC_MO_RELAXED);
  out->enqueue_cas_retries = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->enqueue_cas_retries, BOC_MO_RELAXED);
  out->dequeue_cas_retries = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->dequeue_cas_retries, BOC_MO_RELAXED);
  out->batch_resets = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->batch_resets, BOC_MO_RELAXED);
  out->steal_attempts = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->steal_attempts, BOC_MO_RELAXED);
  out->steal_failures = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->steal_failures, BOC_MO_RELAXED);
  out->parked = boc_atomic_load_u64_explicit((boc_atomic_u64_t *)&src->parked,
                                             BOC_MO_RELAXED);
  out->last_steal_attempt_ns = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->last_steal_attempt_ns, BOC_MO_RELAXED);
  out->fairness_arm_fires = boc_atomic_load_u64_explicit(
      (boc_atomic_u64_t *)&src->fairness_arm_fires, BOC_MO_RELAXED);
  return 0;
}

size_t boc_sched_incarnation_get(void) {
  return (size_t)boc_atomic_load_u64_explicit(&INCARNATION, BOC_MO_RELAXED);
}

Py_ssize_t boc_sched_worker_register(void) {
  uint32_t slot =
      boc_atomic_fetch_add_u32_explicit(&REGISTERED_COUNT, 1, BOC_MO_RELAXED);
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  if ((Py_ssize_t)slot >= wc) {
    boc_atomic_fetch_sub_u32_explicit(&REGISTERED_COUNT, 1, BOC_MO_RELAXED);
    return -1;
  }

  PyInterpreterState *interp = PyInterpreterState_Get();
  WORKERS[slot].owner_interp_id = (Py_ssize_t)PyInterpreterState_GetID(interp);

  current_worker = &WORKERS[slot];

  batch = BOC_BQ_BATCH_SIZE;
  steal_victim = NULL;
  return (Py_ssize_t)slot;
}

boc_sched_worker_t *boc_sched_current_worker(void) { return current_worker; }

static boc_bq_node_t *boc_sched_steal(boc_sched_worker_t *self);

void boc_sched_signal_one(boc_sched_worker_t *target) {
  if (target == NULL) {
    return;
  }
  mtx_lock(&target->cv_mu);
  cnd_signal(&target->cv);
  mtx_unlock(&target->cv_mu);
}

void boc_sched_unpause_all(boc_sched_worker_t *self) {
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  if (self == NULL || wc == 0) {
    return;
  }
  if (boc_atomic_load_u32_explicit(&PARKED_COUNT, BOC_MO_RELAXED) == 0) {
    return;
  }
  // The lone dispatcher that won the UNPAUSE_EPOCH CAS must wake the whole
  // ring: CAS losers do not signal, so any parked worker they passed would be
  // stranded.
  boc_sched_worker_t *w = self->next_in_ring;
  for (Py_ssize_t i = 0; i < wc; ++i) {
    if (boc_atomic_load_bool_explicit(&w->parked, BOC_MO_ACQUIRE)) {
      boc_sched_signal_one(w);
    }
    w = w->next_in_ring;
  }
}

void boc_sched_worker_request_stop_all(void) {
  if (WORKERS == NULL) {
    return;
  }
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  for (Py_ssize_t i = 0; i < wc; ++i) {
    boc_atomic_store_bool_explicit(&WORKERS[i].stop_requested, true,
                                   BOC_MO_RELEASE);
  }
  for (Py_ssize_t i = 0; i < wc; ++i) {
    boc_sched_signal_one(&WORKERS[i]);
  }
}

boc_bq_node_t *boc_sched_worker_pop_slow(boc_sched_worker_t *self) {
  for (;;) {
    if (boc_atomic_load_bool_explicit(&self->stop_requested, BOC_MO_ACQUIRE)) {
      return NULL;
    }

    if (boc_atomic_load_bool_explicit(&self->should_steal_for_fairness,
                                      BOC_MO_ACQUIRE) &&
        !boc_wsq_is_empty(self)) {
      boc_atomic_fetch_add_u64_explicit(&self->stats.fairness_arm_fires, 1,
                                        BOC_MO_RELAXED);
      boc_bq_node_t *stolen = boc_sched_steal(self);
      boc_atomic_store_bool_explicit(&self->should_steal_for_fairness, false,
                                     BOC_MO_RELEASE);
      boc_bq_node_t *tok = (boc_bq_node_t *)boc_atomic_load_ptr_explicit(
          &self->token_work, BOC_MO_ACQUIRE);
      if (tok != NULL) {
        boc_wsq_enqueue(self, tok);
      }
      if (stolen != NULL) {
        return stolen;
      }
    }

    if (pending != NULL) {
      boc_bq_node_t *n = pending;
      pending = NULL;
      batch = BOC_BQ_BATCH_SIZE;
      boc_atomic_fetch_add_u64_explicit(&self->stats.popped_local, 1,
                                        BOC_MO_RELAXED);
      return n;
    }

    boc_bq_node_t *n = boc_wsq_dequeue(self);
    if (n != NULL) {
      batch = BOC_BQ_BATCH_SIZE;
      boc_atomic_fetch_add_u64_explicit(&self->stats.popped_local, 1,
                                        BOC_MO_RELAXED);
      return n;
    }

    n = boc_sched_steal(self);
    if (n != NULL) {
      return n;
    }

    uint64_t ue_snap =
        boc_atomic_load_u64_explicit(&UNPAUSE_EPOCH, BOC_MO_RELAXED);

    // Publish park intent (seq-cst) BEFORE the final work re-scan: a dispatcher
    // that enqueued after our last scan must see this and bump UNPAUSE_EPOCH.
    boc_atomic_fetch_add_u64_explicit(&PAUSE_EPOCH, 1, BOC_MO_SEQ_CST);

#if BOC_HAVE_TRY_STEAL
    if (boc_sched_any_work_visible()) {
      continue;
    }
#else
    if (!boc_wsq_is_empty(self)) {
      continue;
    }
#endif

    Py_BEGIN_ALLOW_THREADS mtx_lock(&self->cv_mu);
    if (boc_atomic_load_bool_explicit(&self->stop_requested, BOC_MO_ACQUIRE)) {
      mtx_unlock(&self->cv_mu);
    } else if (boc_atomic_load_u64_explicit(&UNPAUSE_EPOCH, BOC_MO_ACQUIRE) !=
               ue_snap) {
      mtx_unlock(&self->cv_mu);
    } else {
      boc_atomic_fetch_add_u64_explicit(&self->stats.parked, 1, BOC_MO_RELAXED);
      boc_atomic_store_bool_explicit(&self->parked, true, BOC_MO_RELEASE);
      boc_atomic_fetch_add_u32_explicit(&PARKED_COUNT, 1, BOC_MO_ACQ_REL);
      cnd_wait(&self->cv, &self->cv_mu);
      boc_atomic_fetch_sub_u32_explicit(&PARKED_COUNT, 1, BOC_MO_ACQ_REL);
      boc_atomic_store_bool_explicit(&self->parked, false, BOC_MO_RELEASE);
      mtx_unlock(&self->cv_mu);
    }
    Py_END_ALLOW_THREADS
  }
}

boc_bq_node_t *boc_sched_worker_pop_fast(boc_sched_worker_t *self) {
  if (self == NULL) {
    return NULL;
  }

  if (pending != NULL && batch > 0) {
    boc_bq_node_t *n = pending;
    pending = NULL;
    batch--;
    boc_atomic_fetch_add_u64_explicit(&self->stats.popped_local, 1,
                                      BOC_MO_RELAXED);
    return n;
  }

  if (boc_atomic_load_bool_explicit(&self->should_steal_for_fairness,
                                    BOC_MO_ACQUIRE) &&
      !boc_wsq_is_empty(self)) {
    return NULL;
  }

  boc_bq_node_t *n = boc_wsq_dequeue(self);
  if (n != NULL) {
    if (pending != NULL) {
      boc_atomic_fetch_add_u64_explicit(&self->stats.batch_resets, 1,
                                        BOC_MO_RELAXED);
    }
    batch = BOC_BQ_BATCH_SIZE;
    boc_atomic_fetch_add_u64_explicit(&self->stats.popped_local, 1,
                                      BOC_MO_RELAXED);
    return n;
  }

  if (pending != NULL) {
    boc_bq_node_t *p = pending;
    pending = NULL;
    batch = BOC_BQ_BATCH_SIZE;
    boc_atomic_fetch_add_u64_explicit(&self->stats.popped_local, 1,
                                      BOC_MO_RELAXED);
    return p;
  }

  return NULL;
}

int boc_sched_dispatch(boc_bq_node_t *n) {
  boc_sched_worker_t *self = current_worker;

  if (self == NULL) {
    Py_ssize_t wc =
        (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_ACQUIRE);
    if (wc == 0) {
      PyErr_SetString(
          PyExc_RuntimeError,
          "cannot schedule behavior: bocpy runtime is not running. "
          "Call bocpy.start() before scheduling, or avoid scheduling "
          "after wait() / stop() has shut the runtime down.");
      return -1;
    }
  }

  if (boc_behavior_node_is_pinned(n)) {
    return boc_main_pinned_enqueue(n);
  }

  boc_sched_worker_t *target;

  if (self != NULL) {
    if (pending != NULL) {
      boc_wsq_enqueue(self, pending);
      boc_atomic_fetch_add_u64_explicit(&self->stats.pushed_local, 1,
                                        BOC_MO_RELAXED);
    } else {
      boc_atomic_fetch_add_u64_explicit(&self->stats.dispatched_to_pending, 1,
                                        BOC_MO_RELAXED);
    }
    pending = n;
    target = self;
  } else {
    size_t inc_now =
        (size_t)boc_atomic_load_u64_explicit(&INCARNATION, BOC_MO_ACQUIRE);
    if (rr_nonlocal == NULL || rr_incarnation != inc_now) {
      rr_nonlocal = &WORKERS[0];
      rr_incarnation = inc_now;
    }
    target = rr_nonlocal;
    boc_wsq_enqueue(target, n);
    boc_atomic_fetch_add_u64_explicit(&target->stats.pushed_remote, 1,
                                      BOC_MO_RELAXED);
    rr_nonlocal = rr_nonlocal->next_in_ring;
  }

  uint64_t pe = boc_atomic_load_u64_explicit(&PAUSE_EPOCH, BOC_MO_ACQUIRE);
  uint64_t ue = boc_atomic_load_u64_explicit(&UNPAUSE_EPOCH, BOC_MO_ACQUIRE);
  if (pe != ue) {
    if (boc_atomic_compare_exchange_strong_u64_explicit(
            &UNPAUSE_EPOCH, &ue, pe, BOC_MO_ACQ_REL, BOC_MO_ACQUIRE)) {
      boc_sched_unpause_all(self != NULL ? self : target);
    }
  }

  if (self == NULL || target != self) {
    boc_sched_signal_one(target);
  }

  return 0;
}

/// @brief Single-victim work-stealing attempt for @p self.
/// @details Reads the per-thread @c steal_victim cursor (lazy-
/// initialised to @c self->next_in_ring), tries to steal one node
/// from the victim's WSQ sub-queue selected by @c self->steal_index,
/// advances the victim cursor, and returns the stolen node (or NULL
/// on miss). Verona equivalent: `SchedulerThread::try_steal`
/// (`schedulerthread.h:237-254`) calling
/// `WorkStealingQueue::steal` (`workstealingqueue.h:103-114`).
///
/// **Steal-cursor advance.** Verona only advances `steal_index` on
/// the self-victim case (`if (&victim == this) { ++steal_index;
/// return nullptr; }`); successful steals from non-self victims
/// keep the cursor — the next attempt naturally picks a different
/// victim's *same* sub-queue index, which is the spread the design
/// relies on (in concert with @ref boc_wsq_enqueue_spread on the
/// thief side).
///
/// **Splice contract.** `boc_bq_dequeue_all` returns a segment of
/// every node visible at the call (modulo concurrent enqueuers
/// mid-link). After taking the head we splice the remainder via
/// @ref boc_wsq_enqueue_spread so the work is reachable from all
/// of @p self's sub-queues — diluting collisions when more thieves
/// subsequently attempt to steal from @p self.
///
/// **No-op for self-victim.** A single-worker runtime has
/// `self->next_in_ring == self`. Per verona we advance
/// @c steal_index and return NULL.
///
/// @param self Calling worker (must be non-NULL; caller guarantees).
/// @return Stolen node, or NULL if (a) the victim was self,
///         (b) the victim's sub-queue was empty, or (c) the steal
///         spuriously failed (link not yet visible). The caller
///         decides whether to retry against the next victim or
///         park.

static boc_bq_node_t *boc_sched_try_steal(boc_sched_worker_t *self) {
  if (steal_victim == NULL) {
    steal_victim = self->next_in_ring;
  }

  boc_sched_worker_t *victim = steal_victim;
  steal_victim = steal_victim->next_in_ring;

  boc_atomic_store_u64_explicit(&self->stats.last_steal_attempt_ns,
                                boc_now_ns(), BOC_MO_RELAXED);

  boc_atomic_fetch_add_u64_explicit(&self->stats.steal_attempts, 1,
                                    BOC_MO_RELAXED);

  if (victim == self) {
    boc_wsq_pre_inc(&self->steal_index);
    boc_atomic_fetch_add_u64_explicit(&self->stats.steal_failures, 1,
                                      BOC_MO_RELAXED);
    return NULL;
  }

  size_t vidx = self->steal_index.idx;
  boc_bq_segment_t seg = boc_bq_dequeue_all(&victim->q[vidx]);

  boc_bq_node_t *r = boc_bq_segment_take_one(&seg);
  if (r == NULL) {
    if (seg.end == NULL) {
      boc_atomic_fetch_add_u64_explicit(&self->stats.steal_failures, 1,
                                        BOC_MO_RELAXED);
      return NULL;
    }
    if (seg.start != NULL && seg.end == &seg.start->next_in_queue) {
      r = seg.start;
      boc_atomic_fetch_add_u64_explicit(&self->stats.popped_via_steal, 1,
                                        BOC_MO_RELAXED);
      return r;
    }
    boc_wsq_enqueue_spread(self, seg);
    boc_atomic_fetch_add_u64_explicit(&self->stats.steal_failures, 1,
                                      BOC_MO_RELAXED);
    return NULL;
  }

  boc_wsq_enqueue_spread(self, seg);
  boc_atomic_fetch_add_u64_explicit(&self->stats.popped_via_steal, 1,
                                    BOC_MO_RELAXED);
  return r;
}

#ifndef BOC_STEAL_QUIESCENCE_NS
#define BOC_STEAL_QUIESCENCE_NS 100000ULL
#endif

#ifndef BOC_STEAL_BACKOFF_NS
#define BOC_STEAL_BACKOFF_NS 5000ULL
#endif

/// @brief Multi-victim steal with a brief quiescence window.
/// @details Single full ring of @ref boc_sched_try_steal calls;
/// repeats while @ref BOC_STEAL_QUIESCENCE_NS has not elapsed.
/// Returns the first successfully stolen node, or NULL if the
/// quiescence window expires with every ring round empty (in which
/// case the caller should commit to parking).
///
/// **stop_requested honour.** Checked at the top of every round so
/// shutdown is observed even mid-spin.
///
/// **Own-queue catch.** Before each ring we re-check `self->q`: a
/// concurrent producer (cross-worker dispatch, or another thief
/// splicing remainder onto us) may have published since the last
/// `pop_fast` attempt.
///
/// @param self Calling worker (must be non-NULL).
/// @return Stolen node, or NULL if the quiescence window expired
///         or shutdown was requested.
static boc_bq_node_t *boc_sched_steal(boc_sched_worker_t *self) {
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  if (wc <= 1) {
    return NULL;
  }

  const uint64_t deadline = boc_now_ns() + BOC_STEAL_QUIESCENCE_NS;

  for (;;) {
    if (boc_atomic_load_bool_explicit(&self->stop_requested, BOC_MO_ACQUIRE)) {
      return NULL;
    }

    BOC_SCHED_YIELD();

    boc_bq_node_t *n = boc_wsq_dequeue(self);
    if (n != NULL) {
      return n;
    }

    for (Py_ssize_t i = 0; i < wc - 1; ++i) {
      n = boc_sched_try_steal(self);
      if (n != NULL) {
        return n;
      }
    }

    if (boc_now_ns() >= deadline) {
      return NULL;
    }

    boc_sleep_ns(BOC_STEAL_BACKOFF_NS);
  }
}

int boc_sched_set_token_node(Py_ssize_t worker_index, boc_bq_node_t *node) {
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  if (worker_index < 0 || worker_index >= wc) {
    return -1;
  }
  boc_atomic_store_ptr_explicit(&WORKERS[worker_index].token_work, (void *)node,
                                BOC_MO_RELEASE);
  return 0;
}

boc_bq_node_t *boc_sched_get_token_node(Py_ssize_t worker_index) {
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  if (worker_index < 0 || worker_index >= wc) {
    return NULL;
  }
  return (boc_bq_node_t *)boc_atomic_load_ptr_explicit(
      &WORKERS[worker_index].token_work, BOC_MO_ACQUIRE);
}

void boc_sched_set_steal_flag(boc_sched_worker_t *self, bool value) {
  if (self == NULL) {
    return;
  }
  boc_atomic_store_bool_explicit(&self->should_steal_for_fairness, value,
                                 BOC_MO_RELEASE);
}

bool boc_sched_any_work_visible(void) {
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  for (Py_ssize_t i = 0; i < wc; ++i) {
    if (!boc_wsq_is_empty(&WORKERS[i])) {
      return true;
    }
  }
  return false;
}