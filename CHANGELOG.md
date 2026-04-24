## 2026-04-17 - Version 0.4.0
Noticeboard, distributed scheduler, and a relocated examples package.

**New Features**

- **Noticeboard** — a shared key-value store (up to 64 keys) that
  behaviors can read and write without acquiring cowns. Writes
  (`notice_write`, `notice_delete`) are non-blocking; reads
  (`noticeboard`, `notice_read`) return a cached snapshot taken once
  per behavior execution. Atomic read-modify-write is available via
  `notice_update`, which accepts a picklable function and an optional
  default. Returning the `REMOVED` sentinel from the update function
  deletes the entry. Mutations are serialized through a single
  dedicated noticeboard thread so the C-level read-modify-write stays
  consistent without forcing behaviors to take a mutex.
- **`notice_sync`** — new public API that blocks until the caller's
  prior `notice_write` / `notice_update` / `notice_delete` mutations
  have been committed, providing a read-your-writes barrier for code
  that hands work off to a subsequent behavior.
- **`noticeboard_version`** — new public API returning a global,
  monotonic version counter that increments on every successful
  noticeboard commit. Useful as a cheap change-detection hint without
  taking a full snapshot.
- **Distributed scheduler** — the central scheduler thread has been
  removed. Two-phase locking, request linking, and dispatch now run
  in C (`BehaviorCapsule.schedule`) directly on the caller's thread,
  and cown release runs on the worker thread that just executed the
  behavior. Waiters are tracked with an MCS-style intrusive linked
  list per cown, so resolving a behavior hands off straight to the
  next waiter without bouncing through any central queue. The
  C-level terminator is now the only pending counter.
- **`Cown.exception` property** — new boolean property on `Cown` that
  indicates whether the held value is the result of an unhandled
  exception. Workers now call `set_exception` instead of `set_result`
  when a behavior raises.
- **Prime factor example** (`examples/prime_factor.py`, entry point
  `bocpy-prime-factor`) — demonstrates parallel factorisation using
  Pollard's rho algorithm with early termination coordinated via the
  noticeboard.
- **Benchmark harness** (`examples/benchmark.py`, entry point
  `bocpy-bench`) — a new micro-benchmark suite covering scheduling
  throughput, message-queue latency, and noticeboard contention.

**Bug Fixes**

- **Transpiler aliased imports** — `visit_Import` and `visit_ImportFrom`
  now track the alias name (`import X as Y` / `from X import Y as Z`)
  instead of the original name, preventing spurious "name not found"
  errors and duplicate `whencall` injection.
- **Global variable capture** — `@when` closure capture now falls back
  to `frame.f_globals` when a name is not found in any local scope,
  fixing `NameError` for module-level variables used inside behaviors.

**Improvements**

- **C mutex abstraction** — platform-specific mutex and condition-variable
  code (`SRWLock`/`pthread`/C11 `mtx_t`) is now wrapped behind a
  unified `BOCMutex`/`BOCCond` inline API, reducing `#ifdef` clutter
  and simplifying future platform work.
- **Matrix docstrings** — all `Matrix` C methods now carry built-in
  docstrings visible to `help()` and Sphinx autodoc.
- **Worker noticeboard hygiene** — workers clear the per-thread
  noticeboard cache before each behavior and on shutdown, preventing
  stale reads across behaviors.
- **Examples package relocated** — example scripts moved from
  `src/bocpy/examples/` to a top-level `examples/` directory, mapped
  back into the `bocpy.examples` package via
  `[tool.setuptools.package-dir]`. Console-script entry points are
  unchanged.
- **Filtered PyPI README** — `setup.py` now strips
  `<!-- pypi-skip-start -->...<!-- pypi-skip-end -->` regions from
  `README.md` before publishing, so unsupported content (e.g. Mermaid
  diagrams) does not appear as raw text on PyPI. The project metadata
  switches to `dynamic = ["readme"]` to enable this.
- **Documentation refresh** — `README.md`, `sphinx/source/index.rst`,
  and `sphinx/source/api.rst` have been substantially expanded to
  cover the noticeboard, the distributed scheduler model, and the new
  public APIs.
- **New `thinking-in-boc` skill** — guidance for writing BOC code
  without reaching for classical synchronization primitives.

**Tests**

- **`test/test_noticeboard.py`** — new suite covering snapshot
  semantics, `notice_update` atomicity, `REMOVED`, `notice_sync`,
  and version-counter monotonicity.
- **`test/test_scheduling_stress.py`** — new stress suite for the
  distributed scheduler covering 2PL ordering, duplicate-cown
  handling, exception propagation, and high-fan-out workloads.
- **`test/test_transpiler.py`** — new direct tests for AST extraction,
  capture rewriting, aliased imports, and module export.

## 2026-04-02 - Version 0.3.1
CownCapsule serialization support for nested cowns.

**Bug Fixes**

- Removed the ownership check in `_cown_shared` that prevented a
  `CownCapsule` from being serialized to XIData when it was the value
  of another `Cown`. The check was unnecessary — `_cown_shared` only
  stores a pointer and ownership is enforced at acquire time.

**Improvements**

- Added `CownCapsule.__reduce__` with `COWN_INCREF` pinning so that a
  `CownCapsule` embedded in a container (dict, list, etc.) can survive
  the pickle round-trip used by `object_to_xidata`. A module-level
  reconstructor (`_cown_capsule_from_pointer`) inherits the pin without
  a redundant `COWN_INCREF`, and validates the process ID on unpickle to
  guard against cross-process misuse.

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