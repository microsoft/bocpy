/// @file _internal_test_wsq.c
/// @brief WSQ-domain (work-stealing queue cursor + spread) tests for
///        `bocpy._internal_test`.
///
/// Exposes the inline `boc_wsq_*` helpers from `sched.h` so
/// `test/test_internal_wsq.py` can verify the cursor-wrap arithmetic
/// and the `enqueue_spread` distribution invariant directly, without
/// going through the full scheduler runtime.
///
/// Only `boc_wsq_pre_inc`, `boc_wsq_post_dec`, `boc_wsq_enqueue`, and
/// `boc_wsq_enqueue_spread` are exercised here; the dispatch / steal
/// integration is covered by the existing
/// `test_scheduler_steal.py` / `test_scheduler_integration.py` suites
/// once the wiring is live.
///
/// Worker fixtures here are bare `boc_sched_worker_t` allocations
/// initialised by `boc_bq_init` per sub-queue and zeroed cursors —
/// the rest of the worker struct (mutex, cv, ring link) is unused
/// and remains zero. This is sound because the WSQ helpers touch
/// only `q[]` and the three cursors.

#define PY_SSIZE_T_CLEAN

#include <Python.h>

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "compat.h"
#include "sched.h"

// ---------------------------------------------------------------------------
// Worker fixture capsule
// ---------------------------------------------------------------------------

#define WSQ_WORKER_CAPSULE_NAME "bocpy._internal_test.wsq_worker"
#define WSQ_NODE_CAPSULE_NAME "bocpy._internal_test.wsq_node"

/// @brief Test node carrying an integer identity for FIFO checks.
typedef struct {
  boc_bq_node_t node;
  int64_t id;
} wsq_test_node_t;

static void wsq_worker_capsule_destructor(PyObject *capsule) {
  boc_sched_worker_t *w = (boc_sched_worker_t *)PyCapsule_GetPointer(
      capsule, WSQ_WORKER_CAPSULE_NAME);
  if (w == NULL) {
    return;
  }
  // Drain every sub-queue so destroy_assert_empty does not abort if
  // a test left items behind. We do NOT free the test nodes here —
  // they are owned by the Python side via their own capsules.
  for (size_t i = 0; i < (size_t)BOC_WSQ_N; ++i) {
    while (boc_bq_dequeue(&w->q[i]) != NULL) {
      // discard
    }
    boc_bq_destroy_assert_empty(&w->q[i]);
  }
  PyMem_RawFree(w);
}

static void wsq_node_capsule_destructor(PyObject *capsule) {
  wsq_test_node_t *n =
      (wsq_test_node_t *)PyCapsule_GetPointer(capsule, WSQ_NODE_CAPSULE_NAME);
  if (n != NULL) {
    PyMem_RawFree(n);
  }
}

static boc_sched_worker_t *wsq_worker_from_capsule(PyObject *capsule) {
  return (boc_sched_worker_t *)PyCapsule_GetPointer(capsule,
                                                    WSQ_WORKER_CAPSULE_NAME);
}

// ---------------------------------------------------------------------------
// Methods
// ---------------------------------------------------------------------------

static PyObject *wsq_n(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(args)) {
  return PyLong_FromSize_t((size_t)BOC_WSQ_N);
}

static PyObject *wsq_make_worker(PyObject *Py_UNUSED(self),
                                 PyObject *Py_UNUSED(args)) {
  // Calloc so all unused worker fields (mutex, cv, ring link, stats,
  // owner_interp_id, ...) are zero. The WSQ helpers only touch q[]
  // and the three cursors, all of which we re-init explicitly.
  boc_sched_worker_t *w = PyMem_RawCalloc(1, sizeof(boc_sched_worker_t));
  if (w == NULL) {
    return PyErr_NoMemory();
  }
  for (size_t i = 0; i < (size_t)BOC_WSQ_N; ++i) {
    boc_bq_init(&w->q[i]);
  }
  w->enqueue_index.idx = 0;
  w->dequeue_index.idx = 0;
  w->steal_index.idx = 0;
  PyObject *capsule =
      PyCapsule_New(w, WSQ_WORKER_CAPSULE_NAME, wsq_worker_capsule_destructor);
  if (capsule == NULL) {
    for (size_t i = 0; i < (size_t)BOC_WSQ_N; ++i) {
      boc_bq_destroy_assert_empty(&w->q[i]);
    }
    PyMem_RawFree(w);
    return NULL;
  }
  return capsule;
}

