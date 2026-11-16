/// @file boc_terminator.h
/// @brief Root and scope rundown counter API shared between TUs.
///
/// The terminator is the C-level barrier that gates `wait()` /
/// `stop()`. Increment before the schedule call and decrement after
/// `behavior_release_all` completes. Each open scope also reserves the
/// root until its output has been finalized.
///
/// State is NOT per-interpreter: sub-interpreters share each
/// terminator's counter, mutex, and condvar.
///
/// Lifecycle:
///   - @ref terminator_reset claims startup with a fresh, closed root
///     after the previous root has been retired.
///   - @ref terminator_activate opens admission once runtime resources are ready.
///   - @ref terminator_inc returns -1 once @ref terminator_close has
///     been called, so the scheduling path can refuse new work
///     without racing teardown.
///   - @ref boc_terminator_waiter_wait blocks on the condvar until count reaches 0.
///     New work may arrive before the waiter resumes.
///   - @ref terminator_close fences admission only at zero reservations.
///   - @ref terminator_retire detaches the closed, drained root without
///     invalidating references held by existing waiters.

#ifndef BOCPY_TERMINATOR_H
#define BOCPY_TERMINATOR_H

#include <stdbool.h>
#include <stdint.h>

/// @brief Initialize the root and pinned-wait registry mutexes.
/// @details Called once from `_core_module_exec` on first interpreter
/// load. The registry locks intentionally outlive module unload (no
/// matching destroy); instance locks are freed with their terminator.
void terminator_init(void);

/// @brief Increment the counter, refusing if closed.
/// @note Prevent root retirement until the matching decrement; production
/// scheduling retains the instance with @ref terminator_reserve.
/// @return Post-increment count on success, or -1 if the terminator is
///         absent or closed (runtime is shutting down).
int_least64_t terminator_inc(void);

/// @brief Decrement the counter. Wakes waiters on
///        0-transition.
/// @return The new count, or -1 if the root is absent or already zero.
int_least64_t terminator_dec(void);

/// @brief Set the closed bit. Future @ref terminator_inc calls return
///        -1.
/// @details Refuses closure unless the current counter is zero.
/// Cancels a pending startup claim without retiring the root.
/// @return True if fenced or absent; false without changing admission if busy.
bool terminator_close(void);

/// @brief Claim a fresh runtime start while keeping admission closed
/// @details Requires the GIL. Allocates a new root rather than resetting
/// an existing instance; the previous root must have been retired.
/// Call @ref terminator_activate only after runtime resources are ready.
/// @return True on success; false with RuntimeError if a root remains,
///         or MemoryError if allocation fails.
bool terminator_reset(void);

/// @brief Open admission for the pending startup claim
/// @details Requires the GIL. Serializes with root retirement and fencing.
/// @return True on success; false with RuntimeError unless startup is pending.
bool terminator_activate(void);

/// @brief Read the current counter.
/// @details Includes behavior and scope-lifetime reservations; absent root returns zero.
int_least64_t terminator_count(void);

/// @brief Broadcast on registered terminator condvars without changing the
///        counter.
/// @details Wakes registered pumpable waiters to re-evaluate pinned
/// queue depth; completion generations are unchanged. Used by
/// @c boc_main_pinned_enqueue to nudge a main-thread @c wait()/pump
/// cycle when pinned work arrives without changing the global active count.
void terminator_wake_all(void);

/// @brief Wake reasons returned by @ref boc_terminator_waiter_wait
/// @details The auto-pump loop in `wait()` distinguishes three exits
/// so the caller can pump, drain, or time out without re-querying
/// shared state.
typedef enum boc_terminator_wake_reason {
  BOC_TERMINATOR_TERMINATED = 0,
  BOC_TERMINATOR_PUMP_READY = 1,
  BOC_TERMINATOR_WAIT_TIMED_OUT = 2,
} boc_terminator_wake_reason_t;

/// @brief Native shared counter with reference-held lifetime
typedef struct boc_terminator BOCTerminator;

/// @brief Allocate a closed terminator with zero reservations and one reference
/// @param is_root Immutable role; root instances do not reserve an enclosing root
/// @return Owned instance, or NULL on allocation failure without a Python exception
BOCTerminator *boc_terminator_new(bool is_root);

/// @brief Retain an instance without reserving work; NULL is a no-op
void boc_terminator_incref(BOCTerminator *term);

/// @brief Release a reference, destroying instance locks on final release; NULL is a no-op
/// @note Callers must retain an instance throughout operations and outstanding work.
void boc_terminator_decref(BOCTerminator *term);

