/// @file sched.h
/// @brief Work-stealing scheduler: per-worker MPMC queues, parking, stats.
///
/// This translation unit owns:
///   - the Verona-style intrusive MPMC behaviour queue (`boc_bq_*`),
///   - per-worker statistics POD (@ref boc_sched_stats_t),
///   - the process-global worker array (allocated by @ref boc_sched_init),
///   - the per-start incarnation counter (@ref boc_sched_incarnation_get),
///   - the dispatch / fast-pop / park-and-wait / work-stealing primitives,
///   - per-worker fairness tokens.
///
/// Verona reference: `verona-rt/src/rt/sched/schedulerstats.h`,
/// `mpmcq.h`, `core.h`, `schedulerthread.h`, `threadpool.h`.

#ifndef BOCPY_SCHED_H
#define BOCPY_SCHED_H

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <Python.h>

#include "compat.h"

// ---------------------------------------------------------------------------
// Verona MPMC behaviour queue (`boc_bq_*`)
// ---------------------------------------------------------------------------
//
// Port of `verona-rt/src/rt/sched/mpmcq.h`. Memory orderings match
// `mpmcq.h` line-for-line; deviations are called out in the
// doc-comments.
//
// The queue is intrusive: each node carries an `_Atomic` link
// (`boc_bq_node_t::next_in_queue`). Production users embed a
// `boc_bq_node_t` field (`BOCBehavior::bq_node`) and pass its address
// to the enqueue/dequeue API; the queue never dereferences anything
// other than the link, so larger user-defined payloads are reached
// via container_of-style arithmetic at the call site.

/// @brief Verona-style intrusive link node.
/// @details Embedded at a struct-end position inside @c BOCBehavior
/// (see `_core.c`). The queue treats nodes as opaque: the only field
/// it reads or writes is @c next_in_queue. Test code may allocate
/// bare @c boc_bq_node_t instances.
typedef struct boc_bq_node {
  /// @brief Intrusive forward link, payload type
  /// `struct boc_bq_node *` stored in a `boc_atomic_ptr_t` slot for
  /// MSVC compatibility (see `compat.h`).
  /// @details Reads use @c BOC_MO_ACQUIRE (mpmcq.h:78,145); writes
  /// use @c BOC_MO_RELEASE (mpmcq.h:113,174) or @c BOC_MO_RELAXED
  /// (mpmcq.h:103,131) per Verona.
  boc_atomic_ptr_t next_in_queue;
} boc_bq_node_t;

/// @brief Half-open contiguous range of nodes built by
///        `boc_bq_dequeue_all` / consumed by `boc_bq_enqueue_segment`.
/// @details Mirrors `MPMCQ::Segment` (mpmcq.h:58-90). `start` is the
/// first node of the segment (NULL → empty); `end` points at the
/// `next_in_queue` slot inside the *last* node, ready to be
/// rewritten by the next enqueue.
typedef struct boc_bq_segment {
  /// @brief First node in the segment (NULL → empty segment).
  boc_bq_node_t *start;
  /// @brief Address of the `next_in_queue` slot of the last node
  /// (a `boc_atomic_ptr_t` whose payload type is
  /// `struct boc_bq_node *`).
  boc_atomic_ptr_t *end;
} boc_bq_segment_t;

/// @brief MPMC behaviour queue.
/// @details Empty representation: @c back == @c &front (mpmcq.h:36).
/// Cacheline-padded so producers (writing @c back) do not false-share
/// with consumers (reading @c front).
typedef struct boc_bq {
  /// @brief Multi-threaded producer end. Payload type is
  /// `boc_atomic_ptr_t *` (the address of either `front` or some
  /// node's `next_in_queue` slot); stored as `boc_atomic_ptr_t` for
  /// MSVC.
  boc_atomic_ptr_t back;
  /// @brief Multi-threaded consumer end. Payload type is
  /// `struct boc_bq_node *`.
  boc_atomic_ptr_t front;
  /// @brief Padding so the next `boc_bq_t` does not share a line.
  char _pad[64 - 2 * sizeof(void *)];
} boc_bq_t;

/// @brief Default batch size.
/// @details Mirrors Verona's `BATCH_SIZE` (`schedulerthread.h`).
/// Consumed by the per-worker `pending`/batch accounting.
static const size_t BOC_BQ_BATCH_SIZE = 100;

/// @brief Optional schedule-perturbation hook.
/// @details Expands to nothing in release builds; to `sched_yield()`
/// when the TU is compiled with `-DBOC_SCHED_SYSTEMATIC`. Mirrors
/// every `Systematic::yield()` site in `mpmcq.h` so the
/// schedule-perturbation points the Verona authors validated against
/// are preserved.
#ifdef BOC_SCHED_SYSTEMATIC
#include <sched.h>
#define BOC_SCHED_YIELD() (void)sched_yield()
#else
#define BOC_SCHED_YIELD() ((void)0)
#endif

// --- Lifecycle -------------------------------------------------------------

/// @brief Initialise an empty queue in place.
/// @details Sets `back == &front` and `front == NULL`. Safe to call
/// on a zeroed allocation.
/// @param q The queue to initialise (must be non-NULL).
void boc_bq_init(boc_bq_t *q);

/// @brief Assert the queue is empty and tear it down.
/// @details Mirrors Verona's `~MPMCQ` (mpmcq.h:213-217). Aborts via
/// @c assert(3) in debug builds if the queue still holds nodes.
/// @param q The queue to destroy (must be non-NULL).
void boc_bq_destroy_assert_empty(boc_bq_t *q);

// --- Producers -------------------------------------------------------------

/// @brief Enqueue a single node at the back of the queue.
/// @details Equivalent to `boc_bq_enqueue_segment({n, &n->next_in_queue})`.
/// The node's `next_in_queue` is overwritten. Mirrors `MPMCQ::enqueue`
/// (mpmcq.h:118-121).
/// @param q The queue (must be non-NULL).
/// @param n The node to enqueue (must be non-NULL).
void boc_bq_enqueue(boc_bq_t *q, boc_bq_node_t *n);

