#define PY_SSIZE_T_CLEAN

#include "compat.h"
#include "cown.h"
#include "noticeboard.h"
#include "sched.h"
#include "tags.h"
#include "terminator.h"
#include "xidata.h"

// Forward declaration — BOCQueue is defined below.
typedef struct boc_queue BOCQueue;

/// @brief Initialize the park mutex and condition variable for a queue
/// @param q The queue to initialize
static inline void boc_park_init(BOCQueue *q);

/// @brief Destroy the park mutex and condition variable for a queue
/// @param q The queue to destroy
static inline void boc_park_destroy(BOCQueue *q);

/// @brief Lock the park mutex for a queue
/// @param q The queue whose mutex to lock
static inline void boc_park_lock(BOCQueue *q);

/// @brief Unlock the park mutex for a queue
/// @param q The queue whose mutex to unlock
static inline void boc_park_unlock(BOCQueue *q);

/// @brief Wake one thread parked on a queue's condition variable
/// @note Caller MUST hold park_mutex
/// @param q The queue to signal
static inline void boc_park_signal(BOCQueue *q);

/// @brief Wake all threads parked on a queue's condition variable
/// @note Caller MUST hold park_mutex
/// @param q The queue to broadcast
static inline void boc_park_broadcast(BOCQueue *q);

/// @brief Park the calling thread on a queue's condition variable
/// @note Caller MUST hold park_mutex. The mutex is atomically released during
/// the wait and re-acquired before returning.
/// @param q The queue to park on
static inline void boc_park_wait(BOCQueue *q);

const struct timespec SLEEP_TS = {0, 1000};
const char *BOC_TIMEOUT = "__timeout__";
const int BOC_CAPACITY = 1024 * 16;
const PY_INT64_T NO_OWNER = -2;
atomic_int_least64_t BOC_COUNT = 0;
atomic_int_least64_t BOC_COWN_COUNT = 0;

#define BOC_SPIN_COUNT 64
#define BOC_BACKOFF_CAP_NS 1000000 // 1 ms

// #define BOC_REF_TRACKING
// #define BOC_TRACE

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

  /// @brief Number of threads parked on this queue's condvar
  atomic_int_least64_t waiters;
  /// @brief Mutex protecting condvar signal/wait
  BOCMutex park_mutex;
  /// @brief Condition variable for parking receivers
  BOCCond park_cond;

  // Contention counters. Bumped with BOC_MO_RELAXED inside
  // boc_enqueue / boc_dequeue. Read by `_core.queue_stats()`. Grouped
  // and padded so they sit on their own cacheline and do not
  // false-share with the hot head/tail/state above. Typed via
  // `compat.h` so the build works on MSVC (which has no `_Atomic`).
  /// @brief CAS retries observed by enqueuers contending on @c tail.
  boc_atomic_u64_t enqueue_cas_retries;
  /// @brief CAS retries observed by dequeuers contending on @c head.
  boc_atomic_u64_t dequeue_cas_retries;
  /// @brief Successful enqueues (post-CAS).
  boc_atomic_u64_t pushed_total;
  /// @brief Successful dequeues (post-CAS).
  boc_atomic_u64_t popped_total;
  /// @brief Padding so the next BOCQueue starts on a fresh cacheline.
  char _pad_counters[64 - (4 * sizeof(uint64_t)) % 64];
} BOCQueue;

#define BOC_QUEUE_COUNT 16
const int_least64_t BOC_QUEUE_UNASSIGNED = 0;
const int_least64_t BOC_QUEUE_ASSIGNED = 1;
const int_least64_t BOC_QUEUE_DISABLED = 2;
static BOCQueue BOC_QUEUES[BOC_QUEUE_COUNT];
static BOCRecycleQueue *BOC_RECYCLE_QUEUE_TAIL = NULL;
static atomic_intptr_t BOC_RECYCLE_QUEUE_HEAD = 0;

// Platform condvar implementation
// ---------------------------------------------------------------------------

static inline void boc_park_init(BOCQueue *q) {
  boc_mtx_init(&q->park_mutex);
  cnd_init(&q->park_cond);
}

static inline void boc_park_destroy(BOCQueue *q) {
  cnd_destroy(&q->park_cond);
  mtx_destroy(&q->park_mutex);
}

static inline void boc_park_lock(BOCQueue *q) { mtx_lock(&q->park_mutex); }

static inline void boc_park_unlock(BOCQueue *q) { mtx_unlock(&q->park_mutex); }

static inline void boc_park_signal(BOCQueue *q) { cnd_signal(&q->park_cond); }

static inline void boc_park_broadcast(BOCQueue *q) {
  cnd_broadcast(&q->park_cond);
}

static inline void boc_park_wait(BOCQueue *q) {
  cnd_wait(&q->park_cond, &q->park_mutex);
}

// Noticeboard function implementations are below object_to_xidata

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
#ifdef _WIN32
  timespec_get(&ts, TIME_UTC);
#else
  clock_gettime(CLOCK_REALTIME, &ts);
#endif
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
  intptr_t old_head_ptr =
      atomic_exchange_intptr(&BOC_RECYCLE_QUEUE_HEAD, queue_ptr);
  if (old_head_ptr == 0) {
    return queue;
  }

  BOCRecycleQueue *old_head = (BOCRecycleQueue *)old_head_ptr;
  old_head->index = index;
  old_head->tail = node;
  atomic_store_intptr(&old_head->head, node_ptr);
  atomic_store_intptr(&old_head->next, queue_ptr);

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

// ---------------------------------------------------------------------------
// Noticeboard C functions
// ---------------------------------------------------------------------------

/// @brief Write a key-value pair into the noticeboard under mutex
/// @details The value is serialized to XIData here (in the main interpreter),
/// so XIDATA_FREE is always safe to call from the same interpreter. The
/// optional third argument is a sequence of CownCapsule objects whose
/// underlying BOCCowns are referenced by the serialized bytes; the
/// noticeboard takes a strong reference on each so that they outlive
/// every reader's pickled view, regardless of whether the original
/// CownCapsule is dropped by user code.
/// @param self The module
/// @param args Tuple of (key: str, value: object[, cowns: sequence])
/// @return Py_None on success, NULL on error
static PyObject *_core_noticeboard_write_direct(PyObject *self,
                                                PyObject *args) {
  BOC_STATE_SET(self);

  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "noticeboard_write_direct must be called from the primary "
                    "interpreter");
    return NULL;
  }

  if (noticeboard_check_thread("noticeboard_write_direct") < 0) {
    return NULL;
  }

  const char *key;
  Py_ssize_t key_len;
  PyObject *value;
  PyObject *cowns = Py_None;

  if (!PyArg_ParseTuple(args, "s#O|O", &key, &key_len, &value, &cowns)) {
    return NULL;
  }

  // Pin the cowns BEFORE serializing so an error here does not leave us
  // with a stored entry whose cowns can be freed under us.
  BOCCown **new_pins = NULL;
  int new_pin_count = 0;
  if (nb_pin_cowns(cowns, &new_pins, &new_pin_count) < 0) {
    return NULL;
  }

  // Serialize the value to XIData in the main interpreter.
  XIDATA_T *xidata = NULL;
  PyObject *pickled = object_to_xidata(value, &xidata);
  if (pickled == NULL) {
    if (xidata != NULL) {
      XIDATA_FREE(xidata);
    }
    // Roll back the pins we just took.
    for (int i = 0; i < new_pin_count; i++) {
      COWN_DECREF(new_pins[i]);
    }
    PyMem_RawFree(new_pins);
    return NULL;
  }

  bool is_pickled = (pickled == Py_True);
  Py_DECREF(pickled);

  // noticeboard_write takes ownership of xidata + pins on success and
  // frees them on failure.
  if (noticeboard_write(key, key_len, xidata, is_pickled, new_pins,
                        new_pin_count) < 0) {
    return NULL;
  }

  // Note: this thread's snapshot cache is intentionally NOT cleared.
  // Within a behavior, a writer must not observe its own write — that
  // is the no-polling invariant. The cache will be lazily revalidated
  // at the next behavior boundary (see _core_noticeboard_cache_clear).

  Py_RETURN_NONE;
}

/// @brief Return a cached read-only snapshot of the noticeboard
/// @details Three fast paths, in order:
///   1. If @ref NB_VERSION_CHECKED is true, the cached proxy was already
///      validated for this behavior; return it without consulting
///      @ref NB_VERSION. This preserves the no-polling invariant: a
///      behavior cannot observe writes that happened mid-flight, even
///      its own.
///   2. If the cached dict's @ref NB_SNAPSHOT_VERSION matches the
///      current @ref NB_VERSION, the cache is still fresh; mark it
///      checked and return the proxy.
///   3. Otherwise, drop the cache and fall through to the rebuild.
/// The rebuild reads all entries under @ref Noticeboard::mutex,
/// captures the version while still holding the mutex (so a writer
/// cannot race past us), deserializes non-pickled values immediately,
/// and defers @c pickle.loads to after the mutex is released. The
/// returned object is a @c types.MappingProxyType wrapping the cached
/// dict; user code cannot mutate the cache through it.
/// @param self The module
/// @return A read-only mapping (MappingProxyType) of str → Python object
static PyObject *_core_noticeboard_snapshot(PyObject *self,
                                            PyObject *Py_UNUSED(dummy)) {
  BOC_STATE_SET(self);
  return noticeboard_snapshot(BOC_STATE->loads);
}

/// @brief Clear all noticeboard entries and free their XIData and pins
/// @details Safe to call XIDATA_FREE directly because all noticeboard XIData
/// is created by the main interpreter (in write_direct). Also drops every
/// entry's pinned cowns (COWN_DECREF) and clears the calling thread's
/// snapshot cache so that any cached proxy from before the clear is not
/// reused after a runtime restart. Other threads' caches will lazily
/// revalidate on their next snapshot call thanks to the @ref NB_VERSION
/// bump; their cached CownCapsules keep the underlying BOCCowns alive
/// until each cache is dropped.
/// @param self The module (unused)
/// @param args Unused
/// @return Py_None
static PyObject *_core_noticeboard_clear(PyObject *self,
                                         PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);

  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "noticeboard_clear must be called from the primary "
                    "interpreter");
    return NULL;
  }

  noticeboard_clear();
  Py_RETURN_NONE;
}

/// @brief Delete a single noticeboard entry by key
/// @details Acquires mutex, finds the entry, frees its XIData and pinned
/// cowns, shifts remaining entries down, and decrements count. No-op if
/// key not found.
/// @param self The module
/// @param args Tuple of (key: str)
/// @return Py_None on success, NULL on error
static PyObject *_core_noticeboard_delete(PyObject *self, PyObject *args) {
  BOC_STATE_SET(self);

  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "noticeboard_delete must be called from the primary "
                    "interpreter");
    return NULL;
  }

  if (noticeboard_check_thread("noticeboard_delete") < 0) {
    return NULL;
  }

  const char *key;
  Py_ssize_t key_len;

  if (!PyArg_ParseTuple(args, "s#", &key, &key_len)) {
    return NULL;
  }

  if (noticeboard_delete(key, key_len) < 0) {
    return NULL;
  }

  // Note: this thread's snapshot cache is intentionally NOT cleared;
  // the no-polling invariant applies equally to deletes.

  Py_RETURN_NONE;
}

/// @brief Re-arm the per-behavior version check on the cached snapshot
/// @details Called by the worker loop at every behavior boundary. Does
/// NOT drop @ref NB_SNAPSHOT_PROXY: the cache may still be valid, and
/// the next call to @ref _core_noticeboard_snapshot will perform exactly
/// one atomic load against @ref NB_VERSION to find out. Within a
/// behavior, the cache is then returned unconditionally for any further
/// calls, preserving the no-polling invariant.
/// @param self The module (unused)
/// @param args Unused
/// @return Py_None
static PyObject *_core_noticeboard_cache_clear(PyObject *self,
                                               PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  noticeboard_cache_clear_for_behavior();
  Py_RETURN_NONE;
}

/// @brief Return the current noticeboard version counter
/// @details The counter is incremented under @ref Noticeboard::mutex on
/// every successful @c notice_write, @c notice_delete, or
/// @c noticeboard_clear. Read with sequentially-consistent semantics.
/// Two reads returning the same value mean no commit happened between
/// them; a strictly larger value means at least one commit happened.
/// Useful for detecting noticeboard changes without taking a full
/// snapshot.
/// @param self The module (unused)
/// @param args Unused
/// @return A Python int with the current noticeboard version
static PyObject *_core_noticeboard_version(PyObject *self,
                                           PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  return PyLong_FromLongLong((long long)noticeboard_version());
}

/// @brief Register the calling thread as the noticeboard mutator thread
/// @details Must be called from the noticeboard thread before it processes
/// any noticeboard mutation messages. Subsequent calls to
/// @ref _core_noticeboard_write_direct or @ref _core_noticeboard_delete
/// from any other thread will raise @c RuntimeError. Pass with no
/// arguments to install the current thread; the registration is global
/// and persists until the runtime stops.
/// @param self The module (unused)
/// @param args Unused
/// @return Py_None
static PyObject *_core_set_noticeboard_thread(PyObject *self,
                                              PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "set_noticeboard_thread must be called from the primary "
                    "interpreter");
    return NULL;
  }
  if (noticeboard_set_thread() < 0) {
    return NULL;
  }
  Py_RETURN_NONE;
}

/// @brief Clear the registered noticeboard mutator thread
/// @details Restores the permissive (pre-startup) check. Called by the
/// Python @c Behaviors.stop path after the noticeboard thread has joined
/// so that subsequent main-thread calls (e.g. @c noticeboard_clear from
/// a runtime restart cycle) are not rejected.
/// @param self The module (unused)
/// @param args Unused
/// @return Py_None
static PyObject *_core_clear_noticeboard_thread(PyObject *self,
                                                PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "clear_noticeboard_thread must be called from the "
                    "primary interpreter");
    return NULL;
  }
  noticeboard_clear_thread();
  Py_RETURN_NONE;
}

/// @brief Allocate a fresh notice_sync sequence number.
/// @details Atomically increments @ref NB_SYNC_REQUESTED and returns the
/// new value. The caller posts @c ("sync", N) on the @c boc_noticeboard
/// tag and then waits via @ref _core_notice_sync_wait until that sequence
/// has been processed.
/// @param self The module (unused)
/// @param args Unused
/// @return A Python int with the caller's seq.
static PyObject *_core_notice_sync_request(PyObject *self,
                                           PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  return PyLong_FromLongLong((long long)notice_sync_request());
}

/// @brief Mark a notice_sync sequence as processed and wake waiters.
/// @details Called from the noticeboard-thread Python arm when it pops
/// a @c ("sync", N) sentinel off the queue. Stores @c max(processed, N)
/// into @ref NB_SYNC_PROCESSED (defensive against any reordering, though
/// the MPSC tag is FIFO) and broadcasts @ref NB_SYNC_COND.
/// @param self The module (unused)
/// @param args A tuple @c (N,) — the sequence number being completed
/// @return Py_None
static PyObject *_core_notice_sync_complete(PyObject *self, PyObject *args) {
  BOC_STATE_SET(self);
  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "notice_sync_complete must be called from the primary "
                    "interpreter");
    return NULL;
  }
  long long seq;
  if (!PyArg_ParseTuple(args, "L", &seq)) {
    return NULL;
  }

  Py_BEGIN_ALLOW_THREADS notice_sync_complete((int_least64_t)seq);
  Py_END_ALLOW_THREADS

      Py_RETURN_NONE;
}

