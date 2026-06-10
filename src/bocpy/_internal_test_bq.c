/// @file _internal_test_bq.c
/// @brief BQ-domain (Verona MPMC behaviour queue) tests for
///        `bocpy._internal_test`.
///
/// Exposes the `boc_bq_*` API from `boc_sched.h` to Python so
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

#include "boc_compat.h"
#include "boc_sched.h"

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
    boc_bq_node_t *n;
    while ((n = boc_bq_dequeue(q)) != NULL) {
      (void)n;
    }
    boc_bq_destroy_assert_empty(q);
    PyMem_RawFree(q);
  }
}

static void bq_node_capsule_destructor(PyObject *capsule) {
  bq_test_node_t *n =
      (bq_test_node_t *)PyCapsule_GetPointer(capsule, BQ_NODE_CAPSULE_NAME);
  if (n != NULL) {
    PyMem_RawFree(n);
  }
}

static boc_bq_t *bq_queue_from_capsule(PyObject *capsule) {
  return (boc_bq_t *)PyCapsule_GetPointer(capsule, BQ_QUEUE_CAPSULE_NAME);
}

static bq_test_node_t *bq_node_from_capsule(PyObject *capsule) {
  return (bq_test_node_t *)PyCapsule_GetPointer(capsule, BQ_NODE_CAPSULE_NAME);
}

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
    if (seg.end == &seg.start->next_in_queue) {
      break;
    }
    Py_BEGIN_ALLOW_THREADS while (
        boc_atomic_load_ptr_explicit(&seg.start->next_in_queue,
                                     BOC_MO_ACQUIRE) == NULL) {}
    Py_END_ALLOW_THREADS
  }
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
