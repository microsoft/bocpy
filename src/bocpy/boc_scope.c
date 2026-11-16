/// @file boc_scope.c
/// @brief Scope owner lifecycle and interpreter-local scheduling handles

#include "boc_scope.h"
#include "boc_execution.h"
#include "boc_noticeboard.h"
#include <bocpy/bocpy.h>
#include <bocpy/xidata.h>

thread_local BOCScopeOwner *CURRENT_SCOPE = NULL;
thread_local BOCScopeOwner *EXECUTING_SCOPE = NULL;

typedef struct scope_capsule_object {
  PyObject_HEAD
  BOCScopeOwner *owner;
  BOCCown *output;
  BOCScopeOwner *saved;
  boc_atomic_u64_t host_thread;
  bool waiting;
} ScopeCapsuleObject;

/// @brief Allow scheduled output access until draining work has released its reservations
bool scope_owner_accepts_work(BOCScopeOwner *owner) {
  int_least64_t state = atomic_load(&owner->state);
  return state == BOC_SCOPE_ACTIVE || state == BOC_SCOPE_DRAINING;
}

/// @brief Preserve a scope identity independently of its completion count
void scope_owner_incref(BOCScopeOwner *owner) {
  if (owner != NULL) {
    atomic_fetch_add(&owner->refs, 1);
  }
}

void scope_owner_decref(BOCScopeOwner *owner) {
  if (owner != NULL && atomic_fetch_add(&owner->refs, -1) == 1) {
    scope_owner_decref(owner->parent);
    boc_terminator_decref(owner->term);
    PyMem_RawFree(owner);
  }
}

/// @brief Test an immutable entered-scope ancestor chain
bool scope_owner_contains(BOCScopeOwner *ancestor, BOCScopeOwner *owner) {
  for (; owner != NULL; owner = owner->parent) {
    if (owner == ancestor) {
      return true;
    }
  }
  return false;
}

BOCScopeOwner *scope_capsule_owner(PyObject *op) {
  return ((ScopeCapsuleObject *)op)->owner;
}

/// @brief Allocate a single-use scope and bind its output before entry
static PyObject *ScopeCapsule_new(PyTypeObject *type, PyObject *args,
                                  PyObject *kwargs) {
  scope_capsule_type(PyType_GetModule(type));
  static char *names[] = {"output_cown_impl", NULL};
  PyObject *output;
  if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O:ScopeCapsule", names,
                                   &output)) {
    return NULL;
  }
  BOCCown *cown = cown_unwrap(output);
  if (cown == NULL || !cown_scope_available(cown)) {
    return NULL;
  }
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)type->tp_alloc(type, 0);
  if (self == NULL) {
    return NULL;
  }
  BOCScopeOwner *owner = PyMem_RawCalloc(1, sizeof(*owner));
  if (owner == NULL) {
    Py_DECREF(self);
    return PyErr_NoMemory();
  }
  owner->term = boc_terminator_new(false);
  if (owner->term == NULL) {
    PyMem_RawFree(owner);
    Py_DECREF(self);
    return PyErr_NoMemory();
  }
  atomic_store(&owner->refs, 1);
  atomic_store(&owner->state, BOC_SCOPE_CREATED);
  self->owner = owner;
  if (cown_scope_bind(cown, owner) < 0) {
    Py_DECREF(self);
    return NULL;
  }
  self->output = cown;
  COWN_INCREF(cown);
  return (PyObject *)self;
}

static void ScopeCapsule_dealloc(PyObject *op) {
  scope_capsule_type(PyType_GetModule(Py_TYPE(op)));
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  if (self->output != NULL) {
    COWN_DECREF(self->output);
  }
  scope_owner_decref(self->owner);
  Py_TYPE(op)->tp_free(op);
}