/// @brief Block until @p my_seq has been processed by the noticeboard thread.
/// @details Loops on @ref NB_SYNC_COND under @ref NB_SYNC_MUTEX until
/// @ref NB_SYNC_PROCESSED is at least @p my_seq, or until @p timeout
/// seconds elapse. A negative or @c None timeout means wait forever.
/// Releases the GIL across the wait.
/// @param self The module (unused)
/// @param args A tuple @c (my_seq, timeout) — int and float-or-None
/// @return @c True on success, @c False on timeout.
static PyObject *_core_notice_sync_wait(PyObject *self, PyObject *args) {
  BOC_STATE_SET(self);
  long long my_seq;
  PyObject *timeout_obj;
  if (!PyArg_ParseTuple(args, "LO", &my_seq, &timeout_obj)) {
    return NULL;
  }

  bool wait_forever = false;
  double timeout = 0.0;
  if (timeout_obj == Py_None) {
    wait_forever = true;
  } else {
    timeout = PyFloat_AsDouble(timeout_obj);
    if (timeout == -1.0 && PyErr_Occurred()) {
      return NULL;
    }
    // Boundary validation: rejects NaN as ValueError, maps +Inf to
    // wait_forever, clamps negatives to 0. Centralised so future
    // wait entry points can reuse it.
    if (boc_validate_finite_timeout(timeout, &timeout, &wait_forever) < 0) {
      return NULL;
    }
  }

  bool ok;
  Py_BEGIN_ALLOW_THREADS ok =
      notice_sync_wait((int_least64_t)my_seq, timeout, wait_forever);
  Py_END_ALLOW_THREADS

      if (ok) {
    Py_RETURN_TRUE;
  }
  Py_RETURN_FALSE;
}

// ---------------------------------------------------------------------------
// Terminator entry points.
// ---------------------------------------------------------------------------

/// @brief Try to register a new behavior with the terminator.
/// @details Returns the post-increment count on success, or -1 if the
/// terminator is closed (runtime is shutting down). The double-check of
/// TERMINATOR_CLOSED around the fetch_add closes the close-vs-inc race:
/// if close() lands between our first check and our fetch_add, the
/// second check sees it and we undo, signalling on a 0-transition so a
/// concurrent terminator_wait() does not miss the wakeup. Uses the
/// portable plain-atomic forms (seq_cst) — see the polyfill block at
/// the top of this file; the terminator is not on a hot path so the
/// stronger ordering is free.
/// @param self The module (unused)
/// @param args Unused
/// @return Python int — new count on success, -1 if closed.
static PyObject *_core_terminator_inc(PyObject *self,
                                      PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  return PyLong_FromLongLong((long long)terminator_inc());
}

/// @brief Decrement the terminator. Wakes terminator_wait on 0-transition.
/// @param self The module (unused)
/// @param args Unused
/// @return Python int — the new count.
static PyObject *_core_terminator_dec(PyObject *self,
                                      PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  return PyLong_FromLongLong((long long)terminator_dec());
}

/// @brief Set the closed bit. Future terminator_inc() calls return -1.
/// @param self The module (unused)
/// @param args Unused
/// @return Py_None
static PyObject *_core_terminator_close(PyObject *self,
                                        PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "terminator_close must be called from the primary "
                    "interpreter");
    return NULL;
  }
  terminator_close();
  Py_RETURN_NONE;
}

/// @brief Block until TERMINATOR_COUNT reaches 0.
/// @details A negative or @c None timeout means wait forever.
/// Releases the GIL across the wait.
/// @param self The module (unused)
/// @param args A tuple @c (timeout,) — float-or-None
/// @return @c True on success, @c False on timeout.
static PyObject *_core_terminator_wait(PyObject *self, PyObject *args) {
  BOC_STATE_SET(self);
  PyObject *timeout_obj;
  if (!PyArg_ParseTuple(args, "O", &timeout_obj)) {
    return NULL;
  }

  bool wait_forever = false;
  double timeout = 0.0;
  if (timeout_obj == Py_None) {
    wait_forever = true;
  } else {
    timeout = PyFloat_AsDouble(timeout_obj);
    if (timeout == -1.0 && PyErr_Occurred()) {
      return NULL;
    }
    // Boundary validation: rejects NaN as ValueError, maps +Inf to
    // wait_forever, clamps negatives to 0. Centralised so future
    // wait entry points can reuse it.
    if (boc_validate_finite_timeout(timeout, &timeout, &wait_forever) < 0) {
      return NULL;
    }
  }

  bool ok;
  Py_BEGIN_ALLOW_THREADS ok = terminator_wait(timeout, wait_forever);
  Py_END_ALLOW_THREADS

      if (ok) {
    Py_RETURN_TRUE;
  }
  Py_RETURN_FALSE;
}

/// @brief Idempotent one-shot decrement of the Pyrona seed.
/// @details Called by stop()/wait() to remove the seed that keeps the
/// terminator count above zero across momentary quiescence. Safe to call
/// any number of times — only the first call performs the decrement.
/// @param self The module (unused)
/// @param args Unused
/// @return Python bool — True if this call removed the seed, False if
/// the seed was already removed.
static PyObject *_core_terminator_seed_dec(PyObject *self,
                                           PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "terminator_seed_dec must be called from the primary "
                    "interpreter");
    return NULL;
  }
  if (terminator_seed_dec()) {
    Py_RETURN_TRUE;
  }
  Py_RETURN_FALSE;
}

/// @brief Restore terminator state for a fresh runtime start.
/// @details Sets count=1 (the Pyrona seed), clears the closed bit, and
/// re-arms the seed one-shot. Called from Behaviors.start(). Returns
/// the prior @c (count, seeded) tuple so callers can detect drift left
/// over from a previous run that died without reaching its
/// reconciliation point (e.g. KeyboardInterrupt or stop() that raised
/// before the assertion).
/// @param self The module (unused)
/// @param args Unused
/// @return A 2-tuple @c (prior_count, prior_seeded).
static PyObject *_core_terminator_reset(PyObject *self,
                                        PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  if (BOC_STATE->index != 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "terminator_reset must be called from the primary "
                    "interpreter");
    return NULL;
  }
  int_least64_t prior_count = 0;
  int_least64_t prior_seeded = 0;
  terminator_reset(&prior_count, &prior_seeded);
  return Py_BuildValue("(LL)", (long long)prior_count, (long long)prior_seeded);
}

/// @brief Read the current TERMINATOR_SEEDED flag (for reconciliation).
/// @param self The module (unused)
/// @param args Unused
/// @return Python int — 0 or 1.
static PyObject *_core_terminator_seeded(PyObject *self,
                                         PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  return PyLong_FromLongLong((long long)terminator_seeded());
}

/// @brief Read the current terminator count (for reconciliation tests).
/// @param self The module (unused)
/// @param args Unused
/// @return Python int — the current TERMINATOR_COUNT.
static PyObject *_core_terminator_count(PyObject *self,
                                        PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  return PyLong_FromLongLong((long long)terminator_count());
}

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
  /// @brief The last request enqueued on this cown's MCS chain.
  /// @details Stores @c (BOCRequest *) (matching Verona's
  /// @c Slot* in @c boc/cown.h). Updated by
  /// @c request_start_enqueue_inner via @c atomic_exchange on the
  /// 2PL link path; read by successors to discover their
  /// predecessor.
  atomic_intptr_t last; // (BOCRequest *)
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
// Within this TU we want every COWN_INCREF / COWN_DECREF callsite below
// to inline directly into its caller — losing that on the schedule /
// release hot path costs measurable throughput. Mirror CPython's
// Py_INCREF (inline header macro) vs _Py_IncRef (out-of-line ABI export)
// pattern: keep `static inline` bodies as the in-TU implementation,
// expose extern wrappers under the names declared in `cown.h` for
// noticeboard.c, and override the macros from cown.h to bind locally to
// the inline versions. The one earlier callsite (the write_direct error
// rollback above this point) is on an error path and stays bound to the
// extern wrapper from cown.h — not hot.

static inline int_least64_t cown_decref_inline(BOCCown *cown) {
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

/// @brief Out-of-line export consumed by other TUs (see @ref cown.h).
int_least64_t cown_decref(BOCCown *cown) { return cown_decref_inline(cown); }

#define COWN_WEAK_DECREF(c) cown_weak_decref(c)

/// @brief Atomic incref for the cown
/// @param cown the cown to incref
/// @return the new reference count
static inline int_least64_t cown_incref_inline(BOCCown *cown) {
  int_least64_t rc = atomic_fetch_add(&cown->rc, 1) + 1;
  PRINTDBG("cown_incref(%p, cid=%" PRIdLEAST64 ") = %" PRIdLEAST64 "\n", cown,
           cown->id, rc);
  return rc;
}

/// @brief Out-of-line export consumed by other TUs (see @ref cown.h).
int_least64_t cown_incref(BOCCown *cown) { return cown_incref_inline(cown); }

// Rebind COWN_INCREF / COWN_DECREF to the inline forms so every
// remaining callsite below (acquire/release/dispatch hot paths) does
// not pay an indirect call.
#undef COWN_INCREF
#undef COWN_DECREF
#define COWN_INCREF(c) cown_incref_inline((c))
#define COWN_DECREF(c) cown_decref_inline((c))

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

#define COWN_WEAK_INCREF(c) cown_weak_incref((c))
#define COWN_PROMOTE(c) cown_promote((c))

/// @brief Set the value of a cown, clearing the exception flag
/// @note Callers that store an exception must set cown->exception = true
/// after calling this function.
static inline void cown_set_value(BOCCown *cown, PyObject *value) {
  if (value == NULL) {
    Py_XDECREF(cown->value);
    cown->value = NULL;
    cown->exception = false;
    return;
  }

  Py_XSETREF(cown->value, Py_NewRef(value));
  cown->exception = false;
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
  atomic_store_intptr(&cown->last, 0);
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
  intptr_t next_ptr = atomic_load_intptr(&tail->next);
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
      next_ptr = atomic_load_intptr(&tail->next);
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
#if PY_VERSION_HEX >= 0x030D0000
    PyObject *cown_ptr = NULL;
    if (PyDict_GetItemRef(queue->xidata_to_cowns, xidata_ptr, &cown_ptr) > 0) {
      BOCCown *cown = (BOCCown *)PyLong_AsVoidPtr(cown_ptr);
      Py_DECREF(cown_ptr);
      COWN_WEAK_DECREF(cown);
      PyDict_DelItem(queue->xidata_to_cowns, xidata_ptr);
    }
#else
    PyObject *cown_ptr = PyDict_GetItem(queue->xidata_to_cowns, xidata_ptr);
    if (cown_ptr != NULL) {
      BOCCown *cown = (BOCCown *)PyLong_AsVoidPtr(cown_ptr);
      COWN_WEAK_DECREF(cown);
      PyDict_DelItem(queue->xidata_to_cowns, xidata_ptr);
    }
#endif
  } else if (queue->index > 0) {
    fprintf(stderr,
            "Recycling xidata created on interpreter %" PRIdLEAST64
            " after the interpreter "
            "has shut down may result in cown leak.\n",
            queue->index);
  }

  Py_DECREF(xidata_ptr);

  // manual clear
  if (xidata->data != NULL) {
    if (xidata->free != NULL) {
      xidata->free(xidata->data);
    }
    xidata->data = NULL;
  }

  Py_CLEAR(xidata->obj);
  PyMem_RawFree(xidata);
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
  atomic_store_intptr(&node->next, 0);

  // step 1: swap the new node in as the new head
  intptr_t node_ptr = (intptr_t)node;
  intptr_t old_head_ptr = atomic_exchange_intptr(&queue->head, node_ptr);
  BOCRecycleNode *old_head = (BOCRecycleNode *)old_head_ptr;
  // queue is now inconsistent
  // step 2: store the data in this node. This node is somewhere inside the
  // queue.
  old_head->xidata = xidata;
  // step 3: connect everything back together
  atomic_store_intptr(&old_head->next, node_ptr);
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
    assert((intptr_t)queue->tail == atomic_load_intptr(&queue->head));
    assert(atomic_load_intptr(&queue->tail->next) == 0);
  }
}

/// @brief Frees a RecycleQueue.
/// @details This will complete the recycling of any pending XIData objects, if
/// possible.
/// @param queue The queue to free
static void BOCRecycleQueue_free(BOCRecycleQueue *queue) {
  assert(queue->xidata_to_cowns == NULL);
  if (queue->tail != NULL && atomic_load_intptr(&queue->tail->next) != 0) {
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
/// @note The struct is forward-declared near the top of the file (next to
/// the noticeboard helpers) so @c nb_pin_cowns can extract @c BOCCown
/// pointers from a Python CownCapsule. This block carries the doc only;
/// keep the field set in sync with the forward declaration.

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
  if (value == NULL) {
    PyErr_SetString(PyExc_TypeError, "cannot delete value attribute");
    return -1;
  }

  CownCapsuleObject *self = (CownCapsuleObject *)op;

  if (!cown_check_acquired(self->cown, true)) {
    return -1;
  }

  cown_set_value(self->cown, value);
  return 0;
}

/// @brief Returns whether the current interpreter has acquired the cown
/// @param op The CownCapsule object
/// @param Py_UNUSED ignored
/// @return True if acquired, False otherwise
static PyObject *CownCapsule_acquired(PyObject *op,
                                      PyObject *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  return PyBool_FromLong(cown_check_acquired(self->cown, false));
}

/// @brief Attempts to acquire the cown
/// @note On failure, the cown's owner is restored to its prior value: either
/// NO_OWNER (if deserialisation failed after the CAS succeeded) or the actual
/// owning interpreter (if the CAS itself failed). Callers can therefore rely
/// on the invariant that a -1 return never leaves the cown in a half-acquired
/// (owner=me, value=NULL, xidata non-NULL) state. This is required by the
/// worker-side recovery arm in `worker.run_behavior`, which calls
/// `behavior.release()` after an acquire failure.
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
    // Deserialisation failed. We CAS'd owner from NO_OWNER to desired above,
    // so we must roll it back; otherwise the cown is permanently stuck in a
    // (owner=me, value=NULL, xidata non-NULL) half-acquired state and any
    // future acquire from any interpreter (including the worker-side
    // recovery arm) sees "already acquired by N" instead of being able to
    // retry. xidata stays in place for a future retry.
    atomic_store(&cown->owner, (int_least64_t)NO_OWNER);
    return -1;
  }

  BOCRecycleQueue_enqueue(cown->recycle_queue, cown->xidata);
  cown->recycle_queue = NULL;
  cown->xidata = NULL;
  return 0;
}

/// @brief Attempts to acquire the cown
/// @note This will throw an exception if the cown has already been acquired by
/// another interpreter. It will also throw an exception if deserialization
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

/// @brief Abandons the cown value without serializing it
/// @details Clears the value and resets ownership to NO_OWNER. This is used
/// during worker cleanup to safely discard orphan cowns before the owning
/// interpreter is destroyed.
/// @param cown The cown to disown
/// @return -1 if error, 0 otherwise
static int cown_disown(BOCCown *cown) {
  int_least64_t expected = get_interpid();
  int_least64_t owner = atomic_load(&cown->owner);
  if (owner != expected) {
    PyErr_Format(PyExc_RuntimeError,
                 "%" PRIdLEAST64
                 " cannot disown cown (acquired by %" PRIdLEAST64 ")",
                 expected, owner);
    return -1;
  }

  assert(cown->value != NULL);
  assert(cown->xidata == NULL);

  Py_CLEAR(cown->value);

  int_least64_t desired = NO_OWNER;
  if (!atomic_compare_exchange_strong(&cown->owner, &expected, desired)) {
    PyErr_SetString(PyExc_RuntimeError,
                    "Panic: contention on cown during disown");
    return -1;
  }

  return 0;
}

/// @brief Abandons the cown
/// @note Clears the value without serializing and resets ownership. Used during
/// worker shutdown to avoid dangling pointers after interpreter destruction.
/// Will raise an error if the cown is not currently acquired by this
/// interpreter.
/// @param op The CownCapsule object
/// @param Py_UNUSED (ignored)
/// @return None if successful, NULL otherwise
static PyObject *CownCapsule_disown(PyObject *op, PyObject *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  BOCCown *cown = self->cown;

  if (cown_disown(cown) < 0) {
    return NULL;
  }

  Py_RETURN_NONE;
}

