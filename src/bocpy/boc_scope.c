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

/// @brief Reject blocking scope operations from the non-reentrant pinned executor
static bool scope_check_not_pinned(void) {
  if (IN_PUMP_BODY) {
    PyErr_SetString(
        PyExc_RuntimeError,
        "pinned behaviors cannot enter or synchronously join a scope");
    return false;
  }
  return true;
}

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
  if (!scope_check_not_pinned()) {
    return NULL;
  }
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
  if (!scope_check_not_pinned()) {
    return false;
  }
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
  if (!scope_check_not_pinned()) {
    return NULL;
  }
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
  if (!scope_check_not_pinned()) {
    return NULL;
  }
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
static PyObject *scope_finish(PyObject *op, bool publish) {
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

static PyObject *ScopeCapsule_finish(PyObject *op, PyObject *args,
                                     PyObject *kwargs) {
  scope_capsule_type(PyType_GetModule(Py_TYPE(op)));
  if (!scope_check_not_pinned()) {
    return NULL;
  }
  static char *names[] = {"publish", NULL};
  int publish;
  if (!PyArg_ParseTupleAndKeywords(args, kwargs, "p:finish", names, &publish)) {
    return NULL;
  }
  return scope_finish(op, publish);
}

/// @brief Own draining and finalization until output users and host reservations are released
static PyObject *ScopeCapsule_exit(PyObject *op, PyObject *args) {
  scope_capsule_type(PyType_GetModule(Py_TYPE(op)));
  if (!scope_check_not_pinned()) {
    return NULL;
  }
  int publish;
  PyObject *boc_export;
  if (!PyArg_ParseTuple(args, "pO:exit", &publish, &boc_export)) {
    return NULL;
  }
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  if (!scope_check_host(self)) {
    return NULL;
  }
  bool pumpable = bocpy_interpid() == bocpy_main_interpid() && !IN_PUMP_BODY;
  if (pumpable && boc_export == Py_None) {
    PyErr_SetString(PyExc_RuntimeError, "scope exit requires a behavior resolver");
    return NULL;
  }
  atomic_store(&self->owner->state, BOC_SCOPE_DRAINING);
  self->waiting = true;
  PyObject *drained = terminator_wait_driver(self->owner->term, true, 0, true,
                                            boc_export, pumpable, self->owner);
  self->waiting = false;
  Py_XDECREF(drained);
  PyObject *interrupted = PyErr_GetRaisedException();
  PyObject *result = scope_finish(op, publish && interrupted == NULL);
  if (interrupted != NULL) {
    if (result == NULL) {
      PyObject *error = PyErr_GetRaisedException();
      if (error != interrupted) {
        PyException_SetContext(error, interrupted);
      } else {
        Py_DECREF(interrupted);
      }
      PyErr_SetRaisedException(error);
    } else {
      Py_CLEAR(result);
      PyErr_SetRaisedException(interrupted);
    }
  }
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

typedef struct {
  BOCScopeOwner *owner;
  BOCCown *output;
} BOCScopeTransfer;

/// @brief Release a scope transfer after all receiving wrappers are independent
static void scope_transfer_free(void *data) {
  BOCScopeTransfer *transfer = data;
  COWN_DECREF(transfer->output);
  scope_owner_decref(transfer->owner);
  PyMem_RawFree(transfer);
}

/// @brief Reconstruct shared scheduling identity and its output dependency
static PyObject *scope_capsule_wrap_output(BOCScopeOwner *owner,
                                           BOCCown *output) {
  PyTypeObject *type = scope_capsule_type(NULL);
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)type->tp_alloc(type, 0);
  if (self == NULL) {
    return NULL;
  }
  self->owner = owner;
  scope_owner_incref(owner);
  if (output != NULL) {
    self->output = output;
    COWN_INCREF(output);
  }
  return (PyObject *)self;
}

static PyObject *_new_scope_object(XIDATA_T *xidata) {
  BOCScopeTransfer *transfer = xidata->data;
  return scope_capsule_wrap_output(transfer->owner, transfer->output);
}

XIDATA_GETDATA_FUNC(_scope_shared) {
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)obj;
  if (self->output == NULL) {
    PyErr_SetString(PyExc_TypeError,
                    "scheduling-only scope capsule has no output dependency");
    return -1;
  }
  BOCScopeTransfer *transfer = PyMem_RawMalloc(sizeof(*transfer));
  if (transfer == NULL) {
    return PyErr_NoMemory(), -1;
  }
  transfer->owner = self->owner;
  transfer->output = self->output;
  scope_owner_incref(transfer->owner);
  COWN_INCREF(transfer->output);
  XIDATA_INIT(xidata, tstate->interp, transfer, obj, _new_scope_object);
  XIDATA_SET_FREE(xidata, scope_transfer_free);
  return 0;
}

/// @brief Transfer scope identity through its retained output cown
static PyObject *ScopeCapsule_reduce(PyObject *op, PyObject *Py_UNUSED(args)) {
  PyErr_SetString(PyExc_TypeError,
                  "scope capsules cannot be pickled outside bocpy-managed transfers");
  return NULL;
}

/// @brief Reconstruct a scheduling-only handle from its retained output cown
static PyObject *_scope_from_output(PyObject *module, PyObject *args) {
  scope_capsule_type(module);
  PyObject *output;
  if (!PyArg_ParseTuple(args, "O", &output)) {
    return NULL;
  }
  BOCCown *cown = cown_unwrap(output);
  if (cown == NULL) {
    return NULL;
  }
  BOCScopeOwner *owner = cown_scope_owner(cown);
  if (owner == NULL) {
    PyErr_SetString(PyExc_TypeError, "scope output cown has no scope owner");
    return NULL;
  }
  return scope_capsule_wrap_output(owner, cown);
}

/// @brief Expose a retained output cown to bocpy's managed pickler
static PyObject *_scope_capsule_output(PyObject *module, PyObject *op) {
  PyTypeObject *type = scope_capsule_type(module);
  if (!Py_IS_TYPE(op, type)) {
    PyErr_SetString(PyExc_TypeError, "expected ScopeCapsule");
    return NULL;
  }
  ScopeCapsuleObject *self = (ScopeCapsuleObject *)op;
  if (self->output == NULL) {
    PyErr_SetString(PyExc_TypeError,
                    "scheduling-only scope capsules must be transferred with their output cown");
    return NULL;
  }
  if (nb_pickle_borrowing()) {
    BOCNativeRef ref = {self->output, &BOC_COWN_REF_OPS};
    if (nb_pickle_retain(ref) < 0) {
      return NULL;
    }
  }
  return cown_scope_capsule_ref(self->output);
}

static PyMethodDef ScopeCapsule_methods[] = {
    {"quiescence", (PyCFunction)ScopeCapsule_quiescence,
     METH_VARARGS | METH_KEYWORDS, "Wait for local quiescence"},
    {"enter", ScopeCapsule_enter, METH_NOARGS, NULL},
    {"close", ScopeCapsule_close, METH_NOARGS, NULL},
    {"wait", (PyCFunction)ScopeCapsule_wait, METH_VARARGS | METH_KEYWORDS, NULL},
    {"finish", (PyCFunction)ScopeCapsule_finish, METH_VARARGS | METH_KEYWORDS, NULL},
    {"exit", ScopeCapsule_exit, METH_VARARGS, NULL},
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
    {"_scope_from_output", _scope_from_output, METH_VARARGS, NULL},
    {"_scope_capsule_output", _scope_capsule_output, METH_O, NULL},
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
