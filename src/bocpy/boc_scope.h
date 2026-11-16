/// @file boc_scope.h
/// @brief Private scope ownership and cown integration

#ifndef BOCPY_SCOPE_H
#define BOCPY_SCOPE_H

#include "boc_compat.h"
#include "boc_cown.h"
#include "boc_terminator.h"

/// @brief Single-use scope lifecycle, independent of work and reference counts
typedef enum {
  BOC_SCOPE_CREATED,
  BOC_SCOPE_ENTERING,
  BOC_SCOPE_ACTIVE,
  BOC_SCOPE_DRAINING,
  BOC_SCOPE_EXTRACTING,
  BOC_SCOPE_CLOSED,
} BOCScopeState;

/// @brief Shared scope identity; reference lifetime never reserves execution
typedef struct boc_scope_owner {
  atomic_int_least64_t refs;
  BOCTerminator *term;
  BOCTerminator *root;
  struct boc_scope_owner *parent;
  /// @brief ACTIVE publishes initialization; EXTRACTING excludes reentrant host operations
  atomic_int_least64_t state;
} BOCScopeOwner;

extern thread_local BOCScopeOwner *CURRENT_SCOPE;
extern thread_local BOCScopeOwner *EXECUTING_SCOPE;

bool scope_owner_accepts_work(BOCScopeOwner *owner);
void scope_owner_incref(BOCScopeOwner *owner);
void scope_owner_decref(BOCScopeOwner *owner);
bool scope_owner_contains(BOCScopeOwner *ancestor, BOCScopeOwner *owner);
BOCScopeOwner *scope_capsule_owner(PyObject *op);
PyTypeObject *scope_capsule_register(PyObject *module);

/// @brief Select module state when non-NULL and return its interpreter-local scope type
PyTypeObject *scope_capsule_type(PyObject *module);
/// @brief Reject an output already bound, pinned or scheduled
bool cown_scope_available(BOCCown *cown);
/// @brief Release the initial value and retain the scope identity on its output cown
int cown_scope_bind(BOCCown *cown, BOCScopeOwner *owner);
/// @brief Move or discard output after its scope has fenced admission
PyObject *cown_scope_finish(BOCCown *output, bool publish);
BOCCown *cown_unwrap(PyObject *op);

#endif // BOCPY_SCOPE_H
