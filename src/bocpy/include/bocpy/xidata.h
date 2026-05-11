/// @file xidata.h
/// @brief Cross-interpreter data (XIData) compatibility shim for bocpy.
///
/// CPython's cross-interpreter data API has changed names and semantics
/// across releases:
///   - 3.14+: `_PyXIData_*` / `_PyXIData_t`
///   - 3.13:  `_PyCrossInterpreterData_*` / `_PyCrossInterpreterData`
///   - 3.12:  `_PyCrossInterpreterData_*`, partial API
///   - <3.12: no multi-GIL support — provides a stub `xidata_init` so
///            the code compiles, but `BOC_NO_MULTIGIL` is defined and
///            features that depend on cross-interpreter sharing are
///            compiled out at the call site.
///
/// Before this TU split, both `_core.c` and `_math.c` carried near-
/// identical `#if PY_VERSION_HEX` ladders. Centralising them here is a
/// pure mechanical refactor — the macros expand to the same CPython
/// internal symbols on every supported version, so behaviour is
/// unchanged. Helper functions are `static inline` so a TU that does
/// not call (e.g.) `xidata_supported` does not emit an unused-function
/// warning.
///
/// ## Functional overview
///
/// The `XIDATA_*` macros expose three distinct codepaths. Each is
/// shown end-to-end below; see `_math.c` (Matrix) and the consumer
/// template at `templates/c_abi_consumer/` for working examples.
///
/// ### 1. Allocate, initialise, and fill an XIData (producer side)
///
/// Called from inside an `XIDATA_GETDATA_FUNC` callback (which the
/// runtime invokes once per cross-interpreter handoff, on the
/// interpreter that currently owns the object). The job of the
/// callback is to take ownership of the underlying resource on behalf
/// of `XIDATA_T` and record the per-resource `new_object` callback the
/// receiving interpreter will use to reconstruct a Python object.
///
/// @code
///   XIDATA_GETDATA_FUNC(_my_shared) {
///       MyObj *o = (MyObj *)obj;
///
///       // 1a. Hand off / refcount the underlying resource so it
///       //     survives the source object being decref'd.
///       my_impl *impl = my_impl_acquire(o->impl);
///
///       // 1b. Initialise the caller-allocated XIDATA_T with the
///       //     owning interpreter, the raw payload pointer, the
///       //     source PyObject (kept alive via Py_NewRef internally),
///       //     and the new_object reconstruction callback.
///       XIDATA_INIT(xidata, tstate->interp, impl, obj, _new_my_object);
///
///       // 1c. Tell the runtime how to free the payload if the
///       //     receiving interpreter never claims it (or claims and
///       //     later drops it). Skip if XIDATA_INIT already wired
///       //     this for you via new_object's destructor.
///       XIDATA_SET_FREE(xidata, (void (*)(void *))my_impl_release);
///
///       return 0;
///   }
/// @endcode
///
/// The `xidata` buffer itself is allocated by the caller of
/// `XIDATA_GETXIDATA` (typically with `XIDATA_NEW()`):
///
/// @code
///   XIDATA_T *xidata = XIDATA_NEW();
///   if (XIDATA_GETXIDATA(value, xidata) < 0) {
///       PyMem_RawFree(xidata);
///       return NULL;
///   }
///   // xidata is now ready to be enqueued onto a cross-interpreter
///   // channel / queue / behavior payload.
/// @endcode
///
/// ### 2. Free an XIData
///
/// Once a payload has been delivered (or the producer has decided to
/// drop it), call `XIDATA_FREE`. This invokes the `free` callback set
/// during step 1, releases the borrowed `obj` reference, and frees the
/// `XIDATA_T` allocation itself. Always free on the interpreter
/// recorded in `xidata->interp` — not on the receiver — because the
/// `free` callback may touch interpreter-owned state.
///
/// @code
///   XIDATA_FREE(xidata);
/// @endcode
///
/// ### 3. Register a class as cross-interpreter shareable
///
/// The XIData registry is **per interpreter**, so registration must run
/// once in every interpreter that will ever reconstruct an instance of
/// the type. The standard idiom is to call `XIDATA_REGISTERCLASS` from
/// a `Py_mod_exec` slot, so it re-runs on every import in every worker.
///
/// @code
///   static int my_module_exec(PyObject *module) {
///       my_module_state *state = PyModule_GetState(module);
///       // ... create state->my_type ...
///       if (XIDATA_REGISTERCLASS(state->my_type, _my_shared) < 0) {
///           return -1;
///       }
///       return 0;
///   }
/// @endcode
///
/// Registering the same type twice with different callbacks in the
/// same interpreter is undefined behaviour. See `XIDATA_GETDATA_FUNC`
/// (below) for the callback signature shim that hides the pre-/post-3.12
/// argument-list change.

#ifndef BOCPY_XIDATA_H
#define BOCPY_XIDATA_H

#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <stdbool.h>

#if PY_VERSION_HEX >= 0x030D0000
/* `internal/pycore_crossinterp.h` requires Py_BUILD_CORE; save and
 * restore the prior state so a TU that already had it set is not
 * silently turned off after this header. */