static PyObject *CownCapsule_get_impl(PyObject *op, void *Py_UNUSED(dummy)) {
  return Py_NewRef(op);
}

/// @brief Pickle support for CownCapsule
/// @details Returns a (reconstructor, (pointer, pid)) tuple. Does NOT take a
/// COWN_INCREF on the inner BOCCown: the bytes produced by pickling are
/// dead data, not a reference. The caller is responsible for ensuring the
/// underlying BOCCown is kept alive between pickling and unpickling. For
/// transient pickles (send/receive on the message queue), the original
/// CownCapsule held by the sender provides that liveness; for long-lived
/// pickles (the noticeboard), the noticeboard layer pins the BOCCown
/// independently via @ref nb_collect_cowns at write time.
/// @note An earlier design did COWN_INCREF here as a "pin" and had the
/// reconstructor inherit it. That assumed a 1-pickle / 1-unpickle pairing
/// and was broken by the noticeboard, where one write is unpickled by
/// every reader on every worker.
/// @param op The CownCapsule object
/// @param Py_UNUSED (ignored)
/// @return A tuple (reconstructor, (pointer, pid)) for pickle, or NULL on error
static PyObject *CownCapsule_reduce(PyObject *op, PyObject *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;
  BOCCown *cown = self->cown;

  PyObject *ptr = PyLong_FromVoidPtr(cown);
  if (ptr == NULL) {
    return NULL;
  }

#ifdef _WIN32
  long pid = (long)_getpid();
#else
  long pid = (long)getpid();
#endif
  PyObject *pid_obj = PyLong_FromLong(pid);
  if (pid_obj == NULL) {
    Py_DECREF(ptr);
    return NULL;
  }

  PyObject *module = PyImport_ImportModule("bocpy._core");
  if (module == NULL) {
    Py_DECREF(pid_obj);
    Py_DECREF(ptr);
    return NULL;
  }

  PyObject *reconstructor =
      PyObject_GetAttrString(module, "_cown_capsule_from_pointer");
  Py_DECREF(module);
  if (reconstructor == NULL) {
    Py_DECREF(pid_obj);
    Py_DECREF(ptr);
    return NULL;
  }

  PyObject *args = PyTuple_Pack(2, ptr, pid_obj);
  Py_DECREF(ptr);
  Py_DECREF(pid_obj);
  if (args == NULL) {
    Py_DECREF(reconstructor);
    return NULL;
  }

  PyObject *result = PyTuple_Pack(2, reconstructor, args);
  Py_DECREF(reconstructor);
  Py_DECREF(args);
  return result;
}

static PyMethodDef CownCapsule_methods[] = {
    {"get", CownCapsule_get, METH_NOARGS, NULL},
    {"set", CownCapsule_set, METH_VARARGS, NULL},
    {"acquired", CownCapsule_acquired, METH_NOARGS, NULL},
    {"acquire", CownCapsule_acquire, METH_NOARGS, NULL},
    {"release", CownCapsule_release, METH_NOARGS, NULL},
    {"disown", CownCapsule_disown, METH_NOARGS, NULL},
    {"__reduce__", CownCapsule_reduce, METH_NOARGS, NULL},
    {NULL} /* Sentinel */
};

/// @brief Returns whether the cown holds an unhandled exception
/// @param op The CownCapsule object
/// @param Py_UNUSED ignored
/// @return True if the cown holds an exception, False otherwise
static PyObject *CownCapsule_get_exception(PyObject *op,
                                           void *Py_UNUSED(dummy)) {
  CownCapsuleObject *self = (CownCapsuleObject *)op;

  if (!cown_check_acquired(self->cown, true)) {
    return NULL;
  }

  return PyBool_FromLong(self->cown->exception);
}

/// @brief Sets the exception flag on the cown
/// @param op The CownCapsule object
/// @param value A truthy/falsy Python object
/// @param Py_UNUSED ignored
/// @return 0 on success, -1 on error
static int CownCapsule_set_exception(PyObject *op, PyObject *value,
                                     void *Py_UNUSED(dummy)) {
  if (value == NULL) {
    PyErr_SetString(PyExc_TypeError, "cannot delete exception attribute");
    return -1;
  }

  CownCapsuleObject *self = (CownCapsuleObject *)op;

  if (!cown_check_acquired(self->cown, true)) {
    return -1;
  }

  int truthy = PyObject_IsTrue(value);
  if (truthy < 0) {
    return -1;
  }

  self->cown->exception = (bool)truthy;
  return 0;
}

static PyGetSetDef CownCapsule_getset[] = {
    {"value", (getter)CownCapsule_get_value, (setter)CownCapsule_set_value,
     NULL, NULL},
    {"exception", (getter)CownCapsule_get_exception,
     (setter)CownCapsule_set_exception, NULL, NULL},
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

  PRINTDBG("_cown_shared(%p)\n", cown);

  // all we do to initialise the xidata is store a pointer to the cown
  XIDATA_INIT(xidata, tstate->interp, cown, obj, _new_cown_object);
  return 0;
}

/// @brief Frees a message
/// @details Releases @c message->tag (an owning reference taken by
/// @c boc_message_new) and any pending xidata, then frees the message
/// struct itself. Safe to call on a partially-initialized message:
/// @c boc_message_new zero-fills the allocation, so any field that
/// has not yet been assigned reads back as NULL and the corresponding
/// TAG_DECREF / xidata recycle arms are skipped.
/// @param message The message to free
static void boc_message_free(BOCMessage *message) {
  if (message->tag != NULL) {
    TAG_DECREF(message->tag);
  }
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
    // Pre-check the slot state with a non-allocating load before
    // committing to a `tag_from_PyUnicode` allocation. Iterating
    // across many already-ASSIGNED slots while looking for the
    // dedicated queue of a new tag must NOT allocate per iteration:
    // the CAS would fail on every ASSIGNED slot and the speculative
    // tag would immediately be `tag_release`d, turning a cold-start
    // queue scan into O(BOC_QUEUE_COUNT) malloc/free pairs.
    //
    // Only attempt the publish-before-CAS allocation when the slot
    // is actually UNASSIGNED. The CAS that follows is still needed
    // to win the slot against a racing peer; on CAS loss we tag-
    // release and fall through to the discovery branch below
    // exactly as the prior code did.
    int_least64_t observed = atomic_load(&qptr->state);
    if (observed == BOC_QUEUE_UNASSIGNED) {
      // Allocate the tag *before* the CAS so that an allocation failure
      // (UTF-8 error / OOM in tag_from_PyUnicode) leaves the slot in
      // BOC_QUEUE_UNASSIGNED — peer interpreters can re-attempt and we
      // never publish ASSIGNED-with-NULL-tag (which would wedge readers
      // in the busy-wait below). The new tag arrives with rc=1; on CAS
      // loss we tag_release it (the slot is owned by some other peer
      // who is responsible for publishing their own tag).
      BOCTag *new_tag = tag_from_PyUnicode(tag, qptr);
      if (new_tag == NULL) {
        return NULL;
      }
      if (atomic_compare_exchange_strong(&qptr->state, &expected, desired)) {
        // we're the first, this is the new dedicated queue for this tag
        PRINTDBG("Assigning ");
        PRINTOBJDBG(tag);
        PRINTFDBG(" to queue %zu\n", i);
        // Publish the tag pointer with release semantics so the busy-wait
        // below sees the non-NULL tag after observing ASSIGNED. The tag
        // already has rc=1 (queue's owning reference). We then add the
        // per-interpreter cache reference (rc=2). This replaces the prior
        // rc=0-then-double-INCREF idiom whose incref window allowed a
        // racing TAG_DECREF to free a freshly published tag.
        atomic_store_intptr(&qptr->tag, (intptr_t)new_tag);
        BOC_STATE->queue_tags[i] = new_tag;
        TAG_INCREF(new_tag);
        return qptr;
      }

      // CAS lost — another interpreter assigned this slot first. Release
      // our speculative allocation; we'll fall through to the post-CAS
      // discovery branch below to pick up the winner's tag.
      TAG_DECREF(new_tag);
    } else {
      // Slot was already ASSIGNED (or DISABLED) when we looked. Mirror
      // the post-CAS-failure exit values so the discovery branch below
      // sees the same `expected` it would have gotten from a failed CAS.
      expected = observed;
    }

    // this queue has already been assigned
    if (expected == BOC_QUEUE_DISABLED) {
      // queue is being reconfigured by set_tags — skip it
      continue;
    }

    BOCTag *qtag = (BOCTag *)atomic_load_intptr(&qptr->tag);
    while (qtag == NULL) {
      // waiting for another interpreter to allocate and assign
      qtag = (BOCTag *)atomic_load_intptr(&qptr->tag);
    }

    // Discovery path: the qptr->tag pointer is owned by the publisher's
    // queue reference. Add a per-interpreter cache reference.
    BOC_STATE->queue_tags[i] = qtag;
    TAG_INCREF(qtag);

    PRINTDBG("Discovered %s at queue %" PRIdLEAST64 "\n", qtag->str, i);
    if (tag_compare_with_PyUnicode(BOC_STATE->queue_tags[i], tag) == 0) {
      // this is the dedicated queue for this tag
      return qptr;
    } else if (PyErr_Occurred() != NULL) {
      return NULL;
    }

    // not the right queue, keep looking
  }

  // No queue for this tag — dump observed slot state to stderr so that
  // intermittent failures (e.g. memory-ordering races on weak-memory
  // architectures) leave a forensic trail even in release builds.
  fprintf(stderr, "[bocpy] get_queue_for_tag: no queue found for tag ");
  PyObject_Print(tag, stderr, Py_PRINT_RAW);
  fprintf(stderr, " (interpreter index=%" PRIdLEAST64 ")\n", BOC_STATE->index);
  qptr = BOC_QUEUES;
  for (size_t i = 0; i < BOC_QUEUE_COUNT; ++i, ++qptr) {
    int_least64_t state = atomic_load(&qptr->state);
    BOCTag *qtag = (BOCTag *)atomic_load_intptr(&qptr->tag);
    BOCTag *cached = BOC_STATE->queue_tags[i];
    fprintf(stderr,
            "[bocpy]   slot %2zu: state=%" PRIdLEAST64
            " tag=%p tag_str=%s cached=%p cached_str=%s\n",
            i, state, (void *)qtag, qtag != NULL ? qtag->str : "(null)",
            (void *)cached, cached != NULL ? cached->str : "(null)");
  }
  fflush(stderr);
  return NULL;
}

/// @brief Creates a new message.
/// @param module The _core module
/// @param tag The tag associated with the message.
/// @param contents The contents of the message.
/// @return A message object
static BOCMessage *boc_message_new(PyObject *tag, PyObject *contents) {
  // Zero-init so any later boc_message_free on a partially-built
  // message sees NULL for `tag`, `xidata`, and `recycle_queue` and
  // safely no-ops the TAG_DECREF / BOCRecycleQueue_enqueue arms.
  // Without this, callers must remember to PyMem_RawFree (rather
  // than boc_message_free) on every early-error path that occurs
  // before the explicit field assignments below — an invariant
  // that is easy to break when adding new failure points.
  BOCMessage *message = (BOCMessage *)PyMem_RawCalloc(1, sizeof(BOCMessage));
  if (message == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  BOCQueue *qptr = get_queue_for_tag(tag);
  if (qptr == NULL) {
    PyMem_RawFree(message);
    // Only set the capacity-exhaustion KeyError if get_queue_for_tag
    // did not already raise (e.g. UnicodeEncodeError on surrogates,
    // PyMem_RawMalloc OOM in tag_from_PyUnicode). Overwriting a
    // pending exception masks the true failure cause.
    if (!PyErr_Occurred()) {
      PyErr_Format(PyExc_KeyError,
                   "No queue available for tag %R: tag capacity exceeded", tag);
    }
    return NULL;
  }

  BOCTag *qtag = (BOCTag *)atomic_load_intptr(&qptr->tag);
  if (qtag == NULL) {
    // non-assigned tag — allocate one for this message. The new tag
    // arrives with rc=1; ownership transfers to message->tag and is
    // released by boc_message_free.
    message->tag = tag_from_PyUnicode(tag, qptr);
    if (message->tag == NULL) {
      PyMem_RawFree(message);
      return NULL;
    }
  } else {
    // qtag is owned by qptr->tag (publisher's queue reference). Take
    // a separate owning reference for message->tag so a concurrent
    // set_tags that swaps qptr->tag and tag_disables the old one
    // does not free it out from under us.
    message->tag = qtag;
    TAG_INCREF(message->tag);
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
/// @details Each tag's message queue is a fixed-capacity ring
/// (@c BOC_CAPACITY = 16384 slots). Reaching that bound requires more
/// than 16k messages on a single tag to be queued without any
/// consumer draining -- in practice this only happens for a tag
/// where producers vastly outpace consumers. Behaviour dispatch
/// does not go through a tag at all (it routes through per-worker
/// queues in @c sched.c).
///
/// On overflow this returns -1 without setting a Python exception; the
/// caller (typically @c behavior_resolve_one) reports the error. Once
/// a behavior's MCS chains are linked the schedule cannot be undone:
/// the behavior may still execute later if a predecessor releases and
/// re-tries the resolve, otherwise its cowns leak until process exit.
/// A robust fix is a queue redesign (e.g. linked-list MPSC instead of
/// the fixed-capacity ring) rather than the half-step of producer-side
/// reservations -- the latter trades a never-observed failure for an
/// audit surface that silently shrinks queue capacity on any leaked
/// reservation. If the failure is ever observed in practice, redesign
/// the queue.
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

      boc_atomic_fetch_add_u64_explicit(&qptr->pushed_total, 1, BOC_MO_RELAXED);

      // If any receiver is parked on this queue's condvar, wake it.
      // The seq_cst load synchronizes with the consumer's seq_cst increment
      // of waiters, ensuring that either we see the waiter and signal, or the
      // consumer's re-check dequeue (under the same mutex) finds our message.
      if (atomic_load_explicit(&qptr->waiters, memory_order_seq_cst) > 0) {
        boc_park_lock(qptr);
        boc_park_signal(qptr);
        boc_park_unlock(qptr);
      }

      return 0;
    }

    // someone else got there first, try again
    boc_atomic_fetch_add_u64_explicit(&qptr->enqueue_cas_retries, 1,
                                      BOC_MO_RELAXED);
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
    PyErr_Format(PyExc_KeyError, "No message queue found for tag: %R", tag);
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
      boc_atomic_fetch_add_u64_explicit(&qptr->dequeue_cas_retries, 1,
                                        BOC_MO_RELAXED);
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
    boc_atomic_fetch_add_u64_explicit(&qptr->popped_total, 1, BOC_MO_RELAXED);
    PRINTFDBG("Dequeued %s from q%" PRIdLEAST64 "[%" PRIdLEAST64
              "] (%" PRIdLEAST64 " - %" PRIdLEAST64 " = %" PRIdLEAST64 ")\n",
              (*message)->tag->str, qptr->index, head, tail, head + 1,
              tail - head - 1);
    return qptr->index;
  }

  return -1;
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

  Py_BEGIN_ALLOW_THREADS;
  boc_yield();
  Py_END_ALLOW_THREADS;

  Py_RETURN_NONE;
}

/// @brief Double the exponential backoff duration, capped at BOC_BACKOFF_CAP_NS
/// @param backoff The timespec whose tv_nsec field will be doubled
static inline void boc_backoff_double(struct timespec *backoff) {
  backoff->tv_nsec *= 2;
  if (backoff->tv_nsec > BOC_BACKOFF_CAP_NS) {
    backoff->tv_nsec = BOC_BACKOFF_CAP_NS;
  }
}

/// @brief Wake any receivers parked on the queue associated with a tag
/// @param tag The tag whose queue should be signalled (borrowed reference)
static inline void boc_wake_parked_receivers(PyObject *tag) {
  BOCQueue *qptr = get_queue_for_tag(tag);
  if (qptr != NULL &&
      atomic_load_explicit(&qptr->waiters, memory_order_seq_cst) > 0) {
    boc_park_lock(qptr);
    boc_park_broadcast(qptr);
    boc_park_unlock(qptr);
  }
}

