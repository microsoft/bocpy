/// @file boc_registry.h
/// @brief Public API for the global marshalled-behavior code registry.
///
/// The registry is a content-addressed, append-only table mapping a
/// Python-computed canonical hex key to the raw bytes needed to
/// reconstruct a behavior on any
/// interpreter: the @c marshal blob of the behavior's code object, the
/// name of the module the behavior's globals bind to, and an optional
/// source-text string (stored only; not rendered).
///
/// **Lifecycle.** The registry is a single shared-C-heap singleton owned
/// by the process, not by any one (sub-)interpreter. @ref registry_init
/// runs once on the first @c _core module init (the 0->1 transition of
/// the global module counter) beside @c noticeboard_init, and
/// @ref registry_destroy runs once when the last interpreter tears the
/// module down, beside @c noticeboard_destroy. It must NOT be freed on
/// every per-module free: a worker sub-interpreter teardown would
/// otherwise dangle the main interpreter's view of the shared table.
///
/// **Thread model.** All entry points take the registry's own mutex.
/// Writes (@ref registry_register) may originate from ANY interpreter,
/// including worker sub-interpreters that schedule nested behaviors.
/// The table is append-only: an entry, once stored, is immortal for the
/// process lifetime and its blob/module/source buffers are never moved
/// or freed until @ref registry_destroy. Re-registering an existing key
/// is idempotent (the first stored blob is kept).
///
/// **Storage model.** The registry stores RAW C copies
/// (@c PyMem_RawMalloc) of the blob, module name, and optional source —
/// never live @c PyObject* — because sub-interpreters share the C heap
/// but not Python objects. @ref registry_lookup rebuilds fresh
/// @c PyBytes / @c str objects in the CALLING interpreter. Because no
/// Python refcounts are held, @ref registry_destroy simply
/// @c PyMem_RawFrees every buffer; there is nothing to balance.
///
/// **PyErr discipline.** @ref registry_register sets a Python exception
/// and returns -1 on allocation failure. @ref registry_lookup returns a
/// new reference to a 3-tuple on a hit, a new reference to @c Py_None on
/// a miss, and NULL with a Python exception set on failure.
///
/// **Security trust model.** Every blob in this registry is produced
/// in-process by @c bocpy's own @c when / @c whencall machinery via
/// @c marshal.dumps over a code object the running program already
/// holds. Blobs never cross a trust boundary: nothing in bocpy ingests
/// registry bytes from a file, socket, environment variable, or any
/// other external source, and @c marshal.loads is only ever applied to
/// these in-process blobs. The registry is NOT a general-purpose code
/// cache and MUST NOT be repurposed to deserialize untrusted bytes —
/// @c marshal.loads on hostile input is unsafe by construction.

#ifndef BOCPY_REGISTRY_H
#define BOCPY_REGISTRY_H

#define PY_SSIZE_T_CLEAN

#include <Python.h>

#include "boc_compat.h"

/// @brief Initialize the registry's table and mutex.
/// @details Called once in the first-init block of @c _core_module_exec,
/// beside @ref noticeboard_init. Cannot fail (the table starts empty and
/// grows lazily on the first register).
void registry_init(void);

/// @brief Free every stored entry buffer and tear down the mutex.
/// @details Called once at last-interpreter teardown (and in the
/// scheduler-init-failure rollback), beside @ref noticeboard_destroy.
/// @c PyMem_RawFrees each entry's blob, module name, and source copy and
/// the entry array itself. No Python refcounts are held, so there is
/// nothing to balance.
void registry_destroy(void);

/// @brief Idempotently store a behavior blob under a Python-computed key.
/// @details If @p key is already present, the call is a no-op that keeps
/// the first stored blob (two structurally-identical behaviors that
/// differ only in @c co_filename legitimately share a key — intended
/// dedup, not a collision). Otherwise a new entry is appended, copying
/// each input into a fresh @c PyMem_RawMalloc buffer.
/// @param key Canonical hex key (NUL-free), computed in Python.
/// @param key_len Length of @p key in bytes (no trailing NUL).
/// @param blob Marshalled code-object bytes (may contain NUL bytes).
/// @param blob_len Length of @p blob in bytes.
/// @param module_name Name of the module the behavior binds globals to.
/// @param module_len Length of @p module_name in bytes.
/// @param source Optional source text, or NULL when absent (store-only).
/// @param source_len Length of @p source in bytes (0 when @p source is
///        NULL).
/// @return 0 on success (including an idempotent hit), -1 on allocation
///         failure (Python exception set).
int registry_register(const char *key, Py_ssize_t key_len, const char *blob,
                      Py_ssize_t blob_len, const char *module_name,
                      Py_ssize_t module_len, const char *source,
                      Py_ssize_t source_len);

/// @brief Look up a behavior by key, rebuilding objects in the caller.
/// @details Rebuilds fresh @c PyBytes (blob) and @c str (module name, and
/// source or @c None) in the calling interpreter from the stored raw
/// buffers.
/// @param key Canonical hex key (NUL-free).
/// @param key_len Length of @p key in bytes.
/// @return New reference to a @c (blob: bytes, module_name: str,
///         source: str | None) tuple on a hit, new reference to
///         @c Py_None on a miss, or NULL with a Python exception set on
///         failure.
PyObject *registry_lookup(const char *key, Py_ssize_t key_len);

#endif // BOCPY_REGISTRY_H
