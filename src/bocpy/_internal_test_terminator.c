/// @file _internal_test_terminator.c
/// @brief Deterministic waiter scenarios on the test extension's isolated root

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <string.h>

#include "boc_compat.h"
#include "boc_terminator.h"

static boc_atomic_bool_t scenario_running;
static bool initialized;

static uint64_t pinned_ready(void) { return 1; }
static uint64_t pinned_empty(void) { return 0; }

/// @brief Poll the native primitive without holding the GIL
static boc_terminator_wake_reason_t poll_waiter(
    BOCTerminatorWaiter *waiter, uint64_t (*depth)(void)) {
  boc_terminator_wake_reason_t reason;
  Py_BEGIN_ALLOW_THREADS
  reason = boc_terminator_waiter_wait(waiter, 0, false, depth);
  Py_END_ALLOW_THREADS
  return reason;
}

#define CHECK(condition)                                                       \
  do {                                                                         \
    if (!(condition)) {                                                        \
      PyErr_SetString(PyExc_AssertionError, #condition);                        \
      goto cleanup;                                                            \
    }                                                                          \
  } while (0)

/// @brief Exercise a retained native observation without exposing a Python waiter
static PyObject *terminator_scenario(PyObject *Py_UNUSED(self), PyObject *arg) {
  const char *scenario = PyUnicode_AsUTF8(arg);
  if (scenario == NULL) {
    return NULL;
  }
  if (boc_atomic_exchange_bool_explicit(&scenario_running, true, BOC_MO_ACQUIRE)) {
    PyErr_SetString(PyExc_RuntimeError, "terminator scenarios must run sequentially");
    return NULL;
  }
  if (!initialized) {
    terminator_init();
    initialized = true;
  }
  BOCTerminator *term = NULL;
  BOCTerminatorWaiter waiters[4] = {0};
  BOCTerminatorWaiter fresh = {0};
  BOCTerminatorWaiter initial = {0};
  PyObject *result = NULL;
  if (!terminator_reset()) {
    goto cleanup;
  }
  CHECK(terminator_inc() == -1);
  CHECK(terminator_activate());
  term = terminator_root_acquire();
  CHECK(term != NULL);
  boc_terminator_waiter_init(&initial, term, false);
  CHECK(initial.completed);
  CHECK(boc_terminator_inc(term) == 1);
  uint64_t generation = boc_terminator_zero_count(term);
  for (int i = 0; i < 4; ++i) {
    boc_terminator_waiter_init(&waiters[i], term, false);
    CHECK(waiters[i].generation == generation);
    CHECK(!waiters[i].completed);
  }
  if (strcmp(scenario, "initial-zero") == 0) {
    CHECK(poll_waiter(&initial, NULL) == BOC_TERMINATOR_TERMINATED);
    CHECK(boc_terminator_count(term) == 1);
    CHECK(poll_waiter(&waiters[0], NULL) == BOC_TERMINATOR_WAIT_TIMED_OUT);
  } else if (strcmp(scenario, "generation") == 0 ||
             strcmp(scenario, "multiple") == 0 ||
             strcmp(scenario, "pinned") == 0 ||
             strcmp(scenario, "spurious") == 0) {
    for (int round = 0; round < 3; ++round) {
      for (int i = 0; i < 4; ++i) {
        if (strcmp(scenario, "pinned") == 0) {
          CHECK(poll_waiter(&waiters[i], pinned_ready) == BOC_TERMINATOR_PUMP_READY);
          CHECK(poll_waiter(&waiters[i], pinned_ready) == BOC_TERMINATOR_PUMP_READY);
        }
        terminator_wake_all();
        CHECK(poll_waiter(&waiters[i], pinned_empty) == BOC_TERMINATOR_WAIT_TIMED_OUT);
        CHECK(boc_terminator_zero_count(term) == generation);
        CHECK(boc_terminator_count(term) == 1);
      }
    }
    CHECK(boc_terminator_dec(term) == 0);
    CHECK(boc_terminator_inc(term) == 1);
    CHECK(boc_terminator_zero_count(term) == generation + 1);
    for (int i = 0; i < 4; ++i) {
      CHECK(poll_waiter(&waiters[i], pinned_ready) == BOC_TERMINATOR_TERMINATED);
      CHECK(poll_waiter(&waiters[i], NULL) == BOC_TERMINATOR_TERMINATED);
      CHECK(waiters[i].completed);
    }
    CHECK(boc_terminator_count(term) == 1);
    boc_terminator_waiter_init(&fresh, term, false);
    CHECK(poll_waiter(&fresh, NULL) == BOC_TERMINATOR_WAIT_TIMED_OUT);
  } else if (strcmp(scenario, "retirement") == 0) {
    CHECK(boc_terminator_dec(term) == 0);
    CHECK(terminator_close());
    CHECK(terminator_retire());
    boc_terminator_decref(term);
    term = NULL;
    CHECK(terminator_reset());
    CHECK(terminator_inc() == -1);
    CHECK(terminator_activate());
    term = terminator_root_acquire();
    CHECK(boc_terminator_inc(term) == 1);
    CHECK(term != waiters[0].term);
    for (int i = 0; i < 4; ++i) {
      CHECK(poll_waiter(&waiters[i], NULL) == BOC_TERMINATOR_TERMINATED);
      CHECK(boc_terminator_count(waiters[i].term) == 0);
      CHECK(boc_terminator_zero_count(waiters[i].term) == generation + 1);
    }
    boc_terminator_waiter_init(&fresh, term, false);
    CHECK(poll_waiter(&fresh, NULL) == BOC_TERMINATOR_WAIT_TIMED_OUT);
  } else if (strcmp(scenario, "drain") == 0) {
    boc_terminator_waiter_init(&fresh, term, true);
    CHECK(poll_waiter(&fresh, NULL) == BOC_TERMINATOR_WAIT_TIMED_OUT);
    CHECK(boc_terminator_dec(term) == 0);
    CHECK(boc_terminator_inc(term) == 1);
    CHECK(poll_waiter(&waiters[0], NULL) == BOC_TERMINATOR_TERMINATED);
    CHECK(poll_waiter(&fresh, NULL) == BOC_TERMINATOR_WAIT_TIMED_OUT);
    CHECK(boc_terminator_dec(term) == 0);
    CHECK(poll_waiter(&fresh, NULL) == BOC_TERMINATOR_TERMINATED);
    CHECK(boc_terminator_inc(term) == -1);
  } else {
    PyErr_SetString(PyExc_ValueError, "unknown terminator scenario");
    goto cleanup;
  }
  result = Py_NewRef(Py_None);

cleanup:
  if (term != NULL) {
    while (boc_terminator_count(term) > 0) {
      boc_terminator_dec(term);
    }
  }
  terminator_close();
  terminator_retire();
  boc_terminator_waiter_clear(&initial);
  boc_terminator_waiter_clear(&fresh);
  for (int i = 0; i < 4; ++i) {
    boc_terminator_waiter_clear(&waiters[i]);
  }
  boc_terminator_decref(term);
  boc_atomic_store_bool_explicit(&scenario_running, false, BOC_MO_RELEASE);
  return result;
}

#undef CHECK

static PyMethodDef terminator_methods[] = {
    {"terminator_scenario", terminator_scenario, METH_O,
     "Check an isolated native terminator scenario"},
    {NULL} /* Sentinel */
};

int boc_internal_test_register_terminator(PyObject *module) {
  return PyModule_AddFunctions(module, terminator_methods);
}
