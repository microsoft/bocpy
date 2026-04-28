/// @file _internal_test_bq.c
/// @brief BQ-domain (Verona MPMC behaviour queue) tests for
///        `bocpy._internal_test`.
///
/// Exposes the `boc_bq_*` API from `sched.h` to Python so
/// `test/test_internal_mpmcq.py` can stress the queue from multiple
/// real threads. Methods are registered on the `bocpy._internal_test`
/// module under the `bq_*` prefix.
///
/// Nodes here are bare `boc_bq_node_t` allocations carrying an
/// integer identity used by tests to verify FIFO ordering and
/// segment chains. Production code uses `BOCBehavior::bq_node` from
/// `_core.c` (verified via `pahole`); the queue itself is layout-
/// agnostic.

#define PY_SSIZE_T_CLEAN

#include <Python.h>

#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

#include "compat.h"
#include "sched.h"

// ---------------------------------------------------------------------------
// Node and queue capsule helpers
// ---------------------------------------------------------------------------

/// @brief Test node: a `boc_bq_node_t` followed by an integer identity.
typedef struct {
  boc_bq_node_t node; ///< Link field consumed by `boc_bq_*`.
  int64_t id;         ///< Caller-supplied identity for FIFO checks.
} bq_test_node_t;

#define BQ_QUEUE_CAPSULE_NAME "bocpy._internal_test.bq_queue"
#define BQ_NODE_CAPSULE_NAME "bocpy._internal_test.bq_node"

static void bq_queue_capsule_destructor(PyObject *capsule) {
  boc_bq_t *q =
      (boc_bq_t *)PyCapsule_GetPointer(capsule, BQ_QUEUE_CAPSULE_NAME);
  if (q != NULL) {
    // Drain any leftover nodes so destroy_assert_empty does not abort
    // on a leaked test queue. We do NOT free the nodes here; the
    // Python side owns them via their own capsules.
    boc_bq_node_t *n;
    while ((n = boc_bq_dequeue(q)) != NULL) {
      (void)n;
    }
    boc_bq_destroy_assert_empty(q);
    // Raw allocator: bq queues exist precisely to be crossed between
    // sub-interpreters in production (per-worker queues), so the test
    // harness uses the same process-global allocator to avoid masking
    // a cross-interpreter free bug behind a same-interpreter test.
    PyMem_RawFree(q);
  }
}

static void bq_node_capsule_destructor(PyObject *capsule) {
  bq_test_node_t *n =
      (bq_test_node_t *)PyCapsule_GetPointer(capsule, BQ_NODE_CAPSULE_NAME);
  if (n != NULL) {
    // Raw allocator: see bq_queue_capsule_destructor above.
    PyMem_RawFree(n);
  }
}

static boc_bq_t *bq_queue_from_capsule(PyObject *capsule) {
  return (boc_bq_t *)PyCapsule_GetPointer(capsule, BQ_QUEUE_CAPSULE_NAME);
}

static bq_test_node_t *bq_node_from_capsule(PyObject *capsule) {
  return (bq_test_node_t *)PyCapsule_GetPointer(capsule, BQ_NODE_CAPSULE_NAME);
}

// ---------------------------------------------------------------------------
// Methods
// ---------------------------------------------------------------------------

static PyObject *bq_make_queue(PyObject *Py_UNUSED(self),
                               PyObject *Py_UNUSED(args)) {
  boc_bq_t *q = PyMem_RawMalloc(sizeof(boc_bq_t));
  if (q == NULL) {
    return PyErr_NoMemory();
  }
  boc_bq_init(q);
  PyObject *capsule =
      PyCapsule_New(q, BQ_QUEUE_CAPSULE_NAME, bq_queue_capsule_destructor);
  if (capsule == NULL) {
    PyMem_RawFree(q);
    return NULL;
  }
  return capsule;
}

static PyObject *bq_make_node(PyObject *Py_UNUSED(self), PyObject *args) {
  long long id;
  if (!PyArg_ParseTuple(args, "L:bq_make_node", &id)) {
    return NULL;
  }
  bq_test_node_t *n = PyMem_RawMalloc(sizeof(bq_test_node_t));
  if (n == NULL) {
    return PyErr_NoMemory();
  }
  boc_atomic_store_ptr_explicit(&n->node.next_in_queue, NULL, BOC_MO_RELAXED);
  n->id = (int64_t)id;
  PyObject *capsule =
      PyCapsule_New(n, BQ_NODE_CAPSULE_NAME, bq_node_capsule_destructor);
  if (capsule == NULL) {
    PyMem_RawFree(n);
    return NULL;
  }
  return capsule;
}

static PyObject *bq_node_id(PyObject *Py_UNUSED(self), PyObject *args) {
  PyObject *cap;
  if (!PyArg_ParseTuple(args, "O:bq_node_id", &cap)) {
    return NULL;
  }
  bq_test_node_t *n = bq_node_from_capsule(cap);
  if (n == NULL) {
    return NULL;
  }
  return PyLong_FromLongLong((long long)n->id);
}

