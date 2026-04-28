/// @file noticeboard.c
/// @brief Implementation of the global noticeboard subsystem.
///
/// See @ref noticeboard.h for the public API and the thread/PyErr
/// discipline. This TU owns:
///
///   - The fixed-capacity entry table @c NB plus its mutex.
///   - The monotonic version counter @c NB_VERSION.
///   - The per-thread snapshot cache (dict, proxy, version, checked
///     flag).
///   - The single-writer thread-identity check (@c NB_NOTICEBOARD_TID).
///   - The notice_sync barrier primitives (@c NB_SYNC_REQUESTED,
///     @c NB_SYNC_PROCESSED, @c NB_SYNC_MUTEX, @c NB_SYNC_COND).

#include "noticeboard.h"

#include <string.h>

// ---------------------------------------------------------------------------
// File-scope state.
// ---------------------------------------------------------------------------

/// @brief A single noticeboard entry.
typedef struct nb_entry {
  /// @brief The key for this entry (null-terminated UTF-8).
  char key[NB_KEY_SIZE];
  /// @brief The serialized cross-interpreter data.
  XIDATA_T *value;
  /// @brief Whether the value was pickled during serialization.
  bool pickled;
  /// @brief BOCCowns referenced by @ref value, pinned by this entry.
  BOCCown **pinned_cowns;
  /// @brief Number of entries in @ref pinned_cowns.
  int pinned_count;
} NoticeboardEntry;

/// @brief Global noticeboard for cross-behavior key-value storage.
typedef struct noticeboard {
  NoticeboardEntry entries[NB_MAX_ENTRIES];
  int count;
  BOCMutex mutex;
} Noticeboard;

static Noticeboard NB;

/// @brief Monotonic version counter for the noticeboard.
static atomic_int_least64_t NB_VERSION = 0;

/// @brief Thread-local snapshot cache for the current behavior.
static thread_local PyObject *NB_SNAPSHOT_CACHE = NULL;

/// @brief Version of the noticeboard at the time the cached snapshot
///        was built.
static thread_local int_least64_t NB_SNAPSHOT_VERSION = -1;

/// @brief Whether the cached snapshot has been version-checked this
///        behavior.
static thread_local bool NB_VERSION_CHECKED = false;

/// @brief Read-only proxy wrapping the cached snapshot dict.
static thread_local PyObject *NB_SNAPSHOT_PROXY = NULL;

/// @brief Thread identity of the noticeboard mutator thread, or 0 if
///        unset.
static atomic_intptr_t NB_NOTICEBOARD_TID = 0;

/// @brief Monotonic counter incremented by every notice_sync caller.
static atomic_int_least64_t NB_SYNC_REQUESTED = 0;

/// @brief Highest sequence number processed by the noticeboard thread.
static atomic_int_least64_t NB_SYNC_PROCESSED = 0;

/// @brief Mutex protecting NB_SYNC_COND.
static BOCMutex NB_SYNC_MUTEX;

/// @brief Condition variable signalled when NB_SYNC_PROCESSED advances.
static BOCCond NB_SYNC_COND;

// ---------------------------------------------------------------------------
// Module init / teardown.
// ---------------------------------------------------------------------------

void noticeboard_init(void) {
  memset(&NB, 0, sizeof(NB));
  boc_mtx_init(&NB.mutex);
  boc_mtx_init(&NB_SYNC_MUTEX);
  cnd_init(&NB_SYNC_COND);
}

