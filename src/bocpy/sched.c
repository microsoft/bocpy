// sched.c — Work-stealing scheduler.
//
// Owns the per-worker MPMC queues, parking protocol, work-stealing,
// and per-worker fairness tokens.
//
// Verona reference: `verona-rt/src/rt/sched/schedulerstats.h` (counter
// POD subset), `mpmcq.h` (MPMC queue), `schedulerthread.h`
// (`get_work` / `try_steal` / `steal`), `threadpool.h` (per-start
// `incarnation` counter; pause/unpause epoch protocol), and
// `core.h` (fairness token).

#include "sched.h"

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <Python.h>

// ===========================================================================
// Verona MPMC behaviour queue (`boc_bq_*`) — port of
// `verona-rt/src/rt/sched/mpmcq.h`. Memory orderings match Verona
// line-for-line. Cited line numbers refer to that file.
// ===========================================================================

void boc_bq_init(boc_bq_t *q) {
  // Empty representation: back == &front, front == NULL (mpmcq.h:33-37).
  // Use relaxed stores during init: callers must publish the queue
  // through their own release edge before any thread observes it.
  boc_atomic_store_ptr_explicit(&q->front, NULL, BOC_MO_RELAXED);
  boc_atomic_store_ptr_explicit(&q->back, (void *)&q->front, BOC_MO_RELAXED);
}

void boc_bq_destroy_assert_empty(boc_bq_t *q) {
  // Mirrors ~MPMCQ (mpmcq.h:213-217).
  assert(boc_bq_is_empty(q));
  (void)q;
}

boc_bq_node_t *boc_bq_acquire_front(boc_bq_t *q) {
  // Mirrors MPMCQ::acquire_front (mpmcq.h:41-56).
  BOC_SCHED_YIELD();

  // Nothing in the queue (mpmcq.h:46).
  if (boc_atomic_load_ptr_explicit(&q->front, BOC_MO_RELAXED) == NULL) {
    return NULL;
  }

  BOC_SCHED_YIELD();

  // Remove head element. This is like locking the queue for other
  // removals (mpmcq.h:55).
  return (boc_bq_node_t *)boc_atomic_exchange_ptr_explicit(&q->front, NULL,
                                                           BOC_MO_ACQUIRE);
}

void boc_bq_enqueue_segment(boc_bq_t *q, boc_bq_segment_t s) {
  // Mirrors MPMCQ::enqueue_segment (mpmcq.h:97-115).
  BOC_SCHED_YIELD();

  // The element we are writing into must have its next pointer NULL
  // before the back-exchange (mpmcq.h:103); writes to the segment's
  // tail link use relaxed because the publish below carries the
  // happens-before edge.
  boc_atomic_store_ptr_explicit(s.end, NULL, BOC_MO_RELAXED);

  BOC_SCHED_YIELD();

  boc_atomic_ptr_t *b = (boc_atomic_ptr_t *)boc_atomic_exchange_ptr_explicit(
      &q->back, (void *)s.end, BOC_MO_ACQ_REL);

  BOC_SCHED_YIELD();

  // The previous back's slot must currently be NULL (its enqueuer set
  // it that way); we now publish our segment's start there with a
  // release store so consumers reading through next_in_queue with
  // acquire see all the segment's writes (mpmcq.h:113).
  assert(boc_atomic_load_ptr_explicit(b, BOC_MO_RELAXED) == NULL);
  boc_atomic_store_ptr_explicit(b, s.start, BOC_MO_RELEASE);
}

void boc_bq_enqueue(boc_bq_t *q, boc_bq_node_t *n) {
  // Mirrors MPMCQ::enqueue (mpmcq.h:118-121).
  boc_bq_segment_t s = {n, &n->next_in_queue};
  boc_bq_enqueue_segment(q, s);
}

void boc_bq_enqueue_front(boc_bq_t *q, boc_bq_node_t *n) {
  // Mirrors MPMCQ::enqueue_front (mpmcq.h:123-135).
  boc_bq_node_t *old_front = boc_bq_acquire_front(q);
  if (old_front == NULL) {
    // Post to back (mpmcq.h:128).
    boc_bq_enqueue(q, n);
    return;
  }

  // Link into the front (mpmcq.h:132-134).
  boc_atomic_store_ptr_explicit(&n->next_in_queue, old_front, BOC_MO_RELAXED);
  boc_atomic_store_ptr_explicit(&q->front, n, BOC_MO_RELEASE);
}

boc_bq_node_t *boc_bq_dequeue(boc_bq_t *q) {
  // Mirrors MPMCQ::dequeue (mpmcq.h:140-184).
  boc_bq_node_t *old_front = boc_bq_acquire_front(q);

  BOC_SCHED_YIELD();

  // Queue is empty or someone else is stealing (mpmcq.h:147-150).
  if (old_front == NULL) {
    return NULL;
  }

  boc_bq_node_t *new_front = (boc_bq_node_t *)boc_atomic_load_ptr_explicit(
      &old_front->next_in_queue, BOC_MO_ACQUIRE);

  BOC_SCHED_YIELD();

  if (new_front != NULL) {
    // Remove one element from the queue (mpmcq.h:158-160).
    boc_atomic_store_ptr_explicit(&q->front, new_front, BOC_MO_RELEASE);
    return old_front;
  }

  BOC_SCHED_YIELD();

  // Queue contains a single element, attempt to close the queue
  // (mpmcq.h:165-176). The expected `back` value is the address of the
  // singleton node's `next_in_queue` slot; the desired value is the
  // address of `q->front`, restoring the empty representation.
  void *expected = (void *)&old_front->next_in_queue;
  if (boc_atomic_compare_exchange_strong_ptr_explicit(
          &q->back, &expected, (void *)&q->front, BOC_MO_ACQ_REL,
          BOC_MO_RELAXED)) {
    return old_front;
  }

  BOC_SCHED_YIELD();

  // Failed to close the queue, something is being added; restore the
  // front and let the caller retry (mpmcq.h:181-183).
  boc_atomic_store_ptr_explicit(&q->front, old_front, BOC_MO_RELEASE);
  return NULL;
}