/// @brief Receives a message on a single tag using spin-then-park (untimed) or
/// spin-then-backoff (timed)
/// @param tag The tag to receive on (borrowed reference)
/// @param do_timeout Whether a timeout was requested
/// @param end_time Deadline as double-precision seconds (only if do_timeout)
/// @param after Callback to invoke on timeout (or Py_None)
/// @return The received (tag, contents) tuple, timeout sentinel, or NULL on
/// error
static PyObject *receive_single_tag(PyObject *tag, bool do_timeout,
                                    double end_time, PyObject *after) {
  BOCQueue *qptr = get_queue_for_tag(tag);
  BOCMessage *message = NULL;
  struct timespec backoff = {0, 1000}; // 1 µs, only used when do_timeout

  while (true) {
    // Phase 1: Spin
    for (int spin = 0; spin < BOC_SPIN_COUNT; ++spin) {
      BOCRecycleQueue_empty(BOC_STATE->recycle_queue, false);

      int_least64_t queue_index = boc_dequeue(tag, &message);
      if (queue_index >= 0) {
        goto got_message;
      }

      if (PyErr_Occurred() != NULL) {
        return NULL;
      }

      if (do_timeout && boc_now_s() > end_time) {
        goto timed_out;
      }
    }

    // Phase 2a: Timed — exponential backoff (no parking)
    if (do_timeout) {
      if (boc_now_s() > end_time) {
        goto timed_out;
      }

      Py_BEGIN_ALLOW_THREADS;
      thrd_sleep(&backoff, NULL);
      Py_END_ALLOW_THREADS;

      boc_backoff_double(&backoff);

      continue;
    }

    // Phase 2b: Untimed — park on condvar (indefinite wait)
    if (qptr == NULL) {
      PyErr_Format(PyExc_KeyError, "No message queue found for tag: %R", tag);
      return NULL;
    }

    boc_park_lock(qptr);
    // seq_cst increment synchronizes with the seq_cst load in boc_enqueue,
    // ensuring that either the producer sees our waiter count and signals,
    // or our re-check dequeue below finds the producer's message.
    atomic_fetch_add_explicit(&qptr->waiters, 1, memory_order_seq_cst);

    // Re-check under lock (prevents lost wake)
    int_least64_t queue_index = boc_dequeue(tag, &message);
    if (queue_index >= 0) {
      atomic_fetch_sub_explicit(&qptr->waiters, 1, memory_order_seq_cst);
      boc_park_unlock(qptr);
      goto got_message;
    }

    if (PyErr_Occurred() != NULL) {
      atomic_fetch_sub_explicit(&qptr->waiters, 1, memory_order_seq_cst);
      boc_park_unlock(qptr);
      return NULL;
    }

    Py_BEGIN_ALLOW_THREADS;
    boc_park_wait(qptr);
    // Wake: we hold park_mutex but NOT the GIL.
    // Release mutex BEFORE re-acquiring GIL to avoid ABBA deadlock:
    // consumer (mutex → GIL) vs producer (GIL → mutex).
    atomic_fetch_sub_explicit(&qptr->waiters, 1, memory_order_seq_cst);
    boc_park_unlock(qptr);
    Py_END_ALLOW_THREADS;

    // Re-resolve queue pointer (set_tags may have reassigned it)
    BOCQueue *new_qptr = get_queue_for_tag(tag);
    if (new_qptr == NULL) {
      PyErr_SetString(PyExc_RuntimeError, "Tag invalidated during receive");
      return NULL;
    }

    if (new_qptr != qptr) {
      qptr = new_qptr;
    }

    BOCRecycleQueue_empty(BOC_STATE->recycle_queue, false);
  }

got_message:;
#ifdef BOC_TRACE
  if (tag_compare_with_PyUnicode(message->tag, tag) != 0) {
    if (PyErr_Occurred() != NULL) {
      boc_message_free(message);
      return NULL;
    }
  }
#endif

  PyObject *contents = xidata_to_object(message->xidata, message->pickled);
  if (contents == NULL) {
    boc_message_free(message);
    return NULL;
  }

  PyObject *result = PyTuple_Pack(2, tag, contents);
  Py_DECREF(contents);
  boc_message_free(message);
  return result;

timed_out:
  if (!Py_IsNone(after)) {
    return PyObject_CallNoArgs(after);
  }
  return Py_BuildValue("(sO)", BOC_TIMEOUT, Py_None);
}

/// @brief Receives a message on multiple tags using round-robin with
/// exponential backoff
/// @param tags_fast A PySequence_Fast of tag strings (borrowed reference)
/// @param tags_size The number of tags
/// @param do_timeout Whether a timeout was requested
/// @param end_time Deadline as double-precision seconds (only if do_timeout)
/// @param after Callback to invoke on timeout (or Py_None)
/// @return The received (tag, contents) tuple, timeout sentinel, or NULL on
/// error
static PyObject *receive_multi_tag(PyObject *tags_fast, Py_ssize_t tags_size,
                                   bool do_timeout, double end_time,
                                   PyObject *after) {
  BOCMessage *message = NULL;
  size_t tag_index = 0;
  struct timespec backoff = {0, 1000}; // 1 µs

  while (true) {
    BOCRecycleQueue_empty(BOC_STATE->recycle_queue, false);

    // Round-robin: try one tag per iteration
    PyObject *tag = PySequence_Fast_GET_ITEM(tags_fast, tag_index);
    tag_index = (tag_index + 1) % tags_size;

    int_least64_t queue_index = boc_dequeue(tag, &message);
    if (queue_index >= 0) {
#ifdef BOC_TRACE
      if (tag_compare_with_PyUnicode(message->tag, tag) != 0) {
        if (PyErr_Occurred() != NULL) {
          boc_message_free(message);
          Py_DECREF(tags_fast);
          return NULL;
        }
      }
#endif

      PyObject *contents = xidata_to_object(message->xidata, message->pickled);
      if (contents == NULL) {
        boc_message_free(message);
        Py_DECREF(tags_fast);
        return NULL;
      }

      PyObject *result = PyTuple_Pack(2, tag, contents);
      Py_DECREF(contents);
      boc_message_free(message);
      Py_DECREF(tags_fast);
      return result;
    }

    if (PyErr_Occurred() != NULL) {
      Py_DECREF(tags_fast);
      return NULL;
    }

    if (do_timeout && boc_now_s() > end_time) {
      Py_DECREF(tags_fast);
      if (!Py_IsNone(after)) {
        return PyObject_CallNoArgs(after);
      }
      return Py_BuildValue("(sO)", BOC_TIMEOUT, Py_None);
    }

    Py_BEGIN_ALLOW_THREADS;
    thrd_sleep(&backoff, NULL);
    Py_END_ALLOW_THREADS;

    boc_backoff_double(&backoff);
  }
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

  bool do_timeout = false;
  double end_time = 0;
  if (timeout >= 0) {
    do_timeout = true;
    end_time = boc_now_s() + timeout;
  }

  // Dispatch: single-tag vs multi-tag
  if (tags_fast != NULL) {
    return receive_multi_tag(tags_fast, tags_size, do_timeout, end_time, after);
  }

  return receive_single_tag(tag, do_timeout, end_time, after);
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

    boc_wake_parked_receivers(tags);

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

    boc_wake_parked_receivers(tag);
  }

  Py_DECREF(tags_fast);
  Py_RETURN_NONE;
}

/// @brief Atomic counter for BOC behaviors
atomic_int_least64_t BOC_BEHAVIOR_COUNT = 0;

// Forward declaration so BOCBehavior can hold an array of request pointers;
// the BOCRequest struct itself is defined further down (next to the request
// helpers).
struct boc_request;

/// @brief Encapsulates a behavior's request for a cown.
/// @details Hoisted ahead of BOCBehavior so the latter can carry a sized
/// array of these. The actual helpers live further down with the rest of
/// the request lifecycle code.
typedef struct boc_request {
  /// @brief The cown that has been requested
  BOCCown *target;
  /// @brief The ID of the next behavior
  atomic_intptr_t next;
  /// @brief Whether the request has been scheduled
  atomic_int_least64_t scheduled;
  /// @brief Atomic reference count.
  /// @details Starts at 1 (the owner @c BOCBehavior's @c requests array).
  /// A successor that observes this request as its predecessor during
  /// @c request_start_enqueue_inner takes a second ref *immediately
  /// before* publishing @c prev->next, so the predecessor cannot retire
  /// during the spin on @c prev->scheduled that follows. The owner
  /// releases its ref from @c behavior_release_all (or @c behavior_free,
  /// defensively); the successor releases its ref after the spin
  /// completes. The last drop frees the struct. See @c request_decref.
  atomic_int_least64_t rc;
} BOCRequest;

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
  /// @brief Owned, deduped, target-sorted request array.
  /// @details Populated by BehaviorCapsule_create_requests; freed either by
  /// behavior_release_all (the normal MCS-unlink path) or by behavior_free
  /// (defensive fallback if the behavior is destroyed without dispatch).
  struct boc_request **requests;
  /// @brief Number of entries in @c requests (post-dedup, ≤ args_size + 1).
  Py_ssize_t requests_size;
  /// @brief Intrusive link node for the Verona-style behaviour MPMC
  /// queue (`boc_bq_*` API in `sched.{h,c}`).
  /// @details Ports `verona-rt/src/rt/sched/work.h::Work::next_in_queue`.
  /// Initialised to NULL in @c behavior_new under the GIL, before the
  /// behaviour can be reached from any other thread (preserves the
  /// link-loop infallibility invariant). Hooked into the `boc_bq_*`
  /// enqueue/dequeue path by `behavior_resolve_one` and
  /// `request_release_inner`. Placement at struct end is
  /// `pahole`-driven to keep the hot fields on their existing cache
  /// lines.
  boc_bq_node_t bq_node;
  /// @brief Fairness-token discriminator.
  /// @details 0 for ordinary behaviours; 1 for the per-worker
  /// @c token_work sentinel allocated by
  /// @ref _core_scheduler_runtime_start. The worker-pop site checks
  /// this field on every successful pop; if set, the dispatch path
  /// flips @c should_steal_for_fairness on the popping worker and
  /// re-enqueues the token instead of calling @c run_behavior.
  /// Verona equivalent: @c Core::token_work + @c is_token discriminator
  /// (`verona-rt/src/rt/sched/core.h:22-37`). Trailing position keeps
  /// the hot fields (count, rc, thunk) on their existing cache lines;
  /// the byte costs an 8-byte tail pad on x86_64.
  uint8_t is_token;
  /// @brief Index of the worker that owns this fairness token (or
  /// @c -1 for ordinary behaviours).
  /// @details The fairness arm in @ref boc_sched_worker_pop_slow
  /// re-enqueues a worker's token from its own @c token_work slot,
  /// so the heartbeat needs to land back on the owning worker even
  /// when the token was consumed by a thief. The dispatch loop in
  /// @ref _core_scheduler_worker_pop reads this field and calls
  /// @ref boc_sched_set_steal_flag on the owner — never on the
  /// consumer — so the owner's next @c pop_fast routes through
  /// @c pop_slow and re-enqueues its own token. Verona's
  /// equivalent is the captured @c this in @c Closure::make
  /// (`core.h:24-32`): the closure body sets the OWNING core's
  /// flag, not the running thread's.
  ///
  /// Width: @c int16_t. Sized to comfortably exceed any plausible
  /// worker count (≤32767) while preserving the existing 8-byte
  /// trailing pad with @c is_token; struct size is unchanged from
  /// the original @c int8_t encoding (verified by pahole).
  int16_t owner_worker_index;
} BOCBehavior;

/// @brief Capsule for holding a pointer to a behavior
typedef struct behavior_capsule_object {
  PyObject_HEAD BOCBehavior *behavior;
} BehaviorCapsuleObject;

#define BehaviorCapsule_CheckExact(op)                                         \
  Py_IS_TYPE((op), BOC_STATE->behavior_capsule_type)

/// @brief Recover the enclosing @c BOCBehavior from its embedded
/// @c bq_node.
/// @details The dispatch path moves @c BOCBehavior * pointers
/// through the scheduler queue indirectly: the producer hands
/// @c &behavior->bq_node to @ref boc_sched_dispatch, the consumer
/// pops a @c boc_bq_node_t * back, and this macro reverses the
/// embedding offset to recover the owning @c BOCBehavior. Equivalent
/// to the kernel's @c container_of pattern; @c offsetof is the
/// portable C11 idiom.
#define BEHAVIOR_FROM_BQ_NODE(node_ptr)                                        \
  ((BOCBehavior *)((char *)(node_ptr) - offsetof(BOCBehavior, bq_node)))

