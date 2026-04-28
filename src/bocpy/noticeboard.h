/// @file noticeboard.h
/// @brief Public API for the global cross-behavior key-value noticeboard.
///
/// The noticeboard is a fixed-capacity table (max @ref NB_MAX_ENTRIES
/// entries, each keyed by a UTF-8 string of up to @ref NB_KEY_SIZE-1
/// bytes) holding cross-interpreter data plus a list of pinned
/// @ref BOCCown references that the entry's value depends on.
///
/// **Thread model.** All mutations (@ref noticeboard_write,
/// @ref noticeboard_delete, @ref noticeboard_clear) must be called
/// from the **noticeboard thread** registered via
/// @ref noticeboard_set_thread; the runtime guarantees this single-
/// writer invariant, which removes the TOCTOU window from
/// Python-level read-modify-write helpers (e.g. @c notice_update).
/// Snapshot reads (@ref noticeboard_snapshot) are unrestricted —
/// readers cache the result thread-locally and revalidate against
/// @ref noticeboard_version once per behavior boundary.
///
/// **PyErr discipline.** Functions that interact with the Python C
/// API (@ref noticeboard_snapshot, @ref nb_pin_cowns,
/// @ref noticeboard_write, @ref noticeboard_delete) set a Python
/// exception and return -1 / NULL on failure. Functions that are
/// pure C (@ref noticeboard_clear, @ref noticeboard_version,
/// @ref notice_sync_*) cannot fail.

#ifndef BOCPY_NOTICEBOARD_H
#define BOCPY_NOTICEBOARD_H

#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <stdbool.h>
#include <stdint.h>

#include "compat.h"
#include "cown.h"
#include "xidata.h"

/// @brief Maximum number of entries the noticeboard can hold.
#define NB_MAX_ENTRIES 64

/// @brief Maximum size of a key, including the trailing NUL byte.
#define NB_KEY_SIZE 64

/// @brief Initialize the noticeboard's mutex and notice_sync primitives.
/// @details Called once at module init.
void noticeboard_init(void);

/// @brief Drain remaining entries (XIData + pins) and tear down primitives.
/// @details Called once at module free. Drops the calling thread's
/// snapshot cache and frees every entry's @c XIDATA_T plus every
/// pinned cown ref.
void noticeboard_destroy(void);

/// @brief Register the calling thread as the sole noticeboard mutator.
/// @details Returns 0 on success, -1 if a different thread is already
/// registered (PyErr set). Idempotent for the same thread.
int noticeboard_set_thread(void);

/// @brief Forget the registered noticeboard mutator thread.
/// @details Used during Python @c Behaviors.stop after the noticeboard
/// thread has joined. Always succeeds.
void noticeboard_clear_thread(void);

/// @brief Reject a noticeboard mutation called from the wrong thread.
/// @details Returns 0 if the calling thread is the registered mutator
/// (or if no mutator has been registered yet — covers test/main-thread
/// startup). Returns -1 with a Python @c RuntimeError set otherwise.
/// @param op_name The operation name to embed in the error message.
int noticeboard_check_thread(const char *op_name);

/// @brief Drop the calling thread's cached snapshot dict and proxy.
void noticeboard_drop_local_cache(void);

/// @brief Mark the calling thread's cache as needing one version check.
/// @details Called by the worker loop at every behavior boundary so
/// the next @ref noticeboard_snapshot in this thread does exactly one
/// atomic load against @ref noticeboard_version before reusing the
/// cached proxy. Cheaper than dropping the cache outright.
void noticeboard_cache_clear_for_behavior(void);

/// @brief Read the noticeboard's monotonic version counter.
int_least64_t noticeboard_version(void);

/// @brief Walk a Python sequence of integer cown pointers, returning the
///        underlying @ref BOCCown array.
/// @details Each pointer in @p cowns is interpreted as a raw
/// @ref BOCCown pointer (via @c PyLong_AsVoidPtr). The caller is
/// expected to have pre-INCREFed each cown before passing the
/// sequence in (the noticeboard adopts those refs on success). On
/// failure, every transferred ref is rolled back and the output
/// pointer is left NULL.
/// @param cowns Sequence of integer pointer values, or @c Py_None.
/// @param[out] out_array Heap-allocated array (PyMem_RawMalloc) of
///        cown pointers. The caller is responsible for freeing it
///        with @c PyMem_RawFree.
/// @param[out] out_count Number of valid entries in @p out_array.
/// @return 0 on success, -1 on failure (PyErr set).
int nb_pin_cowns(PyObject *cowns, BOCCown ***out_array, int *out_count);

/// @brief Write or overwrite a noticeboard entry.
/// @details On success, the noticeboard takes ownership of @p xidata
/// and the @p pins array (and the strong refs the caller pre-INCREFed
/// onto each cown). On failure, @p xidata is freed via @c XIDATA_FREE
/// and every pin is COWN_DECREFed before @c PyMem_RawFree(@p pins).
/// @param key UTF-8 key (must be NUL-free, up to @ref NB_KEY_SIZE-1
///        bytes long).
/// @param key_len Length of @p key in bytes (does NOT include any
///        trailing NUL).
/// @param xidata Serialized value; ownership transferred on success.
/// @param pickled Whether @p xidata holds pickled bytes.
/// @param pins Heap-allocated cown pin array; ownership transferred
///        on success. May be NULL when @p pin_count is 0.
/// @param pin_count Number of entries in @p pins.
/// @return 0 on success, -1 on failure (PyErr set; @p xidata and
///         @p pins are freed).
int noticeboard_write(const char *key, Py_ssize_t key_len, XIDATA_T *xidata,
                      bool pickled, BOCCown **pins, int pin_count);

/// @brief Delete a single noticeboard entry by key.
/// @details The entry's @c XIDATA_T is freed and all pinned cowns are
/// COWN_DECREFed (after the noticeboard mutex is released). It is not
/// an error for the key to be absent.
/// @return 0 on success, -1 on failure (PyErr set; e.g. key validation).
int noticeboard_delete(const char *key, Py_ssize_t key_len);

/// @brief Drop every entry, freeing XIData and pins.
/// @details Bumps @ref noticeboard_version. Cannot fail.
void noticeboard_clear(void);

/// @brief Build (or reuse) the calling thread's read-only snapshot proxy.
/// @details See @ref noticeboard_snapshot_doc for cache semantics. The
/// returned proxy holds a strong reference to a dict that maps every
/// noticeboard key to the deserialized value. Pickled values are
/// unpickled outside the noticeboard mutex using @p loads as the
/// callable.
/// @param loads The @c pickle.loads callable (caller-owned reference).
/// @return New strong reference to the proxy, or NULL on failure
///         (PyErr set).
PyObject *noticeboard_snapshot(PyObject *loads);

/// @brief Reserve a fresh notice_sync sequence number.
int_least64_t notice_sync_request(void);

/// @brief Mark @p seq as processed and wake any @ref notice_sync_wait
///        callers.
void notice_sync_complete(int_least64_t seq);

/// @brief Block the calling thread until @p seq has been processed.
/// @param seq The sequence number returned by @ref notice_sync_request.
/// @param timeout Maximum wait in seconds. Ignored if @p wait_forever.
/// @param wait_forever If true, ignore @p timeout and wait until signalled.
/// @return true if @p seq has been processed, false on timeout.
bool notice_sync_wait(int_least64_t seq, double timeout, bool wait_forever);

#endif // BOCPY_NOTICEBOARD_H
