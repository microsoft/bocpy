/// @file _internal_test_atomics.c
/// @brief Atomics-domain tests for the `bocpy._internal_test` extension.
///
/// Exposes the typed `boc_atomic_*_explicit` API from `compat.h` to
/// Python so `test/test_compat_atomics.py` can drive the inline
/// atomic primitives from real Python threads (which give us true
/// parallelism either via free-threaded CPython or via
/// `Py_BEGIN_ALLOW_THREADS` on regular CPython). On x86/x64 the test
/// is a smoke test of the dispatch; on ARM64 it is a weak-memory
/// correctness test for the acquire/release pair.
///
/// Methods are exported under the `atomics_*` prefix on the
/// `bocpy._internal_test` module via @ref boc_internal_test_register_atomics.

#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <stdbool.h>
#include <stdint.h>

#include "compat.h"

// Single shared block of atomic slots, accessed by every test entry
// point through a PyCapsule handle. Cacheline-sized (64B) to avoid
// false-sharing between the producer and consumer fields when the
// test spawns multiple threads.
typedef struct {
  boc_atomic_u64_t flag;       // 0 → producer not yet ready
  uint64_t payload;            // plain (non-atomic); guarded by flag
  boc_atomic_u64_t counter64;  // fetch_add / CAS contention slot
  boc_atomic_u32_t counter32;  // 32-bit fetch_add contention slot
  boc_atomic_bool_t bool_slot; // bool exchange / cas test
  boc_atomic_ptr_t ptr_slot;   // ptr exchange / cas test
  char _padding[64];
} hs_state_t;

static void hs_destroy(PyObject *cap) {
  void *p = PyCapsule_GetPointer(cap, "boc_hs_state");
  PyMem_RawFree(p);
}

static hs_state_t *hs_get(PyObject *cap) {
  return (hs_state_t *)PyCapsule_GetPointer(cap, "boc_hs_state");
}

// ---------------------------------------------------------------------------
// State setup / inspection.
// ---------------------------------------------------------------------------

static PyObject *py_make_state(PyObject *Py_UNUSED(self),
                               PyObject *Py_UNUSED(args)) {
  hs_state_t *h = (hs_state_t *)PyMem_RawCalloc(1, sizeof(*h));
  if (h == NULL) {
    return PyErr_NoMemory();
  }
  return PyCapsule_New(h, "boc_hs_state", hs_destroy);
}

static PyObject *py_reset(PyObject *Py_UNUSED(self), PyObject *cap) {
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  boc_atomic_store_u64_explicit(&h->flag, 0, BOC_MO_SEQ_CST);
  h->payload = 0;
  boc_atomic_store_u64_explicit(&h->counter64, 0, BOC_MO_SEQ_CST);
  boc_atomic_store_u32_explicit(&h->counter32, 0, BOC_MO_SEQ_CST);
  boc_atomic_store_bool_explicit(&h->bool_slot, false, BOC_MO_SEQ_CST);
  boc_atomic_store_ptr_explicit(&h->ptr_slot, NULL, BOC_MO_SEQ_CST);
  Py_RETURN_NONE;
}

static PyObject *py_load_counter64(PyObject *Py_UNUSED(self), PyObject *cap) {
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  return PyLong_FromUnsignedLongLong(
      boc_atomic_load_u64_explicit(&h->counter64, BOC_MO_SEQ_CST));
}

static PyObject *py_load_counter32(PyObject *Py_UNUSED(self), PyObject *cap) {
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  return PyLong_FromUnsignedLong((unsigned long)boc_atomic_load_u32_explicit(
      &h->counter32, BOC_MO_SEQ_CST));
}

static PyObject *py_load_bool(PyObject *Py_UNUSED(self), PyObject *cap) {
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  bool v = boc_atomic_load_bool_explicit(&h->bool_slot, BOC_MO_SEQ_CST);
  if (v) {
    Py_RETURN_TRUE;
  }
  Py_RETURN_FALSE;
}

static PyObject *py_load_ptr(PyObject *Py_UNUSED(self), PyObject *cap) {
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  void *v = boc_atomic_load_ptr_explicit(&h->ptr_slot, BOC_MO_SEQ_CST);
  return PyLong_FromVoidPtr(v);
}

// ---------------------------------------------------------------------------
// Acquire / release handshake (the canonical weak-memory test).
// ---------------------------------------------------------------------------

