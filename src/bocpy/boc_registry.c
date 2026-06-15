/// @file boc_registry.c
/// @brief Implementation of the global marshalled-behavior code registry.
///
/// See @ref boc_registry.h for the public API, the lifecycle / thread /
/// storage / PyErr discipline, and the security trust model. This TU
/// owns the append-only entry table @c REG and its mutex.

#include "boc_registry.h"

#include <string.h>

/// @brief A single registry entry: raw C copies of the reconstruction
///        inputs for one behavior. All buffers are owned by the entry
///        and freed only in @ref registry_destroy.
typedef struct registry_entry {
  /// @brief Canonical hex key, NUL-terminated copy (for comparison).
  char *key;
  /// @brief Length of @ref key in bytes (no trailing NUL).
  Py_ssize_t key_len;
  /// @brief Marshalled code-object bytes (may contain NUL bytes).
  char *blob;
  /// @brief Length of @ref blob in bytes.
  Py_ssize_t blob_len;
  /// @brief Module name the behavior binds globals to, NUL-terminated.
  char *module_name;
  /// @brief Length of @ref module_name in bytes.
  Py_ssize_t module_len;
  /// @brief Optional source text (store-only), or NULL when absent.
  char *source;
  /// @brief Length of @ref source in bytes (0 when @ref source is NULL).
  Py_ssize_t source_len;
} RegistryEntry;

/// @brief Global append-only content-addressed behavior registry.
typedef struct registry {
  RegistryEntry *entries;
  int count;
  int capacity;
  BOCMutex mutex;
} Registry;

static Registry REG;

void registry_init(void) {
  memset(&REG, 0, sizeof(REG));
  boc_mtx_init(&REG.mutex);
}

void registry_destroy(void) {
  mtx_lock(&REG.mutex);
  for (int i = 0; i < REG.count; i++) {
    PyMem_RawFree(REG.entries[i].key);
    PyMem_RawFree(REG.entries[i].blob);
    PyMem_RawFree(REG.entries[i].module_name);
    PyMem_RawFree(REG.entries[i].source);
  }
  PyMem_RawFree(REG.entries);
  REG.entries = NULL;
  REG.count = 0;
  REG.capacity = 0;
  mtx_unlock(&REG.mutex);

  mtx_destroy(&REG.mutex);
}

/// @brief Find the index of @p key in the table, or -1 if absent.
/// @details Caller must hold @c REG.mutex. Linear scan; the per-
/// interpreter Resolver attribute cache means a key is looked up at
/// most once per interpreter, so O(n) lookup is not on any hot path.
///
/// Measured (2026-06, CPython 3.14, i7-14700F): N here is the number of
/// *distinct behavior code bodies* in a program -- bounded by source, not
/// by schedule count, because the Python-side @c _CODE_KEY_MEMO (register)
/// and per-interpreter Resolver cache (lookup) each collapse repeated use
/// to a single scan. The scan costs ~1.85 ns/entry up to N~=10k, then goes
/// superlinear as the entry table spills L2/L3 cache. At a realistic
/// N<=1000 a full scan is ~2 us one-time and aggregate registration ~1 ms
/// -- negligible against the ~3.8 us steady-state per-schedule cost. The
/// table only becomes a drag past ~10-20k unique bodies (cache-spill knee)
/// and a real problem past ~50k (O(N^2) startup). Reaching that needs
/// metaprogramming that exec()s a fresh body per item, not ordinary code,
/// so a hash-map replacement is intentionally NOT warranted. Do not
/// "optimize" this without first re-measuring against a plausible N.
static int registry_find_locked(const char *key, Py_ssize_t key_len) {
  for (int i = 0; i < REG.count; i++) {
    if (REG.entries[i].key_len == key_len &&
        memcmp(REG.entries[i].key, key, (size_t)key_len) == 0) {
      return i;
    }
  }
  return -1;
}

/// @brief Duplicate @p src into a fresh NUL-terminated raw buffer.
/// @details Allocates @p len + 1 bytes so the copy is usable both as a
/// sized buffer and as a C string. Returns NULL on allocation failure
/// (no Python exception set; the caller raises).
static char *registry_dup(const char *src, Py_ssize_t len) {
  char *out = (char *)PyMem_RawMalloc((size_t)len + 1);
  if (out == NULL) {
    return NULL;
  }
  if (len > 0) {
    memcpy(out, src, (size_t)len);
  }
  out[len] = '\0';
  return out;
}