/// @brief Enqueue a pre-linked segment at the back of the queue.
/// @details Mirrors `MPMCQ::enqueue_segment` (mpmcq.h:97-115).
/// @param q The queue (must be non-NULL).
/// @param s A non-empty segment.
void boc_bq_enqueue_segment(boc_bq_t *q, boc_bq_segment_t s);

/// @brief Insert a single node at the front of the queue.
/// @details Mirrors `MPMCQ::enqueue_front` (mpmcq.h:123-135). Useful
/// for handing a stolen node back to its owner ahead of any other
/// pending work.
/// @param q The queue (must be non-NULL).
/// @param n The node to insert (must be non-NULL).
void boc_bq_enqueue_front(boc_bq_t *q, boc_bq_node_t *n);

// --- Consumers -------------------------------------------------------------

/// @brief Try to dequeue a single node from the front.
/// @details May spuriously return NULL even when the queue is non-
/// empty (concurrent enqueuer mid-link). Callers must be prepared to
/// retry. Mirrors `MPMCQ::dequeue` (mpmcq.h:140-184).
/// @param q The queue (must be non-NULL).
/// @return The dequeued node, or NULL.
boc_bq_node_t *boc_bq_dequeue(boc_bq_t *q);

/// @brief Try to detach the entire current contents of the queue.
/// @details Returns a segment whose `start` is the old front and whose
/// `end` is the old back; the caller iterates by chasing
/// `next_in_queue`. May return an empty segment spuriously (same race
/// as `boc_bq_dequeue`). Mirrors `MPMCQ::dequeue_all`
/// (mpmcq.h:187-203).
/// @param q The queue (must be non-NULL).
/// @return A (possibly empty) segment.
boc_bq_segment_t boc_bq_dequeue_all(boc_bq_t *q);

/// @brief Atomically take exclusive ownership of the front pointer.
/// @details Returns the old front and replaces it with NULL, making
/// the queue *appear* empty to any concurrent consumer. The caller
/// is responsible for restoring the front (or enqueuing the head
/// elsewhere). Mirrors `MPMCQ::acquire_front` (mpmcq.h:41-56).
/// @param q The queue (must be non-NULL).
/// @return The previous front pointer (may be NULL).
boc_bq_node_t *boc_bq_acquire_front(boc_bq_t *q);

/// @brief Take a single node from the start of a segment in place.
/// @details Mirrors `MPMCQ::Segment::take_one` (mpmcq.h:67-89). May
/// return NULL if (1) the segment is empty, (2) the segment has a
/// single element, or (3) the link from the head has not yet been
/// completed by a concurrent enqueuer.
/// @param s The segment (must be non-NULL); modified in place.
/// @return The detached head, or NULL.
boc_bq_node_t *boc_bq_segment_take_one(boc_bq_segment_t *s);

// --- Inspection ------------------------------------------------------------

/// @brief Best-effort emptiness test.
/// @details Mirrors `MPMCQ::is_empty` (mpmcq.h:206-210). Result may
/// be stale by the time the caller acts on it.
/// @param q The queue (must be non-NULL).
/// @return @c true if the queue currently appears empty.
bool boc_bq_is_empty(boc_bq_t *q);

// ---------------------------------------------------------------------------
// Verona work-stealing queue cursors (`boc_wsq_*`)
// ---------------------------------------------------------------------------
//
// Port of `verona-rt/src/rt/sched/workstealingqueue.h` and
// `ds/wrapindex.h`. A WSQ is N independent `boc_bq_t` sub-queues
// indexed by three plain-`size_t` cursors:
//   - `enqueue_index`: producer side; pre-increment then push.
//   - `dequeue_index`: owner pop side; pre-increment then pop, try
//                       all N before declaring empty.
//   - `steal_index`: thief side; selects which of the *victim*'s
//                     sub-queues to drain in a steal attempt.
//
// All three cursors are owned by the worker that owns the WSQ.
// `enqueue_index` is touched by every thread that pushes onto this
// worker (including remote producers). The race on it is benign:
// (1) `size_t` aligned loads/stores are atomic at the hardware level
// on every ISA bocpy supports; (2) `(idx + 1) % N` is always in
// `[0, N)` regardless of what value was read; (3) the underlying
// `boc_bq_t` is multi-producer-safe; (4) the only observable effect
// is distribution quality, bounded by concurrent-producer count.
// Verona-rt accepts the same race; we make no deviation.

/// @brief Number of sub-queues per worker WSQ.
/// @details Matches verona-rt's `WorkStealingQueue<4>` template
/// instantiation in `core.h`. Tunable at compile time.
#ifndef BOC_WSQ_N
#define BOC_WSQ_N 4
#endif

/// @brief Plain-`size_t` cursor mirroring verona-rt's
///        `WrapIndex<N>` (`ds/wrapindex.h`).
/// @details No atomic; the race on `enqueue_index` between
/// concurrent producers is benign (see header block above).
typedef struct boc_wsq_cursor {
  /// @brief Current index in `[0, BOC_WSQ_N)`.
  size_t idx;
} boc_wsq_cursor_t;

/// @brief Pre-increment the cursor (returns the new index).
/// @details Mirrors `WrapIndex::operator++()` (`ds/wrapindex.h`):
/// `index = (index + 1) % N; return index;`. Used by
/// `enqueue` and the owner-side `dequeue` loop.
/// @param c The cursor (must be non-NULL).
/// @return The new index, in `[0, BOC_WSQ_N)`.
static inline size_t boc_wsq_pre_inc(boc_wsq_cursor_t *c) {
  c->idx = (c->idx + 1u) % (size_t)BOC_WSQ_N;
  return c->idx;
}