// Forward declaration: defined alongside the request helpers further down.
// behavior_free uses it to clean up any unreleased request array if a
// behavior is destroyed without going through behavior_release_all.
static void request_decref(BOCRequest *request);

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
  behavior->requests = NULL;
  behavior->requests_size = 0;
  // Init the boc_bq link before the behaviour becomes reachable from
  // any other thread (we are still under the GIL here). The boc_bq_*
  // enqueue path requires this field to start NULL.
  boc_atomic_store_ptr_explicit(&behavior->bq_node.next_in_queue, NULL,
                                BOC_MO_RELAXED);
  // Ordinary behaviours are not fairness tokens. Token allocation
  // is performed directly in `_core_scheduler_runtime_start` and
  // bypasses `behavior_new`.
  behavior->is_token = 0;
  behavior->owner_worker_index = -1;
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

  if (behavior->requests != NULL) {
    // Defensive cleanup: if a behavior is destroyed without
    // behavior_release_all having been called (e.g. a scheduling failure
    // mid-2PL), drop the owner ref on each request. If a successor is
    // still holding a concurrent ref (unlikely here since the behavior
    // never linked), the free is deferred until that successor's decref.
    for (Py_ssize_t i = 0; i < behavior->requests_size; ++i) {
      if (behavior->requests[i] != NULL) {
        request_decref(behavior->requests[i]);
      }
    }
    PyMem_RawFree(behavior->requests);
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

  // PyMem_RawCalloc with nelem == 0 is implementation-defined (may return
  // NULL legally), so only treat NULL as failure when args_size > 0.
  behavior->group_ids = PyMem_RawCalloc((size_t)args_size, sizeof(int));
  if (args_size > 0 && behavior->group_ids == NULL) {
    Py_DECREF(cowns);
    Py_DECREF(cowns_list_fast);
    PyErr_NoMemory();
    return -1;
  }
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
/// @details Called when a request is at the head of the queue for a
/// particular cown. If this is the last request (count -> 0) the thunk
/// is dispatched: the unique caller that observes the transition takes
/// a queue-owned reference via @c BEHAVIOR_INCREF and hands
/// @c &behavior->bq_node to @ref boc_sched_dispatch. The matching
/// @c BEHAVIOR_DECREF runs when the consumer's freshly allocated
/// @c BehaviorCapsule (built by @c _core.scheduler_worker_pop) is
/// deallocated on the worker side.
///
/// Visibility of the dispatch is carried by the acq-rel fetch_sub on
/// @c count -- only one decrementer can transition to 0, and the
/// behavior payload (cowns / captures / thunk) was published by
/// @c whencall before the 2PL link loop began.
///
/// **Failure surface.** @ref boc_sched_dispatch can fail when called
/// from the off-worker arm if the runtime has been torn down. On
/// failure the queue-owned BEHAVIOR_INCREF taken just before dispatch
/// is rolled back here, the Python exception set by
/// @c boc_sched_dispatch is propagated, and the caller is expected
/// to roll back its terminator hold (the reference path is
/// @c whencall in @c behaviors.py).
///
/// **Cown-side residue on dispatch failure.** When the count==0
/// transition fires here AND @c boc_sched_dispatch returns -1
/// (runtime-down sentinel; see @c boc_sched_dispatch in @c sched.c),
/// the behavior's BOCRequest array has already been linked onto every
/// target cown's MCS chain by the link/finish 2PL phases. The
/// rollback below DECREFs only the queue-owned BEHAVIOR_INCREF; it
/// does NOT walk and unlink the cown chains. Each request still
/// holds its BEHAVIOR_INCREF, so the BOCBehavior cannot be freed,
/// and no worker will ever call @c release_all on it. Any cown that
/// happens to be linked into this stranded chain remains pinned
/// awaiting a behavior that cannot run, until the next @c bocpy.start
/// cycle (which frees the BOCCown via the GC of its owning Python
/// @c Cown). This residue is intentional and only fires on the
/// dying-runtime path; the upstream-detection alternative (an
/// explicit @c scheduler_running check inside @c whencall before the
/// chain link) introduces a TOCTOU window. The dedicated regression
/// is @c test_schedule_after_runtime_stop_raises in
/// @c test_scheduling_stress.py, which exercises this path and
/// itself contributes one stranded chain per test process.
/// @param behavior the behavior whose count to decrement
/// @return 0 on success, -1 if dispatch failed (Python exception set)
static int behavior_resolve_one(BOCBehavior *behavior) {
  int_least64_t count = atomic_fetch_add(&behavior->count, -1) - 1;
  if (count == 0) {
    BEHAVIOR_INCREF(behavior);
    if (boc_sched_dispatch(&behavior->bq_node) < 0) {
      // Roll back the queue-owned reference we just took. The
      // dispatch failure means no consumer will ever see this
      // behavior, so no DECREF will fire from the worker side.
      BEHAVIOR_DECREF(behavior);
      return -1;
    }
  }

  return 0;
}

static PyObject *request_wrap_borrowed(BOCRequest *request);
static BOCRequest *request_new_inner(BOCCown *cown);
static int request_release_inner(BOCRequest *request);
static int request_start_enqueue_inner(BOCRequest *request,
                                       BOCBehavior *behavior);
static void request_finish_enqueue_inner(BOCRequest *request);

/// @brief Comparator for qsort: order requests by target cown pointer.
/// @param a Pointer to a BOCRequest *
/// @param b Pointer to a BOCRequest *
/// @return Negative / zero / positive per the cown pointer ordering
static int request_cmp_target(const void *a, const void *b) {
  BOCRequest *ra = *(BOCRequest *const *)a;
  BOCRequest *rb = *(BOCRequest *const *)b;
  if (ra->target < rb->target) {
    return -1;
  }
  if (ra->target > rb->target) {
    return 1;
  }
  return 0;
}

/// @brief Build the deduped, target-sorted request array for this behavior.
/// @details Allocates @c behavior->requests (owned by the BOCBehavior;
/// freed by @c behavior_release_all on the normal path or @c behavior_free
/// defensively) and returns a Python list of non-owning PyCapsules pointing
/// into that array. Duplicate requests targeting the same cown are dropped
/// and compensated for via @c behavior_resolve_one — the count was sized
/// for the original args list and the dropped requests would never enter
/// the MCS queue. Sorting in C ensures the Python @c Behavior.schedule()
/// 2PL loop walks requests in deterministic cown order without a
/// Python-level sort.
/// @param op The BehaviorCapsule
/// @return A list of borrowed-pointer PyCapsules in MCS-enqueue order
static PyObject *BehaviorCapsule_create_requests(PyObject *op,
                                                 PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = self->behavior;

  if (behavior->requests != NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "create_requests called twice on the same behavior");
    return NULL;
  }

  Py_ssize_t max_size = behavior->args_size + 1;
  BOCRequest **requests =
      (BOCRequest **)PyMem_RawCalloc((size_t)max_size, sizeof(BOCRequest *));
  if (requests == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  // Result cown always gets a request (it cannot collide with any args
  // cown — args cowns are user-visible, the result cown is fresh).
  BOCRequest *result_request = request_new_inner(behavior->result);
  if (result_request == NULL) {
    PyMem_RawFree(requests);
    return NULL;
  }
  requests[0] = result_request;
  Py_ssize_t count = 1;

  BOCCown **ptr = behavior->args;
  for (Py_ssize_t i = 0; i < behavior->args_size; ++i, ++ptr) {
    BOCCown *cown = *ptr;
    // Linear dedup against the existing entries. args_size is small in
    // practice (bounded by the cown count of a single @when call), so
    // O(n^2) here is fine.
    bool seen = false;
    for (Py_ssize_t j = 1; j < count; ++j) {
      if (requests[j]->target == cown) {
        seen = true;
        break;
      }
    }

    if (seen) {
      // Compensate behavior->count for the duplicate that won't enter
      // the MCS queue (and therefore won't call resolve_one itself).
      if (behavior_resolve_one(behavior) < 0) {
        for (Py_ssize_t k = 0; k < count; ++k) {
          request_decref(requests[k]);
        }
        PyMem_RawFree(requests);
        return NULL;
      }
      continue;
    }

    BOCRequest *request = request_new_inner(cown);
    if (request == NULL) {
      for (Py_ssize_t k = 0; k < count; ++k) {
        request_decref(requests[k]);
      }
      PyMem_RawFree(requests);
      return NULL;
    }
    requests[count++] = request;
  }

  // Sort by target so the 2PL enqueue order is deterministic.
  qsort(requests, (size_t)count, sizeof(BOCRequest *), request_cmp_target);

  // Hand ownership of the array to the BOCBehavior.
  behavior->requests = requests;
  behavior->requests_size = count;

  PyObject *list = PyList_New(count);
  if (list == NULL) {
    // Ownership has already been transferred — behavior_free (or
    // behavior_release_all if the caller still tries to dispatch) will
    // clean up.
    return NULL;
  }

  for (Py_ssize_t i = 0; i < count; ++i) {
    PyObject *capsule = request_wrap_borrowed(requests[i]);
    if (capsule == NULL) {
      Py_DECREF(list);
      return NULL;
    }
    PyList_SET_ITEM(list, i, capsule);
  }

  return list;
}

/// @brief Release every request the behavior owns and free the array.
/// @details Walks @c behavior->requests, calling @c request_release_inner
/// (MCS unlink + handoff to next behavior) on each, then frees the
/// per-request structs and the array itself. Invoked by the worker's
/// release arm in place of the per-request Python @c Request.release loop.
/// @param op The BehaviorCapsule whose requests should be released
/// @return Py_None on success, NULL on error
static PyObject *BehaviorCapsule_release_all(PyObject *op,
                                             PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *capsule = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = capsule->behavior;

  if (behavior->requests == NULL) {
    Py_RETURN_NONE;
  }

  // Detach the array from the behavior up front so behavior_free's
  // defensive cleanup will not double-free if anything below raises.
  BOCRequest **requests = behavior->requests;
  Py_ssize_t requests_size = behavior->requests_size;
  behavior->requests = NULL;
  behavior->requests_size = 0;

  for (Py_ssize_t i = 0; i < requests_size; ++i) {
    if (request_release_inner(requests[i]) < 0) {
      // Free the rest of the array even on error to limit the leak.
      for (Py_ssize_t k = i; k < requests_size; ++k) {
        request_decref(requests[k]);
      }
      PyMem_RawFree(requests);
      return NULL;
    }
    request_decref(requests[i]);
  }

  PyMem_RawFree(requests);
  Py_RETURN_NONE;
}

/// @brief Schedule a behavior: build requests then run the 2PL link loop.
/// @details Two-phase locking entry point that consolidates
/// @c create_requests and the link/finish loops into one C call.
/// All allocations happen before the first MCS link op, so failures
/// cannot leave the cown queues in a partial state. The Python
/// @c Behavior.schedule() collapses to a single call to this function.
/// Dispatch itself (the count → 0 transition in
/// @ref behavior_resolve_one) is allocation-free and infallible:
/// @ref boc_sched_dispatch enqueues @c &behavior->bq_node directly
/// onto a worker's per-task queue, so there is nothing to pre-build.
/// @param op The BehaviorCapsule to schedule
/// @return Py_None on success, NULL on error
static PyObject *BehaviorCapsule_schedule(PyObject *op,
                                          PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *capsule = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = capsule->behavior;

  // Drain the caller's recycle queue opportunistically. The main
  // interpreter ordinarily drains via its own receive() loop; a worker
  // that calls @when from inside a behavior body (i.e. is the caller
  // here) would otherwise have to wait until it returns to
  // _core_scheduler_worker_pop before reclaiming any xidata pushed onto
  // its queue by other interpreters. Non-blocking; the recycle queue is
  // single-consumer (this interpreter), so the drain is safe.
  BOCRecycleQueue_empty(BOC_STATE->recycle_queue, false);

  // Build the request array if it has not already been built (e.g. by an
  // external caller having invoked create_requests first). create_requests
  // is idempotent only via its own guard; here we just skip if populated.
  if (behavior->requests == NULL) {
    PyObject *list = BehaviorCapsule_create_requests(op, NULL);
    if (list == NULL) {
      return NULL;
    }
    Py_DECREF(list);
  }

  BOCRequest **requests = behavior->requests;
  Py_ssize_t n = behavior->requests_size;

  // Drop the GIL across the pure-atomic 2PL link/finish span. The
  // inner ops (atomic_exchange on target->last, atomic_store on prev->next,
  // BEHAVIOR_INCREF, the spin on prev->scheduled, behavior_resolve_one's
  // count decrement) touch no Python state. behavior_resolve_one was made
  // int-returning specifically so it has no Py_RETURN_NONE on the hot path.
  //
  // The only Python-state operation reachable from the inner code is the
  // PyErr_SetString / boc_message_free pair on the count==0 + queue-full
  // branch. count is sized args_size + 2 by BehaviorCapsule_init, and the
  // link loop applies at most args_size decrements, so count >= 2 on every
  // iteration -- the count==0 branch is unreachable here. The final
  // behavior_resolve_one below runs UNDER the GIL and may legitimately
  // hit that branch (queue full); it remains the only PyErr surface.
  bool ok = true;
  Py_BEGIN_ALLOW_THREADS for (Py_ssize_t i = 0; i < n; ++i) {
    // Phase 1: link this request into its cown's MCS queue. The only
    // failure mode is the unreachable PyErr path documented above; if it
    // somehow fires, surface it as a generic error after re-acquiring
    // the GIL (we cannot raise here).
    if (request_start_enqueue_inner(requests[i], behavior) < 0) {
      ok = false;
      break;
    }
  }
  if (ok) {
    // Phase 2: mark each request scheduled. Pure atomic stores; releases
    // the spin in any successor that started linking concurrently.
    for (Py_ssize_t i = 0; i < n; ++i) {
      request_finish_enqueue_inner(requests[i]);
    }
  }
  Py_END_ALLOW_THREADS

      if (!ok) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_RuntimeError,
                      "behavior_schedule: link phase failed");
    }
    return NULL;
  }

  // Final resolve_one to account for the +1 the constructor added so
  // dispatch waits for the 2PL to complete (see BehaviorCapsule_init).
  // Runs UNDER the GIL: it is the legitimate dispatcher of the start
  // message and may set a Python exception on a queue-full failure.
  //
  // If the resolve_one below hits the runtime-down sentinel inside
  // @ref boc_sched_dispatch, the BOCRequest chains linked above are
  // intentionally not unwound; see @ref behavior_resolve_one for
  // the full rationale.
  if (behavior_resolve_one(behavior) < 0) {
    return NULL;
  }

  Py_RETURN_NONE;
}

/// @brief Store an exception as the behavior's result
/// @details Sets the result value and marks the exception flag. Intended for
/// the worker exception handler.
/// @param op The BehaviorCapsule object
/// @param args The exception value
/// @return Py_None on success, NULL on error
static PyObject *BehaviorCapsule_set_exception(PyObject *op, PyObject *args) {
  PyObject *value = NULL;

  if (!PyArg_ParseTuple(args, "O", &value)) {
    return NULL;
  }

  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = self->behavior;
  cown_set_value(behavior->result, value);
  behavior->result->exception = true;
  Py_RETURN_NONE;
}

/// @brief Mark a never-executed behavior's result Cown with a drop exception.
/// @details For behaviors drained during stop() that never had a chance to
/// run. The result Cown is in the published-and-released state
/// (owner=NO_OWNER, xidata=set, value=NULL) that ``Cown(None)``'s
/// constructor leaves it in. Mirrors the worker exception path
/// (``worker.py``: acquire → set_exception → release) but condensed into
/// one C call: cown_acquire takes ownership on the main thread, the
/// exception is stored, then cown_release pickles back to NO_OWNER so a
/// caller awaiting ``cown.value`` / ``cown.exception`` after stop()
/// observes a clear diagnostic instead of a permanent ``None``.
/// @param op The BehaviorCapsule object
/// @param args The exception value
/// @return Py_None on success, NULL on error
static PyObject *BehaviorCapsule_set_drop_exception(PyObject *op,
                                                    PyObject *args) {
  PyObject *value = NULL;

  if (!PyArg_ParseTuple(args, "O", &value)) {
    return NULL;
  }

  BehaviorCapsuleObject *self = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = self->behavior;

  if (cown_acquire(behavior->result) < 0) {
    return NULL;
  }
  cown_set_value(behavior->result, value);
  behavior->result->exception = true;
  if (cown_release(behavior->result) < 0) {
    return NULL;
  }
  Py_RETURN_NONE;
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

  bool is_error = false;

  if (result == NULL) {
    is_error = true;
    result = PyErr_GetRaisedException();
    if (result == NULL) {
      result = PyObject_CallFunction(PyExc_RuntimeError, "s",
                                     "Unknown error when executing behavior");
    }
  }

  cown_set_value(behavior->result, result);
  if (is_error) {
    behavior->result->exception = true;
  }
  return behavior->result->value;
}