#ifndef Py_BUILD_CORE
#define Py_BUILD_CORE
#define BOCPY_INTERNAL_DEFINED_PY_BUILD_CORE
#endif
#include <internal/pycore_crossinterp.h>
#ifdef BOCPY_INTERNAL_DEFINED_PY_BUILD_CORE
#undef Py_BUILD_CORE
#undef BOCPY_INTERNAL_DEFINED_PY_BUILD_CORE
#endif
#endif

#if PY_VERSION_HEX >= 0x030E0000 // 3.14

#define XIDATA_FREE _PyXIData_Free
#define XIDATA_SET_FREE _PyXIData_SET_FREE
#define XIDATA_NEW() _PyXIData_New()
#define XIDATA_NEWOBJECT _PyXIData_NewObject
#define XIDATA_GETXIDATA(value, xidata)                                        \
  _PyObject_GetXIDataNoFallback(PyThreadState_GET(), (value), (xidata))
#define XIDATA_INIT _PyXIData_Init
#define XIDATA_REGISTERCLASS(type, cb)                                         \
  _PyXIData_RegisterClass(PyThreadState_GET(), (type),                         \
                          (_PyXIData_getdata_t){.basic = (cb)})
#define XIDATA_T _PyXIData_t

static inline bool xidata_supported(PyObject *op) {
  _PyXIData_getdata_t getdata = _PyXIData_Lookup(PyThreadState_GET(), op);
  return getdata.basic != NULL || getdata.fallback != NULL;
}

#elif PY_VERSION_HEX >= 0x030D0000 // 3.13

#define XIDATA_FREE _PyCrossInterpreterData_Free
#define XIDATA_NEW() _PyCrossInterpreterData_New()
#define XIDATA_NEWOBJECT _PyCrossInterpreterData_NewObject
#define XIDATA_GETXIDATA(value, xidata)                                        \
  _PyObject_GetCrossInterpreterData((value), (xidata))
#define XIDATA_INIT _PyCrossInterpreterData_Init
#define XIDATA_REGISTERCLASS(type, cb)                                         \
  _PyCrossInterpreterData_RegisterClass((type), (crossinterpdatafunc)(cb))
#define XIDATA_T _PyCrossInterpreterData

static inline void xidata_set_free(XIDATA_T *xidata, void (*freefunc)(void *)) {
  xidata->free = freefunc;
}

static inline bool xidata_supported(PyObject *op) {
  crossinterpdatafunc getdata = _PyCrossInterpreterData_Lookup(op);
  return getdata != NULL;
}

#define XIDATA_SET_FREE xidata_set_free

#elif PY_VERSION_HEX >= 0x030C0000 // 3.12

#define XIDATA_NEWOBJECT _PyCrossInterpreterData_NewObject
#define XIDATA_INIT _PyCrossInterpreterData_Init
#define XIDATA_GETXIDATA(value, xidata)                                        \
  _PyObject_GetCrossInterpreterData((value), (xidata))
#define XIDATA_REGISTERCLASS(type, cb)                                         \
  _PyCrossInterpreterData_RegisterClass((type), (crossinterpdatafunc)(cb))
#define XIDATA_T _PyCrossInterpreterData

static inline XIDATA_T *xidata_new(void) {
  XIDATA_T *xidata = (XIDATA_T *)PyMem_RawMalloc(sizeof(XIDATA_T));
  xidata->data = NULL;
  xidata->free = NULL;
  xidata->interp = -1;
  xidata->new_object = NULL;
  xidata->obj = NULL;
  return xidata;
}

static inline void xidata_set_free(XIDATA_T *xidata, void (*freefunc)(void *)) {
  xidata->free = freefunc;
}

static inline bool xidata_supported(PyObject *op) {
  crossinterpdatafunc getdata = _PyCrossInterpreterData_Lookup(op);
  return getdata != NULL;
}

static inline void xidata_free(void *arg) {
  XIDATA_T *xidata = (XIDATA_T *)arg;
  if (xidata->data != NULL) {
    if (xidata->free != NULL) {
      xidata->free(xidata->data);
    }
    xidata->data = NULL;
  }
  Py_CLEAR(xidata->obj);
  PyMem_RawFree(arg);
}

#define XIDATA_SET_FREE xidata_set_free
#define XIDATA_NEW xidata_new
#define XIDATA_FREE xidata_free

#else

/**
 * @brief Internal marker: this CPython has no per-interpreter GIL.
 *
 * Defined only on Python < 3.12. The bocpy runtime uses this internally
 * to fall back to a single-interpreter mode on these versions (workers
 * still live in sub-interpreters but share the global GIL instead of
 * owning per-interpreter ones). Downstream consumers do **not** need
 * to special-case this macro: the `xidata.h` ladder exposes the same
 * `XIDATA_*` macros on every supported CPython, and with one shared
 * GIL there is nothing extra to serialise — the GIL already does.
 */
#define BOC_NO_MULTIGIL