/// @brief Post-decrement the cursor (returns the old index).
/// @details Mirrors `WrapIndex::operator--(int)`
/// (`ds/wrapindex.h`): `auto r = index; index = (r==0?N-1:r-1);
/// return r;`. Reserved for a future `boc_wsq_enqueue_front`
/// wrapper that pushes onto the head of the most-recently-popped
/// sub-queue (verona's `WorkStealingQueue::enqueue_front`); no such
/// wrapper exists in bocpy yet, so the only caller in-tree is the
/// `_internal_test_wsq` shim that exercises the cursor arithmetic
/// directly.
/// @param c The cursor (must be non-NULL).
/// @return The old index, in `[0, BOC_WSQ_N)`.
static inline size_t boc_wsq_post_dec(boc_wsq_cursor_t *c) {
  size_t r = c->idx;
  c->idx = (r == 0u) ? ((size_t)BOC_WSQ_N - 1u) : (r - 1u);
  return r;
}

// ---------------------------------------------------------------------------
// Scheduler instrumentation
// ---------------------------------------------------------------------------

/// @brief Per-worker statistics counter block (POD).
///
/// All fields are plain @c uint64_t so a snapshot is a memcpy. Counters
/// are written by their owning worker thread with
/// @c memory_order_relaxed (see Verona `schedulerstats.h`); readers
/// (the Python @c scheduler_stats accessor) load with the same ordering
/// and accept torn reads — the snapshot is best-effort, not a barrier.
typedef struct boc_sched_stats {
  /// @brief Behaviours this worker pushed onto its own WSQ via the
  /// producer-local arm of @ref boc_sched_dispatch.
  /// @details Bumped only when an existing @c pending occupant is
  /// evicted to the queue to make room for the new dispatch.
  /// Dispatches that install into an empty @c pending slot bump
  /// @ref dispatched_to_pending instead.
  ///
  /// **Reconciliation.** This counter records this worker's *role
  /// as producer*. Across the whole pool the global identity
  /// @c "Σ (pushed_local + dispatched_to_pending + pushed_remote)
  /// == Σ (popped_local + popped_via_steal)" holds at quiescence.
  /// **Per-worker** the same identity does NOT hold: nodes
  /// redistributed onto a thief by @ref boc_wsq_enqueue_spread are
  /// not re-counted on the thief, so a thief's per-worker
  /// @c (pushed_local + dispatched_to_pending + pushed_remote -
  /// popped_local - popped_via_steal) is biased and is **not** a
  /// queue-depth estimate.
  uint64_t pushed_local;
  /// @brief Behaviours dispatched into an empty @c pending slot on
  /// the producer-local arm of @ref boc_sched_dispatch.
  /// @details The 1-deep producer-locality bypass: if @c pending is
  /// NULL when @c boc_sched_dispatch fires, the new node is parked
  /// in @c pending (no queue push) and this counter is bumped. Without
  /// this counter the queue's @c pushed_local underreports total
  /// dispatched work whenever the producer is steady-state
  /// (pop-then-dispatch keeps @c pending empty most cycles), which
  /// makes contention-on-queue gates look quiet even when the
  /// dispatch path is saturated.
  ///
  /// Like @ref pushed_local this counter records this worker's
  /// *producer* role; see the reconciliation note on @ref
  /// pushed_local for the per-worker vs. global identity.
  uint64_t dispatched_to_pending;
  /// @brief Behaviours this worker pushed onto another worker's queue
  /// via the round-robin dispatch path.
  uint64_t pushed_remote;
  /// @brief Behaviours this worker popped from its own queue.
  uint64_t popped_local;
  /// @brief Behaviours this worker stole from another worker's queue.
  uint64_t popped_via_steal;
  /// @brief CAS retries observed in the worker queue's enqueue path.
  uint64_t enqueue_cas_retries;
  /// @brief CAS retries observed in the worker queue's dequeue path.
  uint64_t dequeue_cas_retries;
  /// @brief Times the consumer-side @c BATCH_SIZE accounting forced
  /// a queue dequeue to bypass the @c pending fast path. Verona
  /// equivalent: the `batch == 0` branch in `get_work`
  /// (`schedulerthread.h:122-138`).
  uint64_t batch_resets;
  /// @brief Times this worker entered @c boc_sched_try_steal.
  /// @details Each call counts as one attempt regardless of whether
  /// it returned a node (success bumps @ref popped_via_steal too) or
  /// returned NULL (also bumps @ref steal_failures). Verona
  /// equivalent: `core->stats.steal()` is summed implicitly per
  /// entry in `schedulerthread.h::try_steal`.
  uint64_t steal_attempts;
  /// @brief Subset of @ref steal_attempts that returned NULL.
  /// @details Diagnostic counter; useful for tuning the
  /// quiescence-timeout in the slow-steal loop. Empty-self skips
  /// also bump this.
  uint64_t steal_failures;
  /// @brief Times this worker entered @c cnd_wait under @c cv_mu.
  /// @details Bumped immediately before the @c cnd_wait call in
  /// @ref boc_sched_worker_pop_slow's park arm. Each park entry
  /// counts once regardless of why the worker was woken (signal,
  /// shutdown, spurious wake). Diagnostic; complements the live
  /// @c parked bool on the worker (which is one when currently
  /// blocked, zero otherwise).
  uint64_t parked;
  /// @brief Monotonic timestamp (ns) of this worker's last
  /// @ref boc_sched_try_steal entry.
  /// @details Stamped via @ref boc_now_ns on every
  /// @ref boc_sched_try_steal call (success or failure). Zero if
  /// the worker has never attempted a steal. Used by tests to
  /// detect that the steal arm has actually fired and (with two
  /// snapshots) to bound the duration a worker spent in the slow
  /// steal loop.
  uint64_t last_steal_attempt_ns;
  /// @brief Times the steal-for-fairness arm in @ref
  /// boc_sched_worker_pop_slow actually fired (flag observed set
  /// AND local queue non-empty). Distinguishes "flag was set but
  /// the worker never paid attention" (arm dead) from "flag was
  /// set and the worker honoured it" (arm live). Diagnostic only.
  uint64_t fairness_arm_fires;
} boc_sched_stats_t;

