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

#ifndef BOCPY_XIDATA_H
#define BOCPY_XIDATA_H

#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <stdbool.h>

#if PY_VERSION_HEX >= 0x030D0000
#define Py_BUILD_CORE
#include <internal/pycore_crossinterp.h>
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

#endif // BOCPY_XIDATA_H