/// @brief Install a local owner and reserve the enclosing runtime execution
static PyObject *ScopeCapsule_enter(PyObject *op, PyObject *Py_UNUSED(args)) {
  scope_capsule_type(PyType_GetModule(Py_TYPE(op)));
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  int_least64_t expected = BOC_SCOPE_CREATED;
  if (self->output == NULL ||
      !atomic_compare_exchange_strong(&self->owner->state, &expected,
                                      BOC_SCOPE_ENTERING)) {
    PyErr_SetString(PyExc_RuntimeError, "scope is single-use");
    return NULL;
  }
  if (!boc_terminator_start(self->owner->term, &self->owner->root)) {
    atomic_store(&self->owner->state, BOC_SCOPE_CREATED);
    PyErr_SetString(PyExc_RuntimeError, "runtime is shutting down");
    return NULL;
  }
  self->saved = CURRENT_SCOPE;
  self->owner->parent = CURRENT_SCOPE;
  scope_owner_incref(CURRENT_SCOPE);
  boc_atomic_store_u64_explicit(&self->host_thread,
                                (uint64_t)PyThread_get_thread_ident(),
                                BOC_MO_RELAXED);
  CURRENT_SCOPE = self->owner;
  // Keep the host wrapper alive until finish restores its TLS predecessor.
  Py_INCREF(op);
  // Publish the initialized parent, host identity and counter only after the entry claim is complete.
  atomic_store(&self->owner->state, BOC_SCOPE_ACTIVE);
  Py_RETURN_NONE;
}

/// @brief Require structured same-thread exit by the original host wrapper
static bool scope_check_host(ScopeCapsuleObject *self) {
  if (self->waiting || !scope_owner_accepts_work(self->owner) || self->output == NULL ||
      boc_atomic_load_u64_explicit(&self->host_thread, BOC_MO_RELAXED) !=
          (uint64_t)PyThread_get_thread_ident() ||
      CURRENT_SCOPE != self->owner) {
    PyErr_SetString(PyExc_RuntimeError,
                    "scope exit must run on its entering thread in nesting order");
    return false;
  }
  if (CURRENT_EXECUTION != 0 &&
      scope_owner_contains(self->owner, EXECUTING_SCOPE)) {
    PyErr_SetString(PyExc_RuntimeError, "a behavior cannot wait for its own scope or an ancestor");
    return false;
  }
  return true;
}

static PyObject *ScopeCapsule_close(PyObject *op, PyObject *Py_UNUSED(args)) {
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  if (!scope_check_host(self)) {
    return NULL;
  }
  int_least64_t expected = BOC_SCOPE_ACTIVE;
  atomic_compare_exchange_strong(&self->owner->state, &expected,
                                 BOC_SCOPE_DRAINING);
  Py_RETURN_NONE;
}

/// @brief Require the original active host, excluding execution that contributes to this counter
static bool scope_check_quiescence(ScopeCapsuleObject *host) {
  if (!scope_check_host(host)) {
    return false;
  }
  if (atomic_load(&host->owner->state) != BOC_SCOPE_ACTIVE) {
    PyErr_SetString(PyExc_RuntimeError, "quiescence requires an active scope host");
    return false;
  }
  return true;
}

static PyObject *ScopeCapsule_quiescence(PyObject *op, PyObject *args,
                                        PyObject *kwargs) {
  scope_capsule_type(PyType_GetModule(Py_TYPE(op)));
  double end;
  bool forever;
  PyObject *boc_export;
  if (!wait_arguments(args, kwargs, &end, &forever, &boc_export)) {
    return NULL;
  }
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  if (!scope_check_quiescence(self)) {
    return NULL;
  }
  self->waiting = true;
  bool pumpable = bocpy_interpid() == bocpy_main_interpid() && !IN_PUMP_BODY;
  PyObject *result = terminator_wait_driver(self->owner->term, false, end, forever,
                                           boc_export, pumpable, self->owner);
  self->waiting = false;
  return result;
}

/// @brief Wait for local completion without holding the interpreter lock
static PyObject *ScopeCapsule_wait(PyObject *op, PyObject *args,
                                   PyObject *kwargs) {
  scope_capsule_type(PyType_GetModule(Py_TYPE(op)));
  double end;
  bool forever;
  PyObject *boc_export;
  if (!wait_arguments(args, kwargs, &end, &forever, &boc_export)) {
    return NULL;
  }
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  if (!scope_check_host(self)) {
    return NULL;
  }
  if (atomic_load(&self->owner->state) != BOC_SCOPE_DRAINING) {
    PyErr_SetString(PyExc_RuntimeError, "scope must begin exit before waiting");
    return NULL;
  }
  self->waiting = true;
  bool pumpable = bocpy_interpid() == bocpy_main_interpid() && !IN_PUMP_BODY;
  PyObject *result = terminator_wait_driver(self->owner->term, true, end, forever,
                                           boc_export, pumpable, self->owner);
  self->waiting = false;
  return result;
}