boc_bq_segment_t boc_bq_dequeue_all(boc_bq_t *q) {
  // Mirrors MPMCQ::dequeue_all (mpmcq.h:189-203).
  boc_bq_node_t *old_front = boc_bq_acquire_front(q);

  // Queue is empty or someone else is popping (mpmcq.h:194-197).
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
  // Mirrors MPMCQ::Segment::take_one (mpmcq.h:67-89).
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
  // Mirrors MPMCQ::is_empty (mpmcq.h:206-210).
  BOC_SCHED_YIELD();
  return boc_atomic_load_ptr_explicit(&q->back, BOC_MO_RELAXED) == &q->front;
}

// ===========================================================================
// Per-worker scheduler state
// ===========================================================================

// The per-worker struct (`boc_sched_worker_t`) is defined in `sched.h`
// so dispatch and pop call sites can refer to its fields without an
// extra indirection. Cacheline padding and `static_assert`s live with
// the type definition.

// ---------------------------------------------------------------------------
// File-scope state
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Per-thread state (TLS)
// ---------------------------------------------------------------------------
//
// Each scheduler-aware thread (worker sub-interpreter, or any other
// thread that calls boc_sched_dispatch from a worker context) keeps
// its dispatch state in TLS slots rather than in `boc_sched_worker_t`
// fields. The bocpy precedent: this matches `noticeboard.c`'s
// `nb_cache_*` thread-locals. Verona equivalent: the same fields
// are members of `SchedulerThread`, which is itself one-per-OS-thread
// — TLS is the same effect with one fewer indirection.
//
// All slots use the `compat.h` `thread_local` macro (`_Thread_local`
// on POSIX, `__declspec(thread)` on MSVC) with the **default** TLS
// model.

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

// ---------------------------------------------------------------------------
// Worker registration counter
// ---------------------------------------------------------------------------
//
// Atomic so multiple worker threads racing to claim slots in
// `boc_sched_worker_register` do not collide. Reset to zero in
// `boc_sched_init` so re-entry (`start()`/`wait()`/`start()`) starts
// fresh at slot 0. Read with relaxed ordering — the consumers that
// care about happens-before edges (the `current_worker` TLS write
// and any subsequent dispatch) sequence themselves through
// `WORKERS[slot]` which is itself zero-initialised by `boc_sched_init`
// before this counter is reset.

static boc_atomic_u32_t REGISTERED_COUNT = 0;

// ---------------------------------------------------------------------------
// Park/unpark protocol epochs
// ---------------------------------------------------------------------------
//
// Port of Verona's two-epoch `pause`/`unpause` protocol
// (`verona-rt/src/rt/sched/threadpool.h:282-379`).
//
// `PAUSE_EPOCH` is bumped (seq_cst) by a parker before its
// `check_for_work` walk and `cv_mu` re-check; this is the
// "speak now" point that forces any concurrent producer into the
// CAS arm. `UNPAUSE_EPOCH` is CAS'd forward by a producer that
// observes `PAUSE_EPOCH > UNPAUSE_EPOCH`; the CAS winner takes
// responsibility for issuing one wake. `PARKED_COUNT` is a
// fast-path skip — if zero, the producer's targeted-signal arm
// does not need to consult the epochs at all.
//
// Reset to zero in `boc_sched_init`/`boc_sched_shutdown` so a fresh
// runtime cycle starts with the invariant `PAUSE_EPOCH == UNPAUSE_EPOCH`
// (no parker has spoken; producers take the fast arm).

static boc_atomic_u64_t PAUSE_EPOCH = 0;
static boc_atomic_u64_t UNPAUSE_EPOCH = 0;
static boc_atomic_u32_t PARKED_COUNT = 0;

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

