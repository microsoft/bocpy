/// @file _bocpy_probe.c
/// @brief Non-trivial downstream consumer of the bocpy public C ABI.
///
/// This translation unit is built by `templates/c_abi_consumer/setup.py`
/// against the headers reported by `bocpy.get_include()`. It serves
/// two purposes:
///
///   1. CI smoke test for the public C ABI (compile + import + behave).
///   2. Canonical worked example for downstream extension authors.
///
/// ### Design
///
/// The extension exposes a `Counter` Python type. Each instance wraps
/// a pointer to a heap-allocated `counter_impl` C struct that lives
/// outside any single Python object's lifetime. `Counter` is
/// registered as cross-interpreter shareable via
/// `XIDATA_REGISTERCLASS` with a producer-side getdata callback and a
/// consumer-side reconstruction callback. The reconstructed wrapper
/// shares the same underlying `counter_impl` pointer; an atomic
/// `count` field on the impl is bumped each time the consumer
/// callback runs, so Python tests can observe round-trip identity
/// and ordering as a `Counter` cown is shipped between workers via
/// `@when`.
///
/// ### Ownership (proto-Region semantics)
///
/// `counter_impl` carries an atomic `owner` field tagged with the
/// interpreter ID that may currently read or write it. The producer
/// callback CASes `owner` from `bocpy_interpid()` to `BOCPY_NO_OWNER`
/// before initialising the xidata, and the consumer callback CASes
/// it back from `BOCPY_NO_OWNER` to its own `bocpy_interpid()` before
/// constructing the new wrapper. Reading `count` from a wrapper
/// whose interpreter does not own the impl raises `RuntimeError` —
/// stale wrappers left behind in the producer interpreter cannot
/// observe the value any more. This mirrors `bocpy.Matrix` and is
/// the pattern documented in the C ABI page under "Proto-Region
/// semantics".
///
/// ### Refcounting
///
/// `counter_impl` carries its own atomic refcount. Each `Counter`
/// wrapper holds one ref; `Counter.__dealloc__` drops it. The
/// consumer callback creates a fresh wrapper and bumps the refcount
/// for it. The xidata keeps the producer wrapper alive (via the
/// `obj` slot recorded by `XIDATA_INIT`), so the impl cannot be
/// freed mid-handoff. When the last wrapper goes away, the impl is
/// freed. Refcounting is independent of ownership: any interpreter
/// holding a wrapper drops its ref on dealloc, regardless of who
/// currently owns the impl. This mirrors the `Matrix` pattern in
/// `src/bocpy/_math.c`.
///
/// ### Module init
///
/// The module uses multi-phase initialisation (`Py_mod_exec`) and
/// declares `Py_MOD_PER_INTERPRETER_GIL_SUPPORTED`. bocpy workers
/// always run in sub-interpreters (sharing the legacy global GIL on
/// 3.10/3.11, owning per-interpreter GILs on 3.12+), and
/// `XIDATA_REGISTERCLASS` registers types into a per-interpreter
/// registry, so the registration must run in every interpreter that
/// reconstructs a `Counter`. `Counter` itself is a heap type created
/// via `PyType_FromModuleAndSpec`, owned by per-module state, with
/// the `XIDATA_REGISTERCLASS` call living in the exec slot.

#include <bocpy/bocpy.h>

/* Compile-time guard: bocpy.h must not leak Py_BUILD_CORE. If a
 * future refactor of xidata.h forgets the #undef, this file fails
 * to compile, which fails CI louder than any runtime test could. */
#ifdef Py_BUILD_CORE
#error "Py_BUILD_CORE leaked from bocpy.h"
#endif

#include <stdint.h>

/* ------------------------------------------------------------------ */
/* counter_impl: heap-allocated, shared by reference                  */
/* ------------------------------------------------------------------ */

typedef struct {
  atomic_int_least64_t refcount; /* number of Counter wrappers */
  atomic_int_least64_t count;    /* number of XIData round-trips */
  /* Interpreter ID currently allowed to read/write the impl, or
   * BOCPY_NO_OWNER while the impl is in flight between interpreters.
   * Flipped by the producer/consumer XIData callbacks; checked by
   * Counter_get_count. */
  atomic_int_least64_t owner;
} counter_impl;

