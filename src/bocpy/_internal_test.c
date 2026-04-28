/// @file _internal_test.c
/// @brief Bridge module that aggregates per-domain test helpers under
///        `bocpy._internal_test`.
///
/// Each domain is a separate translation unit (`_internal_test_*.c`)
/// that exposes a `boc_internal_test_register_<domain>` registrar.
/// This file owns only the `PyModuleDef` + `PyInit__internal_test`
/// scaffolding and calls every registrar once at import time.
///
/// Domains so far:
///   - `atomics_*` — typed `boc_atomic_*_explicit` API
///                   (`_internal_test_atomics.c`).
///   - `bq_*`      — Verona-style behaviour MPMC queue
///                   (`_internal_test_bq.c`).
///
/// The module deliberately does NOT link against `_core` or `_math`.
/// It links only the units it tests (`compat.c`, `sched.c`) so the
/// test surface stays minimal and there is no sub-interpreter
/// machinery in the way of the test threads.

#define PY_SSIZE_T_CLEAN

#include <Python.h>

extern int boc_internal_test_register_atomics(PyObject *module);
extern int boc_internal_test_register_bq(PyObject *module);
extern int boc_internal_test_register_wsq(PyObject *module);

/// @brief Multi-phase init: register the test methods on the module.
/// @details Single-phase init re-enables the GIL on free-threaded
/// builds (CPython 3.13t+) because there is no slot to declare GIL
/// independence. Multi-phase init lets us set @c Py_mod_gil to
/// @c Py_MOD_GIL_NOT_USED. The harness only manipulates POD test
/// fixtures (typed atomics under @c _atomics, raw bq nodes under
/// @c _bq) and does not touch any Python state that would race
/// without the GIL.
static int _internal_test_exec(PyObject *m) {
  if (boc_internal_test_register_atomics(m) < 0) {
    return -1;
  }
  if (boc_internal_test_register_bq(m) < 0) {
    return -1;
  }
  if (boc_internal_test_register_wsq(m) < 0) {
    return -1;
  }
  return 0;
}

static PyModuleDef_Slot _internal_test_slots[] = {
    {Py_mod_exec, (void *)_internal_test_exec},
#if PY_VERSION_HEX >= 0x030C0000
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
#endif
#if PY_VERSION_HEX >= 0x030D0000
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
#endif
    {0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_internal_test",
    .m_doc = "Test harness for bocpy internal C primitives "
             "(typed atomics, MPMC queue, ...).",
    .m_size = 0,
    .m_methods = NULL, // methods are added by registrars in exec slot
    .m_slots = _internal_test_slots,
};

PyMODINIT_FUNC PyInit__internal_test(void) {
  return PyModuleDef_Init(&moduledef);
}