int registry_register(const char *key, Py_ssize_t key_len, const char *blob,
                      Py_ssize_t blob_len, const char *module_name,
                      Py_ssize_t module_len, const char *source,
                      Py_ssize_t source_len) {
  mtx_lock(&REG.mutex);

  if (registry_find_locked(key, key_len) >= 0) {
    // Idempotent: keep the first stored blob.
    mtx_unlock(&REG.mutex);
    return 0;
  }

  if (REG.count == REG.capacity) {
    int new_capacity = REG.capacity == 0 ? 16 : REG.capacity * 2;
    RegistryEntry *grown = (RegistryEntry *)PyMem_RawRealloc(
        REG.entries, (size_t)new_capacity * sizeof(RegistryEntry));
    if (grown == NULL) {
      mtx_unlock(&REG.mutex);
      PyErr_NoMemory();
      return -1;
    }
    REG.entries = grown;
    REG.capacity = new_capacity;
  }

  char *key_copy = registry_dup(key, key_len);
  char *blob_copy = registry_dup(blob, blob_len);
  char *module_copy = registry_dup(module_name, module_len);
  char *source_copy = source != NULL ? registry_dup(source, source_len) : NULL;

  if (key_copy == NULL || blob_copy == NULL || module_copy == NULL ||
      (source != NULL && source_copy == NULL)) {
    PyMem_RawFree(key_copy);
    PyMem_RawFree(blob_copy);
    PyMem_RawFree(module_copy);
    PyMem_RawFree(source_copy);
    mtx_unlock(&REG.mutex);
    PyErr_NoMemory();
    return -1;
  }

  RegistryEntry *entry = &REG.entries[REG.count++];
  entry->key = key_copy;
  entry->key_len = key_len;
  entry->blob = blob_copy;
  entry->blob_len = blob_len;
  entry->module_name = module_copy;
  entry->module_len = module_len;
  entry->source = source_copy;
  entry->source_len = source != NULL ? source_len : 0;

  mtx_unlock(&REG.mutex);
  return 0;
}

PyObject *registry_lookup(const char *key, Py_ssize_t key_len) {
  // Copy the (immortal, never-moved) inner buffer pointers out under the
  // mutex, then build Python objects after unlocking. Entry buffers are
  // append-only and freed only in registry_destroy, so the pointers stay
  // valid across the unlock.
  const char *blob = NULL;
  Py_ssize_t blob_len = 0;
  const char *module_name = NULL;
  Py_ssize_t module_len = 0;
  const char *source = NULL;
  Py_ssize_t source_len = 0;

  mtx_lock(&REG.mutex);
  int idx = registry_find_locked(key, key_len);
  if (idx >= 0) {
    RegistryEntry *entry = &REG.entries[idx];
    blob = entry->blob;
    blob_len = entry->blob_len;
    module_name = entry->module_name;
    module_len = entry->module_len;
    source = entry->source;
    source_len = entry->source_len;
  }
  mtx_unlock(&REG.mutex);

  if (idx < 0) {
    Py_RETURN_NONE;
  }

  PyObject *py_blob = PyBytes_FromStringAndSize(blob, blob_len);
  if (py_blob == NULL) {
    return NULL;
  }
  PyObject *py_module = PyUnicode_FromStringAndSize(module_name, module_len);
  if (py_module == NULL) {
    Py_DECREF(py_blob);
    return NULL;
  }
  PyObject *py_source;
  if (source != NULL) {
    py_source = PyUnicode_FromStringAndSize(source, source_len);
    if (py_source == NULL) {
      Py_DECREF(py_blob);
      Py_DECREF(py_module);
      return NULL;
    }
  } else {
    py_source = Py_NewRef(Py_None);
  }

  PyObject *result = PyTuple_Pack(3, py_blob, py_module, py_source);
  Py_DECREF(py_blob);
  Py_DECREF(py_module);
  Py_DECREF(py_source);
  return result;
}
