#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <stdbool.h>
#include <stdint.h>
#include <time.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
typedef volatile int_least64_t atomic_int_least64_t;
typedef volatile intptr_t atomic_intptr_t;

int_least64_t atomic_fetch_add(atomic_int_least64_t *ptr, int_least64_t value) {
  return InterlockedExchangeAdd64(ptr, value);
}

int_least64_t atomic_fetch_sub(atomic_int_least64_t *ptr, int_least64_t value) {
  return InterlockedExchangeAdd64(ptr, -value);
}

bool atomic_compare_exchange_strong(atomic_int_least64_t *ptr,
                                    atomic_int_least64_t *expected,
                                    int_least64_t desired) {
  int_least64_t prev;
  prev = InterlockedCompareExchange64(ptr, desired, *expected);
  if (prev == *expected) {
    return true;
  }

  *expected = prev;
  return false;
}

int_least64_t atomic_load(atomic_int_least64_t *ptr) { return *ptr; }

int_least64_t atomic_exchange(atomic_int_least64_t *ptr, int_least64_t value) {
  return InterlockedExchange64(ptr, value);
}

void atomic_store(atomic_int_least64_t *ptr, int_least64_t value) {
  *ptr = value;
}

#define thread_local __declspec(thread)

#else
#include <stdatomic.h>
#endif

#if defined __APPLE__
#define thrd_sleep nanosleep
#define thread_local _Thread_local
#elif defined _WIN32
void thrd_sleep(const struct timespec *duration, struct timespec *remaining) {
  const DWORD MS_PER_NS = 1000000;
  DWORD ms = (DWORD)duration->tv_sec * 1000;
  ms += (DWORD)duration->tv_nsec / MS_PER_NS;
  Sleep(ms);
}
#else
#include <threads.h>
#endif

#if PY_VERSION_HEX >= 0x030D0000
#define Py_BUILD_CORE
#include <internal/pycore_crossinterp.h>
#endif

const struct timespec SLEEP_TS = {0, 1000};
const char *BOC_TIMEOUT = "__timeout__";
const int BOC_CAPACITY = 1024 * 16;
const PY_INT64_T NO_OWNER = -2;
atomic_int_least64_t BOC_COUNT = 0;
atomic_int_least64_t BOC_COWN_COUNT = 0;

// #define BOC_REF_TRACKING
// #define BOC_TRACE

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