/// @brief Per-worker statistics counter block (live atomic copy).
///
/// Same field set as @ref boc_sched_stats_t but every field is a
/// @c boc_atomic_u64_t so writers can use the @c boc_atomic_*
/// helpers without compiler warnings about plain @c uint64_t*.
/// @ref boc_sched_stats_snapshot loads each field with
/// @c memory_order_relaxed and copies it into a @ref boc_sched_stats_t
/// for the Python-side accessor; the snapshot is best-effort and may
/// observe individual counter values from different points in time.
/// Field order MUST match @ref boc_sched_stats_t one-for-one (the
/// snapshot routine relies on the structural correspondence rather
/// than a memcpy).
typedef struct boc_sched_stats_atomic {
  boc_atomic_u64_t pushed_local;
  boc_atomic_u64_t dispatched_to_pending;
  boc_atomic_u64_t pushed_remote;
  boc_atomic_u64_t popped_local;
  boc_atomic_u64_t popped_via_steal;
  boc_atomic_u64_t enqueue_cas_retries;
  boc_atomic_u64_t dequeue_cas_retries;
  boc_atomic_u64_t batch_resets;
  boc_atomic_u64_t steal_attempts;
  boc_atomic_u64_t steal_failures;
  boc_atomic_u64_t parked;
  boc_atomic_u64_t last_steal_attempt_ns;
  boc_atomic_u64_t fairness_arm_fires;
} boc_sched_stats_atomic_t;

// ---------------------------------------------------------------------------
// Per-worker scheduler state (`boc_sched_worker_t`)
// ---------------------------------------------------------------------------
//
// Holds the per-worker MPMC queue, the fairness-token slot
// (`token_work` / `should_steal_for_fairness`), the parking-protocol
// `cv_mu` / `cv` pair (`compat.h` `BOCMutex` / `BOCCond`, pthread on
// POSIX, SRWLock on MSVC), the ring-link `next_in_ring` pointer, the
// per-worker counter block, and a reserved terminator-delta slot.
// Atomics use the typed `compat.h` shim (`boc_atomic_*_t` +
// `boc_atomic_*_explicit`) so the layout compiles identically on POSIX
// and MSVC ARM64.
//
// Cacheline-aligned at the type level (`alignas(BOC_SCHED_CACHELINE)`)
// and a trailing pad rounds the size up to the next cacheline so that
// arrays of workers do not false-share between adjacent slots. The
// pad size is computed from a `_payload` helper struct so it tracks
// the platform-dependent sizes of `BOCMutex` / `BOCCond` automatically.

#ifndef BOC_SCHED_CACHELINE
#define BOC_SCHED_CACHELINE 64
#endif

/// @brief Forward declaration of @ref BOCBehavior (defined in
///        @c _core.c). The scheduler treats it as opaque; the
///        producer-locality TLS @c pending slot stores it as
///        `void *` to avoid any layout coupling.
struct BOCBehavior;

/// @brief Per-worker scheduler state (forward decl).
typedef struct boc_sched_worker boc_sched_worker_t;

/// @brief Helper struct used only to compute the trailing pad.
/// @details The fields here are duplicated verbatim into
/// @ref boc_sched_worker below; this helper is never instantiated and
/// exists solely so that `sizeof` reports the unpadded payload size
/// for the pad computation. Keeping the two field lists in sync is
/// enforced by a `static_assert` after the real struct definition.
struct boc_sched_worker_payload_ {
  boc_bq_t q[BOC_WSQ_N];
  boc_wsq_cursor_t enqueue_index;
  boc_wsq_cursor_t dequeue_index;
  boc_wsq_cursor_t steal_index;
  boc_atomic_ptr_t token_work;
  boc_atomic_bool_t should_steal_for_fairness;
  boc_atomic_bool_t stop_requested;
  boc_atomic_bool_t parked;
  Py_ssize_t owner_interp_id;
  BOCMutex cv_mu;
  BOCCond cv;
  struct boc_sched_worker *next_in_ring;
  boc_sched_stats_atomic_t stats;
  boc_atomic_u64_t reserved_terminator_delta;
};

/// @brief Trailing-pad byte count.
/// @details Rounds @ref boc_sched_worker_payload_ up to the next
/// multiple of @ref BOC_SCHED_CACHELINE. The outer `% CACHELINE`
/// converts an exact-fit (zero pad needed) into 0 instead of one
/// full cacheline.
#define BOC_SCHED_WORKER_PAD_                                                  \
  ((BOC_SCHED_CACHELINE -                                                      \
    (sizeof(struct boc_sched_worker_payload_) % BOC_SCHED_CACHELINE)) %        \
   BOC_SCHED_CACHELINE)

