/// @file boc_execution.c
/// @brief Execution context and driver-owned completion observations

#include "boc_execution.h"
#include "boc_terminator.h"
#include <math.h>

thread_local bool IN_PUMP_BODY = false;
thread_local bool IN_BEHAVIOR = false;
atomic_int_least64_t WAIT_PUMP_POLL_MS = 50;

/// @brief Normalize arguments before checking host state, since conversion can execute Python
bool wait_arguments(PyObject *args, PyObject *kwargs, double *end,
                    bool *forever, PyObject **boc_export) {
  static char *names[] = {"timeout", "boc_export", NULL};
  PyObject *timeout = Py_None;
  *boc_export = Py_None;
  if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|OO", names, &timeout,
                                   boc_export)) {
    return false;
  }
  *forever = timeout == Py_None;
  double seconds = 0;
  if (!*forever) {
    seconds = PyFloat_AsDouble(timeout);
    if (PyErr_Occurred()) {
      return false;
    }
    if (!isfinite(seconds) || seconds < 0) {
      PyErr_SetString(PyExc_ValueError, "timeout must be finite and nonnegative");
      return false;
    }
  }
  *end = *forever ? 0 : boc_now_s() + seconds;
  return true;
}

/// @brief Own one completion observation through blocking, bounded pumping and signal checks
PyObject *terminator_wait_driver(bool drain,
                                 double end, bool forever,
                                 PyObject *boc_export, bool pumpable) {
  BOCTerminatorWaiter waiter;
  terminator_waiter_init(&waiter, drain);
  PyObject *limit = NULL;
  bool completed = false;
  for (;;) {
    double poll = (double)atomic_load(&WAIT_PUMP_POLL_MS) / 1000.0;
    double slice = fmin(poll, 0.1);
    if (!forever) {
      slice = fmin(slice, fmax(0.0, end - boc_now_s()));
    }
    boc_terminator_wake_reason_t reason;
    Py_BEGIN_ALLOW_THREADS
    reason = terminator_waiter_wait(
        &waiter, slice, false, pumpable ? _core_pinned_depth_load : NULL);
    Py_END_ALLOW_THREADS
    if (PyErr_CheckSignals() < 0) {
      break;
    }
    if (reason == BOC_TERMINATOR_TERMINATED) {
      completed = true;
      break;
    }
    // Completion wins over expiry, but a perpetually ready pinned queue must not bypass the deadline.
    if (slice == 0) {
      break;
    }
    if (reason != BOC_TERMINATOR_PUMP_READY) {
      continue;
    }
    if (boc_export == Py_None) {
      PyErr_SetString(PyExc_RuntimeError, "pinned work requires a behavior resolver");
      break;
    }
    if (limit == NULL) {
      limit = PyLong_FromLong(64);
      if (limit == NULL) {
        break;
      }
    }
    // Re-read after callbacks so a live poll adjustment applies to the next batch.
    long long ms = atomic_load(&WAIT_PUMP_POLL_MS);
    if (ms > 100) {
      ms = 100;
    }
    if (!forever) {
      double remaining_ms = (end - boc_now_s()) * 1000.0;
      if (remaining_ms <= 0) {
        continue;
      }
      if (remaining_ms < ms) {
        ms = (long long)ceil(remaining_ms);
      }
    }
    PyObject *deadline = PyLong_FromLongLong(ms);
    if (deadline == NULL) {
      break;
    }
    PyObject *result = main_pump_bounded_impl(deadline, limit, false,
                                             boc_export, NULL);
    Py_DECREF(deadline);
    if (result == NULL) {
      break;
    }
    Py_DECREF(result);
  }
  Py_XDECREF(limit);
  if (PyErr_Occurred()) {
    return NULL;
  }
  return PyBool_FromLong(completed);
}