/// @brief Run @p k pre-increments on a fresh cursor and return the
///        per-index count as a list of length @c BOC_WSQ_N.
/// @details Pure cursor arithmetic; no worker / queue involvement.
/// Verifies @ref boc_wsq_pre_inc cycles indices uniformly.
static PyObject *wsq_pre_inc_histogram(PyObject *Py_UNUSED(self),
                                       PyObject *args) {
  Py_ssize_t k;
  if (!PyArg_ParseTuple(args, "n:wsq_pre_inc_histogram", &k)) {
    return NULL;
  }
  if (k < 0) {
    PyErr_SetString(PyExc_ValueError, "k must be non-negative");
    return NULL;
  }
  size_t counts[BOC_WSQ_N];
  memset(counts, 0, sizeof(counts));
  boc_wsq_cursor_t c = {0};
  for (Py_ssize_t i = 0; i < k; ++i) {
    size_t idx = boc_wsq_pre_inc(&c);
    counts[idx] += 1u;
  }
  PyObject *out = PyList_New((Py_ssize_t)BOC_WSQ_N);
  if (out == NULL) {
    return NULL;
  }
  for (size_t i = 0; i < (size_t)BOC_WSQ_N; ++i) {
    PyObject *v = PyLong_FromSize_t(counts[i]);
    if (v == NULL) {
      Py_DECREF(out);
      return NULL;
    }
    PyList_SET_ITEM(out, (Py_ssize_t)i, v);
  }
  return out;
}

/// @brief Run @p k post-decrements on a fresh cursor and return the
///        sequence of returned indices.
static PyObject *wsq_post_dec_sequence(PyObject *Py_UNUSED(self),
                                       PyObject *args) {
  Py_ssize_t k;
  if (!PyArg_ParseTuple(args, "n:wsq_post_dec_sequence", &k)) {
    return NULL;
  }
  if (k < 0) {
    PyErr_SetString(PyExc_ValueError, "k must be non-negative");
    return NULL;
  }
  PyObject *out = PyList_New(k);
  if (out == NULL) {
    return NULL;
  }
  boc_wsq_cursor_t c = {0};
  for (Py_ssize_t i = 0; i < k; ++i) {
    size_t r = boc_wsq_post_dec(&c);
    PyObject *v = PyLong_FromSize_t(r);
    if (v == NULL) {
      Py_DECREF(out);
      return NULL;
    }
    PyList_SET_ITEM(out, i, v);
  }
  return out;
}

/// @brief Push @p k freshly-allocated nodes via @ref boc_wsq_enqueue
///        on @p worker, then drain each sub-queue in order and return
///        a list of length @c BOC_WSQ_N giving the count per
///        sub-queue.
/// @details Verifies that single-node enqueues round-robin across
/// the N sub-queues. Each pushed node carries its push-order id; the
/// returned value is `[count[0], count[1], ..., count[N-1]]` so the
/// caller can assert uniformity. Drained nodes are freed.
static PyObject *wsq_enqueue_drain_counts(PyObject *Py_UNUSED(self),
                                          PyObject *args) {
  PyObject *worker_capsule;
  Py_ssize_t k;
  if (!PyArg_ParseTuple(args, "On:wsq_enqueue_drain_counts", &worker_capsule,
                        &k)) {
    return NULL;
  }
  boc_sched_worker_t *w = wsq_worker_from_capsule(worker_capsule);
  if (w == NULL) {
    return NULL;
  }
  if (k < 0) {
    PyErr_SetString(PyExc_ValueError, "k must be non-negative");
    return NULL;
  }
  for (Py_ssize_t i = 0; i < k; ++i) {
    wsq_test_node_t *n = PyMem_RawCalloc(1, sizeof(*n));
    if (n == NULL) {
      return PyErr_NoMemory();
    }
    n->id = (int64_t)i;
    boc_wsq_enqueue(w, &n->node);
  }
  PyObject *out = PyList_New((Py_ssize_t)BOC_WSQ_N);
  if (out == NULL) {
    return NULL;
  }
  for (size_t i = 0; i < (size_t)BOC_WSQ_N; ++i) {
    size_t count = 0;
    boc_bq_node_t *raw;
    while ((raw = boc_bq_dequeue(&w->q[i])) != NULL) {
      wsq_test_node_t *n = (wsq_test_node_t *)raw;
      PyMem_RawFree(n);
      count += 1u;
    }
    PyObject *v = PyLong_FromSize_t(count);
    if (v == NULL) {
      Py_DECREF(out);
      return NULL;
    }
    PyList_SET_ITEM(out, (Py_ssize_t)i, v);
  }
  return out;
}

