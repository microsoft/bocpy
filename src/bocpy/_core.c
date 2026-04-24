#define PY_SSIZE_T_CLEAN

#include <Python.h>
#include <stdbool.h>
#include <stdint.h>
#include <time.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <process.h>
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

// ----- atomic_intptr_t siblings ---------------------------------------------
// The MSVC polyfill defines `atomic_intptr_t` and `atomic_int_least64_t` as
// distinct typedefs; the plain `atomic_load` / `atomic_store` / etc. above
// only accept `atomic_int_least64_t *`. Without these siblings, code that
// touches an `atomic_intptr_t` field (e.g. BOCRequest::next, BOCCown::last,
// BOCRecycleQueue::head, BOCQueue::tag, NB_NOTICEBOARD_TID) would silently
// pass a mistyped pointer to the int64 polyfill on Windows. On POSIX C11 the
// same names are aliased to the generic atomic_* macros (which already
// dispatch on type via _Generic), so user code below is platform-uniform.
//
// All Interlocked*Pointer intrinsics on x86/x64 are full barriers; the
// pointer-width matches `intptr_t` on both Win32 and Win64 (CPython itself
// requires a sane intptr_t == void* relationship).
static inline intptr_t atomic_load_intptr(atomic_intptr_t *ptr) { return *ptr; }

static inline void atomic_store_intptr(atomic_intptr_t *ptr, intptr_t value) {
  *ptr = value;
}

static inline intptr_t atomic_exchange_intptr(atomic_intptr_t *ptr,
                                              intptr_t value) {
  return (intptr_t)InterlockedExchangePointer((PVOID volatile *)ptr,
                                              (PVOID)value);
}

static inline bool atomic_compare_exchange_strong_intptr(atomic_intptr_t *ptr,
                                                         intptr_t *expected,
                                                         intptr_t desired) {
  intptr_t prev = (intptr_t)InterlockedCompareExchangePointer(
      (PVOID volatile *)ptr, (PVOID)desired, (PVOID)*expected);
  if (prev == *expected) {
    return true;
  }
  *expected = prev;
  return false;
}

// All Interlocked* intrinsics on x86/x64 are full barriers, so the
// memory_order argument is accepted but ignored.
// Note: atomic_load_explicit is a plain volatile read. On x86/x64 this
// provides acquire semantics due to TSO. Correctness of the parking
// protocol relies on the mutex-protected re-check, not on seq_cst ordering.
#define atomic_load_explicit(ptr, order) (*(ptr))
#define atomic_fetch_add_explicit(ptr, val, order)                             \
  InterlockedExchangeAdd64((ptr), (val))
#define atomic_fetch_sub_explicit(ptr, val, order)                             \
  InterlockedExchangeAdd64((ptr), -(val))
#define memory_order_seq_cst 0

#define thread_local __declspec(thread)

typedef SRWLOCK BOCMutex;
typedef CONDITION_VARIABLE BOCCond;

static inline void boc_mtx_init(BOCMutex *m) { InitializeSRWLock(m); }

static inline void mtx_destroy(BOCMutex *m) { (void)m; }

static inline void mtx_lock(BOCMutex *m) { AcquireSRWLockExclusive(m); }

static inline void mtx_unlock(BOCMutex *m) { ReleaseSRWLockExclusive(m); }

static inline void cnd_init(BOCCond *c) { InitializeConditionVariable(c); }

static inline void cnd_destroy(BOCCond *c) { (void)c; }

static inline void cnd_signal(BOCCond *c) { WakeConditionVariable(c); }

static inline void cnd_broadcast(BOCCond *c) { WakeAllConditionVariable(c); }

static inline void cnd_wait(BOCCond *c, BOCMutex *m) {
  SleepConditionVariableSRW(c, m, INFINITE, 0);
}

/// @brief Wait on a condition variable for at most @p seconds.
/// @param c The condition variable
/// @param m The mutex (must be held by caller)
/// @return true if signalled (or spurious wake), false if the timeout expired
static inline bool cnd_timedwait_s(BOCCond *c, BOCMutex *m, double seconds) {
  if (seconds < 0)
    seconds = 0;
  DWORD ms = (DWORD)(seconds * 1000.0);
  BOOL ok = SleepConditionVariableSRW(c, m, ms, 0);
  if (!ok && GetLastError() == ERROR_TIMEOUT) {
    return false;
  }
  return true;
}

void thrd_sleep(const struct timespec *duration, struct timespec *remaining) {
  const DWORD MS_PER_NS = 1000000;
  DWORD ms = (DWORD)duration->tv_sec * 1000;
  ms += (DWORD)duration->tv_nsec / MS_PER_NS;
  Sleep(ms);
}

#elif defined __APPLE__
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#define thrd_sleep nanosleep
#define thread_local _Thread_local

typedef pthread_mutex_t BOCMutex;
typedef pthread_cond_t BOCCond;

static inline void boc_mtx_init(BOCMutex *m) { pthread_mutex_init(m, NULL); }

static inline void mtx_destroy(BOCMutex *m) { pthread_mutex_destroy(m); }

static inline void mtx_lock(BOCMutex *m) { pthread_mutex_lock(m); }

static inline void mtx_unlock(BOCMutex *m) { pthread_mutex_unlock(m); }

static inline void cnd_init(BOCCond *c) { pthread_cond_init(c, NULL); }

static inline void cnd_destroy(BOCCond *c) { pthread_cond_destroy(c); }

static inline void cnd_signal(BOCCond *c) { pthread_cond_signal(c); }

static inline void cnd_broadcast(BOCCond *c) { pthread_cond_broadcast(c); }

static inline void cnd_wait(BOCCond *c, BOCMutex *m) {
  pthread_cond_wait(c, m);
}

/// @brief Wait on a condition variable for at most @p seconds.
/// @param c The condition variable
/// @param m The mutex (must be held by caller)
/// @return true if signalled (or spurious wake), false if the timeout expired
static inline bool cnd_timedwait_s(BOCCond *c, BOCMutex *m, double seconds) {
  if (seconds < 0)
    seconds = 0;
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  double total = (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9 + seconds;
  ts.tv_sec = (time_t)total;
  ts.tv_nsec = (long)((total - (double)ts.tv_sec) * 1e9);
  if (ts.tv_nsec >= 1000000000L) {
    ts.tv_sec += 1;
    ts.tv_nsec -= 1000000000L;
  }
  int rc = pthread_cond_timedwait(c, m, &ts);
  return rc != ETIMEDOUT;
}

#else // Linux
#include <errno.h>
#include <stdatomic.h>
#include <threads.h>

typedef mtx_t BOCMutex;
typedef cnd_t BOCCond;

static inline void boc_mtx_init(BOCMutex *m) { mtx_init(m, mtx_plain); }

/// @brief Wait on a condition variable for at most @p seconds.
/// @param c The condition variable
/// @param m The mutex (must be held by caller)
/// @return true if signalled (or spurious wake), false if the timeout expired
static inline bool cnd_timedwait_s(BOCCond *c, BOCMutex *m, double seconds) {
  if (seconds < 0)
    seconds = 0;
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  double total = (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9 + seconds;
  ts.tv_sec = (time_t)total;
  ts.tv_nsec = (long)((total - (double)ts.tv_sec) * 1e9);
  if (ts.tv_nsec >= 1000000000L) {
    ts.tv_sec += 1;
    ts.tv_nsec -= 1000000000L;
  }
  int rc = cnd_timedwait(c, m, &ts);
  return rc != thrd_timedout;
}

#endif

#ifndef _WIN32
// On POSIX the C11 atomic_* macros dispatch on type via _Generic, so the
// `atomic_load(&intptr_var)` form Just Works. The `_intptr` siblings are
// aliased to the generic forms purely so the source reads the same on
// every platform; on Windows they expand to dedicated InterlockedXxxPointer
// shims (see polyfill block above).
#define atomic_load_intptr(ptr) atomic_load(ptr)
#define atomic_store_intptr(ptr, val) atomic_store((ptr), (val))
#define atomic_exchange_intptr(ptr, val) atomic_exchange((ptr), (val))
#define atomic_compare_exchange_strong_intptr(ptr, expected, desired)          \
  atomic_compare_exchange_strong((ptr), (expected), (desired))
#endif

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

/// @brief Returns the current time as double-precision seconds.
/// @return the current time
static double boc_now_s(void);

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

#define BOC_SPIN_COUNT 64
#define BOC_BACKOFF_CAP_NS 1000000 // 1 ms

// Portable yield: relinquish current CPU timeslice.
#ifdef _WIN32
#define boc_yield() SwitchToThread()
#else
#include <sched.h>
#include <unistd.h>
#define boc_yield() sched_yield()
#endif

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

  /// @brief Number of threads parked on this queue's condvar
  atomic_int_least64_t waiters;
  /// @brief Mutex protecting condvar signal/wait
  BOCMutex park_mutex;
  /// @brief Condition variable for parking receivers
  BOCCond park_cond;
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

// ---------------------------------------------------------------------------
// Noticeboard
// ---------------------------------------------------------------------------

#define NB_MAX_ENTRIES 64
#define NB_KEY_SIZE 64

// Forward declarations needed by NoticeboardEntry and the noticeboard
// helpers below. The full definitions of BOCCown and its refcount helpers
// appear further down the file (the noticeboard predates the cown
// machinery in source order, but the new pin-tracking support added for
// the snapshot cache needs the cown refcount macros).
typedef struct boc_cown BOCCown;
static int_least64_t cown_incref(BOCCown *cown);
static int_least64_t cown_decref(BOCCown *cown);
#define COWN_INCREF(c) cown_incref((c))
#define COWN_DECREF(c) cown_decref(c)

// CownCapsule forward declaration so the noticeboard pin helper can fish
// the underlying BOCCown out of a Python CownCapsule. The struct body is
// defined alongside the type's PyTypeObject further down.
typedef struct cown_capsule_object {
  PyObject_HEAD BOCCown *cown;
} CownCapsuleObject;

/// @brief A single noticeboard entry
typedef struct nb_entry {
  /// @brief The key for this entry (null-terminated UTF-8)
  char key[NB_KEY_SIZE];
  /// @brief The serialized cross-interpreter data
  XIDATA_T *value;
  /// @brief Whether the value was pickled during serialization
  bool pickled;
  /// @brief BOCCowns referenced by @ref value, pinned by this entry
  /// @details Allocated with @c PyMem_RawMalloc; each pointer holds one
  /// strong reference (COWN_INCREF). When the entry is overwritten,
  /// deleted, or cleared, every pointer is COWN_DECREFed and the array
  /// is freed. This is the noticeboard's mechanism for keeping the
  /// underlying BOCCowns alive across the 1-pickle / N-unpickle cycle:
  /// pickling no longer adds a pin (see @ref CownCapsule_reduce).
  BOCCown **pinned_cowns;
  /// @brief Number of entries in @ref pinned_cowns
  int pinned_count;
} NoticeboardEntry;

/// @brief Global noticeboard for cross-behavior key-value storage
typedef struct noticeboard {
  /// @brief The stored entries
  NoticeboardEntry entries[NB_MAX_ENTRIES];
  /// @brief The number of entries currently stored
  int count;
  /// @brief Mutex protecting the noticeboard
  BOCMutex mutex;
} Noticeboard;

static Noticeboard NB;

/// @brief Monotonic version counter for the noticeboard
/// @details Incremented under @ref Noticeboard::mutex on every successful
/// write, delete, or clear. Threads use this to lazily invalidate their
/// thread-local snapshot cache without taking the noticeboard mutex on
/// every read. Exposed to Python via @ref _core_noticeboard_version for
/// users who want to detect noticeboard changes without taking a full
/// snapshot.
static atomic_int_least64_t NB_VERSION = 0;

/// @brief Thread-local snapshot cache for the current behavior
static thread_local PyObject *NB_SNAPSHOT_CACHE = NULL;

/// @brief Version of the noticeboard at the time the cached snapshot was built
/// @details Captured under @ref Noticeboard::mutex during the rebuild. A
/// reader that finds @ref NB_VERSION equal to this value can reuse the
/// cached dict without rebuilding.
static thread_local int_least64_t NB_SNAPSHOT_VERSION = -1;

/// @brief Whether the cached snapshot has been version-checked this behavior
/// @details Cleared by @ref _core_noticeboard_cache_clear at every behavior
/// boundary (see @c worker.py). Set to @c true on the first snapshot call
/// of a behavior. Subsequent calls within the same behavior return the
/// cached dict without consulting @ref NB_VERSION at all, preserving the
/// no-polling invariant: the noticeboard cannot be used as a synchronous
/// communication channel between behaviors.
static thread_local bool NB_VERSION_CHECKED = false;

/// @brief Read-only proxy wrapping the cached snapshot dict
/// @details A @c types.MappingProxyType created over @ref NB_SNAPSHOT_CACHE
/// once per rebuild and returned to callers in place of the dict. Prevents
/// user code from mutating the cached snapshot, which would otherwise
/// corrupt every subsequent reader on the same thread until the next
/// @ref NB_VERSION bump.
static thread_local PyObject *NB_SNAPSHOT_PROXY = NULL;

/// @brief Thread identity of the noticeboard mutator thread, or 0 if unset
/// @details Set by @ref _core_set_noticeboard_thread at runtime startup
/// and checked by @ref _core_noticeboard_write_direct and
/// @ref _core_noticeboard_delete to enforce the invariant that only the
/// noticeboard thread mutates the noticeboard. This eliminates the TOCTOU
/// window in the Python-level read-modify-write performed by
/// @c noticeboard_update.
static atomic_intptr_t NB_NOTICEBOARD_TID = 0;

// ---------------------------------------------------------------------------
// notice_sync() — opt-in barrier for the noticeboard thread.
//
// The noticeboard thread runs independently of the behavior dispatch path, so
// notice_write/_update/_delete are fire-and-forget. Callers that need
// read-your-writes ordering use notice_sync():
//   1. notice_sync_request() atomically allocates a monotonic sequence
//      number and returns it.
//   2. The caller posts ("sync", N) on the boc_noticeboard tag.
//   3. The noticeboard-thread arm calls notice_sync_complete(N), which
//      stores N into NB_SYNC_PROCESSED (monotonic, max-of) and broadcasts
//      NB_SYNC_COND.
//   4. The caller blocks in notice_sync_wait(my_seq, timeout) on
//      NB_SYNC_COND until NB_SYNC_PROCESSED >= my_seq, or returns false
//      on timeout.
//
// All synchronization lives in C primitives so the barrier works across
// sub-interpreters (Python locks do not span interpreters).
// ---------------------------------------------------------------------------

/// @brief Monotonic counter incremented by every notice_sync caller.
/// @details Sized for ~292 years of continuous 1 GHz fetch_add traffic
/// before wrap; treated as effectively non-wrapping. If the wrap
/// precondition ever becomes plausible (e.g. a much faster mutator),
/// switch to @c atomic_uint_least64_t and update the wrap arithmetic
/// in @ref _core_notice_sync_wait.
static atomic_int_least64_t NB_SYNC_REQUESTED = 0;

/// @brief Highest sequence number processed by the noticeboard thread.
static atomic_int_least64_t NB_SYNC_PROCESSED = 0;

/// @brief Mutex protecting NB_SYNC_COND.
static BOCMutex NB_SYNC_MUTEX;

/// @brief Condition variable signalled when NB_SYNC_PROCESSED advances.
static BOCCond NB_SYNC_COND;

// ---------------------------------------------------------------------------
// Terminator — C-level run-down counter.
//
// Process-global rundown counter that gates @c terminator_wait. Used by the
// Python @c wait()/@c stop() lifecycle to block until every in-flight
// behavior has retired. The counter is incremented from caller threads in
// @c whencall (before the schedule call) and decremented from worker
// threads after @c behavior_release_all completes. A one-shot "Pyrona
// seed" of 1 keeps the count positive between the runtime starting and
// @c stop() taking it down via @c terminator_seed_dec.
//
// Lifecycle:
//   - @c terminator_reset arms a fresh runtime: count = 1 (the seed),
//     seeded = 1, closed = 0. Returns the prior (count, seeded) so
//     @c Behaviors.start can detect drift carried over from a previous
//     run that died without reconciliation.
//   - @c terminator_inc returns -1 once @c terminator_close has been
//     called, so the @c whencall fast path can refuse new work without
//     racing teardown.
//   - @c terminator_seed_dec is the idempotent one-shot that drops the
//     seed; subsequent calls are no-ops.
//   - @c terminator_wait blocks on the condvar until count reaches 0.
//   - @c terminator_close raises the closed bit so any straggler
//     @c terminator_inc returns -1.
//
// State is process-global (file-scope statics, NOT per-interpreter) so
// every sub-interpreter sees the same counter, mutex, and condvar.
// ---------------------------------------------------------------------------

/// @brief Active behavior count + the Pyrona seed.
static atomic_int_least64_t TERMINATOR_COUNT = 0;

/// @brief Set to 1 by terminator_close() to refuse further increments.
static atomic_int_least64_t TERMINATOR_CLOSED = 0;

/// @brief One-shot guard for the Pyrona seed: 1 = seed still present.
static atomic_int_least64_t TERMINATOR_SEEDED = 0;

/// @brief Mutex protecting TERMINATOR_COND.
static BOCMutex TERMINATOR_MUTEX;

/// @brief Condition variable signalled when TERMINATOR_COUNT reaches 0.
static BOCCond TERMINATOR_COND;

// ---------------------------------------------------------------------------
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

/// @brief Reject a noticeboard mutation called from outside the noticeboard
/// thread.
/// @details Sets a Python @c RuntimeError if a noticeboard thread has been
/// registered (via @ref _core_set_noticeboard_thread) and the calling thread
/// is not it. Prior to runtime startup the check is permissive so that
/// @c Behaviors.stop and unit tests can drive the noticeboard from the
/// main thread before the noticeboard thread is up. The single-writer
/// invariant is what makes the Python-level read-modify-write in
/// @c noticeboard_update TOCTOU-free.
/// @param op_name Name of the operation, used in the error message
/// @return 0 on success, -1 on error (with exception set)
static int nb_check_noticeboard_thread(const char *op_name) {
  uintptr_t owner = (uintptr_t)atomic_load_intptr(&NB_NOTICEBOARD_TID);
  if (owner == 0) {
    return 0;
  }
  uintptr_t current = (uintptr_t)PyThread_get_thread_ident();
  if (current != owner) {
    PyErr_Format(PyExc_RuntimeError,
                 "%s must be called from the noticeboard thread", op_name);
    return -1;
  }
  return 0;
}

/// @brief Take strong references to every CownCapsule in @p cowns
/// @details Allocates a fresh @c BOCCown** array (or returns NULL if
/// @p cowns is empty), iterates the sequence calling @c COWN_INCREF on
/// each entry's underlying BOCCown, and writes the resulting array and
/// count to @p out_array / @p out_count. On error, no INCREFs leak: any
/// already-taken pins are dropped before return.
/// @param cowns A Python sequence of CownCapsule objects (may be NULL or
///   None for "no pins")
/// @param out_array Out param for the allocated array
/// @param out_count Out param for the number of entries
/// @return 0 on success, -1 on error (with exception set)
///
/// @details The caller is expected to pass a sequence of integer pointers
/// to BOCCown structs that have already been COWN_INCREFed by the writer
/// thread (typically via @ref _core_cown_pin_pointers). This function
/// **transfers** those refs into the noticeboard entry: it does not take
/// any additional ref. On error every transferred ref is released so the
/// caller can treat -1 as "ownership not taken, original refs already
/// released".
static int nb_pin_cowns(PyObject *cowns, BOCCown ***out_array, int *out_count) {
  *out_array = NULL;
  *out_count = 0;

  if (cowns == NULL || cowns == Py_None) {
    return 0;
  }

  PyObject *seq =
      PySequence_Fast(cowns, "noticeboard pin list must be a sequence");
  if (seq == NULL) {
    return -1;
  }

  Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
  if (n == 0) {
    Py_DECREF(seq);
    return 0;
  }

  BOCCown **pins = (BOCCown **)PyMem_RawMalloc(sizeof(BOCCown *) * n);
  if (pins == NULL) {
    Py_DECREF(seq);
    PyErr_NoMemory();
    return -1;
  }

  int taken = 0;
  for (Py_ssize_t i = 0; i < n; i++) {
    PyObject *item = PySequence_Fast_GET_ITEM(seq, i);
    BOCCown *cown = (BOCCown *)PyLong_AsVoidPtr(item);
    if (cown == NULL) {
      // PyLong_AsVoidPtr returns NULL both on error and for integer 0.
      // Reject both paths explicitly: a NULL pin would be dereferenced
      // downstream (COWN_DECREF on NULL is UB), and an integer 0 is
      // indistinguishable from a crafted attacker pin pointing at the
      // zero page.
      if (!PyErr_Occurred()) {
        PyErr_SetString(PyExc_ValueError,
                        "noticeboard pin list must not contain NULL / "
                        "integer 0 entries");
      } else {
        PyErr_SetString(PyExc_TypeError,
                        "noticeboard pin list must contain only integer "
                        "BOCCown pointers (use _core.cown_pin_pointers())");
      }
      goto fail;
    }
    pins[taken++] = cown;
  }

  Py_DECREF(seq);
  *out_array = pins;
  *out_count = taken;
  return 0;

fail:
  // Release every transferred ref the writer pre-INCREFed for us. The
  // ones we already stashed into `pins` plus the rest of the sequence
  // we never reached.
  for (int i = 0; i < taken; i++) {
    COWN_DECREF(pins[i]);
  }
  for (Py_ssize_t i = (Py_ssize_t)taken + 1; i < n; i++) {
    PyObject *item = PySequence_Fast_GET_ITEM(seq, i);
    BOCCown *c = (BOCCown *)PyLong_AsVoidPtr(item);
    if (c != NULL) {
      COWN_DECREF(c);
    } else {
      PyErr_Clear();
    }
  }
  PyMem_RawFree(pins);
  Py_DECREF(seq);
  return -1;
}

/// @brief Drop the calling thread's snapshot cache and proxy
/// @details Both objects are decref-cleared and the per-behavior version
/// state is reset. Safe to call when nothing is cached.
static void nb_drop_local_cache(void) {
  Py_CLEAR(NB_SNAPSHOT_PROXY);
  Py_CLEAR(NB_SNAPSHOT_CACHE);
  NB_SNAPSHOT_VERSION = -1;
  NB_VERSION_CHECKED = false;
}

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

  if (nb_check_noticeboard_thread("noticeboard_write_direct") < 0) {
    return NULL;
  }

  const char *key;
  Py_ssize_t key_len;
  PyObject *value;
  PyObject *cowns = Py_None;

  if (!PyArg_ParseTuple(args, "s#O|O", &key, &key_len, &value, &cowns)) {
    return NULL;
  }

  if (key_len >= NB_KEY_SIZE) {
    PyErr_SetString(PyExc_ValueError,
                    "noticeboard key too long (max 63 UTF-8 bytes)");
    return NULL;
  }

  if (memchr(key, '\0', key_len) != NULL) {
    PyErr_SetString(PyExc_ValueError,
                    "noticeboard key must not contain NUL characters");
    return NULL;
  }

  // Pin the cowns BEFORE serializing so an error here does not leave us
  // with a stored entry whose cowns can be freed under us.
  BOCCown **new_pins = NULL;
  int new_pin_count = 0;
  if (nb_pin_cowns(cowns, &new_pins, &new_pin_count) < 0) {
    return NULL;
  }

  // Serialize the value to XIData in the main interpreter
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

  mtx_lock(&NB.mutex);

  // find existing entry or allocate new one
  NoticeboardEntry *target = NULL;
  for (int i = 0; i < NB.count; i++) {
    if (strncmp(NB.entries[i].key, key, NB_KEY_SIZE) == 0) {
      target = &NB.entries[i];
      break;
    }
  }

  if (target == NULL) {
    if (NB.count >= NB_MAX_ENTRIES) {
      mtx_unlock(&NB.mutex);
      XIDATA_FREE(xidata);
      for (int i = 0; i < new_pin_count; i++) {
        COWN_DECREF(new_pins[i]);
      }
      PyMem_RawFree(new_pins);
      PyErr_SetString(PyExc_RuntimeError, "Noticeboard is full (max 64)");
      return NULL;
    }
    target = &NB.entries[NB.count++];
    strncpy(target->key, key, NB_KEY_SIZE - 1);
    target->key[NB_KEY_SIZE - 1] = '\0';
    target->value = NULL;
    target->pinned_cowns = NULL;
    target->pinned_count = 0;
  }

  // Stash old value and old pins to free after releasing the mutex —
  // XIDATA_FREE / COWN_DECREF may invoke Python __del__ which could
  // re-enter the noticeboard.
  XIDATA_T *old_value = target->value;
  BOCCown **old_pins = target->pinned_cowns;
  int old_pin_count = target->pinned_count;

  target->value = xidata;
  target->pickled = is_pickled;
  target->pinned_cowns = new_pins;
  target->pinned_count = new_pin_count;

  // Bump the version under mutex so readers' acquire loads can lazily
  // invalidate their thread-local snapshot caches without us touching
  // their cache directly.
  atomic_fetch_add(&NB_VERSION, 1);

  mtx_unlock(&NB.mutex);

  if (old_value != NULL) {
    XIDATA_FREE(old_value);
  }
  if (old_pins != NULL) {
    for (int i = 0; i < old_pin_count; i++) {
      COWN_DECREF(old_pins[i]);
    }
    PyMem_RawFree(old_pins);
  }

  // Note: this thread's NB_SNAPSHOT_CACHE is intentionally NOT cleared.
  // Within a behavior, a writer must not observe its own write — that is
  // the no-polling invariant. The cache will be lazily revalidated at
  // the next behavior boundary (see _core_noticeboard_cache_clear).

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

  if (NB_SNAPSHOT_PROXY != NULL) {
    if (NB_VERSION_CHECKED) {
      // Within-behavior repeat call: same proxy, no atomic load.
      Py_INCREF(NB_SNAPSHOT_PROXY);
      return NB_SNAPSHOT_PROXY;
    }
    // First snapshot call this behavior: do exactly one version check.
    int_least64_t current = atomic_load(&NB_VERSION);
    if (current == NB_SNAPSHOT_VERSION) {
      NB_VERSION_CHECKED = true;
      Py_INCREF(NB_SNAPSHOT_PROXY);
      return NB_SNAPSHOT_PROXY;
    }
    nb_drop_local_cache();
  }

  PyObject *dict = PyDict_New();
  if (dict == NULL) {
    return NULL;
  }

  // Deferred entries: pickled values whose bytes were extracted under mutex
  // but need unpickling outside the lock.
  PyObject *deferred_keys[NB_MAX_ENTRIES];
  PyObject *deferred_bytes[NB_MAX_ENTRIES];
  int deferred_count = 0;

  // Keepalive pins: while we hold the mutex we take an extra COWN_INCREF
  // on every pin reachable from a deferred (pickled) entry. The bytes we
  // are about to unpickle outside the mutex contain raw BOCCown pointers
  // whose validity depends on the entry's pin list. Without this extra
  // ref, a concurrent writer could overwrite the entry the instant we
  // drop the mutex, release the old pins, and free the BOCCowns before
  // we touch them — UAF in _cown_capsule_from_pointer. Released after
  // the deferred unpickling completes. Each deferred entry contributes
  // a heap-allocated pin pointer array sized to its pin count.
  BOCCown **keepalive_pins[NB_MAX_ENTRIES];
  int keepalive_counts[NB_MAX_ENTRIES];
  for (int i = 0; i < NB_MAX_ENTRIES; i++) {
    keepalive_pins[i] = NULL;
    keepalive_counts[i] = 0;
  }

  mtx_lock(&NB.mutex);

  // Capture the noticeboard version while still holding the mutex so
  // that no concurrent writer can bump it between snapshot completion
  // and version capture.
  int_least64_t built_version = atomic_load(&NB_VERSION);

  for (int i = 0; i < NB.count; i++) {
    NoticeboardEntry *entry = &NB.entries[i];
    if (entry->value == NULL) {
      continue;
    }

    // XIDATA_NEWOBJECT is lightweight (no Python code execution)
    PyObject *raw = XIDATA_NEWOBJECT(entry->value);
    if (raw == NULL) {
      mtx_unlock(&NB.mutex);
      goto fail_deferred;
    }

    PyObject *key = PyUnicode_FromString(entry->key);
    if (key == NULL) {
      Py_DECREF(raw);
      mtx_unlock(&NB.mutex);
      goto fail_deferred;
    }

    if (!entry->pickled) {
      // Non-pickled: add directly to dict
      if (PyDict_SetItem(dict, key, raw) < 0) {
        Py_DECREF(key);
        Py_DECREF(raw);
        mtx_unlock(&NB.mutex);
        goto fail_deferred;
      }
      Py_DECREF(key);
      Py_DECREF(raw);
    } else {
      // Pickled: defer unpickling to outside the mutex. Take a fresh
      // COWN_INCREF on every pin so the BOCCowns referenced by the bytes
      // survive past mtx_unlock — see keepalive_pins comment above.
      if (entry->pinned_count > 0) {
        BOCCown **pins = (BOCCown **)PyMem_RawMalloc(sizeof(BOCCown *) *
                                                     entry->pinned_count);
        if (pins == NULL) {
          Py_DECREF(key);
          Py_DECREF(raw);
          mtx_unlock(&NB.mutex);
          PyErr_NoMemory();
          goto fail_deferred;
        }
        for (int j = 0; j < entry->pinned_count; j++) {
          pins[j] = entry->pinned_cowns[j];
          COWN_INCREF(pins[j]);
        }
        keepalive_pins[deferred_count] = pins;
        keepalive_counts[deferred_count] = entry->pinned_count;
      }
      deferred_keys[deferred_count] = key;
      deferred_bytes[deferred_count] = raw;
      deferred_count++;
    }
  }

  mtx_unlock(&NB.mutex);

  // Unpickle deferred entries outside the mutex
  for (int i = 0; i < deferred_count; i++) {
    PyObject *value = _PyPickle_Loads(deferred_bytes[i]);
    Py_DECREF(deferred_bytes[i]);
    deferred_bytes[i] = NULL;

    if (value == NULL) {
      Py_DECREF(deferred_keys[i]);
      deferred_keys[i] = NULL;
      // Clean up remaining deferred entries
      for (int j = i + 1; j < deferred_count; j++) {
        Py_DECREF(deferred_keys[j]);
        Py_DECREF(deferred_bytes[j]);
      }
      // Release every keepalive pin (including the one for this entry).
      for (int j = 0; j < deferred_count; j++) {
        if (keepalive_pins[j] != NULL) {
          for (int k = 0; k < keepalive_counts[j]; k++) {
            COWN_DECREF(keepalive_pins[j][k]);
          }
          PyMem_RawFree(keepalive_pins[j]);
          keepalive_pins[j] = NULL;
        }
      }
      Py_DECREF(dict);
      return NULL;
    }

    if (PyDict_SetItem(dict, deferred_keys[i], value) < 0) {
      Py_DECREF(deferred_keys[i]);
      Py_DECREF(value);
      for (int j = i + 1; j < deferred_count; j++) {
        Py_DECREF(deferred_keys[j]);
        Py_DECREF(deferred_bytes[j]);
      }
      for (int j = 0; j < deferred_count; j++) {
        if (keepalive_pins[j] != NULL) {
          for (int k = 0; k < keepalive_counts[j]; k++) {
            COWN_DECREF(keepalive_pins[j][k]);
          }
          PyMem_RawFree(keepalive_pins[j]);
          keepalive_pins[j] = NULL;
        }
      }
      Py_DECREF(dict);
      return NULL;
    }

    Py_DECREF(deferred_keys[i]);
    Py_DECREF(value);

    // Successful unpickle: the snapshot dict (and its CownCapsules)
    // now hold their own refs on every BOCCown referenced by the bytes.
    // Drop our keepalive pin for this entry.
    if (keepalive_pins[i] != NULL) {
      for (int k = 0; k < keepalive_counts[i]; k++) {
        COWN_DECREF(keepalive_pins[i][k]);
      }
      PyMem_RawFree(keepalive_pins[i]);
      keepalive_pins[i] = NULL;
    }
  }

  PyObject *proxy = PyDictProxy_New(dict);
  if (proxy == NULL) {
    Py_DECREF(dict);
    return NULL;
  }

  // The proxy holds a strong reference to dict; we keep our own as well so
  // that the dict is reachable for direct mutation in the rebuild path
  // and the proxy survives at least as long as the dict.
  NB_SNAPSHOT_CACHE = dict;
  NB_SNAPSHOT_PROXY = proxy;
  NB_SNAPSHOT_VERSION = built_version;
  NB_VERSION_CHECKED = true;
  Py_INCREF(proxy);
  return proxy;

fail_deferred:
  for (int i = 0; i < deferred_count; i++) {
    Py_DECREF(deferred_keys[i]);
    Py_DECREF(deferred_bytes[i]);
    if (keepalive_pins[i] != NULL) {
      for (int k = 0; k < keepalive_counts[i]; k++) {
        COWN_DECREF(keepalive_pins[i][k]);
      }
      PyMem_RawFree(keepalive_pins[i]);
      keepalive_pins[i] = NULL;
    }
  }
  Py_DECREF(dict);
  return NULL;
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

  // Collect entries to free after releasing the mutex — XIDATA_FREE and
  // COWN_DECREF may invoke Python __del__ which could re-enter the
  // noticeboard.
  XIDATA_T *to_free[NB_MAX_ENTRIES];
  BOCCown **to_unpin[NB_MAX_ENTRIES];
  int to_unpin_count[NB_MAX_ENTRIES];
  int to_free_count = 0;
  int to_unpin_entries = 0;

  mtx_lock(&NB.mutex);

  for (int i = 0; i < NB.count; i++) {
    if (NB.entries[i].value != NULL) {
      to_free[to_free_count++] = NB.entries[i].value;
      NB.entries[i].value = NULL;
    }
    if (NB.entries[i].pinned_cowns != NULL) {
      to_unpin[to_unpin_entries] = NB.entries[i].pinned_cowns;
      to_unpin_count[to_unpin_entries] = NB.entries[i].pinned_count;
      to_unpin_entries++;
      NB.entries[i].pinned_cowns = NULL;
      NB.entries[i].pinned_count = 0;
    }
  }
  NB.count = 0;
  memset(NB.entries, 0, sizeof(NB.entries));

  // Bump the version under mutex; see noticeboard_write_direct for
  // rationale.
  atomic_fetch_add(&NB_VERSION, 1);

  mtx_unlock(&NB.mutex);

  for (int i = 0; i < to_free_count; i++) {
    XIDATA_FREE(to_free[i]);
  }
  for (int i = 0; i < to_unpin_entries; i++) {
    for (int j = 0; j < to_unpin_count[i]; j++) {
      COWN_DECREF(to_unpin[i][j]);
    }
    PyMem_RawFree(to_unpin[i]);
  }

  // Drop this thread's cache so a subsequent runtime cycle does not
  // reuse a stale proxy. Other threads will revalidate via NB_VERSION.
  nb_drop_local_cache();

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

  if (nb_check_noticeboard_thread("noticeboard_delete") < 0) {
    return NULL;
  }

  const char *key;
  Py_ssize_t key_len;

  if (!PyArg_ParseTuple(args, "s#", &key, &key_len)) {
    return NULL;
  }

  if (key_len >= NB_KEY_SIZE) {
    PyErr_SetString(PyExc_ValueError,
                    "noticeboard key too long (max 63 UTF-8 bytes)");
    return NULL;
  }

  if (memchr(key, '\0', key_len) != NULL) {
    PyErr_SetString(PyExc_ValueError,
                    "noticeboard key must not contain NUL characters");
    return NULL;
  }

  mtx_lock(&NB.mutex);

  int found = -1;
  for (int i = 0; i < NB.count; i++) {
    if (strncmp(NB.entries[i].key, key, NB_KEY_SIZE) == 0) {
      found = i;
      break;
    }
  }

  // Stash the entry's XIData and pins to free after releasing the mutex.
  XIDATA_T *deleted_value = NULL;
  BOCCown **deleted_pins = NULL;
  int deleted_pin_count = 0;

  if (found >= 0) {
    deleted_value = NB.entries[found].value;
    deleted_pins = NB.entries[found].pinned_cowns;
    deleted_pin_count = NB.entries[found].pinned_count;

    // shift remaining entries down
    for (int i = found; i < NB.count - 1; i++) {
      NB.entries[i] = NB.entries[i + 1];
    }

    // clear the last slot and decrement
    memset(&NB.entries[NB.count - 1], 0, sizeof(NoticeboardEntry));
    NB.count--;

    // Bump the version under mutex; see noticeboard_write_direct.
    atomic_fetch_add(&NB_VERSION, 1);
  }

  mtx_unlock(&NB.mutex);

  if (deleted_value != NULL) {
    XIDATA_FREE(deleted_value);
  }
  if (deleted_pins != NULL) {
    for (int i = 0; i < deleted_pin_count; i++) {
      COWN_DECREF(deleted_pins[i]);
    }
    PyMem_RawFree(deleted_pins);
  }

  // Note: this thread's NB_SNAPSHOT_CACHE is intentionally NOT cleared;
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

  NB_VERSION_CHECKED = false;

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
  return PyLong_FromLongLong((long long)atomic_load(&NB_VERSION));
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
  uintptr_t tid = (uintptr_t)PyThread_get_thread_ident();
  // One-shot per runtime: refuse if the slot is already owned.
  // clear_noticeboard_thread() resets NB_NOTICEBOARD_TID to 0 at stop(),
  // so a fresh start() cycle is fine. This closes the hijack-the-
  // mutator-slot hole identified by the security lens.
  intptr_t expected = 0;
  if (!atomic_compare_exchange_strong_intptr(&NB_NOTICEBOARD_TID, &expected,
                                             (intptr_t)tid)) {
    PyErr_SetString(PyExc_RuntimeError,
                    "set_noticeboard_thread: noticeboard mutator thread "
                    "is already registered");
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
  (void)atomic_exchange_intptr(&NB_NOTICEBOARD_TID, (intptr_t)0);
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
  int_least64_t seq = atomic_fetch_add(&NB_SYNC_REQUESTED, 1) + 1;
  return PyLong_FromLongLong((long long)seq);
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

  Py_BEGIN_ALLOW_THREADS mtx_lock(&NB_SYNC_MUTEX);
  // Defense in depth: with a single noticeboard thread draining the
  // FIFO boc_noticeboard tag, `seq` arrives strictly monotonically and
  // a plain `atomic_store(seq)` would be correct. We keep the max-of
  // pattern so that if a future change introduces a second mutator
  // thread or any out-of-order delivery, NB_SYNC_PROCESSED can never
  // regress and unblock waiters early. Both load and store happen under
  // NB_SYNC_MUTEX (the only writer is here), so this is not a TOCTOU.
  int_least64_t cur = atomic_load(&NB_SYNC_PROCESSED);
  if ((int_least64_t)seq > cur) {
    atomic_store(&NB_SYNC_PROCESSED, (int_least64_t)seq);
  }
  cnd_broadcast(&NB_SYNC_COND);
  mtx_unlock(&NB_SYNC_MUTEX);
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

  bool do_timeout = false;
  double end_time = 0.0;
  if (timeout_obj != Py_None) {
    double timeout = PyFloat_AsDouble(timeout_obj);
    if (timeout == -1.0 && PyErr_Occurred()) {
      return NULL;
    }
    if (timeout >= 0.0) {
      do_timeout = true;
      end_time = boc_now_s() + timeout;
    }
  }

  bool ok = true;
  Py_BEGIN_ALLOW_THREADS mtx_lock(&NB_SYNC_MUTEX);
  while (atomic_load(&NB_SYNC_PROCESSED) < (int_least64_t)my_seq) {
    if (do_timeout) {
      double now = boc_now_s();
      if (now >= end_time) {
        ok = false;
        break;
      }
      cnd_timedwait_s(&NB_SYNC_COND, &NB_SYNC_MUTEX, end_time - now);
    } else {
      cnd_wait(&NB_SYNC_COND, &NB_SYNC_MUTEX);
    }
  }
  mtx_unlock(&NB_SYNC_MUTEX);
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
  if (atomic_load(&TERMINATOR_CLOSED)) {
    return PyLong_FromLongLong(-1);
  }
  int_least64_t newval = atomic_fetch_add(&TERMINATOR_COUNT, 1) + 1;
  if (atomic_load(&TERMINATOR_CLOSED)) {
    int_least64_t after = atomic_fetch_add(&TERMINATOR_COUNT, -1) - 1;
    if (after == 0) {
      mtx_lock(&TERMINATOR_MUTEX);
      cnd_broadcast(&TERMINATOR_COND);
      mtx_unlock(&TERMINATOR_MUTEX);
    }
    return PyLong_FromLongLong(-1);
  }
  return PyLong_FromLongLong((long long)newval);
}

/// @brief Decrement the terminator. Wakes terminator_wait on 0-transition.
/// @param self The module (unused)
/// @param args Unused
/// @return Python int — the new count.
static PyObject *_core_terminator_dec(PyObject *self,
                                      PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  int_least64_t newval = atomic_fetch_add(&TERMINATOR_COUNT, -1) - 1;
  if (newval == 0) {
    mtx_lock(&TERMINATOR_MUTEX);
    cnd_broadcast(&TERMINATOR_COND);
    mtx_unlock(&TERMINATOR_MUTEX);
  }
  return PyLong_FromLongLong((long long)newval);
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
  atomic_store(&TERMINATOR_CLOSED, 1);
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

  bool do_timeout = false;
  double end_time = 0.0;
  if (timeout_obj != Py_None) {
    double timeout = PyFloat_AsDouble(timeout_obj);
    if (timeout == -1.0 && PyErr_Occurred()) {
      return NULL;
    }
    if (timeout >= 0.0) {
      do_timeout = true;
      end_time = boc_now_s() + timeout;
    }
  }

  bool ok = true;
  Py_BEGIN_ALLOW_THREADS mtx_lock(&TERMINATOR_MUTEX);
  while (atomic_load(&TERMINATOR_COUNT) != 0) {
    if (do_timeout) {
      double now = boc_now_s();
      if (now >= end_time) {
        ok = false;
        break;
      }
      cnd_timedwait_s(&TERMINATOR_COND, &TERMINATOR_MUTEX, end_time - now);
    } else {
      cnd_wait(&TERMINATOR_COND, &TERMINATOR_MUTEX);
    }
  }
  mtx_unlock(&TERMINATOR_MUTEX);
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
  int_least64_t prev = atomic_exchange(&TERMINATOR_SEEDED, 0);
  if (prev == 1) {
    int_least64_t newval = atomic_fetch_add(&TERMINATOR_COUNT, -1) - 1;
    if (newval == 0) {
      mtx_lock(&TERMINATOR_MUTEX);
      cnd_broadcast(&TERMINATOR_COND);
      mtx_unlock(&TERMINATOR_MUTEX);
    }
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
  // Fence: raise the closed bit before we touch anything else so any
  // stray thread still holding a reference to the previous runtime
  // (e.g. a late whencall call) is refused by terminator_inc rather
  // than slipping a new behavior past the reset boundary. We clear
  // the bit again at the end, once the new COUNT/SEEDED values have
  // been published, so a fresh start() sees closed=0.
  atomic_store(&TERMINATOR_CLOSED, 1);
  mtx_lock(&TERMINATOR_MUTEX);
  int_least64_t prior_count = atomic_load(&TERMINATOR_COUNT);
  int_least64_t prior_seeded = atomic_load(&TERMINATOR_SEEDED);
  atomic_store(&TERMINATOR_COUNT, 1);
  atomic_store(&TERMINATOR_SEEDED, 1);
  atomic_store(&TERMINATOR_CLOSED, 0);
  cnd_broadcast(&TERMINATOR_COND);
  mtx_unlock(&TERMINATOR_MUTEX);
  return Py_BuildValue("(LL)", (long long)prior_count, (long long)prior_seeded);
}

/// @brief Read the current TERMINATOR_SEEDED flag (for reconciliation).
/// @param self The module (unused)
/// @param args Unused
/// @return Python int — 0 or 1.
static PyObject *_core_terminator_seeded(PyObject *self,
                                         PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  return PyLong_FromLongLong((long long)atomic_load(&TERMINATOR_SEEDED));
}

/// @brief Read the current terminator count (for reconciliation tests).
/// @param self The module (unused)
/// @param args Unused
/// @return Python int — the current TERMINATOR_COUNT.
static PyObject *_core_terminator_count(PyObject *self,
                                        PyObject *Py_UNUSED(args)) {
  BOC_STATE_SET(self);
  return PyLong_FromLongLong((long long)atomic_load(&TERMINATOR_COUNT));
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
static int_least64_t cown_decref(BOCCown *cown) {
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

#define COWN_WEAK_DECREF(c) cown_weak_decref(c)

/// @brief Atomic incref for the cown
/// @param cown the cown to incref
/// @return the new reference count
static int_least64_t cown_incref(BOCCown *cown) {
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

      atomic_store_intptr(&qptr->tag, (intptr_t)qtag);
      TAG_INCREF(qtag);
      BOC_STATE->queue_tags[i] = qtag;
      TAG_INCREF(qtag);
      return qptr;
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
  BOCMessage *message = (BOCMessage *)PyMem_RawMalloc(sizeof(BOCMessage));
  if (message == NULL) {
    PyErr_NoMemory();
    return NULL;
  }

  BOCQueue *qptr = get_queue_for_tag(tag);
  if (qptr == NULL) {
    PyMem_RawFree(message);
    PyErr_Format(PyExc_KeyError,
                 "No queue available for tag %R: tag capacity exceeded", tag);
    return NULL;
  }

  BOCTag *qtag = (BOCTag *)atomic_load_intptr(&qptr->tag);
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
/// @details The @c boc_worker queue is a fixed-capacity ring
/// (@c BOC_CAPACITY = 16384 slots). Reaching that bound requires more
/// than 16k behaviors to be simultaneously runnable but not yet picked
/// up by any worker -- in practice, only a producer scheduling against
/// many disjoint cowns far faster than every worker can drain. MCS
/// chaining keeps behaviors that share a cown out of the queue until
/// their predecessor releases, so chains do not exhaust capacity.
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
  // Prefer clock_gettime on POSIX: timespec_get requires macOS 10.15+ while
  // Python's default macOS deployment target is older, producing an
  // -Wunguarded-availability-new warning. clock_gettime has been available on
  // macOS since 10.12. Windows UCRT provides timespec_get but not
  // clock_gettime, so fall back there.
#ifdef _WIN32
  timespec_get(&ts, TIME_UTC);
#else
  clock_gettime(CLOCK_REALTIME, &ts);
#endif
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
  /// @brief Pre-built dispatch message for the BehaviorCapsule.
  /// @details Allocated by behavior_prepare_start before the 2PL link loop,
  /// claimed by the unique caller that observes @c count → 0 inside
  /// behavior_resolve_one. Targets @c boc_worker directly with the bare
  /// BehaviorCapsule as the payload. Visibility is carried by the acq-rel
  /// fetch_sub on @c count — no separate atomic on this field is required.
  /// Freed defensively by behavior_free if a behavior is destroyed without
  /// dispatching.
  struct boc_message *start_message;
} BOCBehavior;

/// @brief Capsule for holding a pointer to a behavior
typedef struct behavior_capsule_object {
  PyObject_HEAD BOCBehavior *behavior;
} BehaviorCapsuleObject;

#define BehaviorCapsule_CheckExact(op)                                         \
  Py_IS_TYPE((op), BOC_STATE->behavior_capsule_type)

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
  behavior->start_message = NULL;
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

  if (behavior->start_message != NULL) {
    // Defensive cleanup: prepare_start succeeded but the message was
    // never claimed (e.g. resolve_one was never called because
    // schedule() failed mid-link). Free the unclaimed message — it
    // never made it onto the queue, so this is just our private
    // allocation.
    boc_message_free(behavior->start_message);
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
/// cown. If this is the last request, then the thunk is scheduled. The unique
/// caller that observes count -> 0 claims the pre-built start message stashed
/// by behavior_prepare_start and enqueues it.
/// Visibility of the start_message pointer is carried by the acq-rel
/// fetch_sub on count -- the only writer (prepare_start) ran before the link
/// loop began, and only one decrementer can transition to 0. This path
/// performs no allocation and therefore cannot fail past prepare.
///
/// Returns @c int rather than @c PyObject* so the count > 0 path is
/// pure-atomic and can be invoked from inside a @c Py_BEGIN_ALLOW_THREADS
/// span (no @c Py_RETURN_NONE = no Py_None refcount touch). The only
/// Python-state operation remaining is @c PyErr_SetString on the
/// @c boc_enqueue-full error path; that path requires @c count == 0 which
/// is unreachable mid link-loop because @c BehaviorCapsule_init sizes
/// @c count to @c args_size + 2. Callers that hit the error path must hold
/// the GIL.
///
/// If @c boc_enqueue overflows the @c boc_worker ring, this raises
/// @c RuntimeError("Message queue is full"); see @c boc_enqueue for the
/// queue-full failure mode and recovery analysis.
/// @param behavior the behavior whose count to decrement
/// @return 0 on success, -1 on error with a Python exception set (caller
///         must hold the GIL on the error path)
static int behavior_resolve_one(BOCBehavior *behavior) {
  int_least64_t count = atomic_fetch_add(&behavior->count, -1) - 1;
  if (count == 0) {
    BOCMessage *message = behavior->start_message;
    behavior->start_message = NULL;
    if (message == NULL) {
      // Defensive: prepare_start was never called. This should not happen
      // on the production path; raise so the failure is loud.
      PyErr_SetString(PyExc_RuntimeError,
                      "behavior_resolve_one: start message not prepared");
      return -1;
    }

    if (boc_enqueue(message) < 0) {
      boc_message_free(message);
      PyErr_SetString(PyExc_RuntimeError, "Message queue is full");
      return -1;
    }
  }

  return 0;
}

/// @brief Pre-allocate the dispatch message for the BehaviorCapsule.
/// @details Performs every fallible operation up front so the subsequent 2PL
/// link loop is infallible. On success, the
/// message is stashed on behavior->start_message and consumed by the unique
/// caller that drives behavior->count to 0 in behavior_resolve_one. On
/// failure, no state is published -- the caller (whencall) rolls back the
/// terminator. Dispatch goes directly to @c boc_worker carrying the
/// bare BehaviorCapsule (no @c ("start", ...) tuple, no central scheduler hop).
/// @param behavior The behavior to prepare
/// @return 0 on success, -1 on failure with a Python exception set
static int behavior_prepare_start(BOCBehavior *behavior) {
  if (behavior->start_message != NULL) {
    PyErr_SetString(PyExc_RuntimeError, "behavior_prepare_start called twice");
    return -1;
  }

  // Wrap the BOCBehavior in a fresh BehaviorCapsule. The queue's XIData
  // layer will keep this object alive until the message is consumed.
  PyTypeObject *type = BOC_STATE->behavior_capsule_type;
  BehaviorCapsuleObject *capsule =
      (BehaviorCapsuleObject *)type->tp_alloc(type, 0);
  if (capsule == NULL) {
    return -1;
  }
  capsule->behavior = behavior;
  BEHAVIOR_INCREF(behavior);

  // Dispatch the BehaviorCapsule directly to a worker. Workers match
  // ["boc_worker", behavior] and run it. The capsule is the message
  // payload; the queue's XIData layer keeps it alive in flight.
  PyObject *contents = (PyObject *)capsule; // borrow the new reference
  PyObject *tag = PyUnicode_FromString("boc_worker");
  if (tag == NULL) {
    Py_DECREF(capsule);
    return -1;
  }

  BOCMessage *message = boc_message_new(tag, contents);
  Py_DECREF(capsule);
  Py_DECREF(tag);
  if (message == NULL) {
    return -1;
  }

  behavior->start_message = message;
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

/// @brief Schedule a behavior: prepare-then-link, infallible past prepare.
/// @details Two-phase locking entry point that consolidates create_requests,
/// prepare_start, and the link/finish loops into one C call.
/// All allocations happen before the first
/// MCS link op, so failures cannot leave the cown queues in a partial
/// state. The Python @c Behavior.schedule() collapses to a single call to
/// this function.
/// @param op The BehaviorCapsule to schedule
/// @return Py_None on success, NULL on error
static PyObject *BehaviorCapsule_schedule(PyObject *op,
                                          PyObject *Py_UNUSED(dummy)) {
  BehaviorCapsuleObject *capsule = (BehaviorCapsuleObject *)op;
  BOCBehavior *behavior = capsule->behavior;

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

  // Pre-allocate the start message. From this point onwards the link loop
  // is infallible: no Python allocation, no callbacks.
  if (behavior_prepare_start(behavior) < 0) {
    return NULL;
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
/// @details Pure C, no Python allocation, no exception. The only failure
/// surface is propagated by behavior_resolve_one (which can fail if the
/// queue is full); we return its NULL/non-NULL via int. Callers that have
/// already pre-allocated the start message via behavior_prepare_start can
/// treat this as infallible from the link-loop perspective.
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
  // wait for the previous request to be scheduled
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

    // assign a new tag
    BOCTag *oldtag =
        (BOCTag *)atomic_exchange_intptr(&qptr->tag, (intptr_t)qtag);
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
    {"cowns", _core_cowns, METH_NOARGS, NULL},
    {"set_tags", _core_set_tags, METH_VARARGS,
     "set_tags($module, tags, /)\n--\n\nAssigns tags to message queues."},
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
      memset(qptr->messages, 0, BOC_CAPACITY * sizeof(BOCMessage *));
      qptr->head = 0;
      qptr->tail = 0;
      qptr->state = BOC_QUEUE_UNASSIGNED;
      qptr->tag = 0;
      qptr->waiters = 0;
      boc_park_init(qptr);
    }

    BOCRecycleQueue *queue_stub =
        (BOCRecycleQueue *)PyMem_RawMalloc(sizeof(BOCRecycleQueue));
    queue_stub->head = 0;
    queue_stub->tail = NULL;
    queue_stub->next = 0;
    atomic_store_intptr(&BOC_RECYCLE_QUEUE_HEAD, (intptr_t)queue_stub);
    BOC_RECYCLE_QUEUE_TAIL = queue_stub;

    // Initialize the noticeboard
    memset(&NB, 0, sizeof(NB));
    boc_mtx_init(&NB.mutex);

    // Initialize the notice_sync barrier primitives.
    boc_mtx_init(&NB_SYNC_MUTEX);
    cnd_init(&NB_SYNC_COND);

    // Initialize the terminator primitives.
    // The Pyrona seed (count=1, seeded=1) is set by terminator_reset()
    // when the runtime starts; here we only initialize the kernel objects.
    boc_mtx_init(&TERMINATOR_MUTEX);
    cnd_init(&TERMINATOR_COND);

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
  Py_CLEAR(state->loads);
  Py_CLEAR(state->dumps);
  Py_CLEAR(state->pickle);
  Py_CLEAR(state->cown_capsule_type);
  Py_CLEAR(state->behavior_capsule_type);
  // this needs to be cleared here, as it was allocated on this interpreter.
  Py_CLEAR(state->recycle_queue->xidata_to_cowns);
  // Clear the thread-local snapshot cache so the GC can collect any
  // reference cycles anchored through the cached dict / proxy.
  nb_drop_local_cache();
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

    // Clear the thread-local snapshot cache before freeing entries
    Py_CLEAR(NB_SNAPSHOT_CACHE);

    // Collect noticeboard entries to free after releasing the mutex.
    XIDATA_T *nb_to_free[NB_MAX_ENTRIES];
    int nb_to_free_count = 0;

    mtx_lock(&NB.mutex);
    for (int i = 0; i < NB.count; i++) {
      if (NB.entries[i].value != NULL) {
        nb_to_free[nb_to_free_count++] = NB.entries[i].value;
        NB.entries[i].value = NULL;
      }
    }
    NB.count = 0;
    mtx_unlock(&NB.mutex);

    for (int i = 0; i < nb_to_free_count; i++) {
      XIDATA_FREE(nb_to_free[i]);
    }

    // Destroy noticeboard mutex
    mtx_destroy(&NB.mutex);

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