static PyObject *py_producer(PyObject *Py_UNUSED(self), PyObject *args) {
  PyObject *cap;
  unsigned long long payload;
  if (!PyArg_ParseTuple(args, "OK", &cap, &payload)) {
    return NULL;
  }
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  Py_BEGIN_ALLOW_THREADS
      // Plain non-atomic write of the payload, then a release store of
      // the flag. A consumer that observes flag==1 with an acquire load
      // MUST see the payload write (acq-rel synchronises-with).
      h->payload = (uint64_t)payload;
  boc_atomic_store_u64_explicit(&h->flag, 1, BOC_MO_RELEASE);
  Py_END_ALLOW_THREADS Py_RETURN_NONE;
}

static PyObject *py_consumer(PyObject *Py_UNUSED(self), PyObject *cap) {
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  uint64_t got;
  Py_BEGIN_ALLOW_THREADS while (
      boc_atomic_load_u64_explicit(&h->flag, BOC_MO_ACQUIRE) == 0) {
    // tight spin; the producer thread is the only writer
  }
  got = h->payload;
  Py_END_ALLOW_THREADS return PyLong_FromUnsignedLongLong(
      (unsigned long long)got);
}

// ---------------------------------------------------------------------------
// Multi-thread fetch_add contention (relaxed counter).
// ---------------------------------------------------------------------------

static PyObject *py_fetch_add_loop_u64(PyObject *Py_UNUSED(self),
                                       PyObject *args) {
  PyObject *cap;
  Py_ssize_t iters;
  if (!PyArg_ParseTuple(args, "On", &cap, &iters)) {
    return NULL;
  }
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  Py_BEGIN_ALLOW_THREADS for (Py_ssize_t i = 0; i < iters; ++i) {
    boc_atomic_fetch_add_u64_explicit(&h->counter64, 1, BOC_MO_RELAXED);
  }
  Py_END_ALLOW_THREADS Py_RETURN_NONE;
}

static PyObject *py_fetch_add_loop_u32(PyObject *Py_UNUSED(self),
                                       PyObject *args) {
  PyObject *cap;
  Py_ssize_t iters;
  if (!PyArg_ParseTuple(args, "On", &cap, &iters)) {
    return NULL;
  }
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  Py_BEGIN_ALLOW_THREADS for (Py_ssize_t i = 0; i < iters; ++i) {
    boc_atomic_fetch_add_u32_explicit(&h->counter32, 1, BOC_MO_RELAXED);
  }
  Py_END_ALLOW_THREADS Py_RETURN_NONE;
}

// ---------------------------------------------------------------------------
// Multi-thread CAS contention loop (acq_rel on success, relaxed on failure).
// ---------------------------------------------------------------------------

static PyObject *py_cas_increment_loop_u64(PyObject *Py_UNUSED(self),
                                           PyObject *args) {
  PyObject *cap;
  Py_ssize_t iters;
  if (!PyArg_ParseTuple(args, "On", &cap, &iters)) {
    return NULL;
  }
  hs_state_t *h = hs_get(cap);
  if (h == NULL) {
    return NULL;
  }
  Py_BEGIN_ALLOW_THREADS for (Py_ssize_t i = 0; i < iters; ++i) {
    uint64_t cur = boc_atomic_load_u64_explicit(&h->counter64, BOC_MO_RELAXED);
    while (!boc_atomic_compare_exchange_strong_u64_explicit(
        &h->counter64, &cur, cur + 1, BOC_MO_ACQ_REL, BOC_MO_RELAXED)) {
      // CAS updates `cur` on failure; loop body is empty.
    }
  }
  Py_END_ALLOW_THREADS Py_RETURN_NONE;
}

// ---------------------------------------------------------------------------
// Single-threaded round-trip: every (op, type, order) at least once.
// ---------------------------------------------------------------------------
//
// On Linux the typed API is a thin wrapper around <stdatomic.h>, so this
// is mostly a "does it compile and link" smoke. On MSVC it exercises the
// per-order Interlocked* dispatch; on ARM64 MSVC it exercises the
// __ldar*/__stlr* fast paths.