#define XIDATA_NEWOBJECT _PyCrossInterpreterData_NewObject
#define XIDATA_GETXIDATA(value, xidata)                                        \
  _PyObject_GetCrossInterpreterData((value), (xidata))
#define XIDATA_REGISTERCLASS(type, cb)                                         \
  _PyCrossInterpreterData_RegisterClass((type), (crossinterpdatafunc)(cb))
#define XIDATA_T _PyCrossInterpreterData

static inline void xidata_set_free(XIDATA_T *xidata, void (*freefunc)(void *)) {
  xidata->free = freefunc;
}

static inline void xidata_free(void *arg) {
  XIDATA_T *xidata = (XIDATA_T *)arg;
  if (xidata->data != NULL) {
    if (xidata->free != NULL) {
      xidata->free(xidata->data);
    }
    xidata->data = NULL;
  }
  Py_CLEAR(xidata->obj);
  PyMem_RawFree(arg);
}

static inline XIDATA_T *xidata_new(void) {
  XIDATA_T *xidata = (XIDATA_T *)PyMem_RawMalloc(sizeof(XIDATA_T));
  xidata->data = NULL;
  xidata->free = NULL;
  xidata->interp = -1;
  xidata->new_object = NULL;
  xidata->obj = NULL;
  return xidata;
}

static inline void
xidata_init(XIDATA_T *data, PyInterpreterState *interp, void *shared,
            PyObject *obj, PyObject *(*new_object)(_PyCrossInterpreterData *)) {
  assert(data->data == NULL);
  assert(data->obj == NULL);
  *data = (_PyCrossInterpreterData){0};
  data->interp = -1;

  assert(data != NULL);
  assert(new_object != NULL);
  data->data = shared;
  if (obj != NULL) {
    assert(interp != NULL);
    data->obj = Py_NewRef(obj);
  }
  data->interp = (interp != NULL) ? PyInterpreterState_GetID(interp) : -1;
  data->new_object = new_object;
}

#define XIDATA_SET_FREE xidata_set_free
#define XIDATA_NEW xidata_new
#define XIDATA_INIT xidata_init
#define XIDATA_FREE xidata_free

static inline bool xidata_supported(PyObject *op) {
  crossinterpdatafunc getdata = _PyCrossInterpreterData_Lookup(op);
  return getdata != NULL;
}

static inline PyObject *PyErr_GetRaisedException(void) {
  PyObject *et = NULL;
  PyObject *ev = NULL;
  PyObject *tb = NULL;
  PyErr_Fetch(&et, &ev, &tb);
  assert(et);
  PyErr_NormalizeException(&et, &ev, &tb);
  if (tb != NULL) {
    PyException_SetTraceback(ev, tb);
    Py_DECREF(tb);
  }
  Py_XDECREF(et);

  return ev;
}

#endif

/**
 * @brief Declare an `XIDATA_REGISTERCLASS` getdata callback.
 *
 * The CPython getdata callback signature changes shape across the
 * per-interpreter-GIL boundary:
 *
 *   - Python  3.12+ : `(PyThreadState *tstate, PyObject *obj, XIDATA_T *)`
 *   - Python <3.12  : `(PyObject *obj, XIDATA_T *)` (BOC_NO_MULTIGIL)
 *
 * On <3.12, `tstate` is not a parameter — the runtime passes only
 * `(obj, xidata)`. This macro hides that split by emitting a
 * trampoline on the legacy path that calls a `_xi_body` function with
 * the unified 3-arg signature. The user writes the body once, in
 * `(tstate, obj, xidata)` form, and it works on every supported
 * CPython:
 *
 * @code
 *   XIDATA_GETDATA_FUNC(_my_shared) {
 *       MyObj *o = (MyObj *)obj;
 *       XIDATA_INIT(xidata, tstate->interp, o->impl, obj,
 *                   _new_my_object);
 *       return 0;
 *   }
 *
 *   // In the module exec slot:
 *   XIDATA_REGISTERCLASS(state->my_type, _my_shared);
 * @endcode
 *
 * On <3.12 the macro emits an extra `<name>_xi_body` symbol and one
 * stack frame of indirection per callback invocation. `<name>` itself
 * is always the public symbol that callers (and `XIDATA_REGISTERCLASS`)
 * see, regardless of CPython version.
 */
#ifndef BOC_NO_MULTIGIL
#define XIDATA_GETDATA_FUNC(name)                                              \
  static int name(PyThreadState *tstate, PyObject *obj, XIDATA_T *xidata)
#else
#define XIDATA_GETDATA_FUNC(name)                                              \
  static int name##_xi_body(PyThreadState *, PyObject *, XIDATA_T *);          \
  static int name(PyObject *obj, XIDATA_T *xidata) {                           \
    return name##_xi_body(PyThreadState_GET(), obj, xidata);                   \
  }                                                                            \
  static int name##_xi_body(PyThreadState *tstate, PyObject *obj,              \
                            XIDATA_T *xidata)
#endif

#endif // BOCPY_XIDATA_H