static counter_impl *counter_impl_new(void) {
  counter_impl *impl = PyMem_RawMalloc(sizeof(*impl));
  if (impl == NULL) {
    return NULL;
  }
  atomic_store(&impl->refcount, 1);
  atomic_store(&impl->count, 0);
  /* Born owned by the constructing interpreter. */
  atomic_store(&impl->owner, bocpy_interpid());
  return impl;
}

static void counter_impl_incref(counter_impl *impl) {
  atomic_fetch_add(&impl->refcount, 1);
}

static void counter_impl_decref(counter_impl *impl) {
  /* fetch_add returns the *old* value; if it was 1 we are the last
   * holder. */
  int_least64_t old = atomic_fetch_add(&impl->refcount, -1);
  if (old == 1) {
    PyMem_RawFree(impl);
  }
}

/* Returns true if the current interpreter currently owns the impl.
 * Used by data-reading accessors (Counter_get_count). Identity-only
 * accessors (address, refcount) deliberately do not call this: they
 * are valid to inspect from any interpreter holding a wrapper, the
 * same way you may print the address of a Region handle without
 * being inside the Region. */
static bool counter_impl_check_acquired(counter_impl *impl, bool set_error) {
  if (bocpy_interpid() != atomic_load(&impl->owner)) {
    if (set_error) {
      PyErr_SetString(PyExc_RuntimeError,
                      "the current interpreter does not own this Counter");
    }
    return false;
  }
  return true;
}

/* ------------------------------------------------------------------ */
/* Counter: Python wrapper around a counter_impl pointer              */
/* ------------------------------------------------------------------ */

/* Forward declaration so the per-interpreter state lookup helper can
 * reference the module def by address. */
static struct PyModuleDef _bocpy_probe_module;

/* Per-interpreter module state. Each interpreter that imports the
 * module gets its own copy, with its own heap-allocated `counter_type`.
 * `LOCAL_STATE` is a thread-local cache populated by the exec slot,
 * so callbacks (the XIData consumer side, methods, …) can reach the
 * right `counter_type` without re-walking PyModule_GetState every
 * call. Mirrors the LOCAL_STATE pattern in `src/bocpy/_math.c`. */
typedef struct {
  PyTypeObject *counter_type;
} _bocpy_probe_module_state;

static thread_local _bocpy_probe_module_state *LOCAL_STATE;

#define LOCAL_STATE_SET(m)                                                     \
  do {                                                                         \
    LOCAL_STATE = (_bocpy_probe_module_state *)PyModule_GetState(m);           \
  } while (0)

typedef struct {
  PyObject_HEAD counter_impl *impl;
} CounterObject;

static int Counter_init(CounterObject *self, PyObject *args, PyObject *kwds) {
  /* Counter takes no arguments; reject anything passed in to surface
   * mistakes loudly rather than silently dropping kwargs. */
  static char *kwlist[] = {NULL};
  if (!PyArg_ParseTupleAndKeywords(args, kwds, ":Counter", kwlist)) {
    return -1;
  }
  /* Guard against re-initialisation: __init__ is callable more than
   * once on the same instance, and without this check the second
   * call would leak the first impl. */
  if (self->impl != NULL) {
    counter_impl_decref(self->impl);
    self->impl = NULL;
  }
  self->impl = counter_impl_new();
  if (self->impl == NULL) {
    PyErr_NoMemory();
    return -1;
  }
  return 0;
}

static void Counter_dealloc(PyObject *op) {
  CounterObject *self = (CounterObject *)op;
  if (self->impl != NULL) {
    counter_impl_decref(self->impl);
    self->impl = NULL;
  }
  Py_TYPE(self)->tp_free(self);
}

static PyObject *Counter_get_count(CounterObject *self, void *closure) {
  if (self->impl == NULL) {
    PyErr_SetString(PyExc_RuntimeError, "Counter not initialised");
    return NULL;
  }
  if (!counter_impl_check_acquired(self->impl, true)) {
    return NULL;
  }
  return PyLong_FromLongLong((long long)atomic_load(&self->impl->count));
}