static int round_trip_u64(void) {
  boc_atomic_u64_t slot = 0;
  const boc_memory_order_t orders[] = {BOC_MO_RELAXED, BOC_MO_ACQUIRE,
                                       BOC_MO_RELEASE, BOC_MO_ACQ_REL,
                                       BOC_MO_SEQ_CST};
  for (size_t i = 0; i < sizeof(orders) / sizeof(orders[0]); ++i) {
    boc_memory_order_t o = orders[i];
    // store/load round-trip.
    boc_atomic_store_u64_explicit(&slot, 0x1234567890ABCDEFULL, o);
    if (boc_atomic_load_u64_explicit(&slot, o) != 0x1234567890ABCDEFULL) {
      return -1;
    }
    // exchange returns previous, installs new.
    uint64_t prev = boc_atomic_exchange_u64_explicit(&slot, 42ULL, o);
    if (prev != 0x1234567890ABCDEFULL ||
        boc_atomic_load_u64_explicit(&slot, o) != 42ULL) {
      return -1;
    }
    // fetch_add / fetch_sub.
    if (boc_atomic_fetch_add_u64_explicit(&slot, 8ULL, o) != 42ULL ||
        boc_atomic_load_u64_explicit(&slot, o) != 50ULL) {
      return -1;
    }
    if (boc_atomic_fetch_sub_u64_explicit(&slot, 5ULL, o) != 50ULL ||
        boc_atomic_load_u64_explicit(&slot, o) != 45ULL) {
      return -1;
    }
    // CAS success.
    uint64_t exp = 45ULL;
    if (!boc_atomic_compare_exchange_strong_u64_explicit(&slot, &exp, 99ULL, o,
                                                         BOC_MO_RELAXED) ||
        boc_atomic_load_u64_explicit(&slot, o) != 99ULL) {
      return -1;
    }
    // CAS failure must update `exp` to the current value.
    exp = 0ULL;
    if (boc_atomic_compare_exchange_strong_u64_explicit(&slot, &exp, 7ULL, o,
                                                        BOC_MO_RELAXED) ||
        exp != 99ULL) {
      return -1;
    }
  }
  return 0;
}

static int round_trip_u32(void) {
  boc_atomic_u32_t slot = 0;
  const boc_memory_order_t orders[] = {BOC_MO_RELAXED, BOC_MO_ACQUIRE,
                                       BOC_MO_RELEASE, BOC_MO_ACQ_REL,
                                       BOC_MO_SEQ_CST};
  for (size_t i = 0; i < sizeof(orders) / sizeof(orders[0]); ++i) {
    boc_memory_order_t o = orders[i];
    boc_atomic_store_u32_explicit(&slot, 0xCAFEBABEU, o);
    if (boc_atomic_load_u32_explicit(&slot, o) != 0xCAFEBABEU) {
      return -1;
    }
    uint32_t prev = boc_atomic_exchange_u32_explicit(&slot, 7U, o);
    if (prev != 0xCAFEBABEU || boc_atomic_load_u32_explicit(&slot, o) != 7U) {
      return -1;
    }
    if (boc_atomic_fetch_add_u32_explicit(&slot, 3U, o) != 7U ||
        boc_atomic_load_u32_explicit(&slot, o) != 10U) {
      return -1;
    }
    if (boc_atomic_fetch_sub_u32_explicit(&slot, 4U, o) != 10U ||
        boc_atomic_load_u32_explicit(&slot, o) != 6U) {
      return -1;
    }
    uint32_t exp = 6U;
    if (!boc_atomic_compare_exchange_strong_u32_explicit(&slot, &exp, 99U, o,
                                                         BOC_MO_RELAXED) ||
        boc_atomic_load_u32_explicit(&slot, o) != 99U) {
      return -1;
    }
    exp = 0U;
    if (boc_atomic_compare_exchange_strong_u32_explicit(&slot, &exp, 7U, o,
                                                        BOC_MO_RELAXED) ||
        exp != 99U) {
      return -1;
    }
  }
  return 0;
}

static int round_trip_bool(void) {
  boc_atomic_bool_t slot = false;
  const boc_memory_order_t orders[] = {BOC_MO_RELAXED, BOC_MO_ACQUIRE,
                                       BOC_MO_RELEASE, BOC_MO_ACQ_REL,
                                       BOC_MO_SEQ_CST};
  for (size_t i = 0; i < sizeof(orders) / sizeof(orders[0]); ++i) {
    boc_memory_order_t o = orders[i];
    boc_atomic_store_bool_explicit(&slot, true, o);
    if (!boc_atomic_load_bool_explicit(&slot, o)) {
      return -1;
    }
    bool prev = boc_atomic_exchange_bool_explicit(&slot, false, o);
    if (!prev || boc_atomic_load_bool_explicit(&slot, o)) {
      return -1;
    }
    bool exp = false;
    if (!boc_atomic_compare_exchange_strong_bool_explicit(&slot, &exp, true, o,
                                                          BOC_MO_RELAXED) ||
        !boc_atomic_load_bool_explicit(&slot, o)) {
      return -1;
    }
    exp = false;
    if (boc_atomic_compare_exchange_strong_bool_explicit(&slot, &exp, false, o,
                                                         BOC_MO_RELAXED) ||
        exp != true) {
      return -1;
    }
  }
  return 0;
}

