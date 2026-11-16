/// @file boc_execution.c
/// @brief Execution context and driver-owned completion observations

#include "boc_execution.h"
#include "boc_terminator.h"
#include "boc_sched.h"
#include <math.h>

thread_local bool IN_PUMP_BODY = false;
thread_local uint64_t CURRENT_EXECUTION = 0;
atomic_int_least64_t NEXT_EXECUTION = 1;
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

/// @brief Publish a blocking worker's private pending work without executing it
static void scope_publish_pending(void) {
  boc_sched_worker_t *worker = boc_sched_current_worker();
  if (worker == NULL) {
    return;
  }
  boc_sched_worker_t *target = worker->next_in_ring;
  bool fairness = boc_atomic_load_bool_explicit(
      &worker->should_steal_for_fairness, BOC_MO_RELAXED);
  boc_sched_set_steal_flag(worker, false);
  // A depleted batch may pop a queue entry first; its reset makes a second pop consume any private pending entry.
  for (int i = 0; i < 2; ++i) {
    boc_bq_node_t *node = boc_sched_worker_pop_fast(worker);
    if (node == NULL) {
      break;
    }
    boc_wsq_enqueue(target, node);
    boc_atomic_fetch_add_u64_explicit(&target->stats.pushed_remote, 1,
                                      BOC_MO_RELAXED);
  }
  boc_sched_set_steal_flag(worker, fairness);
  boc_sched_unpause_all(worker);
  boc_sched_signal_one(target);
}

/// @brief Preserve each interruption while strict scope draining protects live output users
static void wait_defer_exception(PyObject **interrupted) {
  PyObject *error = PyErr_GetRaisedException();
  if (*interrupted != NULL) {
    if (error != *interrupted) {
      PyException_SetContext(error, *interrupted);
    } else {
      Py_DECREF(*interrupted);
    }
  }
  *interrupted = error;
}

/// @brief Own one completion observation through blocking, bounded pumping and signal checks
PyObject *terminator_wait_driver(BOCTerminator *term, bool drain,
                                 double end, bool forever,
                                 PyObject *boc_export, bool pumpable,
                                 BOCScopeOwner *host) {
  bool defer = drain && host != NULL;
  BOCTerminatorWaiter waiter;
  boc_terminator_waiter_init(&waiter, term, drain);
  if (host != NULL) {
    scope_publish_pending();
  }
  PyObject *interrupted = NULL;
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
    reason = boc_terminator_waiter_wait(
        &waiter, slice, false, pumpable ? _core_pinned_depth_load : NULL);
    Py_END_ALLOW_THREADS
    if (PyErr_CheckSignals() < 0) {
      if (!defer) {
        break;
      }
      wait_defer_exception(&interrupted);
      forever = true;
    }
    if (host != NULL && CURRENT_SCOPE != host) {
      PyErr_SetString(PyExc_RuntimeError, "scope nesting changed during wait");
      break;
    }
    if (reason == BOC_TERMINATOR_TERMINATED) {
      completed = true;
      break;
    }
    // Completion wins over expiry, but a perpetually ready pinned queue must not bypass the deadline.
    if (!forever && slice == 0) {
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
    bool started = false;
    PyObject *result = main_pump_bounded_impl(deadline, limit, false,
                                             boc_export, &started);
    Py_DECREF(deadline);
    if (result == NULL) {
      // Gate failures cannot make progress on retry; only execution interruptions are deferred.
      if (!defer || !started) {
        break;
      }
      wait_defer_exception(&interrupted);
      forever = true;
    } else {
      Py_DECREF(result);
    }
  }
  Py_XDECREF(limit);
  if (interrupted != NULL) {
    if (PyErr_Occurred()) {
      wait_defer_exception(&interrupted);
    }
    PyErr_SetRaisedException(interrupted);
  }
  boc_terminator_waiter_clear(&waiter);
  if (PyErr_Occurred()) {
    return NULL;
  }
  return PyBool_FromLong(completed);
}