static PyMethodDef BehaviorCapsule_methods[] = {
    {"set_exception", BehaviorCapsule_set_exception, METH_VARARGS, NULL},
    {"set_drop_exception", BehaviorCapsule_set_drop_exception, METH_VARARGS,
     NULL},
    {"acquire", BehaviorCapsule_acquire, METH_NOARGS, NULL},
    {"release", BehaviorCapsule_release, METH_NOARGS, NULL},
    {"release_all", BehaviorCapsule_release_all, METH_NOARGS, NULL},
    {"schedule", BehaviorCapsule_schedule, METH_NOARGS, NULL},
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

/// @brief Free a BOCRequest's owned references and the struct itself.
/// @details Private to @c request_decref. Drops the cown ref taken in
/// @c request_new_inner and the behavior ref taken when a successor was
/// linked into @c next during @c request_start_enqueue.
/// @param request The request to free
static void request_free_inner(BOCRequest *request) {
  PRINTDBG("request_free_inner(%p)\n", request);
  COWN_DECREF(request->target);
  BOCBehavior *behavior = (BOCBehavior *)atomic_load_intptr(&request->next);
  if (behavior != NULL) {
    BEHAVIOR_DECREF(behavior);
  }
  PyMem_RawFree(request);
}

/// @brief Drop one reference to a BOCRequest; free on last drop.
/// @details Starts with the owner ref (@c rc = 1) from @c request_new_inner.
/// A successor acquires a second ref in @c request_start_enqueue_inner
/// before reading @c prev->scheduled, so the predecessor cannot be freed
/// under the successor's spin. See the @c rc field comment on BOCRequest.
/// @param request The request to decref
static void request_decref(BOCRequest *request) {
  int_least64_t newval = atomic_fetch_add(&request->rc, -1) - 1;
  assert(newval >= 0);
  if (newval == 0) {
    request_free_inner(request);
  }
}

/// @brief Allocate a new BOCRequest targeting @p cown.
/// @details Increments the cown's refcount; the request takes ownership of
/// that reference until @c request_free_inner releases it. Starts with
/// refcount 1 (the owner ref held by the behavior's @c requests array).
/// @param cown The cown the request targets
/// @return A new BOCRequest, or NULL on allocation failure
static BOCRequest *request_new_inner(BOCCown *cown) {
  BOCRequest *request = (BOCRequest *)PyMem_RawMalloc(sizeof(BOCRequest));
  if (request == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  request->target = cown;
  PRINTDBG("request_new_inner(%p)\n", request);
  COWN_INCREF(cown);
  request->next = 0;
  request->scheduled = 0;
  atomic_store(&request->rc, 1);
  return request;
}

/// @brief Wrap an existing BOCRequest in a non-owning PyCapsule.
/// @details The capsule shares the BOCRequest pointer with the C array on
/// the owning BOCBehavior. The destructor is NULL — the C array is
/// responsible for freeing the request via @c behavior_release_all.
/// @param request The request to wrap
/// @return A new PyCapsule, or NULL on error
static PyObject *request_wrap_borrowed(BOCRequest *request) {
  return PyCapsule_New((void *)request, "boc_request", NULL);
}

/// @brief Release a single request, walking the MCS queue to hand off.
/// @details Called by @c behavior_release_all on every request in the
/// behavior's owned array. The request struct itself is NOT freed here —
/// the caller frees the array as a whole afterwards.
/// @param request The request to release
/// @return 0 on success, -1 on error (Python exception set)
static int request_release_inner(BOCRequest *request) {
  // This code is effectively a MCS-style queue lock release.
  BOCBehavior *next = (BOCBehavior *)atomic_load_intptr(&request->next);
  if (next == NULL) {
    intptr_t expected_ptr = (intptr_t)request;
    if (atomic_compare_exchange_strong_intptr(&request->target->last,
                                              &expected_ptr, 0)) {
      return 0;
    }
  }

  // Wait for the next pointer to be set by a successor's
  // request_start_enqueue_inner. Release the GIL across the spin: the
  // successor is advancing on another thread, may itself be under
  // Py_BEGIN_ALLOW_THREADS (see BehaviorCapsule_schedule's link loop),
  // and should not be blocked here. target->last has already been set
  // past this request by the successor, so this spin terminates as
  // soon as the successor's `atomic_store(&prev->next, behavior_ptr)`
  // is visible. The spin is therefore bounded by another thread's
  // atomic store; if it failed to terminate the runtime invariants
  // would already be violated, so there is no useful interrupt to
  // poll for here.
  Py_BEGIN_ALLOW_THREADS while (true) {
    next = (BOCBehavior *)atomic_load_intptr(&request->next);
    if (next) {
      break;
    }
  }
  Py_END_ALLOW_THREADS

      if (behavior_resolve_one(next) < 0) {
    return -1;
  }
  return 0;
}

/// @brief Release the cown to the next behavior.
/// @details The public release entry point is @c behavior_release_all; the
/// @c request_release_inner helper above is what walks the MCS queue.

/// @brief Enqueue body called by @c behavior_schedule.
/// @details Pure C, no Python allocation. The only failure surface
/// is propagated by @ref behavior_resolve_one, which forwards a
/// dispatch failure from @ref boc_sched_dispatch (e.g. the runtime
/// was torn down between the caller's @c terminator_inc and our
/// dispatch). On failure a Python exception is set and the link
/// loop's caller is expected to roll back its terminator hold;
/// see @c whencall in @c behaviors.py.
/// @param request The request to enqueue
/// @param behavior The behavior owning the request
/// @return 0 on success, -1 on error with a Python exception set
static int request_start_enqueue_inner(BOCRequest *request,
                                       BOCBehavior *behavior) {
  intptr_t request_ptr = (intptr_t)request;
  intptr_t prev_ptr =
      atomic_exchange_intptr(&request->target->last, request_ptr);
  if (prev_ptr == 0) {
    // there is no prior request queued on the cown, so we can immediately
    // proceed
    if (behavior_resolve_one(behavior) < 0) {
      return -1;
    }
    return 0;
  }

  intptr_t behavior_ptr = (intptr_t)behavior;
  BOCRequest *prev = (BOCRequest *)prev_ptr;
  // Take a temporary ref on the predecessor request: we are about to
  // spin on prev->scheduled below, and prev's owning behavior can run
  // release_all concurrently once we have stored prev->next. Without
  // this ref, the predecessor could be freed between our store of
  // prev->next and our next load of prev->scheduled -- a UAF the
  // distributed-release design must guard against because release runs
  // on the worker thread, not on the same thread as the link loop. The
  // matching decref happens after the spin completes. At the moment of
  // the fetch_add, prev is still in
  // the MCS queue for this cown (our exchange on target->last showed
  // prev_ptr there), so prev cannot have been freed yet.
  atomic_fetch_add(&prev->rc, 1);
  assert(atomic_load_intptr(&prev->next) == 0);
  atomic_store_intptr(&prev->next, behavior_ptr);
  PRINTDBG("request->next = bid=%" PRIdLEAST64 "\n", behavior->id);
  BEHAVIOR_INCREF(behavior);
  // Order note: bocpy stores prev->next BEFORE spinning on
  // prev->scheduled, the opposite of Verona's Slot::set_next which
  // observes the predecessor's scheduled flag first. The inversion
  // is safe because (a) the prev->rc++ above keeps prev alive across
  // the window where prev's owning behavior may run release_all
  // concurrently once prev->next is published, preventing the UAF
  // such ordering would otherwise admit (see the rc-comment block
  // above); and (b) the behavior dispatch invariant ensures no
  // successor can run user code until ALL its requests have
  // completed phase 2 (request_finish_enqueue_inner), so the
  // predecessor cannot retire the chain prematurely while we spin.
  while (true) {
    if (atomic_load(&prev->scheduled)) {
      break;
    }
  }
  // Drop the temporary ref; this may be the final ref if the
  // predecessor's owner has already run release_all.
  request_decref(prev);

  return 0;
}

/// @brief Atomic-only finish of the second 2PL phase.
/// @details Releases the spin in @c request_start_enqueue_inner waiting on
/// the predecessor's scheduled flag. Pure atomic store, infallible.
/// @param request The request to mark scheduled
static void request_finish_enqueue_inner(BOCRequest *request) {
  atomic_exchange(&request->scheduled, true);
}

/// @brief Whether this module is the "primary" module, i.e. the one owned by
/// the main interpreter that drives runtime lifecycle.
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

/// @brief Return the best-effort physical CPU count for the process.
/// @details Thin wrapper around @ref boc_physical_cpu_count. Returns 0
/// if the platform-specific detection failed; the Python caller falls
/// back to the logical CPU count in that case.
/// @param module The _core module (unused)
/// @param Py_UNUSED unused arg
/// @return PyLong of the physical core count, or 0 on detection failure.
static PyObject *_core_physical_cpu_count(PyObject *Py_UNUSED(module),
                                          PyObject *Py_UNUSED(dummy)) {
  return PyLong_FromLong((long)boc_physical_cpu_count());
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
      BOCTag *oldtag =
          (BOCTag *)atomic_exchange_intptr(&qptr->tag, (intptr_t)NULL);
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

    // assign a new tag. tag_from_PyUnicode returned with rc=1, which
    // is exactly the queue's owning reference — no extra TAG_INCREF
    // is needed here. The previously-installed tag (if any) is
    // disabled and released so any in-flight messages still holding
    // owning refs to it can complete and free the tag when done.
    BOCTag *oldtag =
        (BOCTag *)atomic_exchange_intptr(&qptr->tag, (intptr_t)qtag);
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

  // Wake any receivers parked on condvars so they re-resolve their tags
  qptr = BOC_QUEUES;
  for (Py_ssize_t i = 0; i < BOC_QUEUE_COUNT; ++i, ++qptr) {
    if (atomic_load_explicit(&qptr->waiters, memory_order_seq_cst) > 0) {
      boc_park_lock(qptr);
      boc_park_broadcast(qptr);
      boc_park_unlock(qptr);
    }
  }

  Py_RETURN_NONE;
}

/// @brief Reconstructs a CownCapsule from a pickled pointer
/// @details Used by CownCapsule.__reduce__ to unpickle. Inherits the
/// COWN_INCREF pin from __reduce__ (no additional INCREF). Must not use
/// cown_capsule_wrap which would double-INCREF.
/// @param module The _core module (unused)
/// @param args Tuple of (pointer_as_int, process_id)
/// @return A new CownCapsule, or NULL on error
static PyObject *_cown_capsule_from_pointer(PyObject *module, PyObject *args) {
  PyObject *ptr_obj, *pid_obj;
  if (!PyArg_ParseTuple(args, "OO", &ptr_obj, &pid_obj)) {
    return NULL;
  }

  long pickled_pid = PyLong_AsLong(pid_obj);
  if (pickled_pid == -1 && PyErr_Occurred()) {
    return NULL;
  }

#ifdef _WIN32
  long current_pid = (long)_getpid();
#else
  long current_pid = (long)getpid();
#endif
  if (pickled_pid != current_pid) {
    PyErr_SetString(PyExc_RuntimeError,
                    "CownCapsule cannot be unpickled in a different process");
    return NULL;
  }

  BOCCown *cown = (BOCCown *)PyLong_AsVoidPtr(ptr_obj);
  if (cown == NULL) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_ValueError, "Invalid cown pointer");
    }
    return NULL;
  }

  // Take a fresh strong reference for this capsule. Each unpickle is an
  // independent live reference to the BOCCown; the dealloc path does the
  // matching COWN_DECREF. The caller must guarantee the BOCCown is still
  // alive at this point (see CownCapsule_reduce for the contract).
  PyTypeObject *type = BOC_STATE->cown_capsule_type;
  CownCapsuleObject *capsule = (CownCapsuleObject *)type->tp_alloc(type, 0);
  if (capsule == NULL) {
    return NULL;
  }

  COWN_INCREF(cown);
  capsule->cown = cown;
  return (PyObject *)capsule;
}

/// @brief Pre-pin a list of CownCapsules and return their pointers as ints.
/// @details Used by the Python @c notice_write helper to keep every
/// BOCCown reachable from a noticeboard value alive across the message
/// queue's pickle/unpickle gap. The writer thread calls this **before**
/// sending the noticeboard_write message; the returned integer pointers
/// are sent as part of the message and consumed by
/// @ref nb_pin_cowns (which transfers ownership into the noticeboard
/// entry without an extra INCREF). Without this, every CownCapsule in
/// the value would be reduced to a bare pointer at pickle-time, the
/// writer behavior would return and drop its CownCapsule wrappers, the
/// underlying BOCCowns would be freed to recycle memory, and the
/// receiving worker would unpickle and INCREF dangling pointers.
/// @param module Unused
/// @param args Tuple of (pins: sequence of CownCapsule)
/// @return A new Python list of int (BOCCown* cast to integer) on success,
///   NULL on error. On error every INCREF taken so far is rolled back.
static PyObject *_core_cown_pin_pointers(PyObject *module, PyObject *args) {
  BOC_STATE_SET(module);

  PyObject *seq_arg;
  if (!PyArg_ParseTuple(args, "O", &seq_arg)) {
    return NULL;
  }

  PyObject *seq =
      PySequence_Fast(seq_arg, "cown_pin_pointers requires a sequence");
  if (seq == NULL) {
    return NULL;
  }

  Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
  PyObject *result = PyList_New(n);
  if (result == NULL) {
    Py_DECREF(seq);
    return NULL;
  }

  PyTypeObject *capsule_type = BOC_STATE->cown_capsule_type;
  Py_ssize_t i = 0;
  for (; i < n; i++) {
    PyObject *item = PySequence_Fast_GET_ITEM(seq, i);
    if (!PyObject_TypeCheck(item, capsule_type)) {
      PyErr_SetString(PyExc_TypeError,
                      "cown_pin_pointers requires CownCapsule objects");
      goto fail;
    }
    BOCCown *cown = ((CownCapsuleObject *)item)->cown;
    COWN_INCREF(cown);
    PyObject *ptr = PyLong_FromVoidPtr(cown);
    if (ptr == NULL) {
      // Roll back the ref we just took before joining the cleanup loop.
      COWN_DECREF(cown);
      goto fail;
    }
    PyList_SET_ITEM(result, i, ptr); // steals ref
  }

  Py_DECREF(seq);
  return result;

fail:
  // Drop INCREFs for entries we already pre-pinned (indices 0..i-1).
  for (Py_ssize_t j = 0; j < i; j++) {
    PyObject *ptr_obj = PyList_GET_ITEM(result, j);
    BOCCown *c = (BOCCown *)PyLong_AsVoidPtr(ptr_obj);
    if (c != NULL) {
      COWN_DECREF(c);
    } else {
      PyErr_Clear();
    }
  }
  Py_DECREF(result);
  Py_DECREF(seq);
  return NULL;
}

/// @brief Snapshot the per-worker scheduler counters.
/// @details Returns one dict per worker carrying the @ref
/// boc_sched_stats_t fields, or an empty list when the runtime is
/// down (no workers allocated). Reads are best-effort
/// (memory_order_relaxed): values are monotonic counters, so a torn
/// read can only under-report.
/// @param module The _core module
/// @param Py_UNUSED
/// @return A list of per-worker stats dicts, or NULL on error.
static PyObject *_core_scheduler_stats(PyObject *Py_UNUSED(module),
                                       PyObject *Py_UNUSED(dummy)) {
  Py_ssize_t n = boc_sched_worker_count();
  PyObject *result = PyList_New(n);
  if (result == NULL) {
    return NULL;
  }
  for (Py_ssize_t i = 0; i < n; ++i) {
    boc_sched_stats_t s;
    if (boc_sched_stats_snapshot(i, &s) < 0) {
      PyErr_SetString(PyExc_RuntimeError, "boc_sched_stats_snapshot failed");
      Py_DECREF(result);
      return NULL;
    }
    PyObject *d = Py_BuildValue(
        "{s:n,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K}",
        "worker_index", i, "pushed_local", (unsigned long long)s.pushed_local,
        "dispatched_to_pending", (unsigned long long)s.dispatched_to_pending,
        "pushed_remote", (unsigned long long)s.pushed_remote, "popped_local",
        (unsigned long long)s.popped_local, "popped_via_steal",
        (unsigned long long)s.popped_via_steal, "enqueue_cas_retries",
        (unsigned long long)s.enqueue_cas_retries, "dequeue_cas_retries",
        (unsigned long long)s.dequeue_cas_retries, "batch_resets",
        (unsigned long long)s.batch_resets, "steal_attempts",
        (unsigned long long)s.steal_attempts, "steal_failures",
        (unsigned long long)s.steal_failures, "parked",
        (unsigned long long)s.parked, "last_steal_attempt_ns",
        (unsigned long long)s.last_steal_attempt_ns, "fairness_arm_fires",
        (unsigned long long)s.fairness_arm_fires);
    if (d == NULL) {
      Py_DECREF(result);
      return NULL;
    }
    PyList_SET_ITEM(result, i, d); // steals ref
  }
  return result;
}