int boc_sched_init(Py_ssize_t worker_count) {
  // Defensive: refuse a leak if init is called twice without an
  // intervening shutdown.
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
    // PyMem_RawCalloc (not PyMem_Calloc): the WORKERS array is
    // process-global and is touched by every sub-interpreter worker
    // thread. Since CPython 3.12 the object/Mem allocators are
    // per-interpreter, so an allocation made in interpreter A would
    // be invalid (and unfreeable) from interpreter B. The raw
    // allocator is process-wide and GIL-independent. Zero-init gives
    // every counter, every typed atomic slot (compat.h
    // `boc_atomic_*_t` are layout-compatible with the underlying
    // scalar; zero is the well-defined "false" / NULL / 0 state on
    // every supported platform), and every reserved slot the correct
    // starting value.
    WORKERS = (boc_sched_worker_t *)PyMem_RawCalloc((size_t)worker_count,
                                                    sizeof(boc_sched_worker_t));
    if (WORKERS == NULL) {
      PyErr_NoMemory();
      return -1;
    }

    // Per-worker non-trivial initialisation: bq queue, mutex,
    // condvar, owner-interp placeholder, and the ring-link.
    // Mutex and condvar wrappers come from `compat.h` (pthread on
    // POSIX, SRWLock / CONDITION_VARIABLE on MSVC).
    for (Py_ssize_t i = 0; i < worker_count; ++i) {
      boc_sched_worker_t *w = &WORKERS[i];
      // Initialise all N sub-queues of the WSQ. Cursors are
      // zero-initialised by the parent `PyMem_RawCalloc` of the
      // WORKERS array; we re-set them here to make the invariant
      // explicit and survive any future move to non-zeroing
      // allocators.
      for (size_t j = 0; j < (size_t)BOC_WSQ_N; ++j) {
        boc_bq_init(&w->q[j]);
      }
      w->enqueue_index.idx = 0;
      w->dequeue_index.idx = 0;
      w->steal_index.idx = 0;
      boc_mtx_init(&w->cv_mu);
      cnd_init(&w->cv);
      // owner_interp_id is set when the worker calls
      // `boc_sched_worker_register`. -1 means "not yet registered".
      w->owner_interp_id = -1;
      // Ring-link: i -> i+1, last wraps to 0. Immutable after this
      // point.
      w->next_in_ring = &WORKERS[(i + 1) % worker_count];
      // Verona `core.h:23`: `should_steal_for_fairness{true}` — every
      // freshly-constructed Core starts with the flag set, so the
      // first `get_work` call on each worker takes the fairness arm
      // (which is what enqueues the token into the queue for the
      // first time; nothing else seeds it). Release-store so a
      // worker thread that subsequently reads it under acquire sees
      // the initialised value.
      boc_atomic_store_bool_explicit(&w->should_steal_for_fairness, true,
                                     BOC_MO_RELEASE);
    }
  }

  // Initial publish of WORKER_COUNT and INCARNATION. On the GIL
  // build no concurrent producers can exist at this point (workers
  // have not been spawned yet, and `start()` is single-threaded
  // under the GIL), so plain stores would suffice. On the
  // free-threaded build (PEP 703) an off-worker producer surviving
  // a prior stop()/start() cycle can ACQUIRE-load WORKER_COUNT in
  // `boc_sched_dispatch` and see the new non-zero value here. RELAXED
  // stores would only synchronise that ACQUIRE with the previous
  // shutdown's WORKER_COUNT = 0 RELEASE, leaving no happens-before
  // edge with the per-slot `boc_bq_init` / `boc_mtx_init` writes
  // above -- the producer could legally read `wc > 0` and then
  // dereference a `WORKERS[i]` whose mutex is still in pre-init
  // bytewise state. The same hazard applies to the INCARNATION
  // re-seed: a producer ACQUIRE-loading the new incarnation must
  // observe the new WORKERS pointer, not whatever was cached. Use
  // RELEASE so init and shutdown publish-pair symmetrically with
  // the dispatch-side ACQUIRE on every cycle.
  boc_atomic_store_u64_explicit(&WORKER_COUNT, (uint64_t)worker_count,
                                BOC_MO_RELEASE);
  boc_atomic_store_u64_explicit(
      &INCARNATION,
      boc_atomic_load_u64_explicit(&INCARNATION, BOC_MO_RELAXED) + 1,
      BOC_MO_RELEASE);
  // Re-entry safety: every start cycle starts slot allocation at 0.
  // Done after WORKER_COUNT/WORKERS are valid so a racing register()
  // (none expected at this point because workers have not been
  // spawned yet, but defensively correct) sees a consistent state.
  boc_atomic_store_u32_explicit(&REGISTERED_COUNT, 0, BOC_MO_RELAXED);
  // Park/unpark protocol epochs: a fresh runtime cycle starts with
  // the invariant PAUSE_EPOCH == UNPAUSE_EPOCH (no parker has spoken).
  boc_atomic_store_u64_explicit(&PAUSE_EPOCH, 0, BOC_MO_RELAXED);
  boc_atomic_store_u64_explicit(&UNPAUSE_EPOCH, 0, BOC_MO_RELAXED);
  boc_atomic_store_u32_explicit(&PARKED_COUNT, 0, BOC_MO_RELAXED);
  return 0;
}