void noticeboard_destroy(void) {
  // Drop the calling thread's snapshot cache before freeing entries.
  Py_CLEAR(NB_SNAPSHOT_PROXY);
  Py_CLEAR(NB_SNAPSHOT_CACHE);
  NB_SNAPSHOT_VERSION = -1;
  NB_VERSION_CHECKED = false;

  // Collect entries to free after releasing the mutex — XIDATA_FREE
  // and COWN_DECREF can run Python __del__ which may re-enter.
  XIDATA_T *to_free[NB_MAX_ENTRIES];
  int to_free_count = 0;
  BOCCown **to_unpin[NB_MAX_ENTRIES];
  int to_unpin_count[NB_MAX_ENTRIES];
  int to_unpin_entries = 0;

  mtx_lock(&NB.mutex);
  for (int i = 0; i < NB.count; i++) {
    if (NB.entries[i].value != NULL) {
      to_free[to_free_count++] = NB.entries[i].value;
      NB.entries[i].value = NULL;
    }
    if (NB.entries[i].pinned_cowns != NULL) {
      to_unpin[to_unpin_entries] = NB.entries[i].pinned_cowns;
      to_unpin_count[to_unpin_entries] = NB.entries[i].pinned_count;
      to_unpin_entries++;
      NB.entries[i].pinned_cowns = NULL;
      NB.entries[i].pinned_count = 0;
    }
  }
  NB.count = 0;
  memset(NB.entries, 0, sizeof(NB.entries));
  mtx_unlock(&NB.mutex);

  for (int i = 0; i < to_free_count; i++) {
    XIDATA_FREE(to_free[i]);
  }
  for (int i = 0; i < to_unpin_entries; i++) {
    for (int j = 0; j < to_unpin_count[i]; j++) {
      COWN_DECREF(to_unpin[i][j]);
    }
    PyMem_RawFree(to_unpin[i]);
  }

  mtx_destroy(&NB.mutex);
  // NB_SYNC_MUTEX / NB_SYNC_COND are SRWLOCK / CONDITION_VARIABLE on
  // Windows (no destroy needed) and pthread / mtx_t on POSIX (handled
  // by mtx_destroy / cnd_destroy in compat.h shims). The original
  // _core.c module-free path never destroyed these; preserve that
  // behaviour to keep the symbol-additions-only invariant.
}

// ---------------------------------------------------------------------------
// Single-writer thread-identity check.
// ---------------------------------------------------------------------------

int noticeboard_check_thread(const char *op_name) {
  uintptr_t owner = (uintptr_t)atomic_load_intptr(&NB_NOTICEBOARD_TID);
  if (owner == 0) {
    return 0;
  }
  uintptr_t self_id = (uintptr_t)PyThread_get_thread_ident();
  if (owner != self_id) {
    PyErr_Format(PyExc_RuntimeError,
                 "%s must be called from the noticeboard thread", op_name);
    return -1;
  }
  return 0;
}

int noticeboard_set_thread(void) {
  intptr_t expected = 0;
  intptr_t self_id = (intptr_t)(uintptr_t)PyThread_get_thread_ident();
  // One-shot per runtime: refuse if the slot is already owned.
  // noticeboard_clear_thread() resets NB_NOTICEBOARD_TID to 0 at
  // stop(), so a fresh start() cycle is fine. This closes the
  // hijack-the-mutator-slot hole identified by the security lens.
  if (!atomic_compare_exchange_strong_intptr(&NB_NOTICEBOARD_TID, &expected,
                                             self_id)) {
    PyErr_SetString(PyExc_RuntimeError,
                    "set_noticeboard_thread: noticeboard mutator thread "
                    "is already registered");
    return -1;
  }
  return 0;
}

void noticeboard_clear_thread(void) {
  (void)atomic_exchange_intptr(&NB_NOTICEBOARD_TID, (intptr_t)0);
}

// ---------------------------------------------------------------------------
// Snapshot cache primitives.
// ---------------------------------------------------------------------------

void noticeboard_drop_local_cache(void) {
  Py_CLEAR(NB_SNAPSHOT_PROXY);
  Py_CLEAR(NB_SNAPSHOT_CACHE);
  NB_SNAPSHOT_VERSION = -1;
  NB_VERSION_CHECKED = false;
}

void noticeboard_cache_clear_for_behavior(void) { NB_VERSION_CHECKED = false; }

int_least64_t noticeboard_version(void) { return atomic_load(&NB_VERSION); }

// ---------------------------------------------------------------------------
// Pin helper.
// ---------------------------------------------------------------------------