/// @brief Read back the raw bq_node pointer of a node capsule.
/// @details Returns @c &node->node (the embedded @c boc_bq_node_t)
/// as an integer. Used by the dispatch test to compare pointer
/// identities against the integer returned by
/// @c _core.scheduler_pop_fast.
static PyObject *bq_node_ptr(PyObject *Py_UNUSED(self), PyObject *args) {
  PyObject *cap;
  if (!PyArg_ParseTuple(args, "O:bq_node_ptr", &cap)) {
    return NULL;
  }
  bq_test_node_t *n = bq_node_from_capsule(cap);
  if (n == NULL) {
    return NULL;
  }
  // bq_test_node_t puts `node` first, so &n == &n->node, but be
  // explicit for clarity and to keep the test invariant readable.
  return PyLong_FromVoidPtr((void *)&n->node);
}

static PyObject *bq_enqueue(PyObject *Py_UNUSED(self), PyObject *args) {
  PyObject *qcap, *ncap;
  if (!PyArg_ParseTuple(args, "OO:bq_enqueue", &qcap, &ncap)) {
    return NULL;
  }
  boc_bq_t *q = bq_queue_from_capsule(qcap);
  bq_test_node_t *n = bq_node_from_capsule(ncap);
  if (q == NULL || n == NULL) {
    return NULL;
  }
  Py_BEGIN_ALLOW_THREADS boc_bq_enqueue(q, &n->node);
  Py_END_ALLOW_THREADS Py_RETURN_NONE;
}

static PyObject *bq_enqueue_front(PyObject *Py_UNUSED(self), PyObject *args) {
  PyObject *qcap, *ncap;
  if (!PyArg_ParseTuple(args, "OO:bq_enqueue_front", &qcap, &ncap)) {
    return NULL;
  }
  boc_bq_t *q = bq_queue_from_capsule(qcap);
  bq_test_node_t *n = bq_node_from_capsule(ncap);
  if (q == NULL || n == NULL) {
    return NULL;
  }
  Py_BEGIN_ALLOW_THREADS boc_bq_enqueue_front(q, &n->node);
  Py_END_ALLOW_THREADS Py_RETURN_NONE;
}

static PyObject *bq_dequeue(PyObject *Py_UNUSED(self), PyObject *args) {
  PyObject *qcap;
  if (!PyArg_ParseTuple(args, "O:bq_dequeue", &qcap)) {
    return NULL;
  }
  boc_bq_t *q = bq_queue_from_capsule(qcap);
  if (q == NULL) {
    return NULL;
  }
  boc_bq_node_t *raw;
  Py_BEGIN_ALLOW_THREADS raw = boc_bq_dequeue(q);
  Py_END_ALLOW_THREADS if (raw == NULL) { Py_RETURN_NONE; }
  // Recover the embedding test-node and return its id. Tests don't
  // need the original capsule object back; identity is the contract.
  bq_test_node_t *n = (bq_test_node_t *)raw;
  return PyLong_FromLongLong((long long)n->id);
}