void boc_sched_shutdown(void) {
  // Order matters for the off-worker dispatch race.
  // Off-worker producers in `boc_sched_dispatch` acquire-load
  // WORKER_COUNT and treat 0 as the runtime-down sentinel. We must
  // therefore publish WORKER_COUNT = 0 (and bump INCARNATION to
  // self-invalidate any cached `rr_nonlocal` TLS in off-worker
  // threads) BEFORE freeing the WORKERS array, otherwise a racing
  // dispatch could dereference a freed slot.
  Py_ssize_t old_count =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  // Release-store: pairs with the acquire-load in the off-worker
  // arm of `boc_sched_dispatch`. A producer that observes
  // WORKER_COUNT == 0 must NOT then observe a freed WORKERS slot;
  // RELEASE here + ACQUIRE there gives that happens-before edge
  // without an explicit `atomic_thread_fence`.
  boc_atomic_store_u64_explicit(&WORKER_COUNT, 0, BOC_MO_RELEASE);
  // Bump the incarnation so any thread-local `rr_nonlocal` cached
  // by off-worker producers becomes self-invalidating; pairs with
  // the acquire-load in `boc_sched_dispatch`. Doing this here (in
  // addition to `boc_sched_init`) closes the start/stop/start
  // window where a producer's TLS still holds the prior
  // incarnation's worker pointer. RELEASE-store mirrors the
  // WORKER_COUNT = 0 store above.
  boc_atomic_store_u64_explicit(
      &INCARNATION,
      boc_atomic_load_u64_explicit(&INCARNATION, BOC_MO_RELAXED) + 1,
      BOC_MO_RELEASE);
  // No standalone fence needed: the RELEASE stores above already
  // establish the happens-before edge with the dispatch-side
  // ACQUIRE loads. Pairs with the acquire-load in the dispatch
  // path.
  if (WORKERS != NULL) {
    // Per-worker teardown in reverse order. The bq must be empty at
    // this point — `boc_bq_destroy_assert_empty` aborts if not.
    for (Py_ssize_t i = old_count - 1; i >= 0; --i) {
      boc_sched_worker_t *w = &WORKERS[i];
      // Tear down all N sub-queues; each must be empty.
      for (size_t j = 0; j < (size_t)BOC_WSQ_N; ++j) {
        boc_bq_destroy_assert_empty(&w->q[j]);
      }
      cnd_destroy(&w->cv);
      mtx_destroy(&w->cv_mu);
    }
    PyMem_RawFree(WORKERS);
    WORKERS = NULL;
  }
  // Reset the registration counter so external observers see a
  // clean post-stop state. Symmetric with the reset in
  // `boc_sched_init`.
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
  // Best-effort relaxed snapshot. Each field is read independently;
  // the snapshot may observe individual counter values from
  // different points in time. Counters are monotonic, so a torn
  // read between fields can only under-report -- never produce a
  // value greater than the true count.
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

// ---------------------------------------------------------------------------
// Per-worker registration
// ---------------------------------------------------------------------------

Py_ssize_t boc_sched_worker_register(void) {
  // Allocate the next slot. Returns the *previous* value, so the
  // first caller gets 0. Relaxed is fine: the only writer this races
  // with is itself; downstream consumers reach the slot through a
  // subsequent TLS write or through `boc_sched_stats_snapshot`, both
  // of which are sequenced after this call returns.
  uint32_t slot =
      boc_atomic_fetch_add_u32_explicit(&REGISTERED_COUNT, 1, BOC_MO_RELAXED);
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  if ((Py_ssize_t)slot >= wc) {
    // Over-registration: roll back the counter so a subsequent
    // (legitimate) registration would still succeed if a slot frees.
    // Keeps the `registered_count == worker_count` invariant clean
    // after a successful run.
    boc_atomic_fetch_sub_u32_explicit(&REGISTERED_COUNT, 1, BOC_MO_RELAXED);
    return -1;
  }

  // Stamp the slot's owner-witness with the calling sub-interpreter
  // id. This is a debug aid and the wrong-thread assert hook;
  // nothing reads it on a hot path.
  PyInterpreterState *interp = PyInterpreterState_Get();
  WORKERS[slot].owner_interp_id = (Py_ssize_t)PyInterpreterState_GetID(interp);

  // Install the TLS handle. From here on, any dispatch / pop call on
  // this thread finds its worker in O(1) without consulting the
  // WORKERS array.
  current_worker = &WORKERS[slot];

  // Seed the consumer-side batch budget so the first pop on this
  // thread can take pending without first draining the queue. The
  // zero default would otherwise mis-classify the first pop as
  // batch-exhausted and break Verona's `next_work` priority.
  batch = BOC_BQ_BATCH_SIZE;
  // Clear the steal victim cursor: it is lazy-initialised on the
  // first try_steal call. A stale TLS pointer from a previous
  // start cycle would point into a freed worker array.
  steal_victim = NULL;
  return (Py_ssize_t)slot;
}

boc_sched_worker_t *boc_sched_current_worker(void) { return current_worker; }

// ---------------------------------------------------------------------------
// Park/unpark protocol implementation
// ---------------------------------------------------------------------------

// Forward declaration: the slow steal helper is defined further down
// (with `try_steal` and the quiescence-window machinery). `pop_slow`
// calls it between the local-queue dequeue and the park, matching
// Verona's `get_work` ordering (`schedulerthread.h:122-167`).
static boc_bq_node_t *boc_sched_steal(boc_sched_worker_t *self);

void boc_sched_signal_one(boc_sched_worker_t *target) {
  if (target == NULL) {
    return;
  }
  // Lock-then-signal: under cv_mu we serialise against the parker's
  // epoch re-check. If the parker is between its re-check and the
  // `parked = true` store, our signal would otherwise be lost; the
  // mutex acquisition forces us to wait until either the parker has
  // committed to sleep (and `cnd_signal` will wake it) or has bailed
  // out (and our signal is harmless).
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
  // Cheap early-out: if no worker is parked, the walk would do
  // WORKER_COUNT acquire-loads for nothing. The relaxed load is
  // sufficient because a producer that observed PARKED_COUNT == 0
  // and a parker that subsequently parks would, on the next
  // producer's CAS-arm entry, re-publish (pe != ue forces another
  // CAS and wake attempt). The protocol explicitly tolerates a
  // stale zero here.
  if (boc_atomic_load_u32_explicit(&PARKED_COUNT, BOC_MO_RELAXED) == 0) {
    return;
  }
  // Broadcast wake: walk the entire ring starting from
  // self->next_in_ring and signal every parked worker. Mirrors
  // Verona's ThreadSync::unpause_all (threadsync.h:108-128,
  // threadpool.h:367-373). Without the broadcast, a burst of
  // producer publishes that all CAS-lose against a single winner
  // would leave N-1 parkers asleep until they each happen to be
  // signal-targeted by some later off-worker dispatch.
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
  // Phase 1: set stop_requested on every worker (release store so a
  // worker waking from cnd_wait observes the flag with acquire).
  for (Py_ssize_t i = 0; i < wc; ++i) {
    boc_atomic_store_bool_explicit(&WORKERS[i].stop_requested, true,
                                   BOC_MO_RELEASE);
  }
  // Phase 2: signal every worker's condvar under its mutex. We use
  // signal-per-worker rather than broadcast on a global condvar
  // because the bocpy precedent is per-queue waiters; each worker
  // has its own cv. The mutex acquisition serialises against any
  // parker that is between its epoch re-check and the cnd_wait call.
  for (Py_ssize_t i = 0; i < wc; ++i) {
    boc_sched_signal_one(&WORKERS[i]);
  }
}

boc_bq_node_t *boc_sched_worker_pop_slow(boc_sched_worker_t *self) {
  // stop_requested is checked at the top of every loop iteration,
  // BEFORE any pause_epoch bump, so a worker exiting on shutdown
  // does not advance pause_epoch past unpause_epoch.
  for (;;) {
    if (boc_atomic_load_bool_explicit(&self->stop_requested, BOC_MO_ACQUIRE)) {
      return NULL;
    }

    // ----- Steal-for-fairness arm -----
    //
    // Verona `schedulerthread.h::get_work:143-162`. When the
    // fairness flag is set AND the local queue has at least one
    // visible item, attempt one steal pass *before* draining the
    // local queue. If the steal succeeds we still re-enqueue the
    // token and return the stolen item; if it fails we fall through
    // to the local dequeue. The flag is cleared *before* the token
    // re-enqueue (Verona note: "Set the flag before rescheduling
    // the token so that we don't have a race"). The token itself is
    // installed by `_core_scheduler_runtime_start` and is never
    // freed by this path; re-enqueue is a node operation only.
    //
    // Runs BEFORE the defensive `pending` check so the
    // batch==0-forced-queue-drain fall-through from `pop_fast`
    // (which leaves `pending` set when the gate trips) still pays
    // the fairness tax.
    //
    // **WSQ cadence sensitivity.** The token is re-enqueued via
    // `boc_wsq_enqueue` below, which pushes round-robin via
    // `enqueue_index` and so rotates the token across the worker's
    // `BOC_WSQ_N` sub-queues over time. Owner-side
    // `boc_wsq_dequeue` scans sub-queues in `dequeue_index` order,
    // so the token's consumption rate (and therefore the
    // fairness-arm cadence) is proportional to the cursor
    // desynchronisation between `enqueue_index` and `dequeue_index`
    // rather than to absolute local work. This matches verona's
    // design (verona's `Core` carries the same `WrapIndex<N>`
    // cursors and re-enqueues its fairness token via `enqueue`); a
    // regression that pinned the token to one sub-queue would shift
    // `fairness_arm_fires` by a factor of `BOC_WSQ_N` without any
    // test failure today.
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

    // Defensive: under normal flow `pop_fast` exhausts pending
    // before falling through to `pop_slow`, but a future caller may
    // enter slow-path directly (e.g. test harness). Honour pending
    // first so we never park while an unconsumed thread-local item
    // is sitting on this thread.
    if (pending != NULL) {
      boc_bq_node_t *n = pending;
      pending = NULL;
      return n;
    }

    // ----- Local-queue dequeue -----
    //
    // Verona `get_work:165`. With the fairness arm cleared (or
    // skipped) this is the primary work source.
    boc_bq_node_t *n = boc_wsq_dequeue(self);
    if (n != NULL) {
      return n;
    }

    // ----- Empty-queue steal arm -----
    //
    // Verona `get_work:171-178`: an empty local queue is treated
    // "like receiving a token" — try a steal directly. bocpy bundles
    // the multi-victim ring + quiescence-window backoff into
    // `boc_sched_steal`; if it returns non-NULL we have a stolen
    // node (and the splice contract has already moved any remainder
    // onto self->q). Returning NULL is the signal to commit to the
    // park below.
    n = boc_sched_steal(self);
    if (n != NULL) {
      return n;
    }

    // ----- Park-attempt -----
    //
    // Snapshot UNPAUSE_EPOCH BEFORE bumping PAUSE_EPOCH (mirrors
    // Verona `threadpool.h::pause:283-285`). The pre-bump snapshot
    // closes a lost-wakeup race: a producer that publishes between
    // our bump and the snapshot would otherwise advance UNPAUSE_EPOCH
    // to the new pause_epoch, but our (post-bump) snapshot would
    // already see the advanced value, causing the cv_mu re-check
    // below to compare equal and park anyway, consuming the wake.
    // With the pre-bump snapshot, the producer's CAS must advance
    // past `ue_snap`, and the re-check observes the inequality and
    // bails out of the park. Relaxed is sufficient because the
    // seq_cst fetch_add on PAUSE_EPOCH that follows provides the
    // total order with the producer's load of PAUSE_EPOCH.
    uint64_t ue_snap =
        boc_atomic_load_u64_explicit(&UNPAUSE_EPOCH, BOC_MO_RELAXED);

    // Bump PAUSE_EPOCH so any concurrent producer sees pe != ue and
    // is forced into the CAS arm. seq_cst is required: the increment
    // must totally-order with the producer's load-acquire of
    // PAUSE_EPOCH.
    boc_atomic_fetch_add_u64_explicit(&PAUSE_EPOCH, 1, BOC_MO_SEQ_CST);

    // check_for_work: walks ALL workers via
    // `boc_sched_any_work_visible()`. Cheap: one acquire-load per
    // queue, no global lock. A parker that observes work anywhere
    // in the ring re-loops and either dequeues locally or steals.
#if BOC_HAVE_TRY_STEAL
    if (boc_sched_any_work_visible()) {
      continue;
    }
#else
    if (!boc_wsq_is_empty(self)) {
      continue;
    }
#endif

    // Final epoch re-check under cv_mu. Drops the GIL across the
    // wait so other Python work can proceed. terminator_count is
    // NOT consulted here — quiescence is transient; only
    // stop_requested causes exit.
    Py_BEGIN_ALLOW_THREADS mtx_lock(&self->cv_mu);
    if (boc_atomic_load_bool_explicit(&self->stop_requested, BOC_MO_ACQUIRE)) {
      mtx_unlock(&self->cv_mu);
    } else if (boc_atomic_load_u64_explicit(&UNPAUSE_EPOCH, BOC_MO_ACQUIRE) !=
               ue_snap) {
      // A producer caught up between our epoch bump and the lock;
      // skip the wait and re-loop.
      mtx_unlock(&self->cv_mu);
    } else {
      // Bump the cumulative `parked` counter before the actual
      // wait so a snapshot from another thread sees the entry
      // even if the wait blocks indefinitely. Live PARKED_COUNT
      // tracks current depth; stats.parked tracks total entries.
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

// ---------------------------------------------------------------------------
// Dispatch + fast-path pop
// ---------------------------------------------------------------------------

boc_bq_node_t *boc_sched_worker_pop_fast(boc_sched_worker_t *self) {
  if (self == NULL) {
    return NULL;
  }

  // BATCH_SIZE fairness: take pending only while batch > 0. When
  // batch hits 0, fall through to the queue so a producer-local
  // chain (which evicts every prior pending into the queue) cannot
  // run newest-first forever and starve the older queued items.
  // Verona `schedulerthread.h:122-138`.
  if (pending != NULL && batch > 0) {
    boc_bq_node_t *n = pending;
    pending = NULL;
    batch--;
    boc_atomic_fetch_add_u64_explicit(&self->stats.popped_local, 1,
                                      BOC_MO_RELAXED);
    return n;
  }

  // ----- Steal-for-fairness gate (Verona schedulerthread.h:143) -----
  //
  // Verona's `get_work` runs the fairness arm AFTER consuming
  // `next_work` (≈ `pending`) but BEFORE draining the local queue.
  // We mirror that order here: a busy worker steadily draining its
  // own queue still pays the per-token-period fairness tax, by
  // routing through `pop_slow` (which owns the arm body —
  // re-enqueue token, attempt steal, clear flag).
  //
  // Returning NULL here costs the caller one extra function-call
  // (`pop_slow`) per fairness period; the arm itself has the same
  // cost it has always had.
  if (boc_atomic_load_bool_explicit(&self->should_steal_for_fairness,
                                    BOC_MO_ACQUIRE) &&
      !boc_wsq_is_empty(self)) {
    return NULL;
  }

  boc_bq_node_t *n = boc_wsq_dequeue(self);
  if (n != NULL) {
    // Any successful queue dequeue resets the budget; if pending was
    // bypassed because batch had hit 0, count this as a batch_reset
    // for the fairness exit-criterion test. (A first-time pop with
    // an empty pending also resets the budget but does not bump the
    // counter — there was no fast path to bypass.)
    if (pending != NULL) {
      boc_atomic_fetch_add_u64_explicit(&self->stats.batch_resets, 1,
                                        BOC_MO_RELAXED);
    }
    batch = BOC_BQ_BATCH_SIZE;
    boc_atomic_fetch_add_u64_explicit(&self->stats.popped_local, 1,
                                      BOC_MO_RELAXED);
    return n;
  }

  // Queue is empty. If pending is set we exhausted the batch budget
  // but have nothing else to fall back on — take pending and reset.
  // Without this branch a single-worker chain would loop into
  // pop_slow and park the worker against its own pending item.
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
  boc_sched_worker_t *target;

  if (self != NULL) {
    // Producer-local arm (Verona schedule_fifo).
    // Always evict the prior `pending` to the local queue and
    // install `n` as the new pending. The eviction (not the install)
    // is what bumps `pushed_local`: the queue push is the externally
    // visible event for stats purposes; replacing pending with no
    // prior occupant is a free local handoff that costs nothing
    // measurable.
    if (pending != NULL) {
      boc_wsq_enqueue(self, pending);
      boc_atomic_fetch_add_u64_explicit(&self->stats.pushed_local, 1,
                                        BOC_MO_RELAXED);
    } else {
      // Producer-locality bypass: dispatch into an empty `pending`
      // slot. No queue push, no atomic queue-side state mutation,
      // but bump `dispatched_to_pending` so the dispatched-work
      // total remains globally reconcilable as
      // `Σ pushed_local + Σ dispatched_to_pending + Σ pushed_remote
      // == Σ popped_local + Σ popped_via_steal`. Without this
      // bump the queue's `pushed_local` underreports total
      // dispatched work whenever steady-state pop-then-dispatch
      // keeps `pending` empty most cycles.
      boc_atomic_fetch_add_u64_explicit(&self->stats.dispatched_to_pending, 1,
                                        BOC_MO_RELAXED);
    }
    pending = n;
    target = self;
  } else {
    // Off-worker arm: round-robin over the worker ring.
    //
    // Acquire-load WORKER_COUNT and INCARNATION so we observe the
    // RELEASE-stores from `boc_sched_shutdown` BEFORE we could
    // observe a freed WORKERS[] slot. Without this acquire, an
    // off-worker producer running concurrently with shutdown
    // could read a stale WORKER_COUNT > 0 and dereference
    // WORKERS[0] after it had been freed.
    Py_ssize_t wc =
        (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_ACQUIRE);
    // Re-seed `rr_nonlocal` whenever the scheduler incarnation
    // changes so a `start()`/`wait()`/`start()` cycle with a
    // different worker count cannot land on a stale pointer.
    size_t inc_now =
        (size_t)boc_atomic_load_u64_explicit(&INCARNATION, BOC_MO_ACQUIRE);
    // Check WORKER_COUNT FIRST so the runtime-down sentinel is
    // honoured even when the cached `rr_nonlocal` is non-NULL but
    // points into the prior incarnation's freed array (the
    // shutdown-then-restart-with-different-count race).
    if (wc == 0) {
      // No runtime up — surface as a Python exception. Prior
      // behaviour was a silent drop, which left whencall's
      // `terminator_inc` un-rolled-back: the next `wait()` would
      // hang because the caller's hold was never released. The
      // caller (`whencall` in `behaviors.py`) catches this and
      // calls `terminator_dec` to roll back its hold.
      PyErr_SetString(
          PyExc_RuntimeError,
          "cannot schedule behavior: bocpy runtime is not running. "
          "Call bocpy.start() before scheduling, or avoid scheduling "
          "after wait() / stop() has shut the runtime down.");
      return -1;
    }
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

  // ---- Slow arm: pause/unpause-aware wake -----------------------------
  //
  // Producer half of the parking protocol. Loaded with acquire so
  // the parker's seq_cst PAUSE_EPOCH bump is observed in order. If
  // pe == ue the fast path is taken (no parker is racing); otherwise
  // CAS UNPAUSE_EPOCH forward and, on CAS-win, broadcast-wake every
  // parked peer.
  uint64_t pe = boc_atomic_load_u64_explicit(&PAUSE_EPOCH, BOC_MO_ACQUIRE);
  uint64_t ue = boc_atomic_load_u64_explicit(&UNPAUSE_EPOCH, BOC_MO_ACQUIRE);
  if (pe != ue) {
    if (boc_atomic_compare_exchange_strong_u64_explicit(
            &UNPAUSE_EPOCH, &ue, pe, BOC_MO_ACQ_REL, BOC_MO_ACQUIRE)) {
      // Walk from `target` so the wake prefers a peer rather than
      // the worker we just published to (which is either us or the
      // round-robin target — both cases are awake or about to be
      // signalled). For off-worker dispatch `self` is NULL so we
      // pass `target` directly; for producer-local we pass `self`.
      boc_sched_unpause_all(self != NULL ? self : target);
    }
  }

  // Targeted wake when crossing to a different worker. Producer-
  // local dispatch (target == self) skips this: the producer thread
  // is the worker that will run the work, so it cannot be parked.
  if (self == NULL || target != self) {
    boc_sched_signal_one(target);
  }

  return 0;
}

// ---------------------------------------------------------------------------
// Work stealing (`try_steal`)
// ---------------------------------------------------------------------------
//
// Port of the work-stealing primitive from
// `verona-rt/src/rt/sched/schedulerthread.h::try_steal` plus the
// underlying queue-level steal at
// `verona-rt/src/rt/sched/workstealingqueue.h::steal`. Each worker
// owns a `boc_bq_t q[BOC_WSQ_N]` sub-queue array; this thief reads
// the victim's sub-queue indexed by `self->steal_index` (verona's
// `this->steal_index`) and `enqueue_spread`s the remainder across
// its own N sub-queues to dilute thief-vs-thief contention on
// subsequent steals.
//
// `boc_sched_try_steal` is the **single-victim** fast attempt: at
// most one `dequeue_all` call against `victim->q[steal_index]`,
// then the per-thread victim cursor advances unconditionally so the
// next attempt visits a different victim regardless of outcome. The
// slow multi-victim loop with quiescence timeout (Verona's
// `steal()`) follows.

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
  // Lazy-init the cursor on first use. WORKER_COUNT == 0 cannot
  // happen here because every caller has a registered self handle.
  if (steal_victim == NULL) {
    steal_victim = self->next_in_ring;
  }

  boc_sched_worker_t *victim = steal_victim;
  // Advance the victim cursor unconditionally. Verona does this
  // after the steal call (whether the call returned work or not);
  // placing the store before the work-doing code keeps the function
  // tail-clean (no bookkeeping on the success path).
  steal_victim = steal_victim->next_in_ring;

  // Stamp the monotonic timestamp before any other bookkeeping so
  // a snapshot taken concurrently observes the entry even if the
  // call returns NULL early (self-victim, empty victim, etc.).
  // Relaxed is fine: the field is diagnostic; readers tolerate a
  // torn read between this store and the snapshot's load.
  boc_atomic_store_u64_explicit(&self->stats.last_steal_attempt_ns,
                                boc_now_ns(), BOC_MO_RELAXED);

  boc_atomic_fetch_add_u64_explicit(&self->stats.steal_attempts, 1,
                                    BOC_MO_RELAXED);

  // Don't steal from yourself (Verona `WorkStealingQueue::steal`
  // self-check: `if (&victim == this) { ++steal_index; return
  // nullptr; }`). Counts as a failure for diagnostic purposes — a
  // single-worker runtime will see steal_failures == steal_attempts
  // which is the expected steady state.
  if (victim == self) {
    boc_wsq_pre_inc(&self->steal_index);
    boc_atomic_fetch_add_u64_explicit(&self->stats.steal_failures, 1,
                                      BOC_MO_RELAXED);
    return NULL;
  }

  // Pick the victim's sub-queue indexed by *this thief's*
  // steal_index (verona: `victim.queues[steal_index]`, where the
  // index belongs to the calling WSQ — the thief). The cursor is
  // touched only by `self`, so no atomic is needed.
  size_t vidx = self->steal_index.idx;
  boc_bq_segment_t seg = boc_bq_dequeue_all(&victim->q[vidx]);

  // Try to take the head off the segment.
  boc_bq_node_t *r = boc_bq_segment_take_one(&seg);
  if (r == NULL) {
    // take_one returns NULL for three reasons (mpmcq.h:67-89):
    //   1. fully empty segment (start == NULL, end == NULL),
    //   2. single-element segment (end == &start->next_in_queue),
    //   3. first link in segment not yet visible (start != NULL,
    //      next_in_queue still NULL).
    //
    // Case 1: nothing to steal — return NULL. Verona's
    // `WorkStealingQueue::steal` `if (ls.end == nullptr) return
    // nullptr;`.
    if (seg.end == NULL) {
      boc_atomic_fetch_add_u64_explicit(&self->stats.steal_failures, 1,
                                        BOC_MO_RELAXED);
      return NULL;
    }
    // Case 2: the segment IS our stolen node — verona returns
    // `ls.start` directly without spreading anything (there is no
    // remainder). `workstealingqueue.h:107-108`.
    if (seg.start != NULL && seg.end == &seg.start->next_in_queue) {
      r = seg.start;
      boc_atomic_fetch_add_u64_explicit(&self->stats.popped_via_steal, 1,
                                        BOC_MO_RELAXED);
      return r;
    }
    // Case 3: take_one observed start != NULL but start->next not
    // yet visible (the producer has done `back.exchange` but not
    // yet published the next pointer). The segment is "owned" by
    // us (acquire_front succeeded inside dequeue_all) and we
    // cannot safely splice it back into the victim mid-link.
    //
    // Verona faithful: `WorkStealingQueue::steal` falls through to
    // `enqueue_spread(ls); return r;` here, with `r == nullptr`.
    // We do the same — spread the partial segment onto our own
    // sub-queues and return NULL so the caller re-loops to its own
    // dequeue.
    boc_wsq_enqueue_spread(self, seg);
    boc_atomic_fetch_add_u64_explicit(&self->stats.steal_failures, 1,
                                      BOC_MO_RELAXED);
    return NULL;
  }

  // Common case: head taken; spread the rest across self's N
  // sub-queues so subsequent thieves stealing from self see N
  // independent targets instead of one. Verona:
  // `enqueue_spread(ls); return r;`.
  boc_wsq_enqueue_spread(self, seg);
  boc_atomic_fetch_add_u64_explicit(&self->stats.popped_via_steal, 1,
                                    BOC_MO_RELAXED);
  return r;
}

// ---------------------------------------------------------------------------
// Slow steal loop
// ---------------------------------------------------------------------------
//
// Port of `verona-rt/src/rt/sched/schedulerthread.h::steal` adapted
// for bocpy's parking protocol. The main differences:
//
//   * Verona has no separate park primitive: its `steal()` busy-spins
//     with a TSC-quiescence backoff and only commits to the global
//     `pause` state after the timeout. bocpy already has a condvar
//     park, so the slow loop's job is *not* to outwait contention —
//     it just gives a producer a small pre-park grace window in case
//     work is about to be published, then returns NULL so the caller
//     (`pop_slow`) parks under cv_mu.
//
//   * Verona walks `running` (a flag flipped by the global pause()
//     side); bocpy walks `self->stop_requested` (per-worker, set by
//     `boc_sched_worker_request_stop_all`).
//
//   * Verona uses TSC ticks (`DefaultPal::tick`) for the quiescence
//     gate; bocpy uses @ref boc_now_ns (CLOCK_MONOTONIC on POSIX,
//     QueryPerformanceCounter on Windows).
//
// Loop shape (per round):
//   1. stop_requested check.
//   2. yield (BOC_SCHED_YIELD).
//   3. own queue dequeue (catch work that another thread published
//      onto our q since the last pop attempt).
//   4. one full ring of `try_steal` calls (bounded at
//      `WORKER_COUNT - 1` distinct victims; self-victim is skipped
//      and counted as a failure).
//   5. on miss, sample the monotonic clock; if the elapsed time
//      since loop entry exceeds @ref BOC_STEAL_QUIESCENCE_NS,
//      return NULL → caller parks. Otherwise sleep briefly and
//      retry.
//
// The constant @ref BOC_STEAL_QUIESCENCE_NS is a tunable; 100µs
// matches Verona's `TSC_QUIESCENCE_TIMEOUT` order of magnitude on
// contemporary CPUs. The pre-park backoff is a `nanosleep`-style
// short sleep rather than a busy spin so two parked workers do not
// race their own backoff loops to 100% CPU.

#ifndef BOC_STEAL_QUIESCENCE_NS
#define BOC_STEAL_QUIESCENCE_NS 100000ULL // 100µs
#endif

#ifndef BOC_STEAL_BACKOFF_NS
#define BOC_STEAL_BACKOFF_NS 5000ULL // 5µs sleep between rounds
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
    // No peers to steal from. Skip the whole loop — the caller
    // will park immediately, which is the only sensible behaviour
    // on a single-worker runtime. We do not bump steal_attempts
    // here: the call did not actually visit a victim.
    return NULL;
  }

  const uint64_t deadline = boc_now_ns() + BOC_STEAL_QUIESCENCE_NS;

  for (;;) {
    if (boc_atomic_load_bool_explicit(&self->stop_requested, BOC_MO_ACQUIRE)) {
      return NULL;
    }

    BOC_SCHED_YIELD();

    // Own-queue catch (Verona schedulerthread.h:269-272).
    boc_bq_node_t *n = boc_wsq_dequeue(self);
    if (n != NULL) {
      return n;
    }

    // One full ring of try_steal. WORKER_COUNT - 1 visits is
    // enough to attempt every distinct peer once; the cursor
    // advances inside try_steal so successive calls see different
    // victims. self-victim is automatically skipped (and counted
    // as a steal_failure) so a single loop iteration may visit
    // self once when WORKER_COUNT == 2 (cursor 0→1→0) — that is
    // benign, the worst case is one wasted check.
    for (Py_ssize_t i = 0; i < wc - 1; ++i) {
      n = boc_sched_try_steal(self);
      if (n != NULL) {
        return n;
      }
    }

    // Quiescence gate: if the window has expired, give up and let
    // the caller park. Without this gate we would either busy-spin
    // forever (waste CPU) or have no preemption between unrelated
    // workers (subtle starvation under the GIL). The window must
    // be short enough that a worker waiting one quiescence-period
    // does not hurt latency-sensitive workloads; 100µs is well
    // below any realistic behaviour body and matches Verona's
    // TSC_QUIESCENCE_TIMEOUT in order of magnitude.
    if (boc_now_ns() >= deadline) {
      return NULL;
    }

    // Brief sleep so two concurrently-failing thieves do not pin
    // their cores. Using `boc_sleep_ns` (compat.h) rather than
    // `sched_yield` because we want a hard backoff: a yield is
    // ineffective when there is no other runnable thread (the
    // case during quiescence).
    boc_sleep_ns(BOC_STEAL_BACKOFF_NS);
  }
}

// ---------------------------------------------------------------------------
// Per-worker fairness token (`token_work`)
// ---------------------------------------------------------------------------
//
// `token_work` is a `boc_atomic_ptr_t` slot inside `boc_sched_worker_t`.
// The token itself is a `BOCBehavior` allocated by
// `_core_scheduler_runtime_start` (which is the only TU that knows
// the `BOCBehavior` layout); this TU treats it as an opaque
// `boc_bq_node_t *`. Lifecycle:
//
//   * `_core_scheduler_runtime_start` calls `boc_sched_init` then, for
//     every worker, allocates a token `BOCBehavior` (zero-initialised,
//     `is_token = 1`) and installs `&token->bq_node` here.
//   * `_core_scheduler_runtime_stop` calls `boc_sched_get_token_node`
//     for each worker to recover the pointer, frees the `BOCBehavior`,
//     then calls `boc_sched_shutdown`.
//
// The slot is never freed by `boc_sched_shutdown` — that would require
// this TU to dereference a `BOCBehavior`, breaking the layered
// boundary. Releasing it before shutdown is a `_core.c` responsibility.

int boc_sched_set_token_node(Py_ssize_t worker_index, boc_bq_node_t *node) {
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  if (worker_index < 0 || worker_index >= wc) {
    return -1;
  }
  // Release-store: a worker thread later doing an acquire-load on
  // `token_work` (e.g. token re-enqueue path) must observe the
  // node and any of its initialised fields written by the producer.
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
  // Release-store: pairs with the acquire-load at the top of the
  // fairness arm in `boc_sched_worker_pop_slow`. Verona equivalent
  // is the closure body in `core.h:28-32`
  // (`this->should_steal_for_fairness = true`).
  boc_atomic_store_bool_explicit(&self->should_steal_for_fairness, value,
                                 BOC_MO_RELEASE);
}

bool boc_sched_any_work_visible(void) {
  Py_ssize_t wc =
      (Py_ssize_t)boc_atomic_load_u64_explicit(&WORKER_COUNT, BOC_MO_RELAXED);
  // Walk the full worker array. `boc_bq_is_empty` is an acquire-
  // load on the queue's `front` pointer — cheap, no global lock.
  // The walk is racy by design (a producer publishing onto a
  // queue we have already passed will force itself through the
  // CAS arm of the parker protocol; see `unpause_all`), so a
  // stale `false` is acceptable: the epoch re-check under `cv_mu`
  // catches it before the parker sleeps.
  for (Py_ssize_t i = 0; i < wc; ++i) {
    if (!boc_wsq_is_empty(&WORKERS[i])) {
      return true;
    }
  }
  return false;
}