/// @brief Consume the locally quiescent output and always restore host ownership
static PyObject *ScopeCapsule_finish(PyObject *op, PyObject *args,
                                     PyObject *kwargs) {
  scope_capsule_type(PyType_GetModule(Py_TYPE(op)));
  static char *names[] = {"publish", NULL};
  int publish;
  if (!PyArg_ParseTupleAndKeywords(args, kwargs, "p:finish", names, &publish)) {
    return NULL;
  }
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  if (!scope_check_host(self)) {
    return NULL;
  }
  if (atomic_load(&self->owner->state) != BOC_SCOPE_DRAINING ||
      !boc_terminator_fence(self->owner->term)) {
    PyErr_SetString(PyExc_RuntimeError, "scope still has outstanding work");
    return NULL;
  }
  // Deserializers and destructors may call Python; reject recursive host operations before running either.
  int_least64_t expected = BOC_SCOPE_DRAINING;
  if (!atomic_compare_exchange_strong(&self->owner->state, &expected,
                                      BOC_SCOPE_EXTRACTING)) {
    PyErr_SetString(PyExc_RuntimeError, "scope is already finalizing");
    return NULL;
  }
  PyObject *result = cown_scope_finish(self->output, publish);
  CURRENT_SCOPE = self->saved;
  self->saved = NULL;
  atomic_store(&self->owner->state, BOC_SCOPE_CLOSED);
  boc_terminator_dec(self->owner->root);
  boc_terminator_decref(self->owner->root);
  self->owner->root = NULL;
  Py_DECREF(op);
  return result;
}

/// @brief Expose the authoritative lifecycle to the owning Python facade
static PyObject *ScopeCapsule_state(PyObject *op, void *Py_UNUSED(args)) {
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  static const char *names[] = {
      "created", "entering", "active", "draining", "extracting", "closed",
  };
  return PyUnicode_FromString(names[atomic_load(&self->owner->state)]);
}

/// @brief Reconstruct only the shared scheduling identity, never host state
static PyObject *scope_capsule_wrap(BOCScopeOwner *owner) {
  PyTypeObject *type = scope_capsule_type(NULL);
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)type->tp_alloc(type, 0);
  if (self == NULL) {
    return NULL;
  }
  self->owner = owner;
  scope_owner_incref(owner);
  return (PyObject *)self;
}

static PyObject *_new_scope_object(XIDATA_T *xidata) {
  return scope_capsule_wrap((BOCScopeOwner *)xidata->data);
}

XIDATA_GETDATA_FUNC(_scope_shared) {
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)obj;
  XIDATA_INIT(xidata, tstate->interp, self->owner, obj, _new_scope_object);
  return 0;
}

static void scope_ref_retain(void *pointer) { scope_owner_incref(pointer); }
static void scope_ref_release(void *pointer) { scope_owner_decref(pointer); }
static const BOCNativeRefOps SCOPE_REF_OPS = {scope_ref_retain, scope_ref_release};

/// @brief Retain the native scheduling identity through the internal pickle path
static PyObject *ScopeCapsule_reduce(PyObject *op, PyObject *Py_UNUSED(args)) {
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  PyObject *module = PyType_GetModule(Py_TYPE(op));
  if (module == NULL) {
    return NULL;
  }
  bool borrowing = nb_pickle_borrowing();
  PyObject *rebuild = PyObject_GetAttrString(
      module, borrowing ? "_scope_from_pointer_borrowing" : "_scope_from_pointer");
  if (rebuild == NULL) {
    return NULL;
  }
#ifdef _WIN32
  long pid = (long)_getpid();
#else
  long pid = (long)getpid();
#endif
  PyObject *pointer = PyLong_FromVoidPtr(self->owner);
  PyObject *result = pointer == NULL ? NULL :
      Py_BuildValue("(O(Ol))", rebuild, pointer, pid);
  Py_XDECREF(pointer);
  Py_DECREF(rebuild);
  if (result != NULL && borrowing) {
    BOCNativeRef ref = {self->owner, &SCOPE_REF_OPS};
    if (nb_pickle_retain(ref) < 0) {
      Py_DECREF(result);
      return NULL;
    }
  } else if (result != NULL) {
    scope_owner_incref(self->owner);
  }
  return result;
}