int nb_pin_cowns(PyObject *cowns, BOCCown ***out_array, int *out_count) {
  *out_array = NULL;
  *out_count = 0;

  if (cowns == NULL || cowns == Py_None) {
    return 0;
  }

  PyObject *seq =
      PySequence_Fast(cowns, "noticeboard pin list must be a sequence");
  if (seq == NULL) {
    return -1;
  }

  Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
  if (n == 0) {
    Py_DECREF(seq);
    return 0;
  }

  BOCCown **pins = (BOCCown **)PyMem_RawMalloc(sizeof(BOCCown *) * n);
  if (pins == NULL) {
    Py_DECREF(seq);
    PyErr_NoMemory();
    return -1;
  }

  int taken = 0;
  for (Py_ssize_t i = 0; i < n; i++) {
    PyObject *item = PySequence_Fast_GET_ITEM(seq, i);
    BOCCown *cown = (BOCCown *)PyLong_AsVoidPtr(item);
    if (cown == NULL) {
      // PyLong_AsVoidPtr returns NULL both on error and for integer 0.
      // Reject both paths explicitly: a NULL pin would be dereferenced
      // downstream (COWN_DECREF on NULL is UB), and an integer 0 is
      // indistinguishable from a crafted attacker pin pointing at the
      // zero page.
      if (!PyErr_Occurred()) {
        PyErr_SetString(PyExc_ValueError,
                        "noticeboard pin list must not contain NULL / "
                        "integer 0 entries");
      } else {
        PyErr_SetString(PyExc_TypeError,
                        "noticeboard pin list must contain only integer "
                        "BOCCown pointers (use _core.cown_pin_pointers())");
      }
      goto fail;
    }
    pins[taken++] = cown;
  }

  Py_DECREF(seq);
  *out_array = pins;
  *out_count = taken;
  return 0;

fail:
  // Release every transferred ref the writer pre-INCREFed for us.
  for (int i = 0; i < taken; i++) {
    COWN_DECREF(pins[i]);
  }
  for (Py_ssize_t i = (Py_ssize_t)taken + 1; i < n; i++) {
    PyObject *item = PySequence_Fast_GET_ITEM(seq, i);
    BOCCown *c = (BOCCown *)PyLong_AsVoidPtr(item);
    if (c != NULL) {
      COWN_DECREF(c);
    } else {
      PyErr_Clear();
    }
  }
  PyMem_RawFree(pins);
  Py_DECREF(seq);
  return -1;
}

// ---------------------------------------------------------------------------
// Mutations.
// ---------------------------------------------------------------------------

int noticeboard_write(const char *key, Py_ssize_t key_len, XIDATA_T *xidata,
                      bool pickled, BOCCown **pins, int pin_count) {
  if (key_len >= NB_KEY_SIZE) {
    PyErr_SetString(PyExc_ValueError,
                    "noticeboard key too long (max 63 UTF-8 bytes)");
    goto fail;
  }
  if (memchr(key, '\0', (size_t)key_len) != NULL) {
    PyErr_SetString(PyExc_ValueError,
                    "noticeboard key must not contain NUL characters");
    goto fail;
  }

  mtx_lock(&NB.mutex);

  NoticeboardEntry *target = NULL;
  for (int i = 0; i < NB.count; i++) {
    if (strncmp(NB.entries[i].key, key, NB_KEY_SIZE) == 0) {
      target = &NB.entries[i];
      break;
    }
  }

  if (target == NULL) {
    if (NB.count >= NB_MAX_ENTRIES) {
      mtx_unlock(&NB.mutex);
      PyErr_SetString(PyExc_RuntimeError, "Noticeboard is full (max 64)");
      goto fail;
    }
    target = &NB.entries[NB.count++];
    strncpy(target->key, key, NB_KEY_SIZE - 1);
    target->key[NB_KEY_SIZE - 1] = '\0';
    target->value = NULL;
    target->pinned_cowns = NULL;
    target->pinned_count = 0;
  }

  // Stash old value and old pins to free after releasing the mutex —
  // XIDATA_FREE / COWN_DECREF may invoke Python __del__ which could
  // re-enter the noticeboard.
  XIDATA_T *old_value = target->value;
  BOCCown **old_pins = target->pinned_cowns;
  int old_pin_count = target->pinned_count;

  target->value = xidata;
  target->pickled = pickled;
  target->pinned_cowns = pins;
  target->pinned_count = pin_count;

  atomic_fetch_add(&NB_VERSION, 1);

  mtx_unlock(&NB.mutex);

  if (old_value != NULL) {
    XIDATA_FREE(old_value);
  }
  if (old_pins != NULL) {
    for (int i = 0; i < old_pin_count; i++) {
      COWN_DECREF(old_pins[i]);
    }
    PyMem_RawFree(old_pins);
  }
  return 0;

fail:
  // Roll back: free the new XIData and decref the new pins.
  if (xidata != NULL) {
    XIDATA_FREE(xidata);
  }
  if (pins != NULL) {
    for (int i = 0; i < pin_count; i++) {
      COWN_DECREF(pins[i]);
    }
    PyMem_RawFree(pins);
  }
  return -1;
}