static PyObject *bq_dequeue_all(PyObject *Py_UNUSED(self), PyObject *args) {
  PyObject *qcap;
  if (!PyArg_ParseTuple(args, "O:bq_dequeue_all", &qcap)) {
    return NULL;
  }
  boc_bq_t *q = bq_queue_from_capsule(qcap);
  if (q == NULL) {
    return NULL;
  }
  boc_bq_segment_t seg;
  Py_BEGIN_ALLOW_THREADS seg = boc_bq_dequeue_all(q);
  Py_END_ALLOW_THREADS

      PyObject *list = PyList_New(0);
  if (list == NULL) {
    return NULL;
  }
  if (seg.start == NULL) {
    return list;
  }
  // Walk the segment via segment_take_one. take_one returns NULL for
  // three reasons (mpmcq.h:67-89, also documented at
  // sched.c::boc_sched_steal):
  //   1. fully empty (impossible here — guarded above),
  //   2. singleton segment (end == &start->next_in_queue) — append
  //      start as the tail and return,
  //   3. broken link: producer P has CASed itself onto the queue
  //      tail (back.exchange) but has not yet completed the
  //      "publish next pointer" store. seg.start->next_in_queue
  //      reads as NULL, but the segment is NOT singleton — there
  //      is at least one more node the producer is mid-publish.
  //
  // Verona's WorkStealingQueue::steal handles case 3 by spreading
  // the partial segment back across its multi-N WSQ. The bocpy
  // production caller (boc_sched_steal) handles it by splicing the
  // partial segment onto self->q, deferring the missing tail to a
  // subsequent dequeue once the producer's store lands.
  //
  // For a test helper there is no other queue to spread/splice
  // onto, AND the test contract is "every enqueued item is observed
  // exactly once". The pragmatic answer is to BUSY-SPIN on the
  // broken next pointer until the producer's store becomes visible.
  // The producer is mid-call (between `back.exchange` and
  // `b->store(seg.start, release)` — three instructions wide), so
  // the spin is bounded by producer scheduling latency. Without
  // this spin the previous implementation silently dropped the
  // entire post-broken-link tail, manifesting as the
  // `[8-100000]` stress test losing 1-227 items per run.
  for (;;) {
    boc_bq_node_t *taken = boc_bq_segment_take_one(&seg);
    if (taken != NULL) {
      bq_test_node_t *n = (bq_test_node_t *)taken;
      PyObject *id = PyLong_FromLongLong((long long)n->id);
      if (id == NULL || PyList_Append(list, id) < 0) {
        Py_XDECREF(id);
        Py_DECREF(list);
        return NULL;
      }
      Py_DECREF(id);
      continue;
    }
    // take_one returned NULL. Distinguish singleton from broken-link
    // (case 1 is impossible; we guarded seg.start != NULL above and
    // each take_one advances seg.start to a known-non-NULL node).
    if (seg.end == &seg.start->next_in_queue) {
      // Singleton tail — done.
      break;
    }
    // Broken-link case: spin until the producer publishes. The wait
    // is bounded by producer scheduling latency; under TSan or
    // heavy oversubscription it could be milliseconds, but it is
    // never unbounded — the producer is mid-call by construction.
    // Drop the GIL across the spin so other Python threads (e.g.
    // the other consumer in the stress test) can make progress.
    Py_BEGIN_ALLOW_THREADS while (
        boc_atomic_load_ptr_explicit(&seg.start->next_in_queue,
                                     BOC_MO_ACQUIRE) == NULL) {
      // Compiler/CPU hint: tight spin on a single cacheline. No
      // platform-specific PAUSE intrinsic here — the spin is short
      // and the cost is dwarfed by GIL re-acquire.
    }
    Py_END_ALLOW_THREADS
    // Producer's store is now visible; loop and let take_one walk it.
  }
  // Append the tail node (seg.start now points at it; its
  // next_in_queue is NULL by segment-end invariant).
  bq_test_node_t *tail = (bq_test_node_t *)seg.start;
  PyObject *tail_id = PyLong_FromLongLong((long long)tail->id);
  if (tail_id == NULL || PyList_Append(list, tail_id) < 0) {
    Py_XDECREF(tail_id);
    Py_DECREF(list);
    return NULL;
  }
  Py_DECREF(tail_id);
  return list;
}

static PyObject *bq_is_empty(PyObject *Py_UNUSED(self), PyObject *args) {
  PyObject *qcap;
  if (!PyArg_ParseTuple(args, "O:bq_is_empty", &qcap)) {
    return NULL;
  }
  boc_bq_t *q = bq_queue_from_capsule(qcap);
  if (q == NULL) {
    return NULL;
  }
  if (boc_bq_is_empty(q)) {
    Py_RETURN_TRUE;
  }
  Py_RETURN_FALSE;
}

// ---------------------------------------------------------------------------
// Method table and registrar
// ---------------------------------------------------------------------------

static PyMethodDef bq_methods[] = {
    {"bq_make_queue", bq_make_queue, METH_NOARGS,
     "Create an empty MPMC behaviour queue. Returns a capsule."},
    {"bq_make_node", bq_make_node, METH_VARARGS,
     "bq_make_node(id) -> capsule. Allocate a test node with the "
     "given integer identity."},
    {"bq_node_id", bq_node_id, METH_VARARGS,
     "bq_node_id(node) -> int. Read back the node's identity."},
    {"bq_node_ptr", bq_node_ptr, METH_VARARGS,
     "bq_node_ptr(node) -> int. Raw boc_bq_node_t* as an integer "
     "(for pointer-identity comparisons against scheduler_pop_fast)."},
    {"bq_enqueue", bq_enqueue, METH_VARARGS,
     "bq_enqueue(q, node). Append a node to the queue."},
    {"bq_enqueue_front", bq_enqueue_front, METH_VARARGS,
     "bq_enqueue_front(q, node). Push a node onto the front of the queue."},
    {"bq_dequeue", bq_dequeue, METH_VARARGS,
     "bq_dequeue(q) -> id or None. Pop one node, returning its identity."},
    {"bq_dequeue_all", bq_dequeue_all, METH_VARARGS,
     "bq_dequeue_all(q) -> list[int]. Pop every currently-enqueued "
     "node in FIFO order."},
    {"bq_is_empty", bq_is_empty, METH_VARARGS,
     "bq_is_empty(q) -> bool. True iff the queue is currently empty."},
    {NULL, NULL, 0, NULL},
};

int boc_internal_test_register_bq(PyObject *module) {
  for (PyMethodDef *def = bq_methods; def->ml_name != NULL; ++def) {
    PyObject *fn = PyCFunction_New(def, NULL);
    if (fn == NULL) {
      return -1;
    }
    if (PyModule_AddObject(module, def->ml_name, fn) < 0) {
      Py_DECREF(fn);
      return -1;
    }
  }
  return 0;
}