/// @brief Reconstruct a same-process handle, borrowing noticeboard pins or consuming a one-shot pickle pin
static PyObject *scope_from_pointer(PyObject *module, PyObject *args,
                                    bool borrowing) {
  scope_capsule_type(module);
  PyObject *pointer;
  long pid;
  if (!PyArg_ParseTuple(args, "Ol", &pointer, &pid)) {
    return NULL;
  }
#ifdef _WIN32
  long current_pid = (long)_getpid();
#else
  long current_pid = (long)getpid();
#endif
  if (pid != current_pid) {
    PyErr_SetString(PyExc_RuntimeError,
                    "scope handles cannot cross process boundaries");
    return NULL;
  }
  BOCScopeOwner *owner = PyLong_AsVoidPtr(pointer);
  if (owner == NULL) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_ValueError, "invalid scope pointer");
    }
    return NULL;
  }
  PyObject *result = scope_capsule_wrap(owner);
  if (!borrowing) {
    scope_owner_decref(owner);
  }
  return result;
}

static PyObject *_scope_from_pointer(PyObject *module, PyObject *args) {
  return scope_from_pointer(module, args, false);
}

static PyObject *_scope_from_pointer_borrowing(PyObject *module, PyObject *args) {
  return scope_from_pointer(module, args, true);
}

static PyMethodDef ScopeCapsule_methods[] = {
    {"quiescence", (PyCFunction)ScopeCapsule_quiescence,
     METH_VARARGS | METH_KEYWORDS, "Wait for local quiescence"},
    {"enter", ScopeCapsule_enter, METH_NOARGS, NULL},
    {"close", ScopeCapsule_close, METH_NOARGS, NULL},
    {"wait", (PyCFunction)ScopeCapsule_wait, METH_VARARGS | METH_KEYWORDS, NULL},
    {"finish", (PyCFunction)ScopeCapsule_finish, METH_VARARGS | METH_KEYWORDS, NULL},
    {"__reduce__", ScopeCapsule_reduce, METH_NOARGS, NULL},
    {NULL} /* Sentinel */
};

static PyGetSetDef ScopeCapsule_getset[] = {
    {"state", ScopeCapsule_state, NULL, "Scope lifecycle state", NULL},
    {NULL} /* Sentinel */
};

static PyType_Slot ScopeCapsule_slots[] = {
    {Py_tp_new, ScopeCapsule_new},
    {Py_tp_dealloc, ScopeCapsule_dealloc},
    {Py_tp_methods, ScopeCapsule_methods},
    {Py_tp_getset, ScopeCapsule_getset},
    {0, NULL} /* Sentinel */
};

static PyType_Spec ScopeCapsule_Spec = {
    .name = "bocpy._core.ScopeCapsule",
    .basicsize = sizeof(ScopeCapsuleObject),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_IMMUTABLETYPE,
    .slots = ScopeCapsule_slots,
};

static PyMethodDef scope_module_methods[] = {
    {"_scope_from_pointer", _scope_from_pointer, METH_VARARGS, NULL},
    {"_scope_from_pointer_borrowing", _scope_from_pointer_borrowing, METH_VARARGS, NULL},
    {NULL} /* Sentinel */
};

/// @brief Register this interpreter's scope type and reconstructors
PyTypeObject *scope_capsule_register(PyObject *module) {
  PyTypeObject *type = (PyTypeObject *)PyType_FromModuleAndSpec(
      module, &ScopeCapsule_Spec, NULL);
  if (type == NULL) {
    return NULL;
  }
  if (PyModule_AddType(module, type) < 0 ||
      PyModule_AddFunctions(module, scope_module_methods) < 0) {
    Py_DECREF(type);
    return NULL;
  }
  if (XIDATA_REGISTERCLASS(type, _scope_shared)) {
    PyErr_SetString(PyExc_RuntimeError,
                    "could not register ScopeCapsule for cross-interpreter sharing");
    Py_DECREF(type);
    return NULL;
  }
  return type;
}