int noticeboard_delete(const char *key, Py_ssize_t key_len) {
  if (key_len >= NB_KEY_SIZE) {
    PyErr_SetString(PyExc_ValueError,
                    "noticeboard key too long (max 63 UTF-8 bytes)");
    return -1;
  }
  if (memchr(key, '\0', (size_t)key_len) != NULL) {
    PyErr_SetString(PyExc_ValueError,
                    "noticeboard key must not contain NUL characters");
    return -1;
  }

  XIDATA_T *deleted_value = NULL;
  BOCCown **deleted_pins = NULL;
  int deleted_pin_count = 0;

  mtx_lock(&NB.mutex);
  int found = -1;
  for (int i = 0; i < NB.count; i++) {
    if (strncmp(NB.entries[i].key, key, NB_KEY_SIZE) == 0) {
      found = i;
      break;
    }
  }

  if (found >= 0) {
    deleted_value = NB.entries[found].value;
    deleted_pins = NB.entries[found].pinned_cowns;
    deleted_pin_count = NB.entries[found].pinned_count;

    for (int i = found; i < NB.count - 1; i++) {
      NB.entries[i] = NB.entries[i + 1];
    }
    memset(&NB.entries[NB.count - 1], 0, sizeof(NoticeboardEntry));
    NB.count--;

    atomic_fetch_add(&NB_VERSION, 1);
  }
  mtx_unlock(&NB.mutex);

  if (deleted_value != NULL) {
    XIDATA_FREE(deleted_value);
  }
  if (deleted_pins != NULL) {
    for (int i = 0; i < deleted_pin_count; i++) {
      COWN_DECREF(deleted_pins[i]);
    }
    PyMem_RawFree(deleted_pins);
  }
  return 0;
}

void noticeboard_clear(void) {
  XIDATA_T *to_free[NB_MAX_ENTRIES];
  int to_free_count = 0;
  BOCCown **to_unpin[NB_MAX_ENTRIES];
  int to_unpin_count[NB_MAX_ENTRIES];
  int to_unpin_entries = 0;

  mtx_lock(&NB.mutex);
  for (int i = 0; i < NB.count; i++) {
    if (NB.entries[i].value != NULL) {
      to_free[to_free_count++] = NB.entries[i].value;
      NB.entries[i].value = NULL;
    }
    if (NB.entries[i].pinned_cowns != NULL) {
      to_unpin[to_unpin_entries] = NB.entries[i].pinned_cowns;
      to_unpin_count[to_unpin_entries] = NB.entries[i].pinned_count;
      to_unpin_entries++;
      NB.entries[i].pinned_cowns = NULL;
      NB.entries[i].pinned_count = 0;
    }
  }
  NB.count = 0;
  memset(NB.entries, 0, sizeof(NB.entries));
  atomic_fetch_add(&NB_VERSION, 1);
  mtx_unlock(&NB.mutex);

  for (int i = 0; i < to_free_count; i++) {
    XIDATA_FREE(to_free[i]);
  }
  for (int i = 0; i < to_unpin_entries; i++) {
    for (int j = 0; j < to_unpin_count[i]; j++) {
      COWN_DECREF(to_unpin[i][j]);
    }
    PyMem_RawFree(to_unpin[i]);
  }

  // Drop this thread's cache so a subsequent same-thread snapshot
  // does not reuse a stale proxy. Other threads will revalidate via
  // NB_VERSION.
  noticeboard_drop_local_cache();
}

// ---------------------------------------------------------------------------
// Snapshot.
// ---------------------------------------------------------------------------

