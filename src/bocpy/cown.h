/// @file cown.h
/// @brief Minimal cross-TU surface for the cown refcount API.
///
/// This header exists so that translation units other than `_core.c`
/// (for now: `noticeboard.c`) can hold strong references to a
/// `BOCCown` without needing to know its layout. The full struct
/// definition and the implementation of @ref cown_incref / @ref
/// cown_decref live in `_core.c`. The per-call cost of the indirect
/// call at noticeboard call sites is negligible: every noticeboard
/// mutation already takes a mutex and performs XIData serialization,
/// both orders of magnitude more expensive than the indirect call.

#ifndef BOCPY_COWN_H
#define BOCPY_COWN_H

#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <stdint.h>

/// @brief Opaque forward declaration. The struct body lives in `_core.c`.
typedef struct boc_cown BOCCown;

/// @brief Python wrapper exposing a single @ref BOCCown to user code.
typedef struct cown_capsule_object {
  PyObject_HEAD BOCCown *cown;
} CownCapsuleObject;

/// @brief Acquire one strong reference on @p cown.
/// @return The post-increment refcount.
int_least64_t cown_incref(BOCCown *cown);

/// @brief Release one strong reference on @p cown.
/// @return The post-decrement refcount.
int_least64_t cown_decref(BOCCown *cown);

#define COWN_INCREF(c) cown_incref((c))
#define COWN_DECREF(c) cown_decref(c)

#endif // BOCPY_COWN_H
