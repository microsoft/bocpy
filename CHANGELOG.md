## 2026-04-29 - Version 0.5.0
Verona-RT-style work-stealing scheduler, C source split into per-subsystem
translation units, and a portable atomics / threading layer.

**New Features**

- **Work-stealing scheduler** — the single behavior queue has been
  replaced with a Verona-RT-inspired distributed scheduler. Each
  worker owns a Multi-Producer Multi-Consumer behavior queue
  (`boc_bq_*`, ported from `verona-rt/src/rt/sched/mpmcq.h`), pops
  work from its own queue first, and steals from peers when empty.
  Idle workers park on a per-worker condition variable and are
  signalled directly by the producer / victim, eliminating the
  central wakeup broadcast. Per-worker statistics (steals, parks,
  fast/slow pops, dispatches) are exposed for benchmarking.
- **Per-worker fairness tokens** — each worker advances a token node
  through its own queue so that long-running behaviors cannot
  monopolise dispatch slots. The token is also used to drive the
  cooperative shutdown handshake.
- **`compat.h` / `compat.c` portability layer** — a single header now
  exposes uniform `BOCMutex`, `BOCCond`, `boc_atomic_*_explicit`,
  monotonic-time, and sleep primitives across MSVC, pthreads, and
  C11 `<threads.h>`. The work-stealing scheduler depends on the
  typed-atomics API for ARM64-correct memory ordering on Windows.
- **`xidata.h` cross-interpreter shim** — the `#if PY_VERSION_HEX`
  ladders for the `_PyXIData_*` / `_PyCrossInterpreterData_*` APIs
  that previously lived in both `_core.c` and `_math.c` have been
  centralised in one header covering CPython 3.12 through 3.15
  (including free-threaded builds).
- **`fanout_benchmark` example** — a fan-out / fan-in benchmark
  harness exercising scheduler throughput under heavy producer
  load.

**Improvements**

- **In-memory transpiled-module loading** — workers no longer write
  the transpiled module to a temporary directory and import it
  through `importlib.util.spec_from_file_location`. Instead, the
  transpiled source is embedded as a string literal in the worker
  bootstrap and `exec`'d into a fresh `types.ModuleType` registered
  in `sys.modules`. The source is also published to `linecache` so
  tracebacks still point at the transpiled lines. This removes the
  `export_dir` argument from `start()` (and the matching tempdir
  cleanup in `wait()`/`stop()`), eliminates a filesystem round-trip
  on every worker startup, and avoids leaving `.py` files behind on
  abnormal exit. Module names are validated as dotted Python
  identifiers at the boundary, and `__main__` is re-aliased to
  `__bocmain__` inside workers so a follow-up `start()` observes a
  clean `sys.modules`.
- **Nested `@when` capture** — the transpiler now recurses into
  `@when`-decorated nested functions when computing the outer
  behavior's captures, so a behavior body can schedule child
  behaviors that close over the outer frame's free names without
  raising `NameError` at dispatch time.
- **C extension split into subsystem TUs** — `_core.c` has been
  reduced from ~5,000 lines to ~3,500 by extracting `sched.{c,h}`
  (work-stealing scheduler), `noticeboard.{c,h}`, `terminator.{c,h}`,
  `tags.{c,h}` (message-queue tag table), `cown.h` (cown refcount
  helpers), and `compat.{c,h}` / `xidata.h` into separate
  translation units. Every public function now has a header
  declaration with Doxygen-style documentation.
- **Direct dispatch on cown release** — `behavior_release_all` now
  hands a resolved successor directly to a worker via the
  work-stealing dispatch path (`boc_sched_dispatch`) instead of
  re-entering the central scheduler, removing one queue hop per
  cown handoff.
- **Cooperative worker shutdown** — `boc_sched_worker_request_stop_all`
  and `boc_sched_unpause_all` provide a clean stop/drain protocol
  that interacts correctly with parked workers and the terminator.

**Internal Test Modules**

- **`_internal_test_atomics`** — pytest-driven correctness tests for
  the `compat.h` typed-atomics API on every supported platform.
- **`_internal_test_bq`** — torture tests for the MPMC behavior
  queue (`boc_bq_*`), covering segmented dequeue, FIFO fairness,
  and concurrent producer / consumer races.
- **`_internal_test_wsq`** — tests for the work-stealing primitives
  (fast pop, slow pop, steal, park / unpark handshake).

**Test Suite**

- New scheduler test files — `test_scheduler_integration.py`,
  `test_scheduler_mpmcq.py`, `test_scheduler_pertask_queue.py`,
  `test_scheduler_stats.py`, `test_scheduler_steal.py`,
  `test_scheduler_wsq.py` — exercise the distributed scheduler end
  to end and per primitive.
- `test_compat_atomics.py` — Python-level smoke tests for the
  portable atomics layer.
- `test_stop_retry_composition.py` — covers `stop()` / `start()` /
  `wait()` retry composition across multiple runtime cycles.
- `test_scheduling_stress.py` substantially expanded with new
  fan-out, work-stealing, and shutdown stress scenarios.
- `test_boc.py` and `test_transpiler.py` extended with regression
  cases discovered during the scheduler rewrite.

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