PyObject *noticeboard_snapshot(PyObject *loads) {
  if (NB_SNAPSHOT_PROXY != NULL) {
    if (NB_VERSION_CHECKED) {
      // Within-behavior repeat call: same proxy, no atomic load.
      Py_INCREF(NB_SNAPSHOT_PROXY);
      return NB_SNAPSHOT_PROXY;
    }
    // First snapshot call this behavior: do exactly one version check.
    int_least64_t current = atomic_load(&NB_VERSION);
    if (current == NB_SNAPSHOT_VERSION) {
      NB_VERSION_CHECKED = true;
      Py_INCREF(NB_SNAPSHOT_PROXY);
      return NB_SNAPSHOT_PROXY;
    }
    noticeboard_drop_local_cache();
  }

  PyObject *dict = PyDict_New();
  if (dict == NULL) {
    return NULL;
  }

  // Deferred entries: pickled values whose bytes were extracted under
  // mutex but need unpickling outside the lock.
  PyObject *deferred_keys[NB_MAX_ENTRIES];
  PyObject *deferred_bytes[NB_MAX_ENTRIES];
  int deferred_count = 0;

  // Keepalive pins: while we hold the mutex we take an extra
  // COWN_INCREF on every pin reachable from a deferred (pickled)
  // entry. The bytes we are about to unpickle outside the mutex
  // contain raw BOCCown pointers whose validity depends on the
  // entry's pin list. Without this extra ref, a concurrent writer
  // could overwrite the entry the instant we drop the mutex, release
  // the old pins, and free the BOCCowns before we touch them — UAF
  // in _cown_capsule_from_pointer. Released after the deferred
  // unpickling completes. Each deferred entry contributes a heap-
  // allocated pin pointer array sized to its pin count.
  BOCCown **keepalive_pins[NB_MAX_ENTRIES];
  int keepalive_counts[NB_MAX_ENTRIES];
  for (int i = 0; i < NB_MAX_ENTRIES; i++) {
    keepalive_pins[i] = NULL;
    keepalive_counts[i] = 0;
  }

  mtx_lock(&NB.mutex);

  // Capture the noticeboard version while still holding the mutex so
  // that no concurrent writer can bump it between snapshot completion
  // and version capture.
  int_least64_t built_version = atomic_load(&NB_VERSION);

  for (int i = 0; i < NB.count; i++) {
    NoticeboardEntry *entry = &NB.entries[i];
    if (entry->value == NULL) {
      continue;
    }

    // XIDATA_NEWOBJECT is lightweight (no Python code execution).
    PyObject *raw = XIDATA_NEWOBJECT(entry->value);
    if (raw == NULL) {
      mtx_unlock(&NB.mutex);
      goto fail_deferred;
    }

    PyObject *key = PyUnicode_FromString(entry->key);
    if (key == NULL) {
      Py_DECREF(raw);
      mtx_unlock(&NB.mutex);
      goto fail_deferred;
    }

    if (!entry->pickled) {
      // Non-pickled: add directly to dict.
      if (PyDict_SetItem(dict, key, raw) < 0) {
        Py_DECREF(key);
        Py_DECREF(raw);
        mtx_unlock(&NB.mutex);
        goto fail_deferred;
      }
      Py_DECREF(key);
      Py_DECREF(raw);
    } else {
      // Pickled: defer unpickling to outside the mutex. Take a fresh
      // COWN_INCREF on every pin so the BOCCowns referenced by the
      // bytes survive past mtx_unlock — see keepalive_pins comment.
      if (entry->pinned_count > 0) {
        BOCCown **pins = (BOCCown **)PyMem_RawMalloc(sizeof(BOCCown *) *
                                                     entry->pinned_count);
        if (pins == NULL) {
          Py_DECREF(key);
          Py_DECREF(raw);
          mtx_unlock(&NB.mutex);
          PyErr_NoMemory();
          goto fail_deferred;
        }
        for (int j = 0; j < entry->pinned_count; j++) {
          pins[j] = entry->pinned_cowns[j];
          COWN_INCREF(pins[j]);
        }
        keepalive_pins[deferred_count] = pins;
        keepalive_counts[deferred_count] = entry->pinned_count;
      }
      deferred_keys[deferred_count] = key;
      deferred_bytes[deferred_count] = raw;
      deferred_count++;
    }
  }

  mtx_unlock(&NB.mutex);

  // Unpickle deferred entries outside the mutex.
  for (int i = 0; i < deferred_count; i++) {
    PyObject *value = PyObject_CallOneArg(loads, deferred_bytes[i]);
    Py_DECREF(deferred_bytes[i]);
    deferred_bytes[i] = NULL;

    if (value == NULL) {
      Py_DECREF(deferred_keys[i]);
      deferred_keys[i] = NULL;
      // Clean up remaining deferred entries.
      for (int j = i + 1; j < deferred_count; j++) {
        Py_DECREF(deferred_keys[j]);
        Py_DECREF(deferred_bytes[j]);
      }
      // Release every keepalive pin (including the one for this
      // entry).
      for (int j = 0; j < deferred_count; j++) {
        if (keepalive_pins[j] != NULL) {
          for (int k = 0; k < keepalive_counts[j]; k++) {
            COWN_DECREF(keepalive_pins[j][k]);
          }
          PyMem_RawFree(keepalive_pins[j]);
          keepalive_pins[j] = NULL;
        }
      }
      Py_DECREF(dict);
      return NULL;
    }

    if (PyDict_SetItem(dict, deferred_keys[i], value) < 0) {
      Py_DECREF(deferred_keys[i]);
      Py_DECREF(value);
      for (int j = i + 1; j < deferred_count; j++) {
        Py_DECREF(deferred_keys[j]);
        Py_DECREF(deferred_bytes[j]);
      }
      for (int j = 0; j < deferred_count; j++) {
        if (keepalive_pins[j] != NULL) {
          for (int k = 0; k < keepalive_counts[j]; k++) {
            COWN_DECREF(keepalive_pins[j][k]);
          }
          PyMem_RawFree(keepalive_pins[j]);
          keepalive_pins[j] = NULL;
        }
      }
      Py_DECREF(dict);
      return NULL;
    }

    Py_DECREF(deferred_keys[i]);
    Py_DECREF(value);

    // Successful unpickle: the snapshot dict (and its CownCapsules)
    // now hold their own refs on every BOCCown referenced by the
    // bytes. Drop our keepalive pin for this entry.
    if (keepalive_pins[i] != NULL) {
      for (int k = 0; k < keepalive_counts[i]; k++) {
        COWN_DECREF(keepalive_pins[i][k]);
      }
      PyMem_RawFree(keepalive_pins[i]);
      keepalive_pins[i] = NULL;
    }
  }

  PyObject *proxy = PyDictProxy_New(dict);
  if (proxy == NULL) {
    Py_DECREF(dict);
    return NULL;
  }

  // The proxy holds a strong reference to dict; we keep our own as
  // well so that the dict is reachable for direct mutation in the
  // rebuild path and the proxy survives at least as long as the dict.
  NB_SNAPSHOT_CACHE = dict;
  NB_SNAPSHOT_PROXY = proxy;
  NB_SNAPSHOT_VERSION = built_version;
  NB_VERSION_CHECKED = true;
  Py_INCREF(proxy);
  return proxy;

fail_deferred:
  for (int i = 0; i < deferred_count; i++) {
    Py_DECREF(deferred_keys[i]);
    Py_DECREF(deferred_bytes[i]);
    if (keepalive_pins[i] != NULL) {
      for (int k = 0; k < keepalive_counts[i]; k++) {
        COWN_DECREF(keepalive_pins[i][k]);
      }
      PyMem_RawFree(keepalive_pins[i]);
      keepalive_pins[i] = NULL;
    }
  }
  Py_DECREF(dict);
  return NULL;
}