/// @brief Snapshot the per-tagged-queue contention counters.
/// @details Returns one dict per assigned BOCQueue (state ==
/// BOC_QUEUE_ASSIGNED) carrying the four @c memory_order_relaxed
/// counters bumped by @c boc_enqueue / @c boc_dequeue. Unassigned
/// queues are skipped because their tag is NULL.
/// @param module The _core module
/// @param Py_UNUSED
/// @return A list of per-queue stats dicts, or NULL on error.
static PyObject *_core_queue_stats(PyObject *Py_UNUSED(module),
                                   PyObject *Py_UNUSED(dummy)) {
  PyObject *result = PyList_New(0);
  if (result == NULL) {
    return NULL;
  }
  BOCQueue *qptr = BOC_QUEUES;
  for (size_t i = 0; i < BOC_QUEUE_COUNT; ++i, ++qptr) {
    int_least64_t state =
        atomic_load_explicit(&qptr->state, memory_order_relaxed);
    if (state != BOC_QUEUE_ASSIGNED) {
      continue;
    }
    BOCTag *tag = (BOCTag *)atomic_load_intptr(&qptr->tag);
    PyObject *tag_obj;
    if (tag != NULL && tag->str != NULL) {
      tag_obj = PyUnicode_FromString(tag->str);
      if (tag_obj == NULL) {
        Py_DECREF(result);
        return NULL;
      }
    } else {
      tag_obj = Py_NewRef(Py_None);
    }
    uint64_t enq_r = boc_atomic_load_u64_explicit(&qptr->enqueue_cas_retries,
                                                  BOC_MO_RELAXED);
    uint64_t deq_r = boc_atomic_load_u64_explicit(&qptr->dequeue_cas_retries,
                                                  BOC_MO_RELAXED);
    uint64_t pushed =
        boc_atomic_load_u64_explicit(&qptr->pushed_total, BOC_MO_RELAXED);
    uint64_t popped =
        boc_atomic_load_u64_explicit(&qptr->popped_total, BOC_MO_RELAXED);
    PyObject *d = Py_BuildValue(
        "{s:n,s:N,s:K,s:K,s:K,s:K}", "queue_index", (Py_ssize_t)qptr->index,
        "tag", tag_obj, // steals ref
        "enqueue_cas_retries", (unsigned long long)enq_r, "dequeue_cas_retries",
        (unsigned long long)deq_r, "pushed_total", (unsigned long long)pushed,
        "popped_total", (unsigned long long)popped);
    if (d == NULL) {
      Py_DECREF(result);
      return NULL;
    }
    if (PyList_Append(result, d) < 0) {
      Py_DECREF(d);
      Py_DECREF(result);
      return NULL;
    }
    Py_DECREF(d);
  }
  return result;
}

/// @brief Initialise the scheduler runtime for a fresh start cycle.
/// @details Tears down any previous per-worker array, then allocates
/// a new one of the requested size and resets the registration
/// counter. Called by @c behaviors.start() exactly once per
/// `start()`/`wait()`/`start()` cycle, before worker sub-interpreters
/// are spawned. Idempotent in the down state.
/// @param module The _core module
/// @param arg PyLong worker_count (must be >= 0)
/// @return Py_None on success, NULL with an exception on failure.
static PyObject *_core_scheduler_runtime_start(PyObject *Py_UNUSED(module),
                                               PyObject *arg) {
  long long n = PyLong_AsLongLong(arg);
  if (n == -1 && PyErr_Occurred()) {
    return NULL;
  }
  if (n < 0) {
    PyErr_SetString(PyExc_ValueError,
                    "scheduler_runtime_start: worker_count must be >= 0");
    return NULL;
  }
  // Idempotent shutdown: safe whether or not a previous cycle ran.
  boc_sched_shutdown();
  if (boc_sched_init((Py_ssize_t)n) < 0) {
    return NULL; // exception already set
  }

  // Allocate one fairness-token BOCBehavior per worker. Tokens
  // are zero-initialised so every refcount / cown-array field is the
  // safe NULL state, and `is_token = 1` discriminates them at the
  // worker-pop site. Allocation lives here (and not in
  // `boc_sched_init`) because `sched.c` deliberately treats
  // `BOCBehavior` as opaque.
  for (Py_ssize_t i = 0; i < (Py_ssize_t)n; ++i) {
    BOCBehavior *token = (BOCBehavior *)PyMem_RawCalloc(1, sizeof(BOCBehavior));
    if (token == NULL) {
      // Roll back any tokens already installed and tear the runtime
      // back down so the caller sees a clean failure (no half-init).
      for (Py_ssize_t j = 0; j < i; ++j) {
        BOCBehavior *prev = NULL;
        boc_bq_node_t *prev_node = boc_sched_get_token_node(j);
        if (prev_node != NULL) {
          prev = BEHAVIOR_FROM_BQ_NODE(prev_node);
        }
        boc_sched_set_token_node(j, NULL);
        if (prev != NULL) {
          PyMem_RawFree(prev);
        }
      }
      boc_sched_shutdown();
      PyErr_NoMemory();
      return NULL;
    }
    // Mark as token. PyMem_RawCalloc has zeroed everything (NULL
    // thunk/result/args/captures/requests, count == rc == 0,
    // bq_node.next_in_queue == NULL). The behaviour is never
    // reference-counted via BEHAVIOR_INCREF/DECREF and never visits
    // the request/cown machinery; it is recycled in place by the
    // token re-enqueue path. We give it an `id` of -1 so any
    // diagnostic that prints `behavior->id` for a token is
    // immediately recognisable.
    token->is_token = 1;
    token->id = -1;
    token->owner_worker_index = (int16_t)i;
    if (boc_sched_set_token_node(i, &token->bq_node) < 0) {
      // worker_index out of range: only possible if WORKER_COUNT
      // changed under us, which the GIL precludes. Defensive.
      PyMem_RawFree(token);
      boc_sched_shutdown();
      PyErr_SetString(PyExc_RuntimeError,
                      "scheduler_runtime_start: token install failed");
      return NULL;
    }
    // Lazy bootstrap (Verona-faithful): we do NOT enqueue the token
    // onto the worker's queue here. The worker's
    // `should_steal_for_fairness` flag is already initialised to
    // true by `boc_sched_init` (mirrors Verona `core.h:23` —
    // `should_steal_for_fairness{true}`). The first time the worker
    // has a non-empty queue and calls `pop_fast`, the fairness gate
    // routes through `pop_slow`, whose arm re-enqueues this token
    // from `self->token_work`. From then on the heartbeat is alive
    // and self-sustaining: every owner-side fairness arm fire
    // re-enqueues the token, and every token consumption (by owner
    // or thief) sets the owner's flag back to true via the dispatch
    // loop in `_core_scheduler_worker_pop`.
  }

  Py_RETURN_NONE;
}

/// @brief Tear down the scheduler runtime at the end of a start cycle.
/// @details Frees the per-worker array and resets the registration
/// counter. Idempotent. Called by @c behaviors.stop_workers after the
/// worker threads have been joined.
/// @param module The _core module
/// @param Py_UNUSED
/// @return Py_None.
static PyObject *_core_scheduler_runtime_stop(PyObject *Py_UNUSED(module),
                                              PyObject *Py_UNUSED(dummy)) {
  // Recover and free per-worker fairness tokens before
  // `boc_sched_shutdown` frees the worker array. Each token is a
  // bare `BOCBehavior` allocated by `_core_scheduler_runtime_start`
  // via PyMem_RawCalloc; it never goes through behavior_free /
  // BEHAVIOR_DECREF (zero refcount, no captured cowns).
  Py_ssize_t worker_count = boc_sched_worker_count();
  for (Py_ssize_t i = 0; i < worker_count; ++i) {
    boc_bq_node_t *node = boc_sched_get_token_node(i);
    if (node == NULL) {
      continue;
    }
    BOCBehavior *token = BEHAVIOR_FROM_BQ_NODE(node);
    boc_sched_set_token_node(i, NULL);
    PyMem_RawFree(token);
  }
  boc_sched_shutdown();
  Py_RETURN_NONE;
}

/// @brief Atomically claim a worker slot for the calling thread.
/// @details Wraps @ref boc_sched_worker_register. Returns the
/// assigned slot index (0..worker_count-1) on success. Raises
/// @c RuntimeError if no free slot remains (over-registration: more
/// callers than @c boc_sched_init was given).
/// @param module The _core module
/// @param Py_UNUSED
/// @return PyLong slot index, or NULL with RuntimeError set.
static PyObject *_core_scheduler_worker_register(PyObject *Py_UNUSED(module),
                                                 PyObject *Py_UNUSED(dummy)) {
  Py_ssize_t slot = boc_sched_worker_register();
  if (slot < 0) {
    PyErr_SetString(
        PyExc_RuntimeError,
        "scheduler_worker_register: no free worker slot (over-registration)");
    return NULL;
  }
  return PyLong_FromSsize_t(slot);
}

/// @brief Set @c stop_requested on every worker and wake them all.
/// @details Wraps @ref boc_sched_worker_request_stop_all. Idempotent.
/// Production callers: @c behaviors.stop_workers and
/// @c Behaviors.terminator_callback (see @c src/bocpy/behaviors.py).
/// @param module The _core module
/// @param Py_UNUSED
/// @return Py_None.
static PyObject *_core_scheduler_request_stop_all(PyObject *Py_UNUSED(module),
                                                  PyObject *Py_UNUSED(dummy)) {
  boc_sched_worker_request_stop_all();
  Py_RETURN_NONE;
}

/// @brief Wait for the next behaviour and return it as a BehaviorCapsule.
/// @details The production consumer entry point. The calling
/// thread must already be registered with @ref boc_sched_worker_register
/// (the worker bootstrap calls that on entry to @c do_work).
///
/// Hot path: @ref boc_sched_worker_pop_fast — pending or own queue.
/// Drops to @ref boc_sched_worker_pop_slow which parks under the
/// worker's @c cv until @ref boc_sched_dispatch wakes it or
/// @ref boc_sched_worker_request_stop_all flips @c stop_requested.
/// Returns @c None when @c pop_slow returns NULL (stop signal); the
/// worker treats that as the loop-exit condition.
///
/// **Refcount transfer.** The producer in
/// @c behavior_resolve_one calls @c BEHAVIOR_INCREF before
/// @c boc_sched_dispatch, taking a queue-owned reference. This
/// function consumes that reference and installs it in the freshly
/// allocated @c BehaviorCapsule. The capsule's @c tp_dealloc runs
/// @c BEHAVIOR_DECREF on the worker side, balancing the producer
/// side. Do not @c BEHAVIOR_INCREF here.
///
/// **GIL.** The slow arm releases the GIL across @c cnd_wait
/// internally (see @ref boc_sched_worker_pop_slow). This wrapper
/// therefore needs no surrounding @c Py_BEGIN_ALLOW_THREADS — the
/// only blocking syscall is wrapped at the C layer.
///
/// **Allocation failure.** If @c tp_alloc fails after a successful
/// pop, the popped behaviour is leaked (its queue-owned reference
/// is never balanced). This is a defensive path that requires
/// memory exhaustion mid-dispatch; logging-and-returning-None would
/// hide the leak. We surface the @c PyErr_NoMemory and let the
/// worker's exception handler log it; the leak is preferable to a
/// double-free.
/// @param module The _core module
/// @param Py_UNUSED unused arg
/// @return Fresh BehaviorCapsule, or @c None on shutdown. NULL on
///         error with a Python exception set.
static PyObject *_core_scheduler_worker_pop(PyObject *Py_UNUSED(module),
                                            PyObject *Py_UNUSED(dummy)) {
  boc_sched_worker_t *self = boc_sched_current_worker();
  if (self == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "scheduler_worker_pop: thread not registered");
    return NULL;
  }
  // Token-loop. Mirrors Verona `SchedulerThread::run_inner`
  // (`schedulerthread.h`), which dequeues a `Work*`, executes its
  // closure, and loops back if the closure was the per-Core
  // `token_work`. bocpy keeps the loop here (rather than inside
  // `boc_sched_worker_pop_*`) so the sched TU stays opaque to
  // `BOCBehavior` layout: only this TU knows how to dereference
  // `is_token`. The token's "thunk" body is the C-side helper
  // `boc_sched_set_steal_flag(self, true)` — same effect as the
  // Verona closure at `core.h:28-32`.
  BOCBehavior *behavior;
  for (;;) {
    // Drain this worker interpreter's recycle queue. Cross-interpreter
    // cown_acquire pushes the previous owner's xidata onto THAT owner's
    // queue; only the owning interpreter is allowed to consume it (the
    // recycle queue is single-consumer). Without this drain the worker
    // never reclaims xidata that other workers/the main interpreter
    // pushed onto its queue, and the corresponding BOCCown weak refs
    // (taken by BOCRecycleQueue_register on every cown_release) are
    // never released -- a steady leak of one BOCCown per cross-worker
    // hop. The legacy receive("boc_behavior") loop drained on every
    // spin (see receive_single_tag); the distributed-scheduler worker
    // bypasses receive entirely, so the drain has to live here.
    BOCRecycleQueue_empty(BOC_STATE->recycle_queue, false);
    boc_bq_node_t *n = boc_sched_worker_pop_fast(self);
    if (n == NULL) {
      n = boc_sched_worker_pop_slow(self);
      if (n == NULL) {
        // pop_slow returns NULL only when stop_requested is set.
        Py_RETURN_NONE;
      }
    }
    behavior = BEHAVIOR_FROM_BQ_NODE(n);
    if (!behavior->is_token) {
      break;
    }
    // Token sentinel: set the OWNING worker's fairness flag, not
    // ours. The token may have been stolen and is now running on
    // a thief — but the heartbeat must report back to the owner so
    // the owner's `pop_slow` fairness arm fires next time it has
    // local work, re-enqueueing the token from the owner's
    // `self->token_work` slot. Verona achieves the same effect by
    // capturing the owning core's `this` in `Closure::make`
    // (`core.h:24-32`); we use an explicit `owner_worker_index`
    // field on the token because closures are not free in C.
    //
    // The token's `bq_node` is dropped here (NOT re-enqueued by
    // this thread). The owner's slow-path arm is the only place
    // that ever re-enqueues a token, and it always uses its own
    // `token_work` slot — so the bq_node is owner-owned and
    // single-producer for re-enqueue purposes (no cross-thread
    // double-enqueue risk).
    boc_sched_worker_t *owner =
        boc_sched_worker_at(behavior->owner_worker_index);
    boc_sched_set_steal_flag(owner, true);
  }
  PyTypeObject *type = BOC_STATE->behavior_capsule_type;
  BehaviorCapsuleObject *capsule =
      (BehaviorCapsuleObject *)type->tp_alloc(type, 0);
  if (capsule == NULL) {
    return NULL;
  }
  // Transfer the queue-owned reference into the capsule. Do NOT
  // BEHAVIOR_INCREF: the producer already incref'd before dispatch.
  capsule->behavior = behavior;
  return (PyObject *)capsule;
}

/// @brief Drain every per-worker queue and return the behaviours
///        as a list of @c BehaviorCapsule objects.
/// @details Used by @c behaviors.stop_workers after the worker
/// threads have joined but before @c scheduler_runtime_stop frees
/// the worker array. Each worker's @c bq_t is repeatedly dequeued
/// until empty; each popped node is wrapped in a fresh
/// @c BehaviorCapsule, transferring the queue-owned reference (no
/// extra @c BEHAVIOR_INCREF). The Python caller then runs
/// @c release_all on each capsule to unwind MCS chains and drop the
/// terminator hold the original @c whencall took. Calling this with
/// the runtime down (@c WORKER_COUNT == 0) returns an empty list.
///
/// **Why not in @c boc_sched_shutdown.** Releasing the cown chain\n/// requires
/// Python-level @c release_all (it touches @c BOCRequest\n/// arrays whose
/// freeing routes through @c COWN_DECREF). Doing this\n/// in C without the GIL
/// would also deadlock against any pending\n/// noticeboard mutator. The Python
/// orchestration layer is the right\n/// place to coordinate.\n/// @param
/// module The _core module\n/// @param Py_UNUSED unused arg\n/// @return Fresh
/// @c list[BehaviorCapsule] (possibly empty), or NULL\n///         on
/// allocation failure with a Python exception set.
static PyObject *_core_scheduler_drain_all_queues(PyObject *Py_UNUSED(module),
                                                  PyObject *Py_UNUSED(dummy)) {
  PyObject *out = PyList_New(0);
  if (out == NULL) {
    return NULL;
  }
  Py_ssize_t worker_count = boc_sched_worker_count();
  PyTypeObject *type = BOC_STATE->behavior_capsule_type;
  for (Py_ssize_t i = 0; i < worker_count; ++i) {
    boc_sched_worker_t *w = boc_sched_worker_at(i);
    if (w == NULL) {
      continue;
    }
    for (;;) {
      boc_bq_node_t *n = boc_wsq_dequeue(w);
      if (n == NULL) {
        break;
      }
      BOCBehavior *behavior = BEHAVIOR_FROM_BQ_NODE(n);
      if (behavior->is_token) {
        // Token sentinels are not reference-counted and own no
        // cowns; they live in the per-worker `token_work` slot and
        // are freed by `_core_scheduler_runtime_stop`. Skip them
        // here so we don't hand a token to the Python release-all
        // path (which would dereference NULL request arrays).
        continue;
      }
      BehaviorCapsuleObject *capsule =
          (BehaviorCapsuleObject *)type->tp_alloc(type, 0);
      if (capsule == NULL) {
        // Rebalance the queue-owned reference we just popped before
        // bailing — otherwise the behaviour leaks.
        BEHAVIOR_DECREF(behavior);
        Py_DECREF(out);
        return NULL;
      }
      capsule->behavior = behavior; // ref transferred in
      if (PyList_Append(out, (PyObject *)capsule) < 0) {
        Py_DECREF(capsule);
        Py_DECREF(out);
        return NULL;
      }
      Py_DECREF(capsule); // list owns it now
    }
  }
  return out;
}

