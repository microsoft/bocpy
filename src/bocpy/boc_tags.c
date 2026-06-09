/// @file boc_tags.c
/// @brief Out-of-line implementations for the message-tag API.
///
/// Hot-path operations (incref / decref / disable check) are
/// `static inline` in `boc_tags.h`; this TU houses the cold helpers
/// (alloc / free / unicode bridges / comparisons).

#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <string.h>

#include "boc_tags.h"

BOCTag *tag_from_PyUnicode(PyObject *unicode, BOCQueue *queue) {
  if (!PyUnicode_CheckExact(unicode)) {
    PyErr_SetString(PyExc_TypeError, "Must be a str");
    return NULL;
  }

  BOCTag *tag = (BOCTag *)PyMem_RawMalloc(sizeof(BOCTag));
  if (tag == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  Py_ssize_t size = -1;
  const char *str = PyUnicode_AsUTF8AndSize(unicode, &size);
  if (str == NULL) {
    PyMem_RawFree(tag);
    return NULL;
  }

  tag->size = size;
  tag->str = (char *)PyMem_RawMalloc(tag->size + 1);

  if (tag->str == NULL) {
    PyErr_NoMemory();
    PyMem_RawFree(tag);
    return NULL;
  }

  memcpy(tag->str, str, tag->size + 1);
  tag->queue = queue;
  atomic_store(&tag->rc, 1);
  atomic_store(&tag->disabled, 0);

  return tag;
}

PyObject *tag_to_PyUnicode(BOCTag *tag) {
  return PyUnicode_FromStringAndSize(tag->str, tag->size);
}

void BOCTag_free(BOCTag *tag) {
  PyMem_RawFree(tag->str);
  PyMem_RawFree(tag);
}

int tag_compare_with_utf8(BOCTag *lhs, const char *rhs_str,
                          Py_ssize_t rhs_size) {
  Py_ssize_t size = lhs->size < rhs_size ? lhs->size : rhs_size;
  char *lhs_ptr = lhs->str;
  const char *rhs_ptr = rhs_str;
  for (Py_ssize_t i = 0; i < size; ++i, ++lhs_ptr, ++rhs_ptr) {
    int8_t a = (int8_t)(*lhs_ptr);
    int8_t b = (int8_t)(*rhs_ptr);

    if (a < b) {
      return -1;
    }
    if (a > b) {
      return 1;
    }
  }

  if (lhs->size < rhs_size) {
    return -1;
  }

  if (lhs->size > rhs_size) {
    return 1;
  }

  return 0;
}

int tag_compare_with_PyUnicode(BOCTag *lhs, PyObject *rhs_op) {
  if (!PyUnicode_CheckExact(rhs_op)) {
    PyErr_SetString(PyExc_TypeError, "Must be a str");
    return -2;
  }

  Py_ssize_t rhs_size = -1;
  const char *rhs_str = PyUnicode_AsUTF8AndSize(rhs_op, &rhs_size);
  if (rhs_str == NULL) {
    return -2;
  }

  return tag_compare_with_utf8(lhs, rhs_str, rhs_size);
}