/// @brief Per-worker scheduler state.
/// @details All field semantics:
///   - @c q: this worker's WSQ — array of @ref BOC_WSQ_N independent
///     MPMC behaviour sub-queues. Pushes / pops / steals select a
///     sub-queue via the three cursors below; mirrors verona-rt's
///     `WorkStealingQueue<N>::queues[N]`.
///   - @c enqueue_index / @c dequeue_index / @c steal_index:
///     plain-`size_t` cursors (`boc_wsq_cursor_t`) ported from
///     verona-rt's `WrapIndex<N>`. See the header block above
///     @ref boc_wsq_cursor_t for the benign-race rationale.
///   - @c token_work: fairness token's queue node.
///   - @c should_steal_for_fairness: flag set when the fairness
///     token is popped; consumed by @ref boc_sched_worker_pop_slow.
///   - @c stop_requested: shutdown signal (`request_stop_all` writes
///     it under release; `pop_slow` reads under acquire). Honoured
///     by the parking loop only — never gated on the terminator.
///   - @c parked: parking-protocol witness (REL/ACQ paired with
///     @c cv_mu).
///   - @c owner_interp_id: sub-interpreter id of the worker that
///     called `boc_sched_worker_register` for this slot. Used for
///     wrong-thread asserts in `pop`.
///   - @c cv_mu / @c cv: parking-protocol mutex/condvar (compat.h
///     wrappers).
///   - @c next_in_ring: forms a circular singly-linked ring over
///     @ref boc_sched_worker_count workers; immutable after
///     @ref boc_sched_init.
///   - @c stats: per-worker counter block.
///   - @c reserved_terminator_delta: placeholder for a future
///     per-worker terminator delta.
struct boc_sched_worker {
  /// @brief First member carries an explicit alignment so the struct
  /// itself is cacheline-aligned (C11: `_Alignas` on a struct-type
  /// definition is a C++ extension; placing the alignment on the
  /// first member is the portable C equivalent and raises the
  /// containing struct's alignment requirement to match).
  ///
  /// @details `q` is an array of `BOC_WSQ_N` independent MPMC sub-
  /// queues; pushes / pops / steals route through different sub-
  /// queues selected by the three cursors below. Mirrors
  /// `WorkStealingQueue<N>::queues[N]` (verona-rt).
  alignas(BOC_SCHED_CACHELINE) boc_bq_t q[BOC_WSQ_N];
  /// @brief Producer cursor (`++` then push). Touched by every
  /// thread that dispatches onto this worker; the race is benign
  /// (see header block above @ref boc_wsq_cursor_t).
  boc_wsq_cursor_t enqueue_index;
  /// @brief Owner-pop cursor (`++` then pop, try all N before
  /// declaring empty). Owner-only.
  boc_wsq_cursor_t dequeue_index;
  /// @brief Thief cursor selecting which of a *victim*'s sub-
  /// queues to drain. Owner-only (this worker, when stealing).
  boc_wsq_cursor_t steal_index;
  boc_atomic_ptr_t token_work;
  boc_atomic_bool_t should_steal_for_fairness;
  boc_atomic_bool_t stop_requested;
  boc_atomic_bool_t parked;
  Py_ssize_t owner_interp_id;
  BOCMutex cv_mu;
  BOCCond cv;
  struct boc_sched_worker *next_in_ring;
  boc_sched_stats_atomic_t stats;
  boc_atomic_u64_t reserved_terminator_delta;
  /// @brief Trailing pad to the next cacheline boundary.
  /// @details Sized so `sizeof(boc_sched_worker_t) % CACHELINE == 0`;
  /// declared as @c [1] when no pad is needed (zero-length arrays are
  /// not portable C). The post-definition `static_assert` guarantees
  /// the array is never read past the live pad.
  char _pad[BOC_SCHED_WORKER_PAD_ > 0 ? BOC_SCHED_WORKER_PAD_ : 1];
};

static_assert(sizeof(boc_sched_worker_t) % BOC_SCHED_CACHELINE == 0,
              "boc_sched_worker_t must be cacheline-multiple in size");
static_assert(alignof(boc_sched_worker_t) >= BOC_SCHED_CACHELINE,
              "boc_sched_worker_t must be cacheline-aligned");

// ---------------------------------------------------------------------------
// Verona work-stealing queue helpers (`boc_wsq_*`)
// ---------------------------------------------------------------------------
//
// Inline routing wrappers around the per-worker WSQ. They mirror
// verona-rt's `WorkStealingQueue<N>` member functions one-for-one;
// the underlying `boc_bq_*` MPMCQ is unchanged. Each wrapper takes a
// `boc_sched_worker_t *` rather than a bare `boc_bq_t *` because the
// cursor lives on the worker.

/// @brief Push a single node onto a worker's WSQ.
/// @details Mirrors `WorkStealingQueue::enqueue` (verona-rt
/// `workstealingqueue.h`): pre-increments @c enqueue_index then
/// pushes onto `q[idx]`. Safe to call from any thread; the cursor
/// race is benign (see header block above @ref boc_wsq_cursor_t).
/// @param w The target worker (must be non-NULL).
/// @param n The node to enqueue (must be non-NULL).
static inline void boc_wsq_enqueue(boc_sched_worker_t *w, boc_bq_node_t *n) {
  size_t idx = boc_wsq_pre_inc(&w->enqueue_index);
  boc_bq_enqueue(&w->q[idx], n);
}

/// @brief Owner-side pop from a worker's WSQ.
/// @details Mirrors `WorkStealingQueue::dequeue` (verona-rt
/// `workstealingqueue.h`): for `i in [0, N)`, pre-increment
/// @c dequeue_index and try `boc_bq_dequeue(&q[idx])`; return the
/// first non-NULL. Owner-only — @c dequeue_index has no atomic.
/// @param w The owning worker (must be non-NULL).
/// @return A behaviour node, or NULL if all N sub-queues appear
///         empty (best-effort; same spurious-NULL caveat as
///         @ref boc_bq_dequeue).
static inline boc_bq_node_t *boc_wsq_dequeue(boc_sched_worker_t *w) {
  for (size_t i = 0; i < (size_t)BOC_WSQ_N; ++i) {
    size_t idx = boc_wsq_pre_inc(&w->dequeue_index);
    boc_bq_node_t *n = boc_bq_dequeue(&w->q[idx]);
    if (n != NULL) {
      return n;
    }
  }
  return NULL;
}

/// @brief Best-effort emptiness test across all N sub-queues.
/// @details Mirrors `WorkStealingQueue::is_empty` (verona-rt
/// `workstealingqueue.h`): scans every sub-queue; first non-empty
/// short-circuits to `false`. Result may be stale by the time the
/// caller acts on it — same caveat as @ref boc_bq_is_empty.
/// @param w The worker to inspect (must be non-NULL).
/// @return @c true if all N sub-queues currently appear empty.
static inline bool boc_wsq_is_empty(boc_sched_worker_t *w) {
  for (size_t i = 0; i < (size_t)BOC_WSQ_N; ++i) {
    if (!boc_bq_is_empty(&w->q[i])) {
      return false;
    }
  }
  return true;
}

/// @brief Spread a segment across @p self's WSQ sub-queues.
/// @details Mirrors `WorkStealingQueue::enqueue_spread` (verona-rt
/// `workstealingqueue.h`):
/// @code
///   while ((n = ls.take_one()) != nullptr) enqueue(n);
///   enqueue(ls);  // residual tail goes onto one sub-queue
/// @endcode
/// Each `take_one` peels one node off the head of the segment; the
/// node is pushed via @ref boc_wsq_enqueue, which pre-increments
/// @c enqueue_index so successive nodes round-robin across the N
/// sub-queues. The final residual (typically a single node, or in
/// the mid-link-race case a partial segment we cannot drain) is
/// enqueued as a segment onto one freshly-chosen sub-queue.
///
/// Caller invariant: @p ls is non-empty (the steal-loop exit
/// conditions guarantee this — fully empty and single-element
/// segments are handled before falling through to spread).
/// @param self The thief worker (must be non-NULL).
/// @param ls   The segment to redistribute.
static inline void boc_wsq_enqueue_spread(boc_sched_worker_t *self,
                                          boc_bq_segment_t ls) {
  for (;;) {
    boc_bq_node_t *n = boc_bq_segment_take_one(&ls);
    if (n == NULL) {
      break;
    }
    boc_wsq_enqueue(self, n);
  }
  // Tail residual: verona pushes the final segment unconditionally
  // onto a single sub-queue via `++enqueue_index`. With N=4 and
  // typical steal segments of dozens of nodes, the spreading has
  // already happened; the tail is at most a singleton (or a
  // mid-link partial we could not drain).
  size_t idx = boc_wsq_pre_inc(&self->enqueue_index);
  boc_bq_enqueue_segment(&self->q[idx], ls);
}

/// @brief Initialise the scheduler module for a fresh runtime cycle.
/// @details Allocates the per-worker array of length @p worker_count
/// (zero-initialised) and increments the per-start incarnation counter
/// (Verona `threadpool.h` precedent). Safe to call with
/// @p worker_count == 0, in which case the scheduler is in a quiescent
/// no-workers state. Called from @c behaviors.start() with the real
/// worker count on every start cycle, after @ref boc_sched_shutdown
/// has freed any prior array, and from @c _core_module_exec at module
/// init with @p worker_count == 0.
///
/// Must be called with the GIL held (sets Python exceptions on
/// failure and is sequenced from Python-visible module init /
/// `behaviors.start()`). The underlying allocation uses
/// @c PyMem_RawCalloc so the array is process-global and remains
/// valid across sub-interpreter boundaries.
/// @param worker_count Number of worker slots to allocate. Pass 0 for
///                     a quiescent no-workers state.
/// @return 0 on success, -1 on allocation failure (Python exception
///         set).
int boc_sched_init(Py_ssize_t worker_count);

/// @brief Tear down the scheduler module's per-worker array.
/// @details Frees the array allocated by @ref boc_sched_init and
/// resets the worker count to 0. Idempotent. Counters are not
/// archived anywhere — callers that want to keep them must snapshot
/// first via @ref boc_sched_stats_snapshot.
///
/// Must be called with the GIL held.
void boc_sched_shutdown(void);

/// @brief Number of worker slots currently allocated.
/// @return 0 if @ref boc_sched_init has not been called or the most
///         recent @ref boc_sched_shutdown has run; otherwise the
///         @c worker_count passed to the most recent @ref
///         boc_sched_init.
Py_ssize_t boc_sched_worker_count(void);

/// @brief Borrow a pointer to one of the worker slots.
/// @details Returns a non-owning pointer into the @c WORKERS array
/// for use with the @c boc_bq_* primitives (e.g. orphan-drain on
/// shutdown calls @c boc_bq_dequeue(&boc_sched_worker_at(i)->q)
/// to walk each per-task queue from outside @c sched.c). The
/// returned pointer is invalidated by @ref boc_sched_shutdown.
/// @param worker_index Zero-based worker slot.
/// @return Borrowed worker pointer, or NULL if @p worker_index is
///         out of range. No Python exception is set on NULL.
boc_sched_worker_t *boc_sched_worker_at(Py_ssize_t worker_index);

/// @brief Copy the snapshot of one worker's counters into @p out.
/// @details Reads use @c memory_order_relaxed; the snapshot is
/// best-effort and may observe individual counter values from
/// different points in time.
/// @param worker_index Zero-based worker slot.
/// @param out Destination POD; must be non-NULL.
/// @return 0 on success, -1 if @p worker_index is out of range or
///         @p out is NULL. No Python exception is set on -1.
int boc_sched_stats_snapshot(Py_ssize_t worker_index, boc_sched_stats_t *out);

/// @brief Read the current scheduler incarnation.
/// @details Increments by exactly one on every @ref boc_sched_init
/// call. TLS round-robin cursors compare against this value to detect
/// that the worker array has been reallocated since they last cached
/// a worker pointer.
/// @return The current incarnation. Plain @c size_t (Verona
///         `threadpool.h:40` precedent: not @c _Atomic).
size_t boc_sched_incarnation_get(void);

// ---------------------------------------------------------------------------
// Per-worker registration
// ---------------------------------------------------------------------------

/// @brief Atomically claim a worker slot for the calling thread.
/// @details Allocates the next free slot in @ref WORKERS using an
/// internal atomic counter that is reset on every @ref boc_sched_init.
/// Stamps the slot's @c owner_interp_id with a witness drawn from
/// the calling sub-interpreter and sets the per-thread @c
/// current_worker TLS handle so subsequent dispatch / pop operations
/// on this thread find their worker without a hashtable lookup.
///
/// **Self-allocation rather than caller-supplied index.** Verona's
/// `SchedulerThread` is constructed by the thread pool with a known
/// index. bocpy worker sub-interpreters share a single
/// `worker_script` that has no static knowledge of which slot it
/// will inhabit, and `_core.index()` is a process-monotonic counter
/// that does not reset across `start()`/`wait()`/`start()` cycles.
/// A self-allocating register() is the cleanest way to map worker
/// threads to slots 0..worker_count-1 in re-entry-safe fashion. The
/// contract is: over-registration returns -1.
///
/// Must be called with the GIL held (writes the TLS handle and
/// reads sub-interpreter state).
/// @return The assigned slot (0 .. worker_count-1) on success, or -1
///         if no free slot remains. No Python exception is set on -1.
Py_ssize_t boc_sched_worker_register(void);