static PyObject *Counter_get_address(CounterObject *self, void *closure) {
  if (self->impl == NULL) {
    PyErr_SetString(PyExc_RuntimeError, "Counter not initialised");
    return NULL;
  }
  return PyLong_FromVoidPtr(self->impl);
}

static PyObject *Counter_get_refcount(CounterObject *self, void *closure) {
  if (self->impl == NULL) {
    PyErr_SetString(PyExc_RuntimeError, "Counter not initialised");
    return NULL;
  }
  return PyLong_FromLongLong((long long)atomic_load(&self->impl->refcount));
}

static PyGetSetDef Counter_getset[] = {
    {"count", (getter)Counter_get_count, NULL,
     "Number of XIData round-trips the underlying impl has seen.", NULL},
    {"address", (getter)Counter_get_address, NULL,
     "Identity of the underlying counter_impl pointer (as int).", NULL},
    {"refcount", (getter)Counter_get_refcount, NULL,
     "Number of Counter wrappers currently holding the impl.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyType_Slot Counter_slots[] = {
    {Py_tp_doc, (void *)"Counter()\n--\n\n"
                        "A refcounted counter shareable across interpreters."},
    {Py_tp_new, PyType_GenericNew},
    {Py_tp_init, (void *)Counter_init},
    {Py_tp_dealloc, (void *)Counter_dealloc},
    {Py_tp_getset, Counter_getset},
    {0, NULL},
};

static PyType_Spec Counter_Spec = {
    .name = "_bocpy_probe.Counter",
    .basicsize = sizeof(CounterObject),
    .itemsize = 0,
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_IMMUTABLETYPE,
    .slots = Counter_slots,
};

/* ------------------------------------------------------------------ */
/* XIData callbacks                                                   */
/* ------------------------------------------------------------------ */

/// @brief Wraps a counter sent from another interpreter.
/// @details The underlying counter_impl, when it arrives at another
/// interpreter, is wrapped by this method in a CounterObject so that
/// it can be used from code running in that interpreter.
/// @param xidata The xidata containing the counter_impl
/// @return a new CounterObject reference, or NULL on error
static PyObject *_new_counter_object(XIDATA_T *xidata) {
  counter_impl *impl = (counter_impl *)xidata->data;

  /* Take ownership of the impl: BOCPY_NO_OWNER -> this interpreter.
   * The producer callback parked the impl at BOCPY_NO_OWNER before
   * handing it off; if the CAS fails, something else has already
   * claimed it (a bug in the cross-interpreter handoff machinery).
   */
  int_least64_t expected = BOCPY_NO_OWNER;
  int_least64_t desired = bocpy_interpid();
  if (!atomic_compare_exchange_strong(&impl->owner, &expected, desired)) {
    PyErr_Format(PyExc_RuntimeError,
                 "cannot acquire Counter (expected BOCPY_NO_OWNER, "
                 "observed owner=%lld)",
                 (long long)expected);
    return NULL;
  }

  atomic_fetch_add(&impl->count, 1);

  /* Use this interpreter's heap-allocated copy of the type. */
  PyTypeObject *type = LOCAL_STATE->counter_type;
  CounterObject *counter = (CounterObject *)type->tp_alloc(type, 0);
  if (counter == NULL) {
    /* Roll the owner back so a future retry of the handoff can
     * succeed and the impl is not stranded with us as owner while
     * we have no wrapper to release it. */
    atomic_store(&impl->owner, BOCPY_NO_OWNER);
    return NULL;
  }
  counter->impl = impl;
  counter_impl_incref(impl);
  return (PyObject *)counter;
}

/// @brief Prepare the underlying counter_impl for sharing with another
/// interpreter.
/// @param tstate The thread state of the current interpreter
/// @param obj The CounterObject instance
/// @param xidata An empty xidata package
/// @return 0 if successful, < 0 on error
XIDATA_GETDATA_FUNC(_counter_shared) {
  CounterObject *counter = (CounterObject *)obj;
  counter_impl *impl = counter->impl;
  if (impl == NULL) {
    PyErr_SetString(PyExc_RuntimeError, "Counter not initialised");
    return -1;
  }

  /* Release ownership: this interpreter -> BOCPY_NO_OWNER. The
   * consumer-side callback will CAS it from NO_OWNER to its own
   * interpreter ID. Failing here means another interpreter already
   * owns the impl, so it cannot be lawfully shipped from us. */
  int_least64_t expected = bocpy_interpid();
  int_least64_t desired = BOCPY_NO_OWNER;
  if (!atomic_compare_exchange_strong(&impl->owner, &expected, desired)) {
    PyErr_Format(PyExc_RuntimeError,
                 "cannot share Counter (owned by interpreter %lld, "
                 "this interpreter is %lld)",
                 (long long)expected, (long long)bocpy_interpid());
    return -1;
  }

  XIDATA_INIT(xidata, tstate->interp, impl, obj, _new_counter_object);
  return 0;
}

/* ------------------------------------------------------------------ */
/* Module-level methods                                               */
/* ------------------------------------------------------------------ */

static PyMethodDef _bocpy_probe_methods[] = {
    {NULL, NULL, 0, NULL},
};

/* ------------------------------------------------------------------ */
/* Module init: multi-phase, per-interpreter-GIL aware                */
/* ------------------------------------------------------------------ */

/// @brief Module exec slot.
///
/// Runs once per interpreter, on every import. Allocates this
/// interpreter's heap-allocated `Counter` type, registers it as
/// cross-interpreter shareable, and primes the `LOCAL_STATE`
/// thread-local cache so XIData callbacks and methods can find the
/// type without walking module state on every call.
///
/// The `XIDATA_REGISTERCLASS` call must happen here (not in `PyInit`)
/// because CPython's cross-interpreter type registry is
/// per-interpreter — each interpreter that wants to share an instance
/// needs the type registered in its own registry.
static int _bocpy_probe_module_exec(PyObject *module) {
  _bocpy_probe_module_state *state =
      (_bocpy_probe_module_state *)PyModule_GetState(module);

  state->counter_type =
      (PyTypeObject *)PyType_FromModuleAndSpec(module, &Counter_Spec, NULL);
  if (state->counter_type == NULL) {
    return -1;
  }
  if (PyModule_AddType(module, state->counter_type) < 0) {
    return -1;
  }

  /* Register Counter as cross-interpreter shareable. The producer
   * callback runs whenever something asks XIData to package a
   * Counter — in particular, every time a `Counter` cown is shipped
   * to a worker via `@when` or sent through the bocpy message queue.
   */
  if (XIDATA_REGISTERCLASS(state->counter_type, _counter_shared)) {
    PyErr_SetString(PyExc_RuntimeError,
                    "could not register Counter for cross-interpreter sharing");
    return -1;
  }

  LOCAL_STATE_SET(module);
  return 0;
}

static int _bocpy_probe_module_clear(PyObject *module) {
  _bocpy_probe_module_state *state =
      (_bocpy_probe_module_state *)PyModule_GetState(module);
  Py_CLEAR(state->counter_type);
  return 0;
}

static void _bocpy_probe_module_free(void *module) {
  (void)_bocpy_probe_module_clear((PyObject *)module);
}

static int _bocpy_probe_module_traverse(PyObject *module, visitproc visit,
                                        void *arg) {
  _bocpy_probe_module_state *state =
      (_bocpy_probe_module_state *)PyModule_GetState(module);
  Py_VISIT(state->counter_type);
  return 0;
}

static PyModuleDef_Slot _bocpy_probe_module_slots[] = {
    {Py_mod_exec, (void *)_bocpy_probe_module_exec},
#if PY_VERSION_HEX >= 0x030C0000
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
#endif
#if PY_VERSION_HEX >= 0x030D0000
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
#endif
    {0, NULL},
};

static struct PyModuleDef _bocpy_probe_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_bocpy_probe",
    .m_doc = "Smoke test and worked example for the bocpy public C ABI.",
    .m_size = sizeof(_bocpy_probe_module_state),
    .m_methods = _bocpy_probe_methods,
    .m_slots = _bocpy_probe_module_slots,
    .m_traverse = _bocpy_probe_module_traverse,
    .m_clear = _bocpy_probe_module_clear,
    .m_free = (freefunc)_bocpy_probe_module_free,
};

PyMODINIT_FUNC PyInit__bocpy_probe(void) {
  return PyModuleDef_Init(&_bocpy_probe_module);
}