static int round_trip_ptr(void) {
  boc_atomic_ptr_t slot = NULL;
  int sentinel_a, sentinel_b;
  void *a = (void *)&sentinel_a;
  void *b = (void *)&sentinel_b;
  const boc_memory_order_t orders[] = {BOC_MO_RELAXED, BOC_MO_ACQUIRE,
                                       BOC_MO_RELEASE, BOC_MO_ACQ_REL,
                                       BOC_MO_SEQ_CST};
  for (size_t i = 0; i < sizeof(orders) / sizeof(orders[0]); ++i) {
    boc_memory_order_t o = orders[i];
    boc_atomic_store_ptr_explicit(&slot, a, o);
    if (boc_atomic_load_ptr_explicit(&slot, o) != a) {
      return -1;
    }
    void *prev = boc_atomic_exchange_ptr_explicit(&slot, b, o);
    if (prev != a || boc_atomic_load_ptr_explicit(&slot, o) != b) {
      return -1;
    }
    void *exp = b;
    if (!boc_atomic_compare_exchange_strong_ptr_explicit(&slot, &exp, a, o,
                                                         BOC_MO_RELAXED) ||
        boc_atomic_load_ptr_explicit(&slot, o) != a) {
      return -1;
    }
    exp = NULL;
    if (boc_atomic_compare_exchange_strong_ptr_explicit(&slot, &exp, b, o,
                                                        BOC_MO_RELAXED) ||
        exp != a) {
      return -1;
    }
  }
  return 0;
}

static PyObject *py_round_trip(PyObject *Py_UNUSED(self),
                               PyObject *Py_UNUSED(args)) {
  if (round_trip_u64() < 0) {
    PyErr_SetString(PyExc_AssertionError, "round_trip_u64 failed");
    return NULL;
  }
  if (round_trip_u32() < 0) {
    PyErr_SetString(PyExc_AssertionError, "round_trip_u32 failed");
    return NULL;
  }
  if (round_trip_bool() < 0) {
    PyErr_SetString(PyExc_AssertionError, "round_trip_bool failed");
    return NULL;
  }
  if (round_trip_ptr() < 0) {
    PyErr_SetString(PyExc_AssertionError, "round_trip_ptr failed");
    return NULL;
  }
  Py_RETURN_NONE;
}

// ---------------------------------------------------------------------------
// Registrar.
// ---------------------------------------------------------------------------

static PyMethodDef methods[] = {
    {"atomics_make_state", py_make_state, METH_NOARGS,
     "Allocate a fresh state slot."},
    {"atomics_reset", py_reset, METH_O, "Reset all slots to zero/null/false."},
    {"atomics_load_counter64", py_load_counter64, METH_O,
     "Load the u64 counter."},
    {"atomics_load_counter32", py_load_counter32, METH_O,
     "Load the u32 counter."},
    {"atomics_load_bool", py_load_bool, METH_O, "Load the bool slot."},
    {"atomics_load_ptr", py_load_ptr, METH_O, "Load the ptr slot as int."},
    {"atomics_producer", py_producer, METH_VARARGS,
     "Write payload, then release-store flag=1."},
    {"atomics_consumer", py_consumer, METH_O,
     "Acquire-spin on flag, then read payload."},
    {"atomics_fetch_add_loop_u64", py_fetch_add_loop_u64, METH_VARARGS,
     "Relaxed fetch_add(+1) on counter64 in a tight loop."},
    {"atomics_fetch_add_loop_u32", py_fetch_add_loop_u32, METH_VARARGS,
     "Relaxed fetch_add(+1) on counter32 in a tight loop."},
    {"atomics_cas_increment_loop_u64", py_cas_increment_loop_u64, METH_VARARGS,
     "Acq_rel CAS-increment of counter64 in a tight loop."},
    {"atomics_round_trip", py_round_trip, METH_NOARGS,
     "Single-threaded smoke test of every (op, type, order)."},
    {NULL, NULL, 0, NULL},
};

int boc_internal_test_register_atomics(PyObject *module) {
  return PyModule_AddFunctions(module, methods);
}