static PyMethodDef _core_module_methods[] = {
    {"send", _core_send, METH_VARARGS,
     "send($module, tag, contents, /)\n--\n\nSends a message."},
    {"receive", (PyCFunction)(void (*)(void))_core_receive,
     METH_VARARGS | METH_KEYWORDS,
     "receive($module, tags, /, timeout=-1, after=None)\n--\n\n"
     "Receives a message."},
    {"drain", _core_drain, METH_VARARGS,
     "drain($module, tags, /)\n--\n\nDrains all messages for the given tags."},
    {"is_primary", _core_is_primary, METH_NOARGS, NULL},
    {"index", _core_index, METH_NOARGS, NULL},
    {"recycle", _core_recycle, METH_NOARGS, NULL},
    {"physical_cpu_count", _core_physical_cpu_count, METH_NOARGS,
     "physical_cpu_count($module, /)\n--\n\n"
     "Best-effort count of physical CPU cores available to this process. "
     "Returns 0 if detection failed; callers should fall back to the logical "
     "CPU count in that case."},
    {"cowns", _core_cowns, METH_NOARGS, NULL},
    {"set_tags", _core_set_tags, METH_VARARGS,
     "set_tags($module, tags, /)\n--\n\nAssigns tags to message queues."},
    {"scheduler_stats", _core_scheduler_stats, METH_NOARGS,
     "scheduler_stats($module, /)\n--\n\n"
     "Snapshot of per-worker scheduler counters (one dict per worker; "
     "empty list when the runtime is down)."},
    {"queue_stats", _core_queue_stats, METH_NOARGS,
     "queue_stats($module, /)\n--\n\n"
     "Snapshot of per-tagged-queue contention counters."},
    {"scheduler_runtime_start", _core_scheduler_runtime_start, METH_O,
     "scheduler_runtime_start($module, worker_count, /)\n--\n\n"
     "Allocate the per-worker scheduler array. Called by behaviors.start()."},
    {"scheduler_runtime_stop", _core_scheduler_runtime_stop, METH_NOARGS,
     "scheduler_runtime_stop($module, /)\n--\n\n"
     "Free the per-worker scheduler array. Called by behaviors.stop_workers."},
    {"scheduler_worker_register", _core_scheduler_worker_register, METH_NOARGS,
     "scheduler_worker_register($module, /)\n--\n\n"
     "Claim the next free worker slot for the calling thread. "
     "Raises RuntimeError on over-registration."},
    {"scheduler_request_stop_all", _core_scheduler_request_stop_all,
     METH_NOARGS,
     "scheduler_request_stop_all($module, /)\n--\n\n"
     "Set stop_requested on every worker and wake them all."},
    {"scheduler_worker_pop", _core_scheduler_worker_pop, METH_NOARGS,
     "scheduler_worker_pop($module, /)\n--\n\n"
     "Wait for and return the next BehaviorCapsule, or None on shutdown."},
    {"scheduler_drain_all_queues", _core_scheduler_drain_all_queues,
     METH_NOARGS,
     "scheduler_drain_all_queues($module, /)\n--\n\n"
     "Drain every per-worker queue. Returns list[BehaviorCapsule]."},
    {"_cown_capsule_from_pointer", _cown_capsule_from_pointer, METH_VARARGS,
     NULL},
    {"cown_pin_pointers", _core_cown_pin_pointers, METH_VARARGS,
     "cown_pin_pointers($module, pins, /)\n--\n\n"
     "INCREF each CownCapsule and return raw pointer ints (transfers refs)."},
    {"noticeboard_write_direct", _core_noticeboard_write_direct, METH_VARARGS,
     "noticeboard_write_direct($module, key, value, /)"
     "\n--\n\nWrites a key-value pair to the noticeboard."},
    {"noticeboard_snapshot", _core_noticeboard_snapshot, METH_NOARGS,
     "noticeboard_snapshot($module, /)"
     "\n--\n\nReturns a cached snapshot of the noticeboard as a dict."},
    {"noticeboard_clear", _core_noticeboard_clear, METH_NOARGS,
     "noticeboard_clear($module, /)"
     "\n--\n\nClears all noticeboard entries."},
    {"noticeboard_delete", _core_noticeboard_delete, METH_VARARGS,
     "noticeboard_delete($module, key, /)"
     "\n--\n\nDeletes a single noticeboard entry by key."},
    {"noticeboard_cache_clear", _core_noticeboard_cache_clear, METH_NOARGS,
     "noticeboard_cache_clear($module, /)"
     "\n--\n\nClears the thread-local snapshot cache."},
    {"noticeboard_version", _core_noticeboard_version, METH_NOARGS,
     "noticeboard_version($module, /)"
     "\n--\n\nReturns the global noticeboard version counter."},
    {"set_noticeboard_thread", _core_set_noticeboard_thread, METH_NOARGS,
     "set_noticeboard_thread($module, /)"
     "\n--\n\nRegisters the calling thread as the noticeboard mutator "
     "thread."},
    {"clear_noticeboard_thread", _core_clear_noticeboard_thread, METH_NOARGS,
     "clear_noticeboard_thread($module, /)"
     "\n--\n\nClears the registered noticeboard mutator thread."},
    {"notice_sync_request", _core_notice_sync_request, METH_NOARGS,
     "notice_sync_request($module, /)"
     "\n--\n\nAllocates a fresh notice_sync sequence number."},
    {"notice_sync_complete", _core_notice_sync_complete, METH_VARARGS,
     "notice_sync_complete($module, seq, /)"
     "\n--\n\nMarks a notice_sync sequence as processed and wakes waiters."},
    {"notice_sync_wait", _core_notice_sync_wait, METH_VARARGS,
     "notice_sync_wait($module, seq, timeout, /)"
     "\n--\n\nBlocks until the given notice_sync sequence is processed."},
    {"terminator_inc", _core_terminator_inc, METH_NOARGS,
     "terminator_inc($module, /)"
     "\n--\n\nIncrement the terminator. Returns new count or -1 if closed."},
    {"terminator_dec", _core_terminator_dec, METH_NOARGS,
     "terminator_dec($module, /)"
     "\n--\n\nDecrement the terminator. Wakes terminator_wait on 0."},
    {"terminator_close", _core_terminator_close, METH_NOARGS,
     "terminator_close($module, /)"
     "\n--\n\nMark the terminator closed; future terminator_inc returns -1."},
    {"terminator_wait", _core_terminator_wait, METH_VARARGS,
     "terminator_wait($module, timeout, /)"
     "\n--\n\nBlock until the terminator count reaches 0 or timeout."},
    {"terminator_seed_dec", _core_terminator_seed_dec, METH_NOARGS,
     "terminator_seed_dec($module, /)"
     "\n--\n\nIdempotent one-shot decrement of the Pyrona seed."},
    {"terminator_reset", _core_terminator_reset, METH_NOARGS,
     "terminator_reset($module, /)"
     "\n--\n\nRestore terminator state for a fresh runtime start. "
     "Returns the prior (count, seeded) for drift detection."},
    {"terminator_count", _core_terminator_count, METH_NOARGS,
     "terminator_count($module, /)"
     "\n--\n\nRead the current terminator count."},
    {"terminator_seeded", _core_terminator_seeded, METH_NOARGS,
     "terminator_seeded($module, /)"
     "\n--\n\nRead the current terminator SEEDED flag."},
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
      if (qptr->messages == NULL) {
        // Unwind the queues we already initialised. boc_park_init has
        // been called for indices [0, i); any messages buffer they hold
        // must be freed.
        for (size_t j = 0; j < i; ++j) {
          PyMem_RawFree(BOC_QUEUES[j].messages);
          BOC_QUEUES[j].messages = NULL;
          boc_park_destroy(&BOC_QUEUES[j]);
        }
        atomic_fetch_sub(&BOC_COUNT, 1);
        PyErr_NoMemory();
        return -1;
      }
      memset(qptr->messages, 0, BOC_CAPACITY * sizeof(BOCMessage *));
      qptr->head = 0;
      qptr->tail = 0;
      qptr->state = BOC_QUEUE_UNASSIGNED;
      qptr->tag = 0;
      qptr->waiters = 0;
      boc_atomic_store_u64_explicit(&qptr->enqueue_cas_retries, 0,
                                    BOC_MO_RELAXED);
      boc_atomic_store_u64_explicit(&qptr->dequeue_cas_retries, 0,
                                    BOC_MO_RELAXED);
      boc_atomic_store_u64_explicit(&qptr->pushed_total, 0, BOC_MO_RELAXED);
      boc_atomic_store_u64_explicit(&qptr->popped_total, 0, BOC_MO_RELAXED);
      boc_park_init(qptr);
    }

    BOCRecycleQueue *queue_stub =
        (BOCRecycleQueue *)PyMem_RawMalloc(sizeof(BOCRecycleQueue));
    if (queue_stub == NULL) {
      // Unwind every queue.
      for (size_t i = 0; i < BOC_QUEUE_COUNT; ++i) {
        PyMem_RawFree(BOC_QUEUES[i].messages);
        BOC_QUEUES[i].messages = NULL;
        boc_park_destroy(&BOC_QUEUES[i]);
      }
      atomic_fetch_sub(&BOC_COUNT, 1);
      PyErr_NoMemory();
      return -1;
    }
    queue_stub->head = 0;
    queue_stub->tail = NULL;
    queue_stub->next = 0;
    atomic_store_intptr(&BOC_RECYCLE_QUEUE_HEAD, (intptr_t)queue_stub);
    BOC_RECYCLE_QUEUE_TAIL = queue_stub;

    // Initialize the noticeboard subsystem (mutex + sync primitives).
    // noticeboard_init / terminator_init currently return void; if
    // they ever start failing, this site will need to propagate the
    // error through `_core_module_exec`.
    noticeboard_init();

    // Initialize the terminator primitives.
    // The Pyrona seed (count=1, seeded=1) is set by terminator_reset()
    // when the runtime starts; here we only initialize the kernel objects.
    terminator_init();

    // Initialize the scheduler module with no workers. The
    // per-worker array stays unallocated and `_core.scheduler_stats()`
    // returns an empty list until `behaviors.start()` calls
    // `scheduler_runtime_start` with the real worker count.
    if (boc_sched_init(0) < 0) {
      // Unwind every globally-allocated subsystem before returning -1
      // so that the BOC_COUNT == 0 invariant ("first interpreter has
      // not yet completed module init") is restored.
      noticeboard_destroy();
      // terminator currently has no destroy entry point; its kernel
      // objects (mutex + cv) are reusable across init/destroy cycles.
      PyMem_RawFree((void *)BOC_RECYCLE_QUEUE_TAIL);
      BOC_RECYCLE_QUEUE_TAIL = NULL;
      atomic_store_intptr(&BOC_RECYCLE_QUEUE_HEAD, 0);
      for (size_t i = 0; i < BOC_QUEUE_COUNT; ++i) {
        PyMem_RawFree(BOC_QUEUES[i].messages);
        BOC_QUEUES[i].messages = NULL;
        boc_park_destroy(&BOC_QUEUES[i]);
      }
      atomic_fetch_sub(&BOC_COUNT, 1);
      return -1;
    }

#ifdef BOC_REF_TRACKING
#ifdef _WIN32
    timespec_get(&BOC_LAST_REF_TRACKING_REPORT, TIME_UTC);
#else
    clock_gettime(CLOCK_REALTIME, &BOC_LAST_REF_TRACKING_REPORT);
#endif
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
  if (state == NULL) {
    return 0;
  }
  Py_CLEAR(state->loads);
  Py_CLEAR(state->dumps);
  Py_CLEAR(state->pickle);
  Py_CLEAR(state->cown_capsule_type);
  Py_CLEAR(state->behavior_capsule_type);
  // The recycle_queue is allocated late in module_exec; it may be NULL if
  // module_exec returned -1 before reaching BOCRecycleQueue_new(). The
  // worker recycle queue's xidata_to_cowns dict is owned by this
  // interpreter and must be cleared here so the GC can collect any
  // reference cycles anchored through it.
  if (state->recycle_queue != NULL) {
    Py_CLEAR(state->recycle_queue->xidata_to_cowns);
  }
  // Clear the thread-local snapshot cache so the GC can collect any
  // reference cycles anchored through the cached dict / proxy.
  noticeboard_drop_local_cache();
  return 0;
}

void _core_module_free(void *module_ptr) {
  PyObject *module = (PyObject *)module_ptr;
  _core_module_state *state = (_core_module_state *)PyModule_GetState(module);
  if (state == NULL) {
    return;
  }

  PRINTDBG("begin boc_free(index=%" PRIdLEAST64 ")\n", state->index);
  PRINTDBG("Emptying _core recycle queue...\n");

  if (state->recycle_queue != NULL) {
    BOCRecycleQueue_empty(state->recycle_queue, true);
  }

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
      boc_park_destroy(qptr);

      if (atomic_load(&qptr->state) == BOC_QUEUE_ASSIGNED) {
        BOCTag *qtag = (BOCTag *)atomic_load_intptr(&qptr->tag);
        assert(qtag->queue == qptr);
        BOCTag_free(qtag);
      }
    }

    BOCRecycleQueue *queue = (BOCRecycleQueue *)BOC_RECYCLE_QUEUE_TAIL;
    while (atomic_load_intptr(&queue->next) != 0) {
      BOCRecycleQueue *next = (BOCRecycleQueue *)queue->next;
      BOCRecycleQueue_free(queue);
      queue = next;
    }

    BOCRecycleQueue_free(queue);
    BOC_RECYCLE_QUEUE_TAIL = NULL;
    atomic_store_intptr(&BOC_RECYCLE_QUEUE_HEAD, 0);

    // Tear down the noticeboard subsystem (snapshot cache, entries,
    // pins, mutex, sync primitives).
    noticeboard_destroy();

    // Tear down the scheduler instrumentation skeleton.
    boc_sched_shutdown();

    BOC_REF_TRACKING_REPORT();
  }

  PRINTDBG("end boc_free(index=%" PRIdLEAST64 ")\n", state->index);
}

static int _core_module_traverse(PyObject *module, visitproc visit, void *arg) {
  _core_module_state *state = (_core_module_state *)PyModule_GetState(module);
  if (state == NULL) {
    return 0;
  }
  Py_VISIT(state->loads);
  Py_VISIT(state->dumps);
  Py_VISIT(state->pickle);
  Py_VISIT(state->cown_capsule_type);
  Py_VISIT(state->behavior_capsule_type);
  // recycle_queue is allocated late in module_exec; if exec failed before
  // reaching BOCRecycleQueue_new() the field is still NULL.
  if (state->recycle_queue != NULL) {
    Py_VISIT(state->recycle_queue->xidata_to_cowns);
  }
  return 0;
}

#ifdef Py_mod_exec
static PyModuleDef_Slot _core_module_slots[] = {
    {Py_mod_exec, (void *)_core_module_exec},
#if PY_VERSION_HEX >= 0x030C0000
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
#endif
#if PY_VERSION_HEX >= 0x030D0000
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
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