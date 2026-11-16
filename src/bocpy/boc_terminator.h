/// @file boc_terminator.h
/// @brief Process-global rundown counter API shared between TUs.
///
/// The terminator is the C-level barrier that gates `wait()` /
/// `stop()`. Increment before the schedule call and decrement after
/// `behavior_release_all` completes.
///
/// State is NOT per-interpreter: sub-interpreters share one
/// counter, mutex, and condvar.
///
/// Lifecycle:
///   - @ref terminator_reset claims startup at zero reservations, keeping
///     admission closed after the previous runtime has closed and drained.
///   - @ref terminator_activate opens admission once runtime resources are ready.
///   - @ref terminator_inc returns -1 once @ref terminator_close has
///     been called, so the scheduling path can refuse new work
///     without racing teardown.
///   - @ref terminator_waiter_wait blocks on the condvar until count reaches 0.
///     New work may arrive before the waiter resumes.
///   - @ref terminator_close fences admission only at zero reservations.

#ifndef BOCPY_TERMINATOR_H
#define BOCPY_TERMINATOR_H

#include <stdbool.h>
#include <stdint.h>

/// @brief Initialize the terminator mutex and condvar.
/// @details Called once from `_core_module_exec` on first interpreter
/// load. The locks intentionally outlive module unload (no matching destroy).
void terminator_init(void);

/// @brief Increment the counter, refusing if closed.
/// @note An open counter can be incremented from zero.
/// @return Post-increment count on success, or -1 if the terminator is
///         closed (runtime is shutting down).
int_least64_t terminator_inc(void);

/// @brief Decrement the counter. Wakes waiters on
///        0-transition.
/// @return The new count, or -1 if already zero.
int_least64_t terminator_dec(void);

/// @brief Set the closed bit. Future @ref terminator_inc calls return
///        -1.
/// @details Refuses closure unless the current counter is zero.
/// Cancels a pending startup claim.
/// @return True if fenced; false without changing admission if busy.
bool terminator_close(void);

/// @brief Claim a fresh runtime start while keeping admission closed
/// @details Requires the GIL. Claims startup only when closed and drained.
/// Call @ref terminator_activate only after runtime resources are ready.
/// Zero-transition history is preserved across starts.
/// @return True on success; false with RuntimeError without changing a starting,
///         open or busy runtime.
bool terminator_reset(void);

/// @brief Open admission for the pending startup claim
/// @details Requires the GIL.
/// @return True on success; false with RuntimeError unless startup is pending.
bool terminator_activate(void);

/// @brief Read the current counter.
/// @return The current count.
int_least64_t terminator_count(void);

/// @brief Broadcast on the terminator condvar without changing the
///        counter.
/// @details Wakes pumpable waiters to re-evaluate pinned
/// queue depth; completion generations are unchanged. Used by
/// @c boc_main_pinned_enqueue to nudge a main-thread @c wait()/pump
/// cycle when pinned work arrives without changing the global active count.
void terminator_wake_all(void);

/// @brief Wake reasons returned by @ref terminator_waiter_wait
/// @details The auto-pump loop in `wait()` distinguishes three exits
/// so the caller can pump, drain, or time out without re-querying
/// shared state.
typedef enum boc_terminator_wake_reason {
  BOC_TERMINATOR_TERMINATED = 0,
  BOC_TERMINATOR_PUMP_READY = 1,
  BOC_TERMINATOR_WAIT_TIMED_OUT = 2,
} boc_terminator_wake_reason_t;

/// @brief Read the zero-transition count under the terminator mutex
uint64_t terminator_zero_count(void);

/// @brief Stack-only event observation or strict drain
/// @details Holds no resource or reservation. Initialize before use and retain
/// across pump iterations; do not reinitialize while a wait is in progress.
typedef struct boc_terminator_waiter {
  /// @brief Captured zero-transition generation
  uint64_t generation;
  /// @brief Sticky completion observed at initialization or during a wait
  bool completed;
  /// @brief Select current-state draining rather than an event observation
  bool drain;
} BOCTerminatorWaiter;

/// @brief Capture the current zero-transition count without reserving work
/// @details Event mode captures zero_count and completes immediately at zero.
/// Drain mode checks current zero and fences admission during the wait.
/// @param waiter Uninitialized stack storage
/// @param drain True for strict drain; false for a zero-transition event
void terminator_waiter_init(BOCTerminatorWaiter *waiter, bool drain);

/// @brief Wait for completion, pinned work, or timeout without holding the GIL
/// @details Event completion survives subsequent increments; a drain requires
/// current zero and atomically closes admission. Spurious wakes satisfy neither.
/// Pinned readiness and timeout preserve the observation for subsequent wait calls.
///
/// The callback runs under the terminator mutex: it must be lock-free and must
/// not call this API. Notifiers take the same mutex to prevent lost wakeups.
/// @note Caller releases the GIL and handles self-wait checks, pumping, and signals.
/// Completion precedes pinned readiness, which precedes timeout; check an
/// overall deadline before another pump batch.
/// @param waiter Initialized observation kept alive throughout the call
/// @param timeout Seconds for this call; non-positive polls unless wait_forever is true
/// @param wait_forever Ignore timeout and wait indefinitely
/// @param pinned_depth_fn Lock-free pinned depth reader, or NULL
/// @return One of @ref boc_terminator_wake_reason_t; completion is sticky
boc_terminator_wake_reason_t terminator_waiter_wait(
    BOCTerminatorWaiter *waiter, double timeout, bool wait_forever,
    uint64_t (*pinned_depth_fn)(void));

#endif // BOCPY_TERMINATOR_H
