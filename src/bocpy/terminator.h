/// @file terminator.h
/// @brief Process-global rundown counter API shared between TUs.
///
/// The terminator is the C-level barrier that gates `Behaviors.wait()` /
/// `stop()`. Increment from caller threads in `whencall` (before the
/// schedule call) and decrement from worker threads after
/// `behavior_release_all` completes. A one-shot "Pyrona seed" of 1 keeps
/// the count positive between the runtime starting and `stop()` taking
/// it down via @ref terminator_seed_dec.
///
/// State is process-global (file-scope statics in `terminator.c`, NOT
/// per-interpreter) so every sub-interpreter sees the same counter,
/// mutex, and condvar.
///
/// Lifecycle:
///   - @ref terminator_reset arms a fresh runtime: count = 1 (the seed),
///     seeded = 1, closed = 0. Returns the prior `(count, seeded)` so
///     `Behaviors.start` can detect drift carried over from a previous
///     run that died without reconciliation.
///   - @ref terminator_inc returns -1 once @ref terminator_close has
///     been called, so the `whencall` fast path can refuse new work
///     without racing teardown.
///   - @ref terminator_seed_dec is the idempotent one-shot that drops
///     the seed; subsequent calls are no-ops.
///   - @ref terminator_wait blocks on the condvar until count reaches 0.
///   - @ref terminator_close raises the closed bit so any straggler
///     @ref terminator_inc returns -1.

#ifndef BOCPY_TERMINATOR_H
#define BOCPY_TERMINATOR_H

#include <stdbool.h>
#include <stdint.h>

/// @brief Initialize the terminator mutex and condvar.
/// @details Called once from `_core_module_exec` on first interpreter
/// load. The kernel objects intentionally outlive module unload (no
/// matching destroy), matching the original behaviour in `_core.c`.
void terminator_init(void);

/// @brief Increment the counter, refusing if closed.
/// @return Post-increment count on success, or -1 if the terminator is
///         closed (runtime is shutting down).
int_least64_t terminator_inc(void);

/// @brief Decrement the counter. Wakes @ref terminator_wait on
///        0-transition.
/// @return The new count.
int_least64_t terminator_dec(void);

/// @brief Set the closed bit. Future @ref terminator_inc calls return
///        -1.
void terminator_close(void);

/// @brief Block until the counter reaches 0.
/// @details Caller MUST release the GIL before invoking. A negative
/// @p timeout or @p wait_forever means wait forever.
/// @param timeout Maximum wait in seconds. Ignored if @p wait_forever.
/// @param wait_forever If true, ignore @p timeout and wait until
///                    signalled.
/// @return true on success, false on timeout.
bool terminator_wait(double timeout, bool wait_forever);

/// @brief Idempotent one-shot decrement of the Pyrona seed.
/// @return true if this call removed the seed, false if it was already
///         removed.
bool terminator_seed_dec(void);

/// @brief Restore terminator state for a fresh runtime start.
/// @details Sets count=1 (seed), clears the closed bit, and re-arms the
/// seed one-shot. Returns the prior `(count, seeded)` via the out
/// parameters so callers can detect drift from a previous run that
/// died without reaching its reconciliation point.
/// @param prior_count Out param for the prior count.
/// @param prior_seeded Out param for the prior seeded flag.
void terminator_reset(int_least64_t *prior_count, int_least64_t *prior_seeded);

/// @brief Read the current seeded flag.
int_least64_t terminator_seeded(void);

/// @brief Read the current counter.
int_least64_t terminator_count(void);

#endif // BOCPY_TERMINATOR_H