/// @brief Build a length-@p L pre-linked segment (no queue
///        involved), call @ref boc_wsq_enqueue_spread on @p worker,
///        then drain each sub-queue and return per-sub-queue counts.
/// @details The segment is constructed by hand: nodes 0..L-1 with
/// `next_in_queue` pre-linked head-to-tail, and the segment's `end`
/// pointing at the tail node's `next_in_queue` slot. This mirrors
/// what `boc_bq_dequeue_all` would have produced for a freshly-
/// stolen victim queue. After spread, every node should have been
/// distributed across `worker`'s sub-queues; the returned count list
/// must sum to @p L.
static PyObject *wsq_spread_segment_counts(PyObject *Py_UNUSED(self),
                                           PyObject *args) {
  PyObject *worker_capsule;
  Py_ssize_t length;
  if (!PyArg_ParseTuple(args, "On:wsq_spread_segment_counts", &worker_capsule,
                        &length)) {
    return NULL;
  }
  boc_sched_worker_t *w = wsq_worker_from_capsule(worker_capsule);
  if (w == NULL) {
    return NULL;
  }
  if (length <= 0) {
    PyErr_SetString(PyExc_ValueError, "length must be positive");
    return NULL;
  }
  // Allocate L nodes and link them head-to-tail. The link payload
  // stored in `next_in_queue` is `boc_bq_node_t *`; we use plain
  // stores via the typed atomic helper to construct the segment.
  wsq_test_node_t **nodes = PyMem_RawCalloc((size_t)length, sizeof(*nodes));
  if (nodes == NULL) {
    return PyErr_NoMemory();
  }
  for (Py_ssize_t i = 0; i < length; ++i) {
    nodes[i] = PyMem_RawCalloc(1, sizeof(wsq_test_node_t));
    if (nodes[i] == NULL) {
      for (Py_ssize_t j = 0; j < i; ++j) {
        PyMem_RawFree(nodes[j]);
      }
      PyMem_RawFree(nodes);
      return PyErr_NoMemory();
    }
    nodes[i]->id = (int64_t)i;
  }
  // Link 0->1->...->L-1; tail's next stays NULL. Relaxed stores
  // are fine — the segment is private to this thread until we hand
  // it to enqueue_spread, which uses the queue's release/acquire
  // protocol on its own.
  for (Py_ssize_t i = 0; i < length - 1; ++i) {
    boc_atomic_store_ptr_explicit(&nodes[i]->node.next_in_queue,
                                  &nodes[i + 1]->node, BOC_MO_RELAXED);
  }
  boc_atomic_store_ptr_explicit(&nodes[length - 1]->node.next_in_queue, NULL,
                                BOC_MO_RELAXED);
  boc_bq_segment_t seg;
  seg.start = &nodes[0]->node;
  seg.end = &nodes[length - 1]->node.next_in_queue;
  PyMem_RawFree(nodes);

  boc_wsq_enqueue_spread(w, seg);

  PyObject *out = PyList_New((Py_ssize_t)BOC_WSQ_N);
  if (out == NULL) {
    return NULL;
  }
  for (size_t i = 0; i < (size_t)BOC_WSQ_N; ++i) {
    size_t count = 0;
    boc_bq_node_t *raw;
    while ((raw = boc_bq_dequeue(&w->q[i])) != NULL) {
      wsq_test_node_t *n = (wsq_test_node_t *)raw;
      PyMem_RawFree(n);
      count += 1u;
    }
    PyObject *v = PyLong_FromSize_t(count);
    if (v == NULL) {
      Py_DECREF(out);
      return NULL;
    }
    PyList_SET_ITEM(out, (Py_ssize_t)i, v);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Registrar
// ---------------------------------------------------------------------------

static PyMethodDef wsq_methods[] = {
    {"wsq_n", wsq_n, METH_NOARGS,
     "Return the compile-time BOC_WSQ_N constant."},
    {"wsq_make_worker", wsq_make_worker, METH_NOARGS,
     "Allocate and initialise a fresh boc_sched_worker_t fixture; "
     "returns a capsule. The fixture's mutex/cv/ring fields are zero "
     "(unused by WSQ helpers)."},
    {"wsq_pre_inc_histogram", wsq_pre_inc_histogram, METH_VARARGS,
     "Run k pre-increments on a fresh cursor; return a length-N list of "
     "per-index counts."},
    {"wsq_post_dec_sequence", wsq_post_dec_sequence, METH_VARARGS,
     "Run k post-decrements on a fresh cursor; return the sequence of "
     "returned indices as a list of length k."},
    {"wsq_enqueue_drain_counts", wsq_enqueue_drain_counts, METH_VARARGS,
     "Push k nodes via boc_wsq_enqueue, drain every sub-queue, return "
     "per-sub-queue counts."},
    {"wsq_spread_segment_counts", wsq_spread_segment_counts, METH_VARARGS,
     "Build a length-L pre-linked segment, call boc_wsq_enqueue_spread, "
     "drain every sub-queue, return per-sub-queue counts."},
    {NULL, NULL, 0, NULL},
};

int boc_internal_test_register_wsq(PyObject *module) {
  if (PyModule_AddFunctions(module, wsq_methods) < 0) {
    return -1;
  }
  return 0;
}
