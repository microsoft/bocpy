/// @file boc_execution.h
/// @brief Private execution context and native wait coordination

#ifndef BOCPY_EXECUTION_H
#define BOCPY_EXECUTION_H

#include "boc_compat.h"
#include "boc_terminator.h"
#include "boc_scope.h"
#include <Python.h>

extern thread_local bool IN_PUMP_BODY;
extern thread_local uint64_t CURRENT_EXECUTION;
extern atomic_int_least64_t NEXT_EXECUTION;
extern atomic_int_least64_t WAIT_PUMP_POLL_MS;

bool wait_arguments(PyObject *args, PyObject *kwargs, double *end,
                    bool *forever, PyObject **boc_export);
PyObject *terminator_wait_driver(BOCTerminator *term, bool drain, double end, bool forever,
                                 PyObject *boc_export, bool pumpable,
                                 BOCScopeOwner *host);

uint64_t _core_pinned_depth_load(void);

typedef enum {
  BOC_PUMP_ERROR = -1,
  BOC_PUMP_OK,
  BOC_PUMP_BUSY,
} BOCPumpStatus;

typedef struct {
  Py_ssize_t executed;
  Py_ssize_t raised;
  bool deadline_reached;
} BOCPumpResult;

BOCPumpStatus main_pump_bounded_impl(uint64_t deadline_ns, long long max_behaviors,
                                    bool raise_on_error, PyObject *boc_export,
                                    BOCPumpResult *result);

#endif // BOCPY_EXECUTION_H