/// @brief Open admission, reserving the root for non-root scope lifetime
/// @details The scope count stays zero. After output finalization, balance the
/// returned root reservation and reference with boc_terminator_dec/decref.
/// @note Caller enforces single-use lifecycle and exclusive initialization.
/// @param term Newly allocated terminator
/// @param root_reservation Required output storage for non-root; unused for root
/// @return True if opened; false without changing admission if root reservation fails
bool boc_terminator_start(BOCTerminator *term, BOCTerminator **root_reservation);

/// @brief Reserve work while admission is open, including from zero
/// @return New count, or -1 for NULL or closed admission
int_least64_t boc_terminator_inc(BOCTerminator *term);

/// @brief Release work, advancing the zero generation and notifying on transition to zero
/// @note Releases no memory reference; balance the reservation on the same instance.
/// @return New count, or -1 for NULL or an already-zero count
int_least64_t boc_terminator_dec(BOCTerminator *term);

/// @brief Read reservations under the instance mutex; NULL returns zero
int_least64_t boc_terminator_count(BOCTerminator *term);

/// @brief Read the zero-transition generation under the instance mutex; NULL returns zero
uint64_t boc_terminator_zero_count(BOCTerminator *term);

/// @brief Fence admission atomically with observing zero reservations
/// @param term Non-NULL terminator
/// @return True if fenced; false without changing admission if busy
bool boc_terminator_fence(BOCTerminator *term);

/// @brief Persistent event observation or strict drain
/// @details The token owns a term reference. The terminator mutex protects completion
/// during simultaneous waits. Initialize before sharing; do not copy,
/// reinitialize, or clear an owning token while it is in use.
typedef struct boc_terminator_waiter {
  /// @brief Owned reference released by boc_terminator_waiter_clear
  BOCTerminator *term;
  /// @brief Captured zero-transition generation
  uint64_t generation;
  /// @brief Sticky completion observed at initialization or during a wait
  bool completed;
  /// @brief Select current-state draining rather than an event observation
  bool drain;
} BOCTerminatorWaiter;

/// @brief Initialize a waiter, retaining term; NULL is immediately complete
/// @details Event mode captures zero_count and completes immediately at zero.
/// Drain mode checks current zero and fences admission during the wait.
/// @param waiter Uninitialized storage without an outstanding reference
/// @param term Instance to retain, or NULL
/// @param drain True for strict drain; false for a zero-transition event
void boc_terminator_waiter_init(BOCTerminatorWaiter *waiter,
                              BOCTerminator *term, bool drain);

/// @brief Release the token's reference and set term to NULL, after all users finish
void boc_terminator_waiter_clear(BOCTerminatorWaiter *waiter);

/// @brief Wait for completion, pinned work, or timeout without holding the GIL
/// @details Event completion survives subsequent increments; a drain requires
/// current zero and atomically closes admission. Spurious wakes satisfy neither.
/// Pinned readiness and timeout preserve the token for subsequent wait calls.
///
/// A non-NULL pinned_depth_fn registers the wait before checking queue depth.
/// The callback runs under the instance mutex: it must be lock-free and must
/// not call this API. Notifiers take the registry lock before instance locks;
/// registration/removal must never take the registry lock under an instance lock.
/// @note Caller releases the GIL and handles self-wait checks, pumping, and signals.
/// Completion precedes pinned readiness, which precedes timeout; check an
/// overall deadline before another pump batch.
/// @param waiter Initialized token kept alive throughout the call
/// @param timeout Seconds for this call; non-positive polls unless forever is true
/// @param forever Ignore timeout and wait indefinitely
/// @param pinned_depth_fn Lock-free pinned depth reader, or NULL
/// @return One of @ref boc_terminator_wake_reason_t; completion is sticky
boc_terminator_wake_reason_t boc_terminator_waiter_wait(
    BOCTerminatorWaiter *waiter, double timeout, bool forever,
    uint64_t (*pinned_depth_fn)(void));

/// @brief Acquire a root reference synchronized with retirement, or NULL when stopped
/// @note Does not reserve work; the caller must decref the returned instance.
BOCTerminator *terminator_root_acquire(void);

/// @brief Reserve root work and return its owned reference, or NULL if absent/closed
/// @note Balance both the reservation and reference on the returned instance.
BOCTerminator *terminator_reserve(void);

/// @brief Detach a closed, drained root without invalidating retained references
/// @note Workers and interpreter-dependent cleanup must already have finished.
/// @return True if retired or absent; false without a Python exception if open or busy
bool terminator_retire(void);

#endif // BOCPY_TERMINATOR_H
