## 2026-04-01 - Version 0.3.0
Spin-then-park receive; free-threaded Python compatibility.

**Improvements**

- Added `CownCapsule.disown()` — abandons a cown's value without
  serializing it and resets ownership to `NO_OWNER`. Used during worker
  cleanup to safely discard orphan cowns before the owning interpreter
  is destroyed, preventing dangling Python object references.
- Rewrote `receive` to use a two-phase spin-then-park strategy for
  single-tag untimed receives. Phase 1 spins for `BOC_SPIN_COUNT`
  iterations; Phase 2 parks the thread on a per-queue condvar, eliminating
  busy-wait CPU burn. Timed receives and multi-tag receives use
  spin-then-backoff with exponential sleep (1 µs → 1 ms cap).
- Added platform-abstracted condvar primitives (`BOCParkMutex` /
  `BOCParkCond`) with implementations for Windows (SRWLOCK /
  CONDITION_VARIABLE), macOS (pthreads), and Linux (C11 threads).
- Each `BOCQueue` now carries a `waiters` counter, `park_mutex`, and
  `park_cond`. Producers signal parked receivers after enqueue;
  `drain` and `set_tags` broadcast to wake all parked threads.
- Replaced the fixed `thrd_sleep` in `send` with a `sched_yield` /
  `SwitchToThread`, reducing send-side latency.
- Refactored the monolithic `_core_receive` into `receive_single_tag`
  and `receive_multi_tag`, each with its own backoff/parking logic.
- Moved the `BOC_QUEUE_DISABLED` check earlier in `get_queue_for_tag`
  so callers skip disabled queues instead of returning NULL after
  tag resolution.
- Added Windows-compatible `atomic_load_explicit` /
  `atomic_fetch_add_explicit` / `atomic_fetch_sub_explicit` macros
  using `InterlockedExchangeAdd64`.
- Declared `Py_mod_gil = Py_MOD_GIL_NOT_USED` in both `_core` and
  `_math` C extensions so that importing bocpy on a free-threaded
  Python build (3.13t+) does not re-enable the GIL.
- Replaced `PyDict_GetItem` (borrowed reference) with
  `PyDict_GetItemRef` (strong reference) in `BOCRecycleQueue_recycle`
  on Python 3.13+, improving forward-compatibility with free-threaded
  builds.

**Bug Fixes**

- Fixed a deadlock when the same cown is passed multiple times to `@when`
  (e.g. `@when(c, c)`). Duplicate requests for the same cown caused the
  MCS-queue-based two-phase locking to spin-wait on itself. Requests are
  now deduplicated by target cown in `Behavior.__init__`, with
  compensating `resolve_one` calls to maintain the behavior count
  invariant.

**Tests**

- `TestLostWakeStress`: single-producer random delays, bursty producer,
  and repeated single-message wake to detect lost-wake races.
- `TestMultiTagBackoff`: multi-tag receive correctness — second-tag hit,
  delayed arrival, per-tag FIFO ordering, timeout, and interleaved
  producers.
- `TestTimeoutAccuracy`: lower-bound / upper-bound wall-clock checks and
  zero-timeout immediacy.
- Added tests for duplicate cowns in `@when`: same cown twice, thrice,
  non-adjacent duplicates, duplicates within a group, and mutation
  aliasing semantics.

**CI**

- Added a `free-threaded` CI job that tests against Python 3.13t and
  3.14t on Linux, with explicit assertions that the GIL remains disabled
  after import.

## 2026-03-17 - Version 0.2.2
Point release.

**Improvements**
- Added an ASAN/UBSAN CI job that builds CPython 3.14.2 from source with
  AddressSanitizer and UndefinedBehaviorSanitizer, then runs the full test suite
  against instrumented builds of bocpy.
- Updated GitHub Actions to latest versions (`actions/checkout@v6`,
  `actions/setup-python@v5`).
- Added a Copilot skill for version bumping.

**Bug Fixes**
- Fixed a missing `Py_DECREF` on a temporary `PyObject` in the xidata recycling
  path, plugging a reference leak.
- Fixed `PyMem_RawFree` freeing the wrong pointer (`xidata->obj` instead of
  `xidata`) in the recycling queue cleanup.

## 2026-03-11 - Version 0.2.1
Point release.

**Improvements**
- Adding a repository-level copilot-instructions file
- Properly added the skills files as copilot agent skills

**Bug Fixes**
- Fixed a false positive warning message for deallocation of xidata on the main
  interpreter after module shutdown.
- Changed the clear logic when recycling

## 2026-03-04 - Version 0.2.0
Bugfix release including some minor improvements.

**Improvements**
- Examples are now included in the package, with script entrypoints for each.
- The `drain` low-level API function is now exposed at the package level
- `wait()` will now acquire frame-local `Cown` objects before shutting down the workers

**Dev Tools**
- Added an internal cown and behavior reference tracking utility

**Bug Fixes**
- Fixed a reference counting bug with cown lists
- Fixed an issue where the boids example did not run on windows due a font
  setting.


## 2026-03-02 - Version 0.1.0
Initial Release.