/// @file tags.h
/// @brief Message-tag table API shared between TUs.
///
/// A `BOCTag` names a message stream and pins one of the 16 fixed
/// `BOCQueue` slots in the global queue table. Tags are reference
/// counted so they can be cached on the per-interpreter
/// @c _core_module_state.queue_tags[] array without leaking the queue
/// slot. Hot-path operations (incref / decref / disable check) are
/// `static inline` so every TU that includes this header gets the same
/// inlined code as the original `_core.c`.
///
/// @note `BOCQueue` itself stays opaque here — `BOCTag` only stores a
/// `BOCQueue *` and never reaches into the queue body.

#ifndef BOCPY_TAGS_H
#define BOCPY_TAGS_H

#define PY_SSIZE_T_CLEAN

#include <Python.h>

#include "compat.h"

/// @brief Forward declaration. Body defined in `_core.c` (later
/// `message_queue.h`); tags only carry a pointer.
typedef struct boc_queue BOCQueue;

/// @brief A tag for a BOC message.
typedef struct boc_tag {
  /// @brief The UTF-8 string value of the tag
  char *str;
  /// @brief The number of bytes in str (not including the NULL)
  Py_ssize_t size;
  /// @brief A pointer to the queue that this tag is associated with
  BOCQueue *queue;
  atomic_int_least64_t rc;
  atomic_int_least64_t disabled;
} BOCTag;

/// @brief Creates a new BOCTag object from a Python Unicode string.
/// @details The result object will not be dependent on the argument in any way
/// (i.e., it can be safely deallocated). On success the returned tag has
/// reference count 1; the caller owns one reference and must arrange for
/// it to be released via @c TAG_DECREF (or @c BOCTag_free for non-rc
/// owners such as the @c BehaviorCapsule thunk path) when no longer
/// needed. On failure (non-str argument, UTF-8 encoding error, OOM)
/// returns NULL with a Python exception set; no partial state is left
/// behind.
/// @param unicode A PyUnicode object
/// @param queue The queue to associate with this tag
/// @return a new BOCTag object with rc=1, or NULL on failure
BOCTag *tag_from_PyUnicode(PyObject *unicode, BOCQueue *queue);

/// @brief Converts a BOCTag to a PyUnicode object.
/// @note This method uses PyUnicode_FromStringAndSize() internally.
/// @param tag The tag to convert
/// @return A new reference to a PyUnicode object.
PyObject *tag_to_PyUnicode(BOCTag *tag);

/// @brief Frees a BOCTag object and any associated memory.
/// @param tag The tag to free
void BOCTag_free(BOCTag *tag);

/// @brief Compares a BOCTag with a UTF8 string.
/// @details -1 if the tag should be placed before, 1 if after, 0 if equivalent
/// @param lhs The BOCtag to compare
/// @param rhs_str The string to compare with
/// @param rhs_size The length of the comparison string
/// @return -1 if before, 1 if after, 0 if equivalent
int tag_compare_with_utf8(BOCTag *lhs, const char *rhs_str,
                          Py_ssize_t rhs_size);

/// @brief Compares a BOCTag with a PyUnicode object.
/// @details -1 if the tag should be placed before, 1 if after, 0 if equivalent
/// @param lhs The BOCtag to compare
/// @param rhs_op The PyUnicode to compare with
/// @return -1 if before, 1 if after, 0 if equivalent. -2 on error.
int tag_compare_with_PyUnicode(BOCTag *lhs, PyObject *rhs_op);

// ---------------------------------------------------------------------------
// Hot-path inlines.
//
// These were `static` in `_core.c` and called via the TAG_INCREF /
// TAG_DECREF macros on the send / receive / set_tags paths. Promoting
// them to `static inline` in this header preserves the inlining when
// the macros are used from any including TU (and matches CPython's
// `Py_INCREF` / `Py_DECREF` header-inline pattern).
// ---------------------------------------------------------------------------

static inline int_least64_t tag_decref(BOCTag *tag) {
  int_least64_t rc = atomic_fetch_add(&tag->rc, -1) - 1;
  if (rc == 0) {
    BOCTag_free(tag);
  }

  return rc;
}

#define TAG_DECREF(t) tag_decref(t)

static inline int_least64_t tag_incref(BOCTag *tag) {
  return atomic_fetch_add(&tag->rc, 1) + 1;
}

#define TAG_INCREF(t) tag_incref(t)

static inline bool tag_is_disabled(BOCTag *tag) {
  return atomic_load(&tag->disabled);
}

static inline void tag_disable(BOCTag *tag) { atomic_store(&tag->disabled, 1); }

#endif // BOCPY_TAGS_H