static bool xidata_supported(PyObject *op) {
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

static void xidata_set_free(XIDATA_T *xidata, void (*freefunc)(void *)) {
  xidata->free = freefunc;
}

static bool xidata_supported(PyObject *op) {
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

static XIDATA_T *xidata_new() {
  XIDATA_T *xidata = (XIDATA_T *)PyMem_RawMalloc(sizeof(XIDATA_T));
  xidata->data = NULL;
  xidata->free = NULL;
  xidata->interp = -1;
  xidata->new_object = NULL;
  xidata->obj = NULL;
  return xidata;
}

static void xidata_set_free(XIDATA_T *xidata, void (*freefunc)(void *)) {
  xidata->free = freefunc;
}

static bool xidata_supported(PyObject *op) {
  crossinterpdatafunc getdata = _PyCrossInterpreterData_Lookup(op);
  return getdata != NULL;
}

static void xidata_free(void *arg) {
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

static void xidata_set_free(XIDATA_T *xidata, void (*freefunc)(void *)) {
  xidata->free = freefunc;
}

static void xidata_free(void *arg) {
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

static XIDATA_T *xidata_new() {
  XIDATA_T *xidata = (XIDATA_T *)PyMem_RawMalloc(sizeof(XIDATA_T));
  xidata->data = NULL;
  xidata->free = NULL;
  xidata->interp = -1;
  xidata->new_object = NULL;
  xidata->obj = NULL;
  return xidata;
}

static void xidata_init(XIDATA_T *data, PyInterpreterState *interp,
                        void *shared, PyObject *obj,
                        PyObject *(*new_object)(_PyCrossInterpreterData *)) {
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

static bool xidata_supported(PyObject *op) {
  crossinterpdatafunc getdata = _PyCrossInterpreterData_Lookup(op);
  return getdata != NULL;
}

PyObject *PyErr_GetRaisedException(void) {
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

/// @brief Note in a RecycleQueue.
typedef struct boc_recycle_node {
  /// @brief XIData to free on the source interpreter
  XIDATA_T *xidata;
  /// @brief The next node in the queue
  atomic_intptr_t next;
} BOCRecycleNode;

/// @brief A simple Multi-Producer, Single Consumer (MPSC) queue for use in
/// recycling XIData objects.
typedef struct boc_recycle_queue {
  int_least64_t index;
  /// @brief Pointer to the current head (where nodes will be written)
  atomic_intptr_t head;
  /// @brief Pointer to the current tail (where nodes will be read)
  BOCRecycleNode *tail;
  /// @brief The next RecycleQueue in the list (for use in cleanup)
  atomic_intptr_t next;
  /// @brief Dictionary mapping xidata to cowns
  PyObject *xidata_to_cowns;
} BOCRecycleQueue;

struct boc_tag;

/// @brief A message sent via send()
typedef struct boc_message {
  /// @brief The tag associated with this message.
  /// @details This will be used by processes calling receive() and will create
  /// an affinity with a queue.
  struct boc_tag *tag;
  /// @brief whether the contents of this message were pickled
  bool pickled;
  /// @brief the threadsafe cross-interpreter data (the contents of the message)
  XIDATA_T *xidata;
  /// @brief The recycle tag of the interpreter which created this message.
  /// @details Used to route it back for GC (and thus avoiding waiting on the
  /// GIL of the source interpreter).
  BOCRecycleQueue *recycle_queue;
} BOCMessage;

/// @brief A message queue
typedef struct boc_queue {
  int_least64_t index;
  /// @brief the statically allocated array of messages
  BOCMessage **messages;
  /// @brief index of the head message (monotonically increasing)
  atomic_int_least64_t head;
  /// @brief index of the tail message (monotonically increasing)
  atomic_int_least64_t tail;

  /// @brief Whether the queue is unassigned, assigned, or disabled
  atomic_int_least64_t state;
  /// @brief tag assigned to this queue.
  /// @details Messages which are sent with this tag will be assigned to this
  /// queue. Calls to receive on the tag will attempt to dequeue from this
  /// queue.
  atomic_intptr_t tag; // (BOCTag *)
} BOCQueue;

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

#define BOC_QUEUE_COUNT 16
const int_least64_t BOC_QUEUE_UNASSIGNED = 0;
const int_least64_t BOC_QUEUE_ASSIGNED = 1;
const int_least64_t BOC_QUEUE_DISABLED = 2;
static BOCQueue BOC_QUEUES[BOC_QUEUE_COUNT];
static BOCRecycleQueue *BOC_RECYCLE_QUEUE_TAIL = NULL;
static atomic_intptr_t BOC_RECYCLE_QUEUE_HEAD = 0;

/// @brief Creates a new BOCTag object from a Python Unicode string.
/// @details The result object will not be dependent on the argument in any way
/// (i.e., it can be safely deallocated).
/// @param unicode A PyUnicode object
/// @param queue The queue to associate with this tag
/// @return a new BOCTag object
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

  const char *str = PyUnicode_AsUTF8AndSize(unicode, &tag->size);
  if (str == NULL) {
    return NULL;
  }

  tag->str = (char *)PyMem_RawMalloc(tag->size + 1);

  if (tag->str == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  memcpy(tag->str, str, tag->size + 1);
  tag->queue = queue;
  atomic_store(&tag->rc, 0);
  atomic_store(&tag->disabled, 0);

  return tag;
}

/// @brief Converts a BOCTag to a PyUnicode object.
/// @note This method uses PyUnicode_FromStringAndSize() internally.
/// @param tag The tag to convert
/// @return A new reference to a PyUnicode object.
PyObject *tag_to_PyUnicode(BOCTag *tag) {
  return PyUnicode_FromStringAndSize(tag->str, tag->size);
}

/// @brief Frees a BOCTag object and any associated memory.
/// @param tag The tag to free
void BOCTag_free(BOCTag *tag) {
  PyMem_RawFree(tag->str);
  PyMem_RawFree(tag);
}

static int_least64_t tag_decref(BOCTag *tag) {
  int_least64_t rc = atomic_fetch_add(&tag->rc, -1) - 1;
  if (rc == 0) {
    BOCTag_free(tag);
  }

  return rc;
}

#define TAG_DECREF(t) tag_decref(t)

static int_least64_t tag_incref(BOCTag *tag) {
  return atomic_fetch_add(&tag->rc, 1) + 1;
}

#define TAG_INCREF(t) tag_incref(t)

bool tag_is_disabled(BOCTag *tag) { return atomic_load(&tag->disabled); }

void tag_disable(BOCTag *tag) { atomic_store(&tag->disabled, 1); }

/// @brief Compares a BOCTag with a UTF8 string.
/// @details -1 if the tag should be placed before, 1 if after, 0 if equivalent
/// @param lhs The BOCtag to compare
/// @param rhs_str The string to compare with
/// @param rhs_size The length of the comparison string
/// @return -1 if before, 1 if after, 0 if equivalent
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

/// @brief Compares a BOCTag with a PyUnicode object.
/// @details -1 if the tag should be placed before, 1 if after, 0 if equivalent
/// @param lhs The BOCtag to compare
/// @param rhs_str The string to compare with
/// @param rhs_size The length of the comparison string
/// @return -1 if before, 1 if after, 0 if equivalent
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

/// @brief State for the module.
typedef struct boc_state {
  /// @brief The index (monotonically increasing) for this module.
  int_least64_t index;
  /// @brief The unique recycle tag for this module. Used for recycling
  /// messages.
  BOCRecycleQueue *recycle_queue;
  /// @brief Cached reference to the pickle module
  PyObject *pickle;
  /// @brief Cached reference to the dumps function in the pickle module
  PyObject *dumps;
  /// @brief Cached reference to the loads function in the pickle module
  PyObject *loads;
  PyTypeObject *cown_capsule_type;
  PyTypeObject *behavior_capsule_type;
  /// @brief PyUnicode objects indicating the string associated with each of the
  /// queues.
  BOCTag *queue_tags[BOC_QUEUE_COUNT];
} _core_module_state;

static thread_local _core_module_state *BOC_STATE;

#define BOC_STATE_SET(m)                                                       \
  do {                                                                         \
    BOC_STATE = (_core_module_state *)PyModule_GetState(m);                    \
  } while (0)

#ifdef BOC_TRACE
static inline void print_obj_debug(PyObject *obj) {
  PyObject_Print(obj, stdout, 0);
}

static inline void print_debug(char *fmt, ...) {
  char debug_fmt[1024];
  va_list args;

  sprintf(debug_fmt, "%" PRIdLEAST64 ": %s", BOC_STATE->index, fmt);
  va_start(args, fmt);
  vprintf(debug_fmt, args);
  va_end(args);
}

char BOC_DEBUG_FMT_BUF[1024];

#define PRINTFDBG printf
#define PRINTDBG print_debug
#define PRINTOBJDBG print_obj_debug
#else
#define PRINTFDBG(...)
#define PRINTDBG(...)
#define PRINTOBJDBG(...)
#endif

#ifdef BOC_REF_TRACKING
atomic_int_least64_t BOC_ACTIVE_COWNS = 0;
atomic_int_least64_t BOC_TOTAL_COWNS = 0;
atomic_int_least64_t BOC_ACTIVE_BEHAVIORS = 0;
atomic_int_least64_t BOC_TOTAL_BEHAVIORS = 0;
struct timespec BOC_LAST_REF_TRACKING_REPORT;

static void boc_ref_tracking_report(const char *prefix) {
  struct timespec ts;
  timespec_get(&ts, TIME_UTC);
  if (ts.tv_sec - BOC_LAST_REF_TRACKING_REPORT.tv_sec > 1) {
    int_least64_t alive = atomic_load(&BOC_ACTIVE_COWNS);
    int_least64_t total = atomic_load(&BOC_TOTAL_COWNS);
    printf("%s%" PRIdLEAST64 ": cowns=(%" PRIdLEAST64 "/%" PRIdLEAST64 ")\n",
           prefix, ts.tv_sec, alive, total);
    alive = atomic_load(&BOC_ACTIVE_BEHAVIORS);
    total = atomic_load(&BOC_TOTAL_BEHAVIORS);
    printf("%s%" PRIdLEAST64 ": behaviors=(%" PRIdLEAST64 "/%" PRIdLEAST64
           ")\n",
           prefix, ts.tv_sec, alive, total);
    BOC_LAST_REF_TRACKING_REPORT = ts;
  }
}

static void boc_ref_tracking(bool is_cown, int_least64_t delta) {
  if (is_cown) {
    atomic_fetch_add(&BOC_ACTIVE_COWNS, delta);
    if (delta > 0) {
      atomic_fetch_add(&BOC_TOTAL_COWNS, delta);
    }
  } else {
    atomic_fetch_add(&BOC_ACTIVE_BEHAVIORS, delta);
    if (delta > 0) {
      atomic_fetch_add(&BOC_TOTAL_BEHAVIORS, delta);
    }
  }

  if (BOC_STATE->index == 0) {
    boc_ref_tracking_report("");
  }
}

#define BOC_REF_TRACKING_ADD_COWN() boc_ref_tracking(true, 1)
#define BOC_REF_TRACKING_REMOVE_COWN() boc_ref_tracking(true, -1)
#define BOC_REF_TRACKING_ADD_BEHAVIOR() boc_ref_tracking(false, 1)
#define BOC_REF_TRACKING_REMOVE_BEHAVIOR() boc_ref_tracking(false, -1)
#define BOC_REF_TRACKING_REPORT() boc_ref_tracking_report("final")
#else
#define BOC_REF_TRACKING_ADD_COWN(...)
#define BOC_REF_TRACKING_REMOVE_COWN(...)
#define BOC_REF_TRACKING_ADD_BEHAVIOR(...)
#define BOC_REF_TRACKING_REMOVE_BEHAVIOR(...)
#define BOC_REF_TRACKING_REPORT(...)
#endif

/// @brief Convenience method to obtain the interpreter ID
/// @return the ID of the currently running interpreter
static inline PY_INT64_T get_interpid() {
  PyThreadState *ts = PyThreadState_GET();
  PyInterpreterState *is = PyThreadState_GetInterpreter(ts);
  return PyInterpreterState_GetID(is);
}

/// @brief Allocates a new MPSC RecycleQueue.
/// @details This will add it to the queue list and then return the reference.
/// @return A new RecycleQueue
static BOCRecycleQueue *BOCRecycleQueue_new(int_least64_t index) {
  BOCRecycleQueue *queue = PyMem_RawMalloc(sizeof(BOCRecycleQueue));
  if (queue == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  // this is both the stub and the allocated space for the next item
  BOCRecycleNode *node =
      (BOCRecycleNode *)PyMem_RawMalloc(sizeof(BOCRecycleNode));
  if (node == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  node->next = 0;
  node->xidata = NULL;
  intptr_t node_ptr = (intptr_t)node;

  queue->xidata_to_cowns = NULL;
  queue->head = 0;
  queue->tail = NULL;
  queue->next = 0;

  intptr_t queue_ptr = (intptr_t)queue;
  intptr_t old_head_ptr = atomic_exchange(&BOC_RECYCLE_QUEUE_HEAD, queue_ptr);
  if (old_head_ptr == 0) {
    return queue;
  }

  BOCRecycleQueue *old_head = (BOCRecycleQueue *)old_head_ptr;
  old_head->index = index;
  old_head->tail = node;
  atomic_store(&old_head->head, node_ptr);
  atomic_store(&old_head->next, queue_ptr);

  old_head->xidata_to_cowns = PyDict_New();
  if (old_head->xidata_to_cowns == NULL) {
    return NULL;
  }

  return old_head;
}

static PyObject *_PyPickle_Dumps(PyObject *obj) {
  PyObject *bytes = PyObject_CallOneArg(BOC_STATE->dumps, obj);
  return bytes;
}

static PyObject *_PyPickle_Loads(PyObject *bytes) {
  PyObject *obj = PyObject_CallOneArg(BOC_STATE->loads, bytes);
  return obj;
}

/// @brief Deserializes a value from cross-interpereter data.
/// @param module The _core module
/// @param xidata The xidata containing the value
/// @param pickled Whether the value is pickled
/// @return A new instance of the object
static PyObject *xidata_to_object(XIDATA_T *xidata, bool pickled) {
  assert(xidata != NULL);
  PyObject *value = XIDATA_NEWOBJECT(xidata);
  if (value == NULL) {
    return NULL;
  }

  if (!pickled) {
    return value;
  }

  PyObject *bytes = value;
  value = _PyPickle_Loads(bytes);
  if (value == NULL) {
    Py_DECREF(bytes);
    return NULL;
  }

  Py_DECREF(bytes);

  return value;
}

/// @brief Serializes an object as xidata
/// @param module The _core module
/// @param contents The value to serialize
/// @param xidata_ptr A pointer to the xidata pointer (will contain the
/// allocated xidata object on success)
/// @return True if pickling was required, False if not, NULL if there was an
/// error
static PyObject *object_to_xidata(PyObject *value, XIDATA_T **xidata_ptr) {
  if (*xidata_ptr == NULL) {
    *xidata_ptr = XIDATA_NEW();
  }

  XIDATA_T *xidata = *xidata_ptr;

  if (xidata == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  xidata->data = NULL;
  xidata->obj = NULL;

  if (XIDATA_GETXIDATA(value, xidata) == 0) {
    Py_RETURN_FALSE;
  }

  PyErr_Clear();

  // no native support, fallback to pickle
  PyObject *bytes = _PyPickle_Dumps(value);
  if (bytes == NULL) {
    return NULL;
  }

  if (XIDATA_GETXIDATA(bytes, xidata) == 0) {
    Py_DECREF(bytes);
    Py_RETURN_TRUE;
  }

  Py_DECREF(bytes);

  PyErr_SetString(PyExc_RuntimeError,
                  "Unable to convert contents to cross-interpreter data");
  return NULL;
}

/// @brief The threadsafe cown object.
/// @details This can be safely referenced and used from multiple processes.
typedef struct boc_cown {
  int_least64_t id;
  /// @brief The python object held in this cown.
  /// @details This is only non-NULL when the cown is acquired.
  PyObject *value;
  /// @brief Whether the value is pickled when serialized
  bool pickled;
  /// @brief Whether the cown holds an exception object
  bool exception;
  /// @brief the threadsafe serialized cown contents
  XIDATA_T *xidata;
  /// @brief the module which last released this cown
  BOCRecycleQueue *recycle_queue;
  /// @brief The ID of the interpreter that currently has acquired this cown.
  atomic_int_least64_t owner;
  /// @brief The last behavior which needs to acquire this cown
  atomic_intptr_t last; // (BOCBehavior *)
  /// @brief Atomic reference count for the cown
  atomic_int_least64_t rc;
  /// @brief Atomic weak reference count for the cown
  atomic_int_least64_t weak_rc;
} BOCCown;

static inline int_least64_t cown_weak_decref(BOCCown *cown) {
  int_least64_t weak_rc = atomic_fetch_add(&cown->weak_rc, -1) - 1;
  PRINTDBG("cown_weak_decref(%p, cid=%" PRIdLEAST64 ") = %" PRIdLEAST64 "\n",
           cown, cown->id, weak_rc);

  if (weak_rc == 0) {
    // reference count is truly zero, we can free the memory
    PyMem_RawFree(cown);
    BOC_REF_TRACKING_REMOVE_COWN();
  }

  return weak_rc;
}

static inline void report_unhandled_exception(BOCCown *cown) {
  fprintf(stderr, "Cown(%p) contains an unhandled exception: ", cown);

  if (cown->value != NULL) {
    PyObject_Print(cown->value, stderr, 0);
    fprintf(stderr, "\n");
    return;
  }

  if (cown->xidata == NULL) {
    fprintf(stderr,
            "<fatal error: value and xidata are NULL on exception cown>\n");
    return;
  }

  cown->value = xidata_to_object(cown->xidata, cown->pickled);

  if (cown->value == NULL) {
    PyErr_Clear();
    fprintf(stderr, "<fatal error: unable to deserialize exception>\n");
    return;
  }

  PyObject_Print(cown->value, stderr, 0);
  fprintf(stderr, "\n");
  return;
}

static void BOCRecycleQueue_enqueue(BOCRecycleQueue *queue, XIDATA_T *xidata);

/// @brief Atomic decref for the cown
/// @param cown the cown to decref
/// @return the new reference count
static inline int_least64_t cown_decref(BOCCown *cown) {
  int_least64_t rc = atomic_fetch_add(&cown->rc, -1) - 1;
  PRINTDBG("cown_decref(%p, cid=%" PRIdLEAST64 ") = %" PRIdLEAST64 "\n", cown,
           cown->id, rc);
  if (rc != 0) {
    return rc;
  }

  PRINTDBG("cleaning cown\n");

  if (cown->exception) {
    report_unhandled_exception(cown);
  }

  // we can clear the object and recycle the xidata
  if (cown->value != NULL) {
    assert(cown->owner == get_interpid());
    Py_CLEAR(cown->value);
  }

  if (cown->xidata != NULL) {
    BOCRecycleQueue_enqueue(cown->recycle_queue, cown->xidata);
  }

  cown_weak_decref(cown);

  return 0;
}

#define COWN_DECREF(c) cown_decref(c)
#define COWN_WEAK_DECREF(c) cown_weak_decref(c)

/// @brief Atomic incref for the cown
/// @param cown the cown to incref
/// @return the new reference count
static inline int_least64_t cown_incref(BOCCown *cown) {
  int_least64_t rc = atomic_fetch_add(&cown->rc, 1) + 1;
  PRINTDBG("cown_incref(%p, cid=%" PRIdLEAST64 ") = %" PRIdLEAST64 "\n", cown,
           cown->id, rc);
  return rc;
}

static inline int_least64_t cown_weak_incref(BOCCown *cown) {
  int_least64_t rc = atomic_fetch_add(&cown->weak_rc, 1) + 1;
  PRINTDBG("cown_weak_incref(%p, cid=%" PRIdLEAST64 ") = %" PRIdLEAST64 "\n",
           cown, cown->id, rc);
  return rc;
}

static inline bool cown_promote(BOCCown *cown) {
  int_least64_t expected;
  int_least64_t desired;
  do {
    expected = atomic_load(&cown->rc);
    if (expected == 0) {
      return false;
    }

    desired = expected + 1;
  } while (!atomic_compare_exchange_strong(&cown->rc, &expected, desired));

  return true;
}

#define COWN_INCREF(c) cown_incref((c))
#define COWN_WEAK_INCREF(c) cown_weak_incref((c))
#define COWN_PROMOTE(c) cown_promote((c))

static inline void cown_set_value(BOCCown *cown, PyObject *value) {
  if (value == NULL) {
    Py_XDECREF(cown->value);
    return;
  }

  Py_XSETREF(cown->value, Py_NewRef(value));
  cown->exception = PyExceptionInstance_Check(value);
}

/// @brief Create a new BOCCown.
/// @param value The initial value.
/// @return A new BOCCown, or NULL on error.
static BOCCown *BOCCown_new(PyObject *value) {
  BOCCown *cown = (BOCCown *)PyMem_RawMalloc(sizeof(BOCCown));
  if (cown == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  cown->id = atomic_fetch_add(&BOC_COWN_COUNT, 1);
  cown->value = NULL;
  cown->recycle_queue = NULL;
  cown->xidata = NULL;
  cown->pickled = false;
  cown->exception = false;
  atomic_store(&cown->last, 0);
  // each cown starts with both a strong and weak reference
  // the weak reference will only be decremented when the strong
  // reference count is zero.
  atomic_store(&cown->rc, 1);
  atomic_store(&cown->weak_rc, 1);
  cown_set_value(cown, value);
  assert(cown->value != NULL);
  atomic_store(&cown->owner, get_interpid());
  PRINTDBG("BOCCown_new(cid=%" PRIdLEAST64 ", value=", cown->id);
  PRINTOBJDBG(value);
  PRINTFDBG(")\n");

  BOC_REF_TRACKING_ADD_COWN();

  return cown;
}

/// @brief Dequeue an XIData object from the queue.
/// @param queue The queue to use
/// @param wait_for_consistency Whether to wait until the queue is in an
/// consistent state before returning
/// @return XIData object if available, NULL if queue is empty or inconsistent
static XIDATA_T *BOCRecycleQueue_dequeue(BOCRecycleQueue *queue,
                                         bool wait_for_consistency) {
  BOCRecycleNode *tail = queue->tail;
  intptr_t tail_ptr = (intptr_t)queue->tail;
  intptr_t next_ptr = atomic_load(&tail->next);
  if (next_ptr == 0) {
    // two possibilities:
    // 1. queue is empty
    // 2. queue is inconsistent
    if (!wait_for_consistency) {
      // whatever this is can wait until the queue is back in a good state
      return NULL;
    }

    if (queue->head == tail_ptr) {
      // the queue is consistent, but empty
      return NULL;
    }

    // the queue is inconsistent, so we spin/wait for step 3 to complete above
    while (next_ptr == 0) {
      next_ptr = atomic_load(&tail->next);
    }
  }

  // we can proceed to dequeue the tail
  XIDATA_T *data = tail->xidata;
  queue->tail = (BOCRecycleNode *)next_ptr;
  PyMem_RawFree(tail);
  return data;
}

static int BOCRecycleQueue_register(BOCRecycleQueue *queue, BOCCown *cown) {
  cown->recycle_queue = queue;
  if (queue->xidata_to_cowns == NULL) {
    PyErr_Format(PyExc_RuntimeError,
                 "Attempt to register cown after interpreter %" PRIdLEAST64
                 " has shut down\n",
                 queue->index);
    return -1;
  }

  PyObject *xidata_ptr = PyLong_FromVoidPtr((void *)cown->xidata);
  PyObject *cown_ptr = PyLong_FromVoidPtr((void *)cown);
  if (PyDict_SetItem(queue->xidata_to_cowns, xidata_ptr, cown_ptr) < 0) {
    Py_DECREF(xidata_ptr);
    Py_DECREF(cown_ptr);
    return -1;
  }

  Py_DECREF(xidata_ptr);
  Py_DECREF(cown_ptr);

  COWN_WEAK_INCREF(cown);

  return 0;
}

static void BOCRecycleQueue_recycle(BOCRecycleQueue *queue, XIDATA_T *xidata) {
  assert(queue == BOC_STATE->recycle_queue);
  PyObject *xidata_ptr = PyLong_FromVoidPtr((void *)xidata);

  if (queue->xidata_to_cowns != NULL) {
    PyObject *cown_ptr = PyDict_GetItem(queue->xidata_to_cowns, xidata_ptr);
    if (cown_ptr != NULL) {
      BOCCown *cown = (BOCCown *)PyLong_AsVoidPtr(cown_ptr);
      COWN_WEAK_DECREF(cown);
      PyDict_DelItem(queue->xidata_to_cowns, xidata_ptr);
    }
  } else {
    fprintf(stderr,
            "Recycling xidata created on interpeter %" PRIdLEAST64
            " after the interpreter "
            "has shut down may result in cown leak.\n",
            queue->index);
  }

  Py_DECREF(xidata_ptr);
  XIDATA_FREE(xidata);
}

/// @brief Enqeues an xidata on the recycling queue.
/// @param queue The queue to use
/// @param xidata The data to enqueue
static void BOCRecycleQueue_enqueue(BOCRecycleQueue *queue, XIDATA_T *xidata) {
#ifdef BOC_TRACE
  if (xidata->obj != NULL) {
    PRINTDBG("enqueueing %s to recycle queue %" PRIdLEAST64 "\n",
             xidata->obj->ob_type->tp_name, queue->index);
  } else {
    PRINTDBG("enqueueing <NULL> to recycle queue %" PRIdLEAST64 "\n",
             queue->index);
  }
#endif

  if (queue == BOC_STATE->recycle_queue) {
    // no need to enqueue, this is on the local interpreter
    BOCRecycleQueue_recycle(queue, xidata);
    return;
  }

  // allocate space for the next item
  BOCRecycleNode *node =
      (BOCRecycleNode *)PyMem_RawMalloc(sizeof(BOCRecycleNode));
  node->xidata = NULL;
  atomic_store(&node->next, 0);

  // step 1: swap the new node in as the new head
  intptr_t node_ptr = (intptr_t)node;
  intptr_t old_head_ptr = atomic_exchange(&queue->head, node_ptr);
  BOCRecycleNode *old_head = (BOCRecycleNode *)old_head_ptr;
  // queue is now inconsistent
  // step 2: store the data in this node. This node is somewhere inside the
  // queue.
  old_head->xidata = xidata;
  // step 3: connect everything back together
  atomic_store(&old_head->next, node_ptr);
  // queue is consistent
}

/// @brief Empty out the queue and free the contents
/// @param queue The queue to empty
/// @param wait_for_consistency Whether to wait until the queue is consistent
static void BOCRecycleQueue_empty(BOCRecycleQueue *queue,
                                  bool wait_for_consistency) {
  XIDATA_T *xidata = BOCRecycleQueue_dequeue(queue, wait_for_consistency);
  while (xidata != NULL) {
    BOCRecycleQueue_recycle(queue, xidata);
    xidata = BOCRecycleQueue_dequeue(queue, wait_for_consistency);
  }

  if (wait_for_consistency) {
    assert((intptr_t)queue->tail == atomic_load(&queue->head));
    assert(atomic_load(&queue->tail->next) == 0);
  }
}

/// @brief Frees a RecycleQueue.
/// @details This will complete the recycling of any pending XIData objects, if
/// possible.
/// @param queue The queue to free
static void BOCRecycleQueue_free(BOCRecycleQueue *queue) {
  assert(queue->xidata_to_cowns == NULL);
  if (queue->tail != NULL && atomic_load(&queue->tail->next) != 0) {
    printf("BOC: recycle queue %" PRIdLEAST64 " not empty during finalize\n",
           queue->index);
    BOCRecycleQueue_empty(queue, true);
  }

  PyMem_RawFree(queue->tail);
  PyMem_RawFree(queue);
}

/// @brief Lightweight capsule object for cowns
/// @details This capsule allows the cown to be exposed to the Python code
/// level. There can be any number of them, and the will perform atomic
/// reference counts on the underlying cown.
typedef struct cown_capsule_object {
  PyObject_HEAD
      /// @brief the actual cown object wrapped by the capsule
      BOCCown *cown;
} CownCapsuleObject;

/// @brief Deallocates the CownCapsule
/// @note This will perform an atomic decref on the underlying cown
/// @param op Pointer to the CownCapsule object
static void CownCapsule_dealloc(PyObject *op) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  PRINTDBG("CownCapsule_dealloc(%p, rc=%" PRIdLEAST64 ")\n", self,
           op->ob_refcnt);
  COWN_DECREF(self->cown);
  Py_TYPE(self)->tp_free(self);
}

/// @brief Creates an empty CownCapsule.
/// @param type CownCapsuleType
/// @param args (ignored)
/// @param kwds (ignored)
/// @return An empty CownCapsule object
static PyObject *CownCapsule_new(PyTypeObject *type, PyObject *args,
                                 PyObject *kwds) {
  CownCapsuleObject *self;
  self = (CownCapsuleObject *)type->tp_alloc(type, 0);
  if (self == NULL) {
    return NULL;
  }

  self->cown = NULL;
  return (PyObject *)self;
}

/// @brief Initialises a cown with an (optional) value.
/// @param op The CownCapsule object
/// @param args At most one object to use as the initial value
/// @param Py_UNUSED (ignored)
/// @return nonzero if there is an error
static int CownCapsule_init(PyObject *op, PyObject *args,
                            PyObject *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  PyObject *value = Py_None;

  if (!PyArg_ParseTuple(args, "|O", &value)) {
    return -1;
  }

  self->cown = BOCCown_new(value);
  if (self->cown == NULL) {
    return -1;
  }

  PRINTDBG("CownCapsule_init(%p, cown=%p, cid=%" PRIdLEAST64 ", value=", self,
           self->cown, self->cown->id);
  PRINTOBJDBG(value);
  PRINTFDBG(")\n");
  return 0;
}

/// @brief Checks that the cown is acquired by the currently running
/// interpreter.
/// @param cown The cown to check
/// @param set_error Whether to set an error message
/// @return whether the currently running interpreter has acquired the cown
static bool cown_check_acquired(BOCCown *cown, bool set_error) {
  PY_INT64_T current_id = get_interpid();
  if (current_id != atomic_load(&cown->owner)) {
    if (set_error) {
      PyErr_SetString(PyExc_RuntimeError,
                      "The current interpreter does not own this cown");
    }

    return false;
  }

  return true;
}

/// @brief Returns the value of the cown, if it has been acquired.
/// @note This will set an exception if the cown is not acquired.
/// @param op The CownCapsule object
/// @param Py_UNUSED (ignored)
/// @return The cown value
static PyObject *CownCapsule_get(PyObject *op, PyObject *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;

  if (!cown_check_acquired(self->cown, true)) {
    return NULL;
  }

  return Py_NewRef(self->cown->value);
}

/// @brief Sets the value of the cown, if it has been acquired.
/// @param op The CownCapsule object
/// @param args The new value
/// @return The previous value of the cown
static PyObject *CownCapsule_set(PyObject *op, PyObject *args) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  PyObject *value = NULL;

  if (!PyArg_ParseTuple(args, "O", &value)) {
    return NULL;
  }

  if (!cown_check_acquired(self->cown, true)) {
    return NULL;
  }

  cown_set_value(self->cown, value);
  if (self->cown->value == NULL) {
    return NULL;
  }

  Py_RETURN_NONE;
}

static PyObject *CownCapsule_get_value(PyObject *op, void *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;

  if (!cown_check_acquired(self->cown, true)) {
    return NULL;
  }

  return Py_NewRef(self->cown->value);
}

static int CownCapsule_set_value(PyObject *op, PyObject *value,
                                 void *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;

  if (!cown_check_acquired(self->cown, true)) {
    return -1;
  }

  cown_set_value(self->cown, value);
  if (self->cown->value == NULL) {
    return -1;
  }

  return 0;
}

/// @brief Returns whether the current interpereter has acquired the cown
/// @param op The CownCapsule object
/// @param Py_UNUSED ignored
/// @return True if acquired, False otherwise
static PyObject *CownCapsule_acquired(PyObject *op,
                                      PyObject *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  return PyBool_FromLong(cown_check_acquired(self->cown, false));
}

/// @brief Attempts to acquire the cown
/// @param cown The cown to acquire
/// @return -1 if failure, 0 if success
static int cown_acquire(BOCCown *cown) {
  int_least64_t expected = NO_OWNER;
  int_least64_t desired = get_interpid();
  if (!atomic_compare_exchange_strong(&cown->owner, &expected, desired)) {
    if (expected == desired) {
      // already acquired by this interpreter
      return 0;
    }

    PyErr_Format(PyExc_RuntimeError,
                 "%" PRIdLEAST64
                 " cannot acquire cown (already acquired by %" PRIdLEAST64 ")",
                 desired, expected);
    return -1;
  }

  assert(cown->value == NULL);
  assert(cown->xidata != NULL);
  cown->value = xidata_to_object(cown->xidata, cown->pickled);

  if (cown->value == NULL) {
    return -1;
  }

  BOCRecycleQueue_enqueue(cown->recycle_queue, cown->xidata);
  cown->recycle_queue = NULL;
  cown->xidata = NULL;
  return 0;
}

/// @brief Attempts to acquire the cown
/// @note This will throw an exception of the cown has already been acquired by
/// another interpreter It will also thrown an exception if deserialization
/// fails.
/// @param op The CownCapsule object
/// @param Py_UNUSED (ignored)
/// @return None on success, NULL otherwise
static PyObject *CownCapsule_acquire(PyObject *op, PyObject *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  BOCCown *cown = self->cown;

  if (cown_acquire(cown) < 0) {
    return NULL;
  }

  Py_RETURN_NONE;
}

/// @brief Releases the cown
/// @param cown The cown to release
/// @return -1 if error, 0 otherwise
static int cown_release(BOCCown *cown) {
  int_least64_t expected = get_interpid();
  int_least64_t owner = atomic_load(&cown->owner);
  if (owner != expected) {
    if (owner == NO_OWNER) {
      // already released
      return 0;
    }

    PyErr_Format(PyExc_RuntimeError,
                 "%" PRIdLEAST64
                 " cannot release cown (acquired by %" PRIdLEAST64 ")",
                 expected, owner);
    return -1;
  }

  assert(cown->value != NULL);
  assert(cown->xidata == NULL);

  PyObject *pickled = object_to_xidata(cown->value, &cown->xidata);

  if (pickled == NULL) {
    return -1;
  }

  if (BOCRecycleQueue_register(BOC_STATE->recycle_queue, cown) < 0) {
    return -1;
  }

  cown->pickled = Py_IsTrue(pickled);
  Py_CLEAR(cown->value);

  int_least64_t desired = NO_OWNER;
  if (!atomic_compare_exchange_strong(&cown->owner, &expected, desired)) {
    // this should never happen
    PyErr_SetString(PyExc_RuntimeError,
                    "Panic: contention on cown during release");
    return -1;
  }

  return 0;
}

/// @brief Releases the cown (if acquired)
/// @note If the cown has not been acquired by the current interpreter, this
/// will thrown an exception. It will also thrown an exception if serialization
/// fails.
/// @param op The CownCapsule object
/// @param Py_UNUSED (ignored)
/// @return None if successful, NULL otherwise
static PyObject *CownCapsule_release(PyObject *op, PyObject *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  BOCCown *cown = self->cown;

  if (cown_release(cown) < 0) {
    return NULL;
  }

  Py_RETURN_NONE;
}

static PyObject *CownCapsule_get_impl(PyObject *op, void *Py_UNUSED(dummy)) {
  return Py_NewRef(op);
}

static PyMethodDef CownCapsule_methods[] = {
    {"get", CownCapsule_get, METH_NOARGS, NULL},
    {"set", CownCapsule_set, METH_VARARGS, NULL},
    {"acquired", CownCapsule_acquired, METH_NOARGS, NULL},
    {"acquire", CownCapsule_acquire, METH_NOARGS, NULL},
    {"release", CownCapsule_release, METH_NOARGS, NULL},
    {NULL} /* Sentinel */
};

static PyGetSetDef CownCapsule_getset[] = {
    {"value", (getter)CownCapsule_get_value, (setter)CownCapsule_set_value,
     NULL, NULL},
    {"impl", (getter)CownCapsule_get_impl, NULL, NULL, NULL},
    {NULL} /* Sentinel */
};

static PyObject *CownCapsule_richcompare(PyObject *self, PyObject *other,
                                         int op) {
  if (Py_TYPE(self) != Py_TYPE(other)) {
    return Py_NotImplemented;
  }

  CownCapsuleObject *a = (CownCapsuleObject *)self;
  CownCapsuleObject *b = (CownCapsuleObject *)other;
  Py_RETURN_RICHCOMPARE(a->cown, b->cown, op);
}

static Py_hash_t CownCapsule_hash(PyObject *op) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  return (Py_hash_t)self->cown;
}

static PyObject *CownCapsule_repr(PyObject *op) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  if (cown_check_acquired(self->cown, false)) {
    return PyUnicode_FromFormat("CownCapsule(%p, value=%R)", self,
                                self->cown->value);
  } else {
    return PyUnicode_FromFormat("CownCapsule(%p, value=<not owner>)", self);
  };
}

static PyType_Slot CownCapsule_slots[] = {
    {Py_tp_new, CownCapsule_new},
    {Py_tp_init, CownCapsule_init},
    {Py_tp_dealloc, CownCapsule_dealloc},
    {Py_tp_methods, CownCapsule_methods},
    {Py_tp_getset, CownCapsule_getset},
    {Py_tp_richcompare, CownCapsule_richcompare},
    {Py_tp_hash, CownCapsule_hash},
    {Py_tp_repr, CownCapsule_repr},
    {0, NULL} /* sentinel */
};

static PyType_Spec CownCapsule_Spec = {.name = "bocpy._core.CownCapsule",
                                       .basicsize = sizeof(CownCapsuleObject),
                                       .itemsize = 0,
                                       .flags = Py_TPFLAGS_DEFAULT |
                                                Py_TPFLAGS_IMMUTABLETYPE,
                                       .slots = CownCapsule_slots};

#define CownCapsule_CheckExact(op)                                             \
  Py_IS_TYPE((op), BOC_STATE->cown_capsule_type)

static PyObject *cown_capsule_wrap(BOCCown *cown, bool promote) {
  if (promote) {
    if (!COWN_PROMOTE(cown)) {
      return NULL;
    }
  } else {
    COWN_INCREF(cown);
  }

  PyTypeObject *type = BOC_STATE->cown_capsule_type;
  CownCapsuleObject *capsule = (CownCapsuleObject *)type->tp_alloc(type, 0);
  if (capsule == NULL) {
    return NULL;
  }

  capsule->cown = cown;
  PRINTDBG("CownCapsule_wrap(%p, rc=%zu)\n", capsule,
           capsule->ob_base.ob_refcnt);
  return (PyObject *)capsule;
}

/// @brief Unwraps a cown from a CownCapsule
/// @param op The CownCapsule object
/// @return A pointer to the cown object
BOCCown *cown_unwrap(PyObject *op) {
  if (!CownCapsule_CheckExact(op)) {
    PyObject *impl = PyObject_GetAttrString(op, "impl");
    if (impl == NULL) {
      return NULL;
    }

    if (!CownCapsule_CheckExact(impl)) {
      PyErr_SetString(PyExc_ValueError, "Expected a CownCapsule");
      Py_DECREF(impl);
      return NULL;
    }

    op = impl;
    Py_DECREF(impl);
  }

  CownCapsuleObject *self = (CownCapsuleObject *)op;
  PRINTDBG("CownCapsule_unwrap(%p, rc=%zu)\n", self, op->ob_refcnt);
  return self->cown;
}

static PyObject *BOCRecycleQueue_promote_cowns(BOCRecycleQueue *queue) {
  if (queue->xidata_to_cowns == NULL) {
    PyErr_Format(PyExc_RuntimeError,
                 "Cannot promote cowns from interpreter %" PRIdLEAST64
                 " after it has been destroyed",
                 queue->index);
    return NULL;
  }

  PyObject *items = PyDict_Items(queue->xidata_to_cowns);
  if (items == NULL) {
    return NULL;
  }

  Py_ssize_t size = PyList_GET_SIZE(items);
  PyObject *cowns = PyTuple_New(size);
  if (cowns == NULL) {
    Py_DECREF(items);
    return NULL;
  }

  for (Py_ssize_t i = 0; i < size; ++i) {
    PyObject *item = PyList_GET_ITEM(items, i);
    PyObject *cown_ptr = PyTuple_GET_ITEM(item, 1);
    BOCCown *cown = (BOCCown *)PyLong_AsVoidPtr(cown_ptr);
    PyObject *cown_capsule = cown_capsule_wrap(cown, true);
    if (cown_capsule != NULL) {
      PyTuple_SET_ITEM(cowns, i, cown_capsule);
      continue;
    }

    COWN_WEAK_DECREF(cown);
    PyTuple_SET_ITEM(cowns, i, Py_None);
  }

  Py_DECREF(items);

  return cowns;
}

/// @brief Creates a new CownCapsule object to wrap an cown that has been sent
/// from another interpreter
/// @param xidata Contains a pointer to the cown
/// @return The new CownCapsule object
static PyObject *_new_cown_object(XIDATA_T *xidata) {
  BOCCown *cown = (BOCCown *)xidata->data;

  PyTypeObject *type = BOC_STATE->cown_capsule_type;
  CownCapsuleObject *capsule = (CownCapsuleObject *)type->tp_alloc(type, 0);
  if (capsule == NULL) {
    return NULL;
  }

  capsule->cown = cown;
  PRINTDBG("_new_cown_object\n");
  COWN_INCREF(cown);
  return (PyObject *)capsule;
}

/// @brief Initialises an xidata that shares a cown.
/// @param tstate the state of the current thread
/// @param obj the CownCapsule object
/// @param fallback a fallback xidata method
/// @param xidata the xidata object
/// @return 0 if successful
static int _cown_shared(
#ifndef BOC_NO_MULTIGIL
    PyThreadState *tstate,
#endif
    PyObject *obj, XIDATA_T *xidata) {
#ifdef BOC_NO_MULTIGIL
  PyThreadState *tstate = PyThreadState_GET();
#endif

  CownCapsuleObject *capsule = (CownCapsuleObject *)obj;
  BOCCown *cown = capsule->cown;

  if (atomic_load(&cown->owner) != NO_OWNER) {
    PyErr_SetString(PyExc_RuntimeError, "cown must be released before sending");
    return -1;
  }

  PRINTDBG("_cown_shared(%p)\n", cown);

  // all we do to initialise the xidata is store a pointer to the cown
  XIDATA_INIT(xidata, tstate->interp, cown, obj, _new_cown_object);
  return 0;
}

/// @brief Frees a message
/// @param message The message to free
static void boc_message_free(BOCMessage *message) {
  if (message->xidata != NULL) {
    BOCRecycleQueue_enqueue(message->recycle_queue, message->xidata);
  }

  PyMem_RawFree(message);
}

/// @brief Struct storing a shallow (i.e., 1 level) sequence of message contents
typedef struct shared_contents {
  /// @brief Number of items in the sequence
  Py_ssize_t num_items;
  /// @brief Array of pointers to XIDATA_T structs
  XIDATA_T **xidata;
  /// @brief Array of boolean values indicating what needed to be pickled
  bool *pickled;
  /// @brief The recycling queue to use for all the XIDATA_T structs
  BOCRecycleQueue *recycle_queue;
} BOCSharedContents;

/// @brief Reads the content values, converts them back into PyObjects, and then
/// puts them in a PyTuple.
/// @param xidata The data containing a pointer to the contents struct
/// @return A PyTuple containing the contents, or NULL on error
PyObject *_new_contents_object(XIDATA_T *xidata) {
  BOCSharedContents *shared = (BOCSharedContents *)xidata->data;

  PRINTDBG("_new_contents_object(%p)\n", shared);

  PyObject *tuple = PyTuple_New(shared->num_items);
  if (tuple == NULL) {
    return NULL;
  }

  for (Py_ssize_t i = 0; i < shared->num_items; ++i) {
    PyObject *item = xidata_to_object(shared->xidata[i], shared->pickled[i]);
    if (item == NULL) {
      Py_DECREF(tuple);
      return NULL;
    }

    PyTuple_SET_ITEM(tuple, i, item);
  }

  return tuple;
}

/// @brief Frees a contents struct.
/// @param data The struct to free
void _contents_shared_free(void *data) {
  BOCSharedContents *shared = (BOCSharedContents *)data;
  PRINTDBG("_contents_shared_free(%p)\n", shared);
  XIDATA_T **xidata_ptr = shared->xidata;
  for (Py_ssize_t i = 0; i < shared->num_items; ++i, ++xidata_ptr) {
    if (*xidata_ptr != NULL) {
      PyObject *obj = (*xidata_ptr)->obj;
      if (obj != NULL) {
        PRINTDBG("_contents_shared_free(%p)[%" PRIdLEAST64
                 "] %s(%p): rc=%" PRIdLEAST64 "\n",
                 shared, i, obj->ob_type->tp_name, obj, Py_REFCNT(obj));
      }

      BOCRecycleQueue_enqueue(shared->recycle_queue, *xidata_ptr);
    }
  }

  if (shared->xidata != NULL) {
    PyMem_RawFree(shared->xidata);
  }

  if (shared->pickled != NULL) {
    PyMem_RawFree(shared->pickled);
  }

  PyMem_RawFree(shared);
}

/// @brief Method to convert an object that has the Sequence interface to a
/// portable contents format.
/// @param module The _core module
/// @param tstate The state of the current thread
/// @param obj The object that implements Sequence
/// @param out_ptr The xidata that will hold the resulting contents struct
/// @return 0 if successful, -1 otherwise
int _contents_shared(PyThreadState *tstate, PyObject *obj, XIDATA_T **out_ptr) {
  if (!PySequence_Check(obj)) {
    // not a sequence
    return -1;
  }

  Py_ssize_t num_items = PySequence_Length(obj);
  if (num_items < 0) {
    // Length not implemented (i.e., not a finite sequence)
    return -1;
  }

  BOCSharedContents *shared = PyMem_RawMalloc(sizeof(BOCSharedContents));
  if (shared == NULL) {
    PyErr_NoMemory();
    return -1;
  }

  shared->num_items = 0;
  shared->pickled = NULL;
  shared->recycle_queue = NULL;
  shared->xidata =
      (XIDATA_T **)PyMem_RawCalloc((size_t)num_items, sizeof(XIDATA_T *));
  if (shared->xidata == NULL) {
    PyErr_NoMemory();
    return -1;
  }

  shared->pickled = PyMem_RawCalloc((size_t)num_items, sizeof(bool));
  if (shared->pickled == NULL) {
    PyErr_NoMemory();
    return -1;
  }

  PRINTDBG("contents_shared(%p)\n", shared);

  shared->recycle_queue = BOC_STATE->recycle_queue;
  shared->num_items = num_items;
  for (Py_ssize_t i = 0; i < num_items; ++i) {
    shared->xidata[i] = NULL;
  }

  XIDATA_T **xidata_ptr = shared->xidata;
  for (Py_ssize_t i = 0; i < num_items; ++i, ++xidata_ptr) {
    PyObject *item = PySequence_GetItem(obj, i);
    if (item == NULL) {
      return -1;
    }

    PyObject *pickled = object_to_xidata(item, xidata_ptr);
    Py_DECREF(item);

    PRINTDBG("contents_shared(%p)[%" PRIdLEAST64 "] %s(%p): rc=%" PRIdLEAST64
             "\n",
             shared, i, item->ob_type->tp_name, item, Py_REFCNT(item));

    if (pickled == NULL) {
      // wasn't possible to convert the object to xidata
      goto error;
    }

    shared->pickled[i] = Py_IsTrue(pickled);
    Py_DECREF(pickled);
  }

  if (*out_ptr == NULL) {
    *out_ptr = XIDATA_NEW();
  }

  XIDATA_T *out = *out_ptr;
  if (out == NULL) {
    PyErr_NoMemory();
    return -1;
  }

  XIDATA_INIT(out, tstate->interp, shared, obj, _new_contents_object);
  XIDATA_SET_FREE(out, _contents_shared_free);
  return 0;

error:
  _contents_shared_free(shared);
  return -1;
}

/// @brief Gets the appropriate queue for a tag
/// @details Queues are assigned to tags on a first-come, first-served basis.
/// Recycle tags, and tags which are used after the first BOC_QUEUE_COUNT queues
/// have been assigned, will be assigned to a general purpose queue.
/// @param module The _core module
/// @param tag The tag to query
/// @return A reference to a BOCQueue
static BOCQueue *get_queue_for_tag(PyObject *tag) {
  if (tag == NULL) {
    return NULL;
  }

  // First we check to see if we already have cached the queue this tag is
  // associated with
  BOCQueue *qptr = BOC_QUEUES;
  for (size_t i = 0; i < BOC_QUEUE_COUNT; ++i, ++qptr) {
    if (BOC_STATE->queue_tags[i] != NULL) {
      if (tag_is_disabled(BOC_STATE->queue_tags[i])) {
        TAG_DECREF(BOC_STATE->queue_tags[i]);
        BOC_STATE->queue_tags[i] = NULL;
      } else {
        if (tag_compare_with_PyUnicode(BOC_STATE->queue_tags[i], tag) == 0) {
          // this is the dedicated queue for this tag
          return qptr;
        } else {
          if (PyErr_Occurred() != NULL) {
            return NULL;
          }
        }

        // not the right queue, keep looking
        continue;
      }
    }

    // check to see if another interpreter has used this queue
    int_least64_t expected = BOC_QUEUE_UNASSIGNED;
    int_least64_t desired = BOC_QUEUE_ASSIGNED;
    if (atomic_compare_exchange_strong(&qptr->state, &expected, desired)) {
      // we're the first, this is the new dedicated queue for this tag
      PRINTDBG("Assigning ");
      PRINTOBJDBG(tag);
      PRINTFDBG(" to queue %zu\n", i);
      BOCTag *qtag = tag_from_PyUnicode(tag, qptr);
      if (qtag == NULL) {
        return NULL;
      }

      atomic_store(&qptr->tag, (intptr_t)qtag);
      TAG_INCREF(qtag);
      BOC_STATE->queue_tags[i] = qtag;
      TAG_INCREF(qtag);
      return qptr;
    }

    // this queue has already been assigned
    BOCTag *qtag = (BOCTag *)atomic_load(&qptr->tag);
    while (qtag == NULL) {
      // waiting for another interpreter to allocate and assign
      qtag = (BOCTag *)atomic_load(&qptr->tag);
    }

    BOC_STATE->queue_tags[i] = qtag;
    TAG_INCREF(qtag);

    PRINTDBG("Discovered %s at queue %" PRIdLEAST64 "\n", qtag->str, i);
    if (tag_compare_with_PyUnicode(BOC_STATE->queue_tags[i], tag) == 0) {
      // this is the dedicated queue for this tag
      if (expected == BOC_QUEUE_DISABLED) {
        // however, it is disabled right now
        return NULL;
      }

      return qptr;
    } else if (PyErr_Occurred() != NULL) {
      return NULL;
    }

    // not the right queue, keep looking
  }

  // no queue for this tag
  return NULL;
}

/// @brief Creates a new message.
/// @param module The _core module
/// @param tag The tag associated with the message.
/// @param contents The contents of the message.
/// @return A message object
static BOCMessage *boc_message_new(PyObject *tag, PyObject *contents) {
  BOCMessage *message = (BOCMessage *)PyMem_RawMalloc(sizeof(BOCMessage));
  if (message == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  BOCQueue *qptr = get_queue_for_tag(tag);
  if (qptr == NULL) {
    PyMem_RawFree(message);
    PyErr_SetString(PyExc_KeyError,
                    "No queue available for tag: tag capacity exceeded");
    return NULL;
  }

  BOCTag *qtag = (BOCTag *)atomic_load(&qptr->tag);
  if (qtag == NULL) {
    // non-assigned tag
    message->tag = tag_from_PyUnicode(tag, qptr);
  } else {
    message->tag = qtag;
  }

  message->recycle_queue = BOC_STATE->recycle_queue;
  message->xidata = NULL;
  message->pickled = false;

  if (!xidata_supported(contents)) {
    if (_contents_shared(PyThreadState_GET(), contents, &message->xidata) ==
        0) {
      return message;
    }
  }

  PyObject *pickled = object_to_xidata(contents, &message->xidata);
  if (pickled == NULL) {
    boc_message_free(message);
    return NULL;
  }

  message->pickled = Py_IsTrue(pickled);
  return message;
}

/// @brief Enqueues a message.
/// @param module the _core module
/// @param message the message to enqueue
/// @return 1 if the message was enqueue, 0 otherwise
static int boc_enqueue(BOCMessage *message) {
  BOCQueue *qptr = message->tag->queue;

  // get the current tail
  int_least64_t tail = atomic_load(&qptr->tail);
  while (true) {
    // get the current head
    int_least64_t head = atomic_load(&qptr->head);
    if (tail - head >= BOC_CAPACITY) {
      // the queue is full
      return -1;
    }

    // attempt to enqueue
    if (atomic_compare_exchange_strong(&qptr->tail, &tail, tail + 1)) {
      PRINTDBG("Enqueued %s at q%" PRIdLEAST64 "[%" PRIdLEAST64
               "] (%" PRIdLEAST64 " - %" PRIdLEAST64 " = %" PRIdLEAST64 ")\n",
               message->tag->str, qptr->index, tail, tail + 1, head,
               tail - head + 1);
      assert(qptr->messages[tail % BOC_CAPACITY] == NULL);
      qptr->messages[tail % BOC_CAPACITY] = message;
      return 0;
    }

    // someone else got there first, try again
  }

  return -1;
}

/// @brief Attempt to dequeue a message
/// @param module The _core module
/// @param tag The tag associated with the message
/// @param message A pointer to the message pointer (will be used to return the
/// dequeued message)
/// @return 0 if no message was dequeued, 1 if a message was dequeued
static int_least64_t boc_dequeue(PyObject *tag, BOCMessage **message) {
  *message = NULL;
  if (!PyUnicode_CheckExact(tag)) {
    PyErr_SetString(PyExc_TypeError, "tag must be a str");
    return -3;
  }

  BOCQueue *qptr = get_queue_for_tag(tag);
  if (qptr == NULL) {
    PyErr_SetString(PyExc_KeyError, "No message queue found for that tag");
    return -2;
  }

  int_least64_t head = atomic_load(&qptr->head);
  int_least64_t tail = atomic_load(&qptr->tail);
  int_least64_t count = tail - head;
  if (count == 0) {
    // queue is empty
    return -1;
  }

  while (head < tail) {
    // attempt to dequeue a message
    if (!atomic_compare_exchange_strong(&qptr->head, &head, head + 1)) {
      if (head >= tail) {
        // queue is empty
        return -1;
      }

      PRINTDBG("Unable to dequeue at head=%" PRIdLEAST64 "\n", head);

      // someone else already consumed this, try again
      tail = atomic_load(&qptr->tail);
      continue;
    }

    int_least64_t index = head % BOC_CAPACITY;
    while (qptr->messages[index] == NULL) {
      // spin in case the message has not yet been written
      Py_BEGIN_ALLOW_THREADS thrd_sleep(&SLEEP_TS, NULL);
      Py_END_ALLOW_THREADS
    }

    *message = qptr->messages[index];
    qptr->messages[index] = NULL;
    PRINTFDBG("Dequeued %s from q%" PRIdLEAST64 "[%" PRIdLEAST64
              "] (%" PRIdLEAST64 " - %" PRIdLEAST64 " = %" PRIdLEAST64 ")\n",
              (*message)->tag->str, qptr->index, head, tail, head + 1,
              tail - head - 1);
    return qptr->index;
  }

  return -1;
}

/// @brief Returns the current time as double-precision seconds.
/// @return the current time
static double boc_now_s() {
  const double S_PER_NS = 1.0e-9;
  struct timespec ts;
  timespec_get(&ts, TIME_UTC);
  double time = (double)ts.tv_sec;
  time += ts.tv_nsec * S_PER_NS;
  return time;
}

/// @brief Sends a message
/// @param module The _core module
/// @param args The message to send
/// @return None if successful, NULL otherwise
static PyObject *_core_send(PyObject *module, PyObject *args) {
  BOC_STATE_SET(module);
  PyObject *tag;
  PyObject *contents;

  if (!PyArg_ParseTuple(args, "O!O", &PyUnicode_Type, &tag, &contents)) {
    return NULL;
  }

  BOCMessage *message = boc_message_new(tag, contents);
  if (message == NULL) {
    return NULL;
  }

  if (boc_enqueue(message) < 0) {
    boc_message_free(message);
    PyErr_SetString(PyExc_RuntimeError, "Message queue is full");
    return NULL;
  }

  Py_BEGIN_ALLOW_THREADS thrd_sleep(&SLEEP_TS, NULL);
  Py_END_ALLOW_THREADS;

  Py_RETURN_NONE;
}

/// @brief Receives a message
/// @param module The _core module
/// @param args The conditions under which to receive a message
/// @param keywds Used to pass optional arguments
/// @return The received message, None (if timed out), or NULL if there was an
/// error
static PyObject *_core_receive(PyObject *module, PyObject *args,
                               PyObject *keywds) {
  PyObject *tag;
  double timeout = -1;
  PyObject *after = Py_None;
  static char *kwlist[] = {"tags", "timeout", "after", NULL};

  BOC_STATE_SET(module);

  if (!PyArg_ParseTupleAndKeywords(args, keywds, "O|dO", kwlist, &tag, &timeout,
                                   &after)) {
    return NULL;
  }

  PyObject *tags_fast = NULL;
  Py_ssize_t tags_size = 0;
  if (PyUnicode_CheckExact(tag)) {
    PRINTDBG("receive(");
    PRINTOBJDBG(tag);
    PRINTFDBG(")\n");
  } else {
    tags_fast = PySequence_Fast(
        tag, "tags must either be a single str or a list/tuple");
    if (tags_fast == NULL) {
      return NULL;
    }

    tags_size = PySequence_Fast_GET_SIZE(tags_fast);
    if (tags_size == 0) {
      PyErr_SetString(PyExc_RuntimeError,
                      "tags must contain at least one value");
      Py_DECREF(tags_fast);
      return NULL;
    }

    if (tags_size == 1) {
      tag = PySequence_Fast_GET_ITEM(tags_fast, 0);
      Py_DECREF(tags_fast);
      tags_fast = NULL;
    } else {
      PRINTDBG("receive(");
      for (Py_ssize_t i = 0; i < tags_size; ++i) {
        tag = PySequence_Fast_GET_ITEM(tags_fast, i);
        if (!PyUnicode_CheckExact(tag)) {
          Py_DECREF(tags_fast);
          PyErr_SetString(PyExc_TypeError,
                          "tags must contain only str objects");
          return NULL;
        }

        PRINTOBJDBG(tag);
        PRINTFDBG(" ");
      }

      PRINTFDBG(")\n");
    }
  }

  // determine if a timeout has been requested, and what clock time that would
  // be
  bool do_timeout = false;
  double end_time = 0;
  if (timeout >= 0) {
    do_timeout = true;
    end_time = boc_now_s() + timeout;
  }

  size_t tag_index = 0;
  BOCMessage *message = NULL;
  while (true) {
    Py_BEGIN_ALLOW_THREADS thrd_sleep(&SLEEP_TS, NULL);
    Py_END_ALLOW_THREADS;

    BOCRecycleQueue_empty(BOC_STATE->recycle_queue, false);

    int_least64_t queue_index = -1;

    if (tags_fast != NULL) {
      // see if there are available messages for any of the tags
      tag = PySequence_Fast_GET_ITEM(tags_fast, tag_index);
      queue_index = boc_dequeue(tag, &message);
      tag_index = (tag_index + 1) % tags_size;
    } else {
      queue_index = boc_dequeue(tag, &message);
    }

    if (queue_index < 0) {
      if (PyErr_Occurred() != NULL) {
        assert(message == NULL);
        Py_XDECREF(tags_fast);
        return NULL;
      }

      // no message was available
      if (do_timeout && boc_now_s() > end_time) {
        // we've timed out
        if (!Py_IsNone(after)) {
          return PyObject_CallNoArgs(after);
        }

        return Py_BuildValue("(sO)", BOC_TIMEOUT, Py_None);
      }

      continue;
    }

#ifdef BOC_TRACE
    if (tag_compare_with_PyUnicode(message->tag, tag) != 0) {
      // this should not happen, and indicates a bug in
      // boc_enqueue()/boc_dequeue()
      if (PyErr_Occurred() != NULL) {
        Py_XDECREF(tags_fast);
        boc_message_free(message);
        return NULL;
      }

      continue;
    }
#endif

    PyObject *contents = xidata_to_object(message->xidata, message->pickled);
    if (contents == NULL) {
      Py_XDECREF(tags_fast);
      boc_message_free(message);
      return NULL;
    }

    PyObject *result = PyTuple_Pack(2, tag, contents);
    Py_DECREF(contents);

    boc_message_free(message);
    Py_XDECREF(tags_fast);
    return result;
  }

  Py_RETURN_NONE;
}

/// @brief Drain all the messages associated with a particular tag
/// @param module The _core module
/// @param args The tag or tags to clear
/// @return None if successful, NULL otherwise
PyObject *_core_drain(PyObject *module, PyObject *args) {
  BOC_STATE_SET(module);

  BOCMessage *message;
  PyObject *tags = NULL;
  if (!PyArg_ParseTuple(args, "O", &tags)) {
    return NULL;
  }

  if (PyUnicode_CheckExact(tags)) {
    PRINTDBG("drain(");
    PRINTOBJDBG(tags);
    PRINTFDBG(")\n");
    while (true) {
      int_least64_t queue_index = boc_dequeue(tags, &message);
      if (queue_index < 0) {
        PyErr_Clear();
        break;
      }

      boc_message_free(message);
    }

    Py_RETURN_NONE;
  }

  PyObject *tags_fast = PySequence_Fast(tags, "tags must be a list/tuple");
  Py_ssize_t tags_size = 0;
  if (tags_fast == NULL) {
    return NULL;
  }

  tags_size = PySequence_Fast_GET_SIZE(tags_fast);
  if (tags_size == 0) {
    Py_RETURN_NONE;
  }

  PRINTDBG("drain( ");
  for (Py_ssize_t i = 0; i < tags_size; ++i) {
    PyObject *tag = PySequence_Fast_GET_ITEM(tags_fast, i);
    if (!PyUnicode_CheckExact(tag)) {
      Py_DECREF(tags_fast);
      PyErr_SetString(PyExc_TypeError, "tags must contain only str objects");
      return NULL;
    }

    PRINTOBJDBG(tag);
    PRINTFDBG(" ");
  }

  PRINTFDBG(")\n");

  for (Py_ssize_t i = 0; i < tags_size; ++i) {
    PyObject *tag = PySequence_Fast_GET_ITEM(tags_fast, i);
    while (true) {
      int_least64_t queue_index = boc_dequeue(tag, &message);
      if (queue_index < 0) {
        PyErr_Clear();
        break;
      }

      boc_message_free(message);
    }
  }

  Py_DECREF(tags_fast);
  Py_RETURN_NONE;
}

/// @brief Atomic counter for BOC behaviors
atomic_int_least64_t BOC_BEHAVIOR_COUNT = 0;

typedef struct behavior_s {
  /// @brief Resource count, set to len(args) + 1
  atomic_int_least64_t count;
  /// @brief Atomic reference count
  atomic_int_least64_t rc;

  /// @brief Unique behavior ID
  int_least64_t id;
  /// @brief Thunk
  BOCTag *thunk;
  /// @brief Cown which stores the result of calling the behavior
  BOCCown *result;
  /// @brief Grouping identifiers, used for reassembling lists of cowns
  int *group_ids;
  /// @brief The args buffer
  BOCCown **args;
  /// @brief The number of args
  Py_ssize_t args_size;
  /// @brief Variables captured by this behavior
  BOCCown **captures;
  /// @brief The number of captured variables
  Py_ssize_t captures_size;
} BOCBehavior;

/// @brief Capsule for holding a pointer to a behavior
typedef struct behavior_capsule_object {
  PyObject_HEAD BOCBehavior *behavior;
} BehaviorCapsuleObject;

BOCBehavior *behavior_new() {
  BOCBehavior *behavior;
  behavior = (BOCBehavior *)PyMem_RawMalloc(sizeof(BOCBehavior));
  if (behavior == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  behavior->id = atomic_fetch_add(&BOC_BEHAVIOR_COUNT, 1);
  behavior->thunk = NULL;
  behavior->result = NULL;
  behavior->rc = 0;
  behavior->group_ids = NULL;
  behavior->args_size = 0;
  behavior->args = NULL;
  behavior->captures_size = 0;
  behavior->captures = NULL;
  BOC_REF_TRACKING_ADD_BEHAVIOR();

  return behavior;
}

void behavior_free(BOCBehavior *behavior) {
  if (behavior->result != NULL) {
    COWN_DECREF(behavior->result);
  }

  if (behavior->group_ids != NULL) {
    PyMem_RawFree(behavior->group_ids);
  }

  if (behavior->args != NULL) {
    BOCCown **ptr = behavior->args;
    for (Py_ssize_t i = 0; i < behavior->args_size; ++i, ++ptr) {
      BOCCown *cown = *ptr;
      if (cown != NULL) {
        PRINTDBG("behavior_free/args\n");
        COWN_DECREF(cown);
      }
    }

    PyMem_RawFree(behavior->args);
  }

  if (behavior->captures != NULL) {
    BOCCown **ptr = behavior->captures;
    for (Py_ssize_t i = 0; i < behavior->captures_size; ++i, ++ptr) {
      BOCCown *cown = *ptr;
      if (cown != NULL) {
        PRINTDBG("behavior_free/captures\n");
        COWN_DECREF(cown);
      }
    }

    PyMem_RawFree(behavior->captures);
  }

  if (behavior->thunk != NULL) {
    BOCTag_free(behavior->thunk);
  }

  PyMem_RawFree(behavior);
  BOC_REF_TRACKING_REMOVE_BEHAVIOR();
}

static inline int_least64_t behavior_decref(BOCBehavior *behavior) {
  int_least64_t rc = atomic_fetch_add(&behavior->rc, -1) - 1;
  PRINTDBG("behavior_decref(bid=%" PRIdLEAST64 ") = %" PRIdLEAST64 "\n",
           behavior->id, rc);
  return rc;
}

#define BEHAVIOR_DECREF(c)                                                     \
  if (behavior_decref((c)) == 0) {                                             \
    behavior_free((c));                                                        \
  }

static inline int_least64_t behavior_incref(BOCBehavior *behavior) {
  int_least64_t rc = atomic_fetch_add(&behavior->rc, 1) + 1;
  PRINTDBG("behavior_incref(bid=%" PRIdLEAST64 ") = %" PRIdLEAST64 "\n",
           behavior->id, rc);
  return rc;
}

#define BEHAVIOR_INCREF(c) behavior_incref((c))

static void BehaviorCapsule_dealloc(PyObject *op) {
  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  PRINTDBG("BehaviorCapsule_dealloc(%p, bid=%" PRIdLEAST64 ")\n", self,
           self->behavior->id);
  if (self->behavior != NULL) {
    BEHAVIOR_DECREF(self->behavior);
    self->behavior = NULL;
  }

  Py_TYPE(self)->tp_free(self);
}

static PyObject *BehaviorCapsule_new(PyTypeObject *type, PyObject *args,
                                     PyObject *kwds) {
  BehaviorCapsuleObject *self;
  self = (BehaviorCapsuleObject *)type->tp_alloc(type, 0);
  if (self == NULL) {
    return NULL;
  }

  self->behavior = NULL;
  return (PyObject *)self;
}

/// @brief Add a sequence of variables to the behavior.
/// @details If an item is not a cown, it will be wrapped in a cown as part of
/// this operation.
/// @param op A PySequence which implements a Fast interface (e.g., PyList,
/// PyTuple)
/// @param size This will be set to the number of vars which were added
/// @return A pointer to the allocated list of cowns
static BOCCown **add_vars(PyObject *op, Py_ssize_t *size) {
  PyObject *items =
      PySequence_Fast(op, "Var sequence must provide a Fast interface");
  if (items == NULL) {
    return NULL;
  }

  Py_ssize_t num_vars = PySequence_Fast_GET_SIZE(items);
  BOCCown **vars =
      (BOCCown **)PyMem_RawCalloc((size_t)num_vars, sizeof(BOCCown *));
  if (vars == NULL) {
    PyErr_NoMemory();
    Py_DECREF(items);
    return NULL;
  }

  BOCCown **ptr = vars;
  for (Py_ssize_t i = 0; i < num_vars; ++i, ++ptr) {
    *ptr = NULL;
    PyObject *item = PySequence_Fast_GET_ITEM(items, i);

    if (CownCapsule_CheckExact(item)) {
      *ptr = cown_unwrap(item);
      if (*ptr == NULL) {
        goto error;
      }

      COWN_INCREF(*ptr);
      continue;
    }

    *ptr = BOCCown_new(item);
    if (*ptr == NULL) {
      goto error;
    }

    if (cown_release(*ptr) < 0) {
      goto error;
    }
  }

  *size = num_vars;
  Py_DECREF(items);

  return vars;

error:
  Py_DECREF(items);
  return NULL;
}

static int BehaviorCapsule_init(PyObject *op, PyObject *args,
                                PyObject *kwargs) {
  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  PyObject *thunk = NULL;
  PyObject *result = NULL;
  PyObject *cowns_list = NULL;
  PyObject *captures = NULL;

  if (!PyArg_ParseTuple(args, "O!O!OO", &PyUnicode_Type, &thunk,
                        BOC_STATE->cown_capsule_type, &result, &cowns_list,
                        &captures)) {
    return -1;
  }

  if (!PySequence_Check(cowns_list)) {
    PyErr_SetString(PyExc_TypeError, "args must be a sequence");
    return -1;
  }

  if (!PySequence_Check(captures)) {
    PyErr_SetString(PyExc_TypeError, "captures must be a sequence");
    return -1;
  }

  BOCBehavior *behavior = behavior_new();
  if (behavior == NULL) {
    return -1;
  }

  PRINTDBG("BehaviorCapsule_init(%p, bid=%" PRIdLEAST64 ")\n", self,
           behavior->id);
  self->behavior = behavior;
  BEHAVIOR_INCREF(behavior);

  behavior->thunk = tag_from_PyUnicode(thunk, NULL);
  if (behavior->thunk == NULL) {
    return -1;
  }

  behavior->result = cown_unwrap(result);
  if (behavior->result == NULL) {
    return -1;
  }

  COWN_INCREF(behavior->result);

  PRINTDBG("BehaviorCapsule(%" PRIdLEAST64 ") adding args...\n", behavior->id);

  PyObject *cowns_list_fast =
      PySequence_Fast(cowns_list, "Args must be provided as a list or tuple");
  if (cowns_list_fast == NULL) {
    return -1;
  }

  Py_ssize_t args_size = PySequence_Fast_GET_SIZE(cowns_list_fast);
  PyObject *cowns = PyTuple_New(args_size);
  if (cowns == NULL) {
    Py_DECREF(cowns_list_fast);
    return -1;
  }

  behavior->group_ids = PyMem_RawCalloc((size_t)args_size, sizeof(int));
  for (Py_ssize_t i = 0; i < args_size; ++i) {
    PyObject *item = PySequence_Fast_GET_ITEM(cowns_list_fast, i);
    int group_id;
    PyObject *cown;
    if (!PyArg_ParseTuple(item, "iO!", &group_id, BOC_STATE->cown_capsule_type,
                          &cown)) {
      Py_DECREF(cowns_list_fast);
      return -1;
    }

    behavior->group_ids[i] = group_id;
    PyTuple_SET_ITEM(cowns, i, Py_NewRef(cown));
  }

  Py_DECREF(cowns_list_fast);

  behavior->args = add_vars(cowns, &behavior->args_size);
  Py_DECREF(cowns);
  if (behavior->args == NULL) {
    return -1;
  }

  PRINTDBG("BehaviorCapsule(%" PRIdLEAST64 ") adding captures...\n",
           behavior->id);

  behavior->captures = add_vars(captures, &behavior->captures_size);
  if (behavior->captures == NULL) {
    return -1;
  }

  // We add two additional counts. One for the result, and another so that
  // the 2PL is finished before we start running the thunk. Without this,
  // the calls to release at the end of the thunk could race with the calls to
  // finish_enqueue in the 2PL.
  behavior->count = (int_least64_t)(behavior->args_size + 2);

  return 0;
}

/// @brief Resolves a single outstanding request for this behavior.
/// @details Called when a request is at the head of the queue for a particular
/// cown. If this is the last request, then the thunk is scheduled.
/// @param module the _core module
/// @param behavior the behavior capsule
/// @return None on success, NULL on error
static PyObject *behavior_resolve_one(BOCBehavior *behavior) {
  int_least64_t count = atomic_fetch_add(&behavior->count, -1) - 1;
  if (count == 0) {
    // send a message to the scheduler that this behavior can start
    PyObject *contents = Py_BuildValue("(si)", "start", behavior->id);
    if (contents == NULL) {
      return NULL;
    }

    PyObject *tag = PyUnicode_FromString("boc_behavior");
    if (tag == NULL) {
      return NULL;
    }

    BOCMessage *message = boc_message_new(tag, contents);
    Py_DECREF(contents);
    Py_DECREF(tag);

    if (message == NULL) {
      return NULL;
    }

    if (boc_enqueue(message) < 0) {
      PyErr_SetString(PyExc_RuntimeError, "Message queue is full");
      return NULL;
    }
  }

  Py_RETURN_NONE;
}

static PyObject *BehaviorCapsule_bid(PyObject *op, PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  return PyLong_FromLongLong(self->behavior->id);
}

static PyObject *BehaviorCapsule_thunk(PyObject *op,
                                       PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  return tag_to_PyUnicode(self->behavior->thunk);
}

static PyObject *request_new(BOCCown *cown);

static PyObject *BehaviorCapsule_create_requests(PyObject *op,
                                                 PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = self->behavior;

  PyObject *list = PyList_New(self->behavior->args_size + 1);
  if (list == NULL) {
    return NULL;
  }

  PyObject *capsule = request_new(self->behavior->result);
  if (capsule == NULL) {
    Py_DECREF(list);
    return NULL;
  }

  PyList_SET_ITEM(list, 0, capsule);

  BOCCown **ptr = behavior->args;
  for (Py_ssize_t i = 1; i <= self->behavior->args_size; ++i, ++ptr) {
    capsule = request_new(*ptr);
    if (capsule == NULL) {
      Py_DECREF(list);
      return NULL;
    }

    PyList_SET_ITEM(list, i, capsule);
  }

  return list;
}

static PyObject *BehaviorCapsule_set_result(PyObject *op, PyObject *args) {
  PyObject *value = NULL;

  if (!PyArg_ParseTuple(args, "O", &value)) {
    return NULL;
  }

  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = self->behavior;
  cown_set_value(behavior->result, value);
  Py_RETURN_NONE;
}

/// @brief Resolves a single outstanding request for this behavior.
/// @param module The _core module
/// @param args The behavior capsule
/// @return None on success, NULL on error
static PyObject *BehaviorCapsule_resolve_one(PyObject *op,
                                             PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  return behavior_resolve_one(self->behavior);
}

static int acquire_vars(BOCCown **vars, Py_ssize_t size) {
  BOCCown **ptr = vars;
  for (Py_ssize_t i = 0; i < size; ++i, ++ptr) {
    if (cown_acquire(*ptr) < 0) {
      return -1;
    }
  }

  return 0;
}

/// @brief Acquire all the cowns for the behavior.
/// @param args The behavior capsule
/// @return Py_None if successful, NULL otherwise
static PyObject *BehaviorCapsule_acquire(PyObject *op,
                                         PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = self->behavior;

  PRINTDBG("behavior_acquire(%" PRIdLEAST64 ")\n", behavior->id);

  if (cown_acquire(behavior->result) < 0) {
    return NULL;
  }

  if (acquire_vars(behavior->args, behavior->args_size) < 0) {
    return NULL;
  }

  if (acquire_vars(behavior->captures, behavior->captures_size) < 0) {
    return NULL;
  }

  Py_RETURN_NONE;
}

static int release_vars(BOCCown **vars, Py_ssize_t size) {
  BOCCown **ptr = vars;
  for (Py_ssize_t i = 0; i < size; ++i, ++ptr) {
    if (cown_release(*ptr) < 0) {
      return -1;
    }
  }

  return 0;
}

/// @brief Release the cowns for this behavior.
/// @param args The behavior capsule
/// @return Py_None if successful, NULL otherwise
static PyObject *BehaviorCapsule_release(PyObject *op,
                                         PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = self->behavior;

  PRINTDBG("behavior_release(%" PRIdLEAST64 ")\n", behavior->id);

  if (cown_release(behavior->result) < 0) {
    return NULL;
  }

  if (release_vars(behavior->args, behavior->args_size) < 0) {
    return NULL;
  }

  if (release_vars(behavior->captures, behavior->captures_size) < 0) {
    return NULL;
  }

  Py_RETURN_NONE;
}

/// @brief Executes the thunk on the behavior.
/// @details Before this function can be called, all of the cowns for the
/// behavior must be acquired.
/// @param args The Behavior, and the object or module which contains the named
/// thunk function.
/// @return The result of calling the thunk
static PyObject *BehaviorCapsule_execute(PyObject *op, PyObject *args) {
  PyObject *boc_export = NULL;

  if (!PyArg_ParseTuple(args, "O", &boc_export)) {
    return NULL;
  }

  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = self->behavior;

  size_t num_groups = 0;
  if (behavior->args_size > 0) {
    num_groups = abs(behavior->group_ids[behavior->args_size - 1]);
  }

  num_groups += behavior->captures_size;

  PyObject *thunk_args = PyTuple_New(num_groups);

  if (thunk_args == NULL) {
    return NULL;
  }

  Py_ssize_t arg_idx = 0;
  BOCCown **ptr = behavior->args;

  PyObject *group_list = NULL;
  int current_group_id = 0;
  // args are passed as CownCapsule objects
  for (Py_ssize_t i = 0; i < behavior->args_size; ++i, ++ptr) {
    PyObject *capsule = cown_capsule_wrap(*ptr, false);
    int group_id = behavior->group_ids[i];

    if (group_id == current_group_id) {
      // in a group, append to the current group
      if (PyList_Append(group_list, capsule) < 0) {
        Py_DECREF(thunk_args);
        Py_DECREF(group_list);
        return NULL;
      }

      Py_DECREF(capsule);
      continue;
    }

    if (group_list != NULL) {
      // the current group is complete, add it
      PyTuple_SET_ITEM(thunk_args, arg_idx, group_list);
      arg_idx += 1;
      group_list = NULL;
      current_group_id = 0;
    }

    if (group_id > 0) {
      // singleton
      PyTuple_SET_ITEM(thunk_args, arg_idx, capsule);
      arg_idx += 1;
      continue;
    }

    // new group
    group_list = PyList_New(1);
    if (group_list == NULL) {
      Py_DECREF(thunk_args);
      return NULL;
    }

    current_group_id = group_id;
    PyList_SET_ITEM(group_list, 0, capsule);
  }

  if (group_list != NULL) {
    // the final arg was a group
    PyTuple_SET_ITEM(thunk_args, arg_idx, group_list);
    arg_idx += 1;
    group_list = NULL;
    current_group_id = 0;
  }

  // captures are passed as raw values
  ptr = behavior->captures;
  for (Py_ssize_t i = 0; i < behavior->captures_size; ++i, ++arg_idx, ++ptr) {
    PyObject *value = Py_NewRef((*ptr)->value);
    PyTuple_SET_ITEM(thunk_args, arg_idx, value);
  }

  PyObject *thunk = PyObject_GetAttrString(boc_export, behavior->thunk->str);

  if (thunk == NULL) {
    return NULL;
  }

  PRINTDBG("Executing thunk...\n");

  PyObject *result = PyObject_Call(thunk, thunk_args, NULL);

  PRINTDBG("Thunk done. Freeing arguments.\n");

  Py_DECREF(thunk_args);
  Py_DECREF(thunk);

  PRINTDBG("Setting result.\n");

  if (result != NULL && strcmp(result->ob_type->tp_name, "Cown") == 0) {
    // attempt to unwrap the cown
    PyObject *capsule = PyObject_GetAttrString(result, "impl");
    Py_DECREF(result);
    result = capsule;
  }

  if (result == NULL) {
    result = PyErr_GetRaisedException();
    if (result == NULL) {
      result = PyObject_CallFunction(PyExc_RuntimeError, "s",
                                     "Unknown error when executing behavior");
    }
  }

  cown_set_value(behavior->result, result);
  return behavior->result->value;
}

static PyMethodDef BehaviorCapsule_methods[] = {
    {"bid", BehaviorCapsule_bid, METH_NOARGS, NULL},
    {"thunk", BehaviorCapsule_thunk, METH_NOARGS, NULL},
    {"create_requests", BehaviorCapsule_create_requests, METH_NOARGS, NULL},
    {"resolve_one", BehaviorCapsule_resolve_one, METH_NOARGS, NULL},
    {"set_result", BehaviorCapsule_set_result, METH_VARARGS, NULL},
    {"acquire", BehaviorCapsule_acquire, METH_NOARGS, NULL},
    {"release", BehaviorCapsule_release, METH_NOARGS, NULL},
    {"execute", BehaviorCapsule_execute, METH_VARARGS, NULL},
    {NULL} /* Sentinel */
};

static PyType_Slot BehaviorCapsule_slots[] = {
    {Py_tp_new, BehaviorCapsule_new},
    {Py_tp_init, BehaviorCapsule_init},
    {Py_tp_dealloc, BehaviorCapsule_dealloc},
    {Py_tp_methods, BehaviorCapsule_methods},
    {0, NULL} /* Sentinel */
};

static PyType_Spec BehaviorCapsule_Spec = {
    .name = "bocpy._core.BehaviorCapsule",
    .basicsize = sizeof(BehaviorCapsuleObject),
    .itemsize = 0,
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_IMMUTABLETYPE,
    .slots = BehaviorCapsule_slots};

#define BehaviorCapsule_CheckExact(op)                                         \
  Py_IS_TYPE((op), BOC_STATE->behavior_capsule_type)

static PyObject *_new_behavior_object(XIDATA_T *xidata) {
  BOCBehavior *behavior = (BOCBehavior *)xidata->data;

  PyTypeObject *type = BOC_STATE->behavior_capsule_type;
  BehaviorCapsuleObject *capsule =
      (BehaviorCapsuleObject *)type->tp_alloc(type, 0);
  if (capsule == NULL) {
    return NULL;
  }

  capsule->behavior = behavior;
  PRINTDBG("_new_behavior_object(%p)\n", capsule);
  BEHAVIOR_INCREF(behavior);
  return (PyObject *)capsule;
}

static int _behavior_shared(
#ifndef BOC_NO_MULTIGIL
    PyThreadState *tstate,
#endif
    PyObject *obj, XIDATA_T *xidata) {
#ifdef BOC_NO_MULTIGIL
  PyThreadState *tstate = PyThreadState_GET();
#endif

  BehaviorCapsuleObject *capsule = (BehaviorCapsuleObject *)obj;
  BOCBehavior *behavior = capsule->behavior;

  // all we do to initialise the xidata is store a pointer to the behavior
  XIDATA_INIT(xidata, tstate->interp, behavior, obj, _new_behavior_object);
  return 0;
}

/// @brief Encapsulates a behavior's request for a cown
typedef struct boc_request {
  /// @brief The cown that has been requested
  BOCCown *target;
  /// @brief The ID of the next behavior
  atomic_intptr_t next;
  /// @brief Whether the request has been scheduled
  atomic_int_least64_t scheduled;
} BOCRequest;

/// @brief Frees the request capsule
/// @param capsule A capsule containing a request object
void request_free(PyObject *capsule) {
  BOCRequest *request =
      (BOCRequest *)PyCapsule_GetPointer(capsule, "boc_request");
  PRINTDBG("request_free(%p)\n", request);
  COWN_DECREF(request->target);
  BOCBehavior *behavior = (BOCBehavior *)atomic_load(&request->next);
  if (behavior != NULL) {
    BEHAVIOR_DECREF(behavior);
  }

  PyMem_RawFree(request);
}

PyObject *request_new(BOCCown *cown) {
  BOCRequest *request = (BOCRequest *)PyMem_RawMalloc(sizeof(BOCRequest));
  if (request == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  request->target = cown;
  PRINTDBG("request_new(%p)\n", request);
  COWN_INCREF(cown);
  request->next = 0;
  request->scheduled = 0;
  PyObject *capsule =
      PyCapsule_New((void *)request, "boc_request", request_free);
  if (capsule == NULL) {
    COWN_DECREF(cown);
    return NULL;
  }

  return capsule;
}

/// @brief Creates a request for a cown.
/// @param module The _core module
/// @param args The CownCapsule object
/// @return A capsule containing the request
static PyObject *request_create(PyObject *module, PyObject *args) {
  BOC_STATE_SET(module);

  PyObject *op;

  if (!PyArg_ParseTuple(args, "O", &op)) {
    return NULL;
  }

  BOCCown *cown = cown_unwrap(op);
  if (cown == NULL) {
    return NULL;
  }

  return request_new(cown);
}

/// @brief Unwraps a request from its capsule.
/// @param op The Capsule object
/// @return a reference to a request, or NULL if there was an error
static BOCRequest *request_unwrap(PyObject *op) {
  if (!PyCapsule_CheckExact(op)) {
    PyErr_SetString(PyExc_ValueError, "Expected a capsule");
    return NULL;
  }

  return (BOCRequest *)PyCapsule_GetPointer(op, "boc_request");
}

// Release the cown to the next behavior.
// This is called when the associated behavior has completed, and thus can
// allow any waiting behavior to run.
// If there is no next behavior, then the cown's `last` pointer is set to null.

/// @brief Release the cown to the next behavior.
/// @details This is called when the associated behavior has completed, and thus
/// can allow any waiting behavior to run. If there is no next behavior, then
/// the cown's `last` pointer is set to null.
/// @param module The _core module
/// @param args The request to release
/// @return None if successful, NULL otherwise
static PyObject *request_release(PyObject *module, PyObject *args) {
  BOC_STATE_SET(module);

  PyObject *op;

  if (!PyArg_ParseTuple(args, "O", &op)) {
    return NULL;
  }

  BOCRequest *request = request_unwrap(op);
  if (request == NULL) {
    return NULL;
  }

  // This code is effectively a MCS-style queue lock release.
  BOCBehavior *next = (BOCBehavior *)atomic_load(&request->next);
  if (next == NULL) {
    intptr_t expected_ptr = (intptr_t)request;
    if (atomic_compare_exchange_strong(&request->target->last, &expected_ptr,
                                       0)) {
      Py_RETURN_NONE;
    }
  }

  // Wait for the next pointer to be set. The target.last != this request
  // so this should not take long.

  while (true) {
    next = (BOCBehavior *)atomic_load(&request->next);
    if (next) {
      break;
    }
  }

  return behavior_resolve_one(next);
}

/// @brief Enqueues this request on the cown
/// @param module The _core module
/// @param args The request to enqueue, and the associated behavior
/// @return None if successful, NULL otherwise
static PyObject *request_start_enqueue(PyObject *module, PyObject *args) {
  BOC_STATE_SET(module);

  PyObject *op;
  PyObject *behavior_op;

  if (!PyArg_ParseTuple(args, "OO", &op, &behavior_op)) {
    return NULL;
  }

  BOCRequest *request = request_unwrap(op);
  if (request == NULL) {
    return NULL;
  }

  if (!BehaviorCapsule_CheckExact(behavior_op)) {
    PyErr_SetString(PyExc_TypeError, "Expected a BehaviorCapsule object");
    return NULL;
  }

  BehaviorCapsuleObject *behavior_capsule =
      (BehaviorCapsuleObject *)behavior_op;
  BOCBehavior *behavior = behavior_capsule->behavior;

  intptr_t request_ptr = (intptr_t)request;
  intptr_t prev_ptr = atomic_exchange(&request->target->last, request_ptr);
  if (prev_ptr == 0) {
    // there is no prior request queued on the cown, so we can immediately
    // proceed
    return behavior_resolve_one(behavior);
  }

  intptr_t behavior_ptr = (intptr_t)behavior;
  BOCRequest *prev = (BOCRequest *)prev_ptr;
  assert(atomic_load(&prev->next) == 0);
  atomic_store(&prev->next, behavior_ptr);
  PRINTDBG("request->next = bid=%" PRIdLEAST64 "\n", behavior->id);
  BEHAVIOR_INCREF(behavior);
  // wait for the previous request to be scheduled
  while (true) {
    if (atomic_load(&prev->scheduled)) {
      break;
    }
  }

  Py_RETURN_NONE;
}

/// @brief Finalises the scheduling of the request.
/// @param module The _core module
/// @param args The request
/// @return None if successful, NULL otherwise
static PyObject *request_finish_enqueue(PyObject *module, PyObject *args) {
  PyObject *op;

  if (!PyArg_ParseTuple(args, "O", &op)) {
    return NULL;
  }

  BOCRequest *request = request_unwrap(op);
  if (request == NULL) {
    return NULL;
  }

  atomic_exchange(&request->scheduled, true);

  Py_RETURN_NONE;
}

static PyObject *request_target(PyObject *module, PyObject *args) {
  PyObject *op;

  if (!PyArg_ParseTuple(args, "O", &op)) {
    return NULL;
  }

  BOCRequest *request = request_unwrap(op);
  if (request == NULL) {
    return NULL;
  }

  return PyLong_FromVoidPtr((void *)request->target);
}

/// @brief Whether this module is the "primary" module, i.e. the one owned by
/// the scheduler.
/// @param module The module to check
/// @param Py_UNUSED
/// @return Whether this is the primary module
static PyObject *_core_is_primary(PyObject *module,
                                  PyObject *Py_UNUSED(dummy)) {
  BOC_STATE_SET(module);
  if (BOC_STATE->index == 0) {
    Py_RETURN_TRUE;
  }

  Py_RETURN_FALSE;
}

/// @brief Returns the unique index of the module.
/// @param module The module to query
/// @param Py_UNUSED
/// @return The unique module index as a PyLong
static PyObject *_core_index(PyObject *module, PyObject *Py_UNUSED(dummy)) {
  BOC_STATE_SET(module);
  return PyLong_FromLongLong(BOC_STATE->index);
}

static PyObject *_core_recycle(PyObject *module, PyObject *Py_UNUSED(dummy)) {
  BOC_STATE_SET(module);
  PRINTDBG("recycle()\n");
  BOCRecycleQueue_empty(BOC_STATE->recycle_queue, true);
  Py_RETURN_NONE;
}

/// @brief Returns any cowns for which this module has a weak reference.
/// @details These are cowns which were sent by this module to another module
/// and have not yet been recycled. As such, those cowns contain XIData which
/// may have one or more objects that were allocated on this interpreter, and
/// need to be deallocated before the interpreter can be destroyed.
/// @param module The module to check
/// @param Py_UNUSED
/// @return A tuple containing cowns that need to be released before the
/// interpreter that owns this module can be destroyed.
static PyObject *_core_cowns(PyObject *module, PyObject *Py_UNUSED(dummy)) {
  BOC_STATE_SET(module);
  BOCRecycleQueue_empty(BOC_STATE->recycle_queue, true);
  PRINTDBG("cowns()\n");

  return BOCRecycleQueue_promote_cowns(BOC_STATE->recycle_queue);
}

/// @brief Method to reset the message queues with new tags.
/// @details Once this function, which can only be called from the main
/// interpreter, is called the messaging system will be temporarily disabled.
/// The queues will be reassigned, and then emptied and reset.
/// @param module The _core module
/// @param args A single argument of a sequence of PyUnicode
/// @return NULL if an error, PyNone otherwise.
static PyObject *_core_set_tags(PyObject *module, PyObject *args) {
  BOC_STATE_SET(module);
  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "set_tags can only be called from the main interpreter");
    return NULL;
  }

  PyObject *tags = NULL;
  if (!PyArg_ParseTuple(args, "O", &tags)) {
    return NULL;
  }

  Py_ssize_t tags_size = PySequence_Size(tags);
  if (tags_size < 0) {
    PyErr_SetString(PyExc_TypeError, "Tags must provide __len__");
    return NULL;
  }

  if (tags_size > BOC_QUEUE_COUNT) {
    PyErr_Format(PyExc_IndexError, "Only %d or fewer tags supported",
                 BOC_QUEUE_COUNT);
    return NULL;
  }

  // go queue by queue, disable it, and set the new tag
  BOCQueue *qptr = BOC_QUEUES;
  for (Py_ssize_t i = 0; i < BOC_QUEUE_COUNT; ++i, ++qptr) {
    // disable the queue
    atomic_store(&qptr->state, BOC_QUEUE_DISABLED);

    if (i >= tags_size) {
      // clear the tags on these unused queues
      BOCTag *oldtag = (BOCTag *)atomic_exchange(&qptr->tag, (intptr_t)NULL);
      if (oldtag != NULL) {
        tag_disable(oldtag);
        TAG_DECREF(oldtag);
      }
      continue;
    }

    PyObject *item = PySequence_GetItem(tags, i);
    if (!PyUnicode_CheckExact(item)) {
      PyErr_SetString(PyExc_TypeError, "Tags must contain only str objects");
      Py_DECREF(item);
      return NULL;
    }

    BOCTag *qtag = tag_from_PyUnicode(item, qptr);
    Py_DECREF(item);

    if (qtag == NULL) {
      return NULL;
    }

    // assign a new tag
    BOCTag *oldtag = (BOCTag *)atomic_exchange(&qptr->tag, (intptr_t)qtag);
    TAG_INCREF(qtag);
    if (oldtag != NULL) {
      tag_disable(oldtag);
      TAG_DECREF(oldtag);
    }
  }

  // now that all of the queue tags have been updated, drain all messages
  qptr = BOC_QUEUES;
  for (Py_ssize_t i = 0; i < BOC_QUEUE_COUNT; ++i, ++qptr) {
    int_least64_t head = atomic_load(&qptr->head);
    int_least64_t tail = atomic_load(&qptr->tail);
    if (head != tail) {
      for (int_least64_t i = head; i < tail; ++i) {
        int_least64_t index = i % BOC_CAPACITY;
        while (qptr->messages[index] == NULL) {
          // spin waiting for the message to be written
          Py_BEGIN_ALLOW_THREADS thrd_sleep(&SLEEP_TS, NULL);
          Py_END_ALLOW_THREADS
        }

        boc_message_free(qptr->messages[index]);
        qptr->messages[index] = NULL;
      }
    }

    // reset the queue
    atomic_store(&qptr->head, 0);
    atomic_store(&qptr->tail, 0);
    if (i < tags_size) {
      atomic_store(&qptr->state, BOC_QUEUE_ASSIGNED);
    } else {
      atomic_store(&qptr->state, BOC_QUEUE_UNASSIGNED);
    }
  }

  Py_RETURN_NONE;
}

static PyMethodDef _core_module_methods[] = {
    {"send", _core_send, METH_VARARGS, NULL},
    {"receive", (PyCFunction)(void (*)(void))_core_receive,
     METH_VARARGS | METH_KEYWORDS, NULL},
    {"drain", _core_drain, METH_VARARGS, NULL},
    {"request_create", request_create, METH_VARARGS, NULL},
    {"request_release", request_release, METH_VARARGS, NULL},
    {"request_start_enqueue", request_start_enqueue, METH_VARARGS, NULL},
    {"request_finish_enqueue", request_finish_enqueue, METH_VARARGS, NULL},
    {"request_target", request_target, METH_VARARGS, NULL},
    {"is_primary", _core_is_primary, METH_NOARGS, NULL},
    {"index", _core_index, METH_NOARGS, NULL},
    {"recycle", _core_recycle, METH_NOARGS, NULL},
    {"cowns", _core_cowns, METH_NOARGS, NULL},
    {"set_tags", _core_set_tags, METH_VARARGS, NULL},
    {NULL} /* Sentinel */
};

static int _core_module_exec(PyObject *module) {
  int_least64_t index = atomic_fetch_add(&BOC_COUNT, 1);
  PRINTFDBG("boc_exec(index=%" PRIdLEAST64 ")\n", index);
  if (index == 0) {
    BOCQueue *qptr = BOC_QUEUES;
    for (size_t i = 0; i < BOC_QUEUE_COUNT; ++i, ++qptr) {
      qptr->index = i;
      qptr->messages =
          (BOCMessage **)PyMem_RawCalloc(BOC_CAPACITY, sizeof(BOCMessage *));
      memset(qptr->messages, 0, BOC_CAPACITY * sizeof(BOCMessage *));
      qptr->head = 0;
      qptr->tail = 0;
      qptr->state = BOC_QUEUE_UNASSIGNED;
      qptr->tag = 0;
    }

    BOCRecycleQueue *queue_stub =
        (BOCRecycleQueue *)PyMem_RawMalloc(sizeof(BOCRecycleQueue));
    queue_stub->head = 0;
    queue_stub->tail = NULL;
    queue_stub->next = 0;
    atomic_store(&BOC_RECYCLE_QUEUE_HEAD, (intptr_t)queue_stub);
    BOC_RECYCLE_QUEUE_TAIL = queue_stub;

#ifdef BOC_REF_TRACKING
    timespec_get(&BOC_LAST_REF_TRACKING_REPORT, TIME_UTC);
#endif
  }

  _core_module_state *state = (_core_module_state *)PyModule_GetState(module);
  state->index = index;
  state->recycle_queue = BOCRecycleQueue_new(index);
  if (state->recycle_queue == NULL) {
    return -1;
  }

  state->pickle = PyImport_ImportModule("pickle");
  if (state->pickle == NULL) {
    return -1;
  }

  state->dumps = PyObject_GetAttrString(state->pickle, "dumps");
  if (state->dumps == NULL) {
    Py_DECREF(state->pickle);
    return -1;
  }

  state->loads = PyObject_GetAttrString(state->pickle, "loads");
  if (state->loads == NULL) {
    Py_DECREF(state->pickle);
    Py_DECREF(state->dumps);
    return -1;
  }

  for (size_t i = 0; i < BOC_QUEUE_COUNT; ++i) {
    state->queue_tags[i] = NULL;
  }

  state->cown_capsule_type =
      (PyTypeObject *)PyType_FromModuleAndSpec(module, &CownCapsule_Spec, NULL);
  if (state->cown_capsule_type == NULL) {
    return -1;
  }

  if (PyModule_AddType(module, state->cown_capsule_type) < 0) {
    return -1;
  }

  if (XIDATA_REGISTERCLASS(state->cown_capsule_type, _cown_shared)) {
    Py_FatalError(
        "could not register CownCapsule for cross-interpreter sharing");
    return -1;
  }

  state->behavior_capsule_type = (PyTypeObject *)PyType_FromModuleAndSpec(
      module, &BehaviorCapsule_Spec, NULL);
  if (state->behavior_capsule_type == NULL) {
    return -1;
  }

  if (PyModule_AddType(module, state->behavior_capsule_type) < 0) {
    return -1;
  }

  if (XIDATA_REGISTERCLASS(state->behavior_capsule_type, _behavior_shared)) {
    Py_FatalError(
        "could not register BehaviorCapsule for cross-interpreter sharing");
    return -1;
  }

  assert(BOC_STATE == NULL);
  BOC_STATE = state;

  PyModule_AddStringConstant(module, "TIMEOUT", BOC_TIMEOUT);
  return 0;
}

static int _core_module_clear(PyObject *module) {
  PRINTDBG("_core_module_clear\n");
  _core_module_state *state = (_core_module_state *)PyModule_GetState(module);
  Py_CLEAR(state->loads);
  Py_CLEAR(state->dumps);
  Py_CLEAR(state->pickle);
  Py_CLEAR(state->cown_capsule_type);
  Py_CLEAR(state->behavior_capsule_type);
  // this needs to be cleared here, as it was allocated on this interpreter.
  Py_CLEAR(state->recycle_queue->xidata_to_cowns);
  return 0;
}

void _core_module_free(void *module_ptr) {
  PyObject *module = (PyObject *)module_ptr;
  _core_module_state *state = (_core_module_state *)PyModule_GetState(module);

  PRINTDBG("begin boc_free(index=%" PRIdLEAST64 ")\n", state->index);
  PRINTDBG("Emptying _core recycle queue...\n");

  BOCRecycleQueue_empty(state->recycle_queue, true);

  _core_module_clear(module);
  for (size_t i = 0; i < BOC_QUEUE_COUNT; ++i) {
    if (state->queue_tags[i] != NULL) {
      TAG_DECREF(state->queue_tags[i]);
    }
  }

  int_least64_t remaining = atomic_fetch_sub(&BOC_COUNT, 1) - 1;
  if (remaining == 0) {
    PRINTDBG("All _core modules have been freed, cleaning up\n");

    // last one, clean up
    BOCQueue *qptr = BOC_QUEUES;
    for (size_t i = 0; i < BOC_QUEUE_COUNT; ++i, ++qptr) {
      PyMem_RawFree(qptr->messages);

      if (atomic_load(&qptr->state) == BOC_QUEUE_ASSIGNED) {
        BOCTag *qtag = (BOCTag *)atomic_load(&qptr->tag);
        assert(qtag->queue == qptr);
        BOCTag_free(qtag);
      }
    }

    BOCRecycleQueue *queue = (BOCRecycleQueue *)BOC_RECYCLE_QUEUE_TAIL;
    while (atomic_load(&queue->next) != 0) {
      BOCRecycleQueue *next = (BOCRecycleQueue *)queue->next;
      BOCRecycleQueue_free(queue);
      queue = next;
    }

    BOCRecycleQueue_free(queue);
    BOC_RECYCLE_QUEUE_TAIL = NULL;
    atomic_store(&BOC_RECYCLE_QUEUE_HEAD, 0);
    BOC_REF_TRACKING_REPORT();
  }

  PRINTDBG("end boc_free(index=%" PRIdLEAST64 ")\n", state->index);
}

static int _core_module_traverse(PyObject *module, visitproc visit, void *arg) {
  _core_module_state *state = (_core_module_state *)PyModule_GetState(module);
  Py_VISIT(state->loads);
  Py_VISIT(state->dumps);
  Py_VISIT(state->pickle);
  Py_VISIT(state->cown_capsule_type);
  Py_VISIT(state->behavior_capsule_type);
  Py_VISIT(state->recycle_queue->xidata_to_cowns);
  return 0;
}

#ifdef Py_mod_exec
static PyModuleDef_Slot _core_module_slots[] = {
    {Py_mod_exec, (void *)_core_module_exec},
#if PY_VERSION_HEX >= 0x030C0000
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
#endif
    {0, NULL},
};
#endif

static PyModuleDef _core_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_core",
    .m_doc = "Provides the underlying C implementation for the core BOC "
             "functionality",
    .m_methods = _core_module_methods,
    .m_free = (freefunc)_core_module_free,
    .m_traverse = _core_module_traverse,
    .m_clear = _core_module_clear,
#ifdef Py_mod_exec
    .m_slots = _core_module_slots,
#endif
    .m_size = sizeof(_core_module_state)};

PyMODINIT_FUNC PyInit__core(void) {
#ifdef Py_mod_exec
  return PyModuleDef_Init(&_core_module);
#else
  PyObject *module;
  module = PyModule_Create(&_core_module);
  if (module == NULL)
    return NULL;

  if (_core_exec(module) != 0) {
    Py_DECREF(module);
    return NULL;
  }

  return module;
#endif
}