// ---------------------------------------------------------------------------
// Park / unpark protocol
// ---------------------------------------------------------------------------
//
// Port of Verona's two-epoch `pause`/`unpause` protocol from
// `verona-rt/src/rt/sched/threadpool.h:282-379`.

/// @brief Pop the next behaviour for the calling worker, blocking
///        until work arrives or shutdown is requested.
/// @details Implements the parker side of the protocol. The
/// caller must have previously called @ref boc_sched_worker_register
/// (so @c current_worker TLS is set; @p self is passed explicitly so
/// the implementation does not have to re-resolve TLS on every loop
/// iteration). Reached on every worker loop iteration in
/// @c worker.py::do_work after the local-queue / pending fast paths
/// return NULL.
///
/// **Returns NULL only when @c self->stop_requested is observed
/// true.** Quiescence (the terminator reaching zero) does not exit
/// the parker; that is the runtime's responsibility, signalled
/// through @ref boc_sched_worker_request_stop_all.
///
/// Releases the GIL across the actual @c cnd_wait so other Python
/// work can proceed while the worker is parked.
///
/// **Returns the dequeued queue node, not the containing
/// @c BOCBehavior.** Callers in @c _core.c convert the node to its
/// owning behaviour via the standard `container_of` arithmetic
/// (see @c BEHAVIOR_FROM_BQ_NODE in @c _core.c). Keeping the
/// scheduler decoupled from the @c BOCBehavior layout avoids a
/// circular header dependency between @c sched.h and
/// @c _core.c's behaviour struct.
boc_bq_node_t *boc_sched_worker_pop_slow(boc_sched_worker_t *self);

/// @brief Set @c stop_requested on every worker and wake them all.
/// @details Issued by @c behaviors.stop_workers() after the runtime
/// is quiescent. Each worker exits @ref boc_sched_worker_pop_slow on
/// its next loop iteration (or immediately, if currently parked).
/// Idempotent.
void boc_sched_worker_request_stop_all(void);

/// @brief Wake every parked worker in the ring.
/// @details Walks the worker ring once starting from
/// @p self->next_in_ring and sends a @c cnd_signal to every worker
/// whose @c parked flag is true. Called from the producer side of
/// the parking protocol when a CAS on @c unpause_epoch wins;
/// mirrors Verona's @c ThreadSync::unpause_all
/// (`verona-rt/src/rt/sched/threadsync.h:108-128`,
/// `threadpool.h:367-373`) which wakes every waiter on the global
/// waiter list. The broadcast lets every parker re-run
/// @c boc_sched_any_work_visible() and either dequeue locally or
/// initiate a steal; parkers that find no work re-loop and re-park.
/// Early-outs when @c PARKED_COUNT is observed as zero so the common
/// no-parker case stays cheap. Safe to pass @p self == NULL (skips
/// the walk).
void boc_sched_unpause_all(boc_sched_worker_t *self);

/// @brief Lock-then-signal a specific worker.
/// @details Used by the producer fast arm to deliver a targeted wake
/// when the off-worker dispatch path lands a behaviour on @p target.
/// No-op if @p target is NULL or already non-parked.
void boc_sched_signal_one(boc_sched_worker_t *target);

/// @brief Read the calling thread's @c current_worker TLS slot.
/// @details Returns the worker handle installed by the most recent
/// @ref boc_sched_worker_register on this thread, or NULL if the
/// thread has never registered. Lets call sites in @c _core.c reach
/// into the TLS without each TU having to declare its own
/// @c thread_local mirror.
boc_sched_worker_t *boc_sched_current_worker(void);

// ---------------------------------------------------------------------------
// Dispatch + fast-path pop
// ---------------------------------------------------------------------------
//
// @ref boc_sched_dispatch is the producer-side entry point. Production
// callers in @c _core.c invoke it as
// @c boc_sched_dispatch(&behavior->bq_node); test code reaches it via
// @c _core.scheduler_dispatch_node / @c _core.scheduler_pop_fast.

/// @brief Schedule a behaviour for execution.
/// @details Producer-side dispatch with two arms (chosen by whether
/// the calling thread is registered as a worker):
///
/// **Producer-local arm** (`current_worker != NULL`). Verona
/// `schedule_fifo` semantics
/// (`schedulerthread.h:86-101`): always evict the prior @c pending
/// to the worker's local queue and install @p n as the new
/// @c pending. Result: the most-recent dispatch runs first when the
/// worker reaches @ref boc_sched_worker_pop_fast, which is the
/// cache-friendly behaviour Verona was tuned for. No targeted wake
/// is issued because the producer is itself the worker that will
/// run the work.
///
/// **Off-worker arm** (`current_worker == NULL`). The main thread
/// (or any non-worker thread) picks a target from the worker ring
/// using a TLS round-robin cursor that re-seeds whenever the
/// scheduler incarnation changes. The behaviour is enqueued
/// directly onto the target's @c q, then a targeted
/// @ref boc_sched_signal_one wake is issued.
///
/// **Slow arm (both producers).** After publish, the
/// pause/unpause-aware wake fires: load `(pe, ue)`; if `pe != ue` a
/// CAS forwards `unpause_epoch` to `pause_epoch`; the CAS winner
/// calls @ref boc_sched_unpause_all to wake every parked peer. This
/// closes the producer-on-other-worker liveness gap.
///
/// **No-runtime case.** If no workers are registered (off-worker
/// arm with @c WORKER_COUNT == 0), the function sets a
/// @c RuntimeError ("scheduler not running") and returns -1. The
/// caller must propagate the failure so the corresponding
/// @c terminator_inc / queue-side reservation is rolled back; the
/// reference behaviour is in @c whencall in @c behaviors.py
/// (try/except around @c BehaviorCapsule.schedule that drops the
/// terminator hold).
///
/// @param n The behaviour's @c bq_node (typically
///          @c &behavior->bq_node from @c _core.c).
/// @return 0 on success, -1 on failure (Python exception set).
int boc_sched_dispatch(boc_bq_node_t *n);