// ---------------------------------------------------------------------------
// notice_sync barrier.
// ---------------------------------------------------------------------------

int_least64_t notice_sync_request(void) {
  return atomic_fetch_add(&NB_SYNC_REQUESTED, 1) + 1;
}

void notice_sync_complete(int_least64_t seq) {
  mtx_lock(&NB_SYNC_MUTEX);
  // Defense in depth: with a single noticeboard thread draining the
  // FIFO boc_noticeboard tag, `seq` arrives strictly monotonically
  // and a plain `atomic_store(seq)` would be correct. We keep the
  // max-of pattern so that if a future change introduces a second
  // mutator thread or any out-of-order delivery, NB_SYNC_PROCESSED
  // can never regress and unblock waiters early.
  int_least64_t cur = atomic_load(&NB_SYNC_PROCESSED);
  if (seq > cur) {
    atomic_store(&NB_SYNC_PROCESSED, seq);
  }
  cnd_broadcast(&NB_SYNC_COND);
  mtx_unlock(&NB_SYNC_MUTEX);
}

bool notice_sync_wait(int_least64_t seq, double timeout, bool wait_forever) {
  bool ok = true;
  double end_time = wait_forever ? 0.0 : boc_now_s() + timeout;

  mtx_lock(&NB_SYNC_MUTEX);
  while (atomic_load(&NB_SYNC_PROCESSED) < seq) {
    if (!wait_forever) {
      double now = boc_now_s();
      if (now >= end_time) {
        ok = false;
        break;
      }
      cnd_timedwait_s(&NB_SYNC_COND, &NB_SYNC_MUTEX, end_time - now);
    } else {
      cnd_wait(&NB_SYNC_COND, &NB_SYNC_MUTEX);
    }
  }
  mtx_unlock(&NB_SYNC_MUTEX);
  return ok;
}