/// @brief Fast-path consumer pop.
/// @details Returns the calling worker's pending-or-queue-head
/// behaviour without parking. NULL means the local fast paths are
/// dry; the caller falls through to @ref boc_sched_worker_pop_slow
/// for the steal/park arm.
/// @param self The calling worker (typically
///             @ref boc_sched_current_worker()).
/// @return The dequeued node, or NULL if pending and the local
///         queue are both empty.
boc_bq_node_t *boc_sched_worker_pop_fast(boc_sched_worker_t *self);

// ---------------------------------------------------------------------------
// Build-time feature gate
// ---------------------------------------------------------------------------
//
// `BOC_HAVE_TRY_STEAL` toggles the parker's `check_for_work` walk
// between "inspect own queue only" (off) and "walk the full ring"
// (on). Defined unconditionally here; the off mode is reserved for
// debugging and is not part of any supported build.
#define BOC_HAVE_TRY_STEAL 1

/// @brief Test whether any worker's queue currently has visible work.
/// @details Walks the full worker array and calls
/// @ref boc_wsq_is_empty on each worker, which itself scans all
/// @c BOC_WSQ_N sub-queues of that worker. Returns @c true on the
/// first non-empty sub-queue found, @c false if every sub-queue of
/// every worker is empty. Cheap: bounded by
/// @c WORKER_COUNT * BOC_WSQ_N @c boc_bq_is_empty reads, each a
/// single acquire-load on the queue's @c front pointer. Mirrors
/// Verona's parker-side @c check_for_work walk
/// ([`threadpool.h::check_for_work`](../../verona-rt/src/rt/sched/threadpool.h)),
/// gated on @c BOC_HAVE_TRY_STEAL.
///
/// **Memory ordering.** Each @ref boc_bq_is_empty read is acquire-
/// ordered. The full walk is *not* a snapshot — a producer racing
/// with this call may publish onto a queue we have already passed.
/// That is fine: the parker has already bumped @c PAUSE_EPOCH
/// (seq_cst) before calling this, so the racing producer is forced
/// into the CAS arm and will signal a parker if needed (see
/// @ref boc_sched_unpause_all). Returning a stale @c false is the
/// only race outcome, and the epoch re-check under @c cv_mu
/// catches it before the worker actually sleeps.
/// @return @c true if at least one worker has visible queue work.
bool boc_sched_any_work_visible(void);

// ---------------------------------------------------------------------------
// Per-worker fairness token (`token_work`)
// ---------------------------------------------------------------------------
//
// Each worker owns a `BOCBehavior`-shaped sentinel whose `is_token`
// discriminator is set to 1. The token is allocated by
// `_core_scheduler_runtime_start` (because it knows the
// `BOCBehavior` layout) and installed into the worker's
// `token_work` slot via @ref boc_sched_set_token_node. On every
// successful pop, the dispatch site checks `is_token`; if set, the
// popping worker flips its `should_steal_for_fairness` flag and
// re-enqueues the token instead of running user code. Verona ports:
// `Core::token_work` (`core.h:22-37`), token-thunk dequeue
// (`schedulerthread.h::run_inner`).

/// @brief Install the per-worker fairness token's queue node.
/// @details Stores @p node into @c WORKERS[worker_index].token_work
/// using @c BOC_MO_RELEASE so a subsequent acquire-load on a worker
/// thread observes the install. Idempotent overwrite (callers are
/// expected to call this at most once per worker per runtime
/// cycle); @p node may be NULL to clear the slot during shutdown.
/// Must be called with the GIL held.
/// @param worker_index Zero-based worker slot.
/// @param node The token's @c bq_node pointer (typically
///             @c &token_behavior->bq_node), or NULL to clear.
/// @return 0 on success, -1 if @p worker_index is out of range. No
///         Python exception is set on -1.
int boc_sched_set_token_node(Py_ssize_t worker_index, boc_bq_node_t *node);

/// @brief Read the per-worker fairness token's queue node.
/// @details Acquire-load of the @c token_work slot. Returns NULL if
/// no token is installed (pre-install or after a
/// @c boc_sched_set_token_node(.., NULL)). Used by the runtime
/// teardown path to recover the token pointer before freeing the
/// per-worker array.
/// @param worker_index Zero-based worker slot.
/// @return The installed token node, or NULL.
boc_bq_node_t *boc_sched_get_token_node(Py_ssize_t worker_index);

/// @brief Set the calling worker's @c should_steal_for_fairness flag.
/// @details Release-store of @p value into
/// @c self->should_steal_for_fairness. This is the C-side body of the
/// Verona token closure
/// ([`core.h:28-33`](../../verona-rt/src/rt/sched/core.h#L28)): when
/// the dispatch site at @ref _core_scheduler_worker_pop pops a node
/// whose owning behaviour has @c is_token set, it calls this with
/// @p value = true so the next @ref boc_sched_worker_pop_slow
/// iteration takes the steal-for-fairness arm. Exposed as a sched
/// helper to keep the per-worker layout opaque to the dispatch TU.
/// @param self The calling worker (must be the result of
///             @ref boc_sched_current_worker on this thread).
/// @param value New flag value (typically @c true from the token
///              thunk; @c false at the steal arm before re-enqueueing
///              the token).
void boc_sched_set_steal_flag(boc_sched_worker_t *self, bool value);

#endif // BOCPY_SCHED_H
