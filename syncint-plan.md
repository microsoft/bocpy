# Scope implementation plan

**Status:** implemented and verified on CPython 3.14; free-threaded execution
has not been validated locally.

**Native wait driver:** wait-and-pump coordination now executes in C with one
stack-owned completion observation across wakeups and bounded pinned batches.
Python retains argument validation and public lifecycle handling, not a second
polling loop or a Python waiter type. Internal C tests cover exact generation
and wake semantics. Full-tree validation: **4102 passed, 16 skipped, 3 xfailed**;
root-only staged commit: **4022 passed, 16 skipped, 3 xfailed**. Lint, example,
strict Sphinx, and targeted review pass.

**Lifecycle refinement:** the shared scope owner uses
`CREATED -> ENTERING -> ACTIVE -> DRAINING -> EXTRACTING -> CLOSED`.
Its former lifecycle flags are replaced by that state machine. Root and scope
terminators share reservation counting, a `zero_count` generation, and
per-instance mutex/condition synchronization. An immutable `is_root` flag
selects whether start also reserves the root for the scope lifetime. There is
no separate host counter: root reservations include scope lifetimes, while a
scope's own counter includes only its behaviors. Reference lifetime, host
thread identity, and output/failure outcomes remain separate.

**Quiescence refinement:** global `quiesce()` and `Scope.quiesce()` retain a
zero-transition observation across wakeups. They do not require the count to
remain zero or close admission. Scope exit and runtime shutdown instead drain
current reservations and atomically fence admission. Global quiescence includes
open scopes; scoped quiescence can still observe idle batches inside an open
scope. A root terminator is retired after shutdown and a fresh
one is allocated at the next startup; retained observations own references to
the original instance.

The refinement passed native rebuild, all 59 scope tests, full lint, the
runnable example, and strict Sphinx. Full `.env314` suite: **4053 passed,
16 skipped, 3 xfailed** (baseline: 4049 passed, 16 skipped, 3 xfailed).
Independent review found no significant issues in the state-machine changes.

**Design:** [Synchronous integration with BOC scopes](syncint.md).
That document defines the intended semantics; this document identifies the
implementation work, dependencies, validation gates, and remaining decisions.

## Scope and constraints

- Add `Scope` for local joins while retaining root-owned scheduling outside
  context managers and global `wait()` for runtime-wide draining and shutdown.
- Resolve ownership as explicit `scope=`, then active scope, then implicit
  root. Apply the same rule to decorated and lower-level scheduling.
- Expose `root_scope` as a runtime-owned scheduling handle, not a public
  terminator or a permanently entered context manager. Keeping a handle alive
  must not prevent completion or runtime restart.
- Support scopes entered from synchronous code and from running behaviors.
- Wait for a scope's work, including descendants, without requiring unrelated
  scopes to finish. Work explicitly assigned outside its subtree can outlive
  the local call; global `wait()` still includes it.
- Transfer the attached output cown's value to `scope.output_value` on successful
  exit. Captured behavior failures do not prevent publication of the scope
  output, which may include mutations made before an exception.
- Preserve existing exception semantics: a behavior's failure goes into its
  own result cown, not the scope output cown. Release acquired cowns without
  poisoning or rollback. Do not add exception aggregation at scope exit or
  global `wait()`, ancestor failure records, or automatic cancellation.
- Preserve single-use states for explicit scopes. A scope's own body/exit or
  transfer failure is distinct from a captured behavior failure.
- Keep the current terminator in thread-local storage internally. Store the
  scheduling terminator on each behavior and restore previous thread-local
  state after behavior execution and scope exit.
- Retain distributed scheduling and the shared worker pool. Do not introduce
  a central scheduler, a separate scope token, or a redundant child counter.
- If global timed shutdown expires while any reservation remains, raise
  `TimeoutError` without stopping the runtime or closing admission. This
  applies equally to root behaviors and open scope lifetimes.
- Accept the documented deadlock hazards of blocking nested scopes. Do not
  silently release parent cowns, reject such dependencies, or introduce worker
  helping as part of this work.

This plan does not include cancellation, timeout semantics for scope exit, or
a broader redesign of ordinary cowns.

## Original implementation and affected files

| Area | Current behavior | Implementation target |
|------|------------------|-----------------------|
| Terminator | One process-global count, seed, closed flag, mutex, and condition variable | [boc_terminator.h](src/bocpy/boc_terminator.h), [boc_terminator.c](src/bocpy/boc_terminator.c) |
| Scheduling | `whencall` reserves the global count; `when` can start the runtime implicitly | [behaviors.py](src/bocpy/behaviors.py) |
| Behavior storage | `BOCBehavior` owns requests, arguments, captures, and a result cown, but no terminator reference | [_core.c](src/bocpy/_core.c) |
| Worker completion | `run_behavior` releases cowns and requests, then decrements the global terminator | [worker.py](src/bocpy/worker.py) |
| Pinned execution | Pump and drain paths execute and release behaviors in C, with global completion accounting | [_core.c](src/bocpy/_core.c), [behaviors.py](src/bocpy/behaviors.py) |
| Value transfer | `CownCapsule_unwrap` requires global quiescence and leaves an ordinary cown reusable | [_core.c](src/bocpy/_core.c) |
| Portability | Existing `thread_local`, mutex, condition-variable, and atomic abstractions | [boc_compat.h](src/bocpy/boc_compat.h) |
| Public API | Exports and stubs describe the existing lifecycle and cown interface | [__init__.py](src/bocpy/__init__.py), [__init__.pyi](src/bocpy/__init__.pyi) |
| User-facing material | Existing examples and documentation assume global lifecycle operations | [README.md](README.md), [examples/](examples/), [src/bocpy/examples/](src/bocpy/examples/), [sphinx/source/](sphinx/source/) |

Reuse the current synchronization and cross-interpreter transfer facilities.
Do not share interpreter-owned Python objects through native terminator fields.

## Decisions to resolve before the affected step

These are implementation blockers to bring to the author, not invitations to
expand the design automatically.

| Decision | Why it matters | Needed before |
|----------|----------------|---------------|
| Runtime restart and supported top-level callers | The pool survives local exits and global `wait()` drains/stops it. Define concurrent host-thread entry and restart/admission behavior, including how a retained `root_scope` handle resolves a new runtime | Step 3 |
| Existing lifecycle options | Preserve the root scheduling/global-wait programming model. Decide how existing worker-count/module selection, timeouts, statistics, and noticeboard snapshots interact with scopes; do not invent new scope options by default | Steps 3 and 7 |
| Noticeboard completion | Noticeboard mutations run on a separate thread. Define whether scope completion promises their visibility and whether they need scoped accounting | Step 3 |
| Ordinary-cown extraction | Preserve a runtime-wide safety check for existing `unwrap()` while adding local transfer. Never substitute the current scope's count for proof that unrelated cown users have finished | Steps 4 and 7 |
| Nested pinned pumping | The current pump rejects nested calls. Decide the behavior when a pinned behavior opens a scope whose descendants also require pumping; do not silently promise reentrant progress | Step 5 |

The accepted worker-exhaustion and parent-cown deadlocks remain accepted.
These decisions must not turn into an open-ended approval loop: record the
specific choice and continue with the affected step.

## Execution discipline

- Before implementation, ask which project virtual environment to use. Run all
  project commands inside that environment.
- Obtain approval of this plan before changing runtime code.
- Record a full-suite baseline and pre-existing failures before the first code
  change. Native internal tests require an editable build with
  `BOCPY_BUILD_INTERNAL_TESTS=1`.
- Add focused tests with each step, then expand validation at integration
  boundaries. Public scope tests live in `test/test_scope.py`.
- Preserve existing root scheduling while introducing explicit scope owners.
  Do not emulate local joins with a global wait merely to keep tests passing.
- At each source-changing step, record files changed, commands run, results,
  and deviations here. Ask the user to commit or stash the tested step before
  starting the next. Do not perform commits or pushes on their behalf.

For this execution, the author requested continuous implementation without
approval/checkpoint pauses. Record the test gates below without stopping for
commits; surface only major unexpected design issues. No commits or pushes
will be made by the implementation agent.

## Implementation log

- Environment: the author selected `.env314`. It was absent, so it was created
  with the installed Python 3.14 interpreter and bootstrapped using the existing
  environment's pip (the system interpreter has no ensurepip).
- Baseline: editable build with internal tests enabled; `pytest -q` reported
  **3994 passed, 16 skipped, 3 xfailed** in 57.17 seconds. No failing tests.
- Existing lifecycle options stay on `start`, `wait`, and `quiesce`; no new
  scope configuration or snapshot options are introduced. Scoped completion
  does not introduce a noticeboard mutation fence.
- Ordinary `unwrap()` retains its runtime-wide safety check. Local output
  transfer is separate. Root execution accounting covers all behaviors and
  active scope hosts; retained handles do not count as outstanding work.
- Nested pumping remains prohibited. A scope inside pinned execution can join
  worker work without recursively pumping; waiting there for pinned descendants
  retains the accepted deadlock risk.
- Native runtime work, Python API integration, regression tests, and public
  documentation are being developed in separate non-overlapping files.
- Python API syntax and lint checks pass; 38 new scope regression cases collect
  successfully. Their execution awaits the native rebuild.
- Public documentation, stubs, and the new scope example pass lint. A strict
  Sphinx build (`-E -W --keep-going`) passes after installing the existing
  documentation dependencies into `.env314`; no dependency manifests changed.
- Integration: native scope ownership exposed an existing pinned-descendant
  module-resolution problem. Registry keys now use the resolver's logical
  module rather than its private main-export module name.
- Focused scope/registry/pinned validation: **125 passed, 1 skipped**.
  Full `.env314` suite: **4032 passed, 16 skipped, 3 xfailed**. Repository lint
  and the runnable scope example pass. Independent correctness review is
  in progress.
- Implementation detail: the legacy root counters and wake channel remain
  static for compatibility, while local scopes own heap terminators. Local
  waits share root notifications (including pinned enqueue), avoiding a
  separate wakeup registry. Native references are independent of execution
  holds. This replaces the planned heap-root refactor without changing the
  scope contract or ordinary global-quiescence safety checks.
- A watchdog-bounded shutdown-timeout probe with an active host scope on another
  thread preserved its output, balanced the global count, and restarted the
  runtime successfully. No free-threaded Python executable is available in this
  environment, so free-threaded execution has not been validated locally.
- Native host-state flags now use portability atomics, and finalization
  disables host operations before invoking payload reconstruction. Added
  persistent regression coverage for simultaneous entry, foreign-thread
  operations, timeout-conversion reentry, and destructor/deserializer reentry.
- A deterministic host-thread/global-wait probe exposed a lifecycle-lock
  deadlock on nested scope entry. Nested entries now reuse their existing
  runtime hold without taking the top-level startup lock; a watchdog-bounded
  subprocess regression passes.
- Independent review found additional lifetime/cleanup cases. Repeatable
  noticeboard decoding now borrows entry-owned scope pins; teardown uses a
  private cown cleanup path without weakening output access restrictions.
  Scoped joins drain through repeated pump exceptions before finalization.
  Startup rejects outstanding old-runtime holds before resetting any state,
  and idempotent startup by active hosts avoids the global-join lock cycle.
- After those fixes: **4046 passed, 16 skipped, 3 xfailed**, including 52 new
  scope regression cases. Full lint, executable example, strict Sphinx, and
  whitespace checks pass. The reviewer is verifying the reported fixes.
- Follow-up review found that destructive timed shutdown could discard an
  output written by a worker while its host scope was still open. The author
  was unavailable when asked about the lifecycle choice, so the safe default
  was selected: reject timed shutdown with `TimeoutError`, leave workers and
  admission open, and allow a later retry after scope hosts finish. This only
  affects explicit scopes; legacy root-only timeout behavior is retained.
  Native scope admission and root closing must share synchronization, and a
  refused close re-arms the runtime seed. Worker-written-output regression
  coverage is being updated for both `wait` and direct `Behaviors.stop`.
- Final validation: **4047 passed, 16 skipped, 3 xfailed**, including **53 new
  scope tests**. Full repository lint, the executable scope example, strict
  Sphinx, editor diagnostics for the Python integration, and whitespace checks
  pass. The independent reviewer re-ran bounded probes for all reported
  defects and found no significant remaining issues in the reviewed changes.
  No commits or pushes were made.
- Public naming revision: the context manager is now `Scope`, with matching
  exports, stubs, diagnostics, examples, and documentation. Tests live in
  `test/test_scope.py`; their numeric wait duration is `WAIT_TIMEOUT`, distinct
  from the public `TIMEOUT` sentinel. Added a public-name/export regression.
  The pre-rename full baseline remained 4047 passed, 16 skipped, 3 xfailed;
  post-rename scope/registry checks report 86 passed, 1 skipped. Full lint,
  the example, and strict Sphinx pass. Other public scope names are unchanged.
- Output naming revision: use `output_cown` for scheduled access and
  `output_value` for the transferred value after scope exit. The former
  scope attribute names are removed; behavior result cowns are unchanged.
  Added a regression for the public names and read-only properties.
  Pre-edit full baseline: 4048 passed, 16 skipped, 3 xfailed. Post-edit
  scope/registry checks: 87 passed, 1 skipped. Full lint, executable example,
  strict Sphinx, editor diagnostics, and whitespace checks pass.
- Unified terminator revision supersedes the earlier static-root/shared-wake
  implementation. Native scheduling now retains root/scope instances and owns
  reservation rollback/completion; Python no longer balances separate root
  holds. Root seed APIs have been removed. Per-instance waiters retain zero
  generations and register only main-thread waits for pinned notifications.
  New scope quiescence tests cover repeated batches, timeout retry, exact zero
  events followed by renewed work, spurious wakes, self/ancestor rejection,
  pinned progress, strict finalization, and retained observations after restart.
  Independent review identified and verified a fix for finite deadlines when
  pinned work remains continuously ready: completion observations stay sticky,
  but another pump batch cannot override an expired deadline. Final `.env314`
  suite: **4079 passed, 16 skipped, 3 xfailed** (baseline: 4053 passed,
  16 skipped, 3 xfailed). Lint, example, strict Sphinx, and whitespace checks
  pass. The targeted reviewer found no significant remaining issues.
- Root-lifetime refinement: immutable `is_root` replaces the separate `hosts`
  counter. Starting each non-root terminator reserves and retains the root;
  scope finalization releases that reservation after output cleanup. Global
  quiescence now includes open scopes, while scoped quiescence remains
  work-only. All work-drain timeouts preserve the running runtime for retry,
  including root-only work. Header Doxygen contracts were restored for lifetime,
  locking, admission, waits, wake reasons, and root retirement.
  Final `.env314` suite: **4088 passed, 16 skipped, 3 xfailed** (baseline:
  4079 passed, 16 skipped, 3 xfailed). All 94 scope/quiescence cases, full lint,
  strict Sphinx, runnable example, and whitespace checks pass. Independent
  targeted review found no significant issues.

## Implementation sequence

### 1. Introduce instance-owned terminators

**Files:** [boc_terminator.h](src/bocpy/boc_terminator.h),
[boc_terminator.c](src/bocpy/boc_terminator.c), and the native test bindings.

1. Use one allocated native `Terminator` implementation for root and scopes:
   work count, zero-transition generation, admission control, and per-instance
   mutex/condition variable. Non-root terminators reserve the root's ordinary
   count for their scope lifetime; no separate host counter is needed.
2. Add explicit instance operations and a thread-local current-terminator
   reference using existing portability abstractions. Retain a runtime root
   instance as the fallback when no explicit scope is active.
3. Define lifetime ownership separately from completion accounting. Scope
   wrappers, queued/running behaviors, saved current references, and result
   ownership checks must not retain dangling pointers.
4. Define host closure and final completion separately: closing the host body
   must still permit a live behavior to register more work. Work can be admitted
   from zero until finalization fences admission.
5. Make waits release the interpreter lock. Reuse existing condition-variable
   logic while making the zero/completed transition and admission atomic.
6. Separate runtime-wide execution accounting from handle reference counts:
   global joining covers root work and active scope executions, but retaining
   a root handle or completed scope does not keep the runtime busy.

**Gate:** independent terminators do not affect one another; host lifetimes prevent
premature shutdown; closed/completed states have the required admission
behavior; allocation failure and final release clean up safely. Exercise both
normal and free-threaded builds before accepting the concurrency primitive.

**Checkpoint:** after the gate passes, ask the user to commit or stash this
step. Do not begin step 2 until that checkpoint exists.

### 2. Carry terminators through the full behavior lifecycle

**Files:** [_core.c](src/bocpy/_core.c),
[behaviors.py](src/bocpy/behaviors.py), [worker.py](src/bocpy/worker.py).

1. Add an owned terminator field to `BOCBehavior`. Initialize and release it on
   every allocation/free path; scheduler fairness tokens are not user behaviors
   and must not acquire scope reservations.
2. Bind scheduling to the resolved terminator before scheduler publication:
   explicit destination, then thread-local owner, then root. Define one
   publication commit point and exact rollback for unpublished work.
3. Install the stored terminator around worker execution and restore the
   previous value on all exits. A nested scope must not change which terminator
   owns the parent behavior's completion reservation.
4. Release the reservation only after result/error reporting, payload release,
   and request unlink/handoff have finished. Completion must imply that cowns
   are no longer held by the completed behavior.
5. Replace unconditional global decrements in normal workers, pinned execution,
   orphan drains, startup aborts, and teardown paths with accounting against
   the stored owner. Retain deliberate runtime-wide accounting separately.
6. Validate explicit destination admission and retain its owner safely across
   interpreters. Inactive/completed local destinations must fail rather than
   falling back to root.

**Gate:** descendant scheduling inherits the correct terminator across worker
interpreters; alternating unrelated behaviors on one worker do not leak
context; exceptional execution restores context; scheduling failures and drain
paths release exactly one reservation. Explicit selection overrides the ambient
scope, inherited descendants keep that owner, and root fallback works outside
context managers. Existing scheduler-ordering tests pass.

**Checkpoint:** ask the user to commit or stash the tested step before step 3.

### 3. Implement scope entry, exit, and internal runtime management

**Files:** [behaviors.py](src/bocpy/behaviors.py),
[_core.c](src/bocpy/_core.c), [worker.py](src/bocpy/worker.py).

1. Resolve the restart, caller, configuration, and noticeboard decisions
   listed above before changing the runtime lifecycle.
2. Implement the single-use scope lifecycle. Entry saves the previous
   terminator and installs a fresh work-only counter. Preserve a separate
   runtime host-lifetime reservation.
3. On exit, drain and fence the scope's work, not the global runtime. Restore
   the saved terminator after finalization, on both success and failure.
   Handle entry failures without leaving partial context. Scoped quiescence
   observes a zero event without leaving ACTIVE or publishing output.
4. Start the runtime lazily and keep it available after local scope exits.
   Global `wait()` joins root work and all active scope executions before
   teardown. Reject it from an explicit scope host body or any running
   behavior, including root-owned and pinned behaviors.
   If its timeout expires while any reservations remain, refuse root closure
   atomically with scope admission and raise `TimeoutError`
   without tearing down workers or the noticeboard.
5. Preserve worker module/bindings resolution: starting through `Scope`
   must resolve the user's module rather than accidentally exporting
   `behaviors.py`. Test file-based and supported interactive entry paths.
6. Expose a root scheduling handle without a host reservation or result slot.
   Synchronize shutdown with admission of root work and new top-level scopes.
   Apply the selected policy to subsequent runtime startup with retained handles.

**Gate:** empty scopes, sequential scopes, host-nested scopes, worker-nested
scopes, and exception paths restore the correct enclosing context. An inner
scope completes while unrelated outer work is still outstanding. Background
work assigned to root can continue after a library call returns;
global waiting includes that work and active scopes on other supported host
threads. Retained handles do not hang global completion. Invalid global waits
raise before disturbing either local or root accounting.

**Checkpoint:** ask the user to commit or stash the tested step before step 4.

### 4. Add scope-owned output cowns and scoped transfer

**Files:** [_core.c](src/bocpy/_core.c), [behaviors.py](src/bocpy/behaviors.py);
use existing native-value and C ABI tests for regression coverage.

1. Attach output ownership and closed-state metadata to the underlying cown,
   not merely to a Python wrapper. Preserve the owner through capsule transfer
   and serialization aliases.
2. Validate that scheduled users belong to the owning scope subtree. Enforce
   direct-access restrictions at native entry points while preserving internal
   scheduler acquire/release operations. Validate the resolved destination:
   root-owned background work must not use a local scope's output cown.
3. Add a private consuming transfer after scoped completion. Do not weaken the
   global safety check on ordinary `Cown.unwrap()` to implement it.
4. Materialize the result on the exiting interpreter, close the source cown,
   and publish `output_value` only after successful transfer. Preserve move-aware
   native backing-store ownership without promising Python object identity.
5. Implement Created, Active, Completed, and Failed access behavior, including
   failure cleanup and rejection of reuse or post-close scheduling. Failed
   refers to the scope's own body/exit/transfer, not a captured behavior
   exception. The scope output cown is separate from each behavior result cown.

**Gate:** scalar, `None`, mutable-container, and native `Matrix` results work;
transfer works inside a worker while unrelated behaviors remain active;
unauthorized scheduling and direct access through aliases fail; reconstruction
errors do not publish a value or leak native resources. A behavior that mutates
the output and then raises leaves that mutation intact: normal scope exit
publishes the output, while the exception remains on the behavior result cown.

**Checkpoint:** ask the user to commit or stash the tested step before step 5.

### 5. Complete scoped pinned execution and wakeups

**Files:** [_core.c](src/bocpy/_core.c),
[boc_terminator.c](src/bocpy/boc_terminator.c),
[behaviors.py](src/bocpy/behaviors.py).

1. Make main-interpreter scope exit pump pinned work while waiting for the
   selected terminator. Preserve the existing single-pumper safeguards.
2. Route pinned-queue notifications to the waiting main-thread path. Waking
   only a child terminator is insufficient when the main thread is waiting on
   its ancestor and must execute that child's pinned work.
3. Save/install/restore each pumped behavior's terminator even when pumping
   work unrelated to the scope being awaited.
4. Implement the selected nested-pump policy. Worker-side waits must not
   execute main-interpreter-only payloads.
5. Preserve the existing pinned exception and interrupt paths, including
   `BaseException` handling. Change their completion owner, not their exception
   policy. Retain cleanup, terminator lifetime, and context-restoration
   guarantees even if a pump or wait exits exceptionally.

**Gate:** main-thread joins make pinned progress; a worker-created child with
pinned work completes while the main thread pumps its ancestor; unrelated
pumped work does not contaminate scope context. Existing pinned exception
tests retain their expected outcomes; no new aggregate-raising path is added.

**Checkpoint:** ask the user to commit or stash the tested step before step 6.

### 6. Verify existing exception semantics across scopes

**Files:** [_core.c](src/bocpy/_core.c),
[worker.py](src/bocpy/worker.py), [behaviors.py](src/bocpy/behaviors.py), and
the existing exception tests plus new scope regression tests.

1. Retain the existing execution path that stores a raised exception in the
   behavior's result cown and sets its exception flag. No per-scope failure
   collection or new cross-interpreter exception representation is required.
2. Verify that release/unlink and exact-once terminator completion still happen
   after a behavior failure. Input cowns remain usable, later behaviors run,
   and mutations made before failure are preserved.
3. Keep local joins and global `wait()` independent of behavior-result
   inspection. Multiple failing behaviors retain separate result cowns rather
   than causing a grouped exception at either join.
4. If the scope's host body raises, finish its join and cleanup, restore the
   prior terminator, and propagate the original exception. Use ordinary Python
   chaining for an additional cleanup/transfer exception. Do not mark ancestors
   as failed after an exception has been caught.
5. Preserve existing behavior-result inspection and `unwrap()` constraints.
   Scope output transfer must not make ordinary behavior-result extraction
   legal while unrelated runtime work remains active.
6. Exercise the existing worker, pinned, and drain `BaseException` paths
   without introducing new suppression or aggregation. Unexpected fatal
   runtime paths remain runtime failures, not successful scope completion.

**Gate:** ordinary and root-directed behavior failures remain on their own
result cowns; neither scope exit nor global `wait()` re-raises them merely
because they occurred. Test multiple failures, partial mutations, subsequent
users of the same cowns, host-body propagation, transfer errors, and context
restoration. Existing exception tests still pass.

**Checkpoint:** ask the user to commit or stash the tested step before step 7.

### 7. Publish scope selection and integrate existing usage

**Files:** [__init__.py](src/bocpy/__init__.py),
[__init__.pyi](src/bocpy/__init__.pyi), [behaviors.py](src/bocpy/behaviors.py),
[test/](test/), examples, and directly affected documentation.

1. Export and type `Scope[T]`, `output_cown`, read-only `output_value`, and the
   `root_scope` handle. Add `scope=` selection to `when` and `whencall` without
   exposing the native terminator.
2. Preserve automatic startup and root fallback for decorated, aliased,
   zero-cown, lower-level, and native scheduling routes. Check invalid
   destinations before publishing any work.
3. Retain global `wait()` and document its runtime-wide join and invalid-call
   guards. Keep root lifecycle tests rather than replacing them all with
   explicit scope tests.
4. Add library examples that accept a background destination, defaulting to
   root, while returning results from a local scope. Update existing examples
   where local joins are useful; preserve ordinary root scheduling usage.
5. Resolve migration of ordinary result-cown extraction, statistics, and
   noticeboard usage rather than silently deleting those capabilities.
6. Validate the design example as an executable example once the API exists.
   Provide enough workers through the approved configuration mechanism.

**Gate:** existing top-level scheduling/global waiting and new local scopes
both work; explicit background ownership allows library calls to return before
maintenance finishes; a caller can substitute an enclosing scope for root.
Exports, stubs, documentation, and runtime behavior agree. All focused and full
regression suites pass relative to the recorded baseline.

**Checkpoint:** ask the user to commit or stash the tested step.

## Validation strategy

Use [test_boc.py](test/test_boc.py),
[test_pinned_pump.py](test/test_pinned_pump.py),
[test_stop_retry_composition.py](test/test_stop_retry_composition.py), the
scheduler integration/stress suites, and native-value/C ABI coverage alongside
the new scope tests.

- Use cown dependencies and behavior chains for deterministic ordering.
  Use messages at the test boundary where observation is needed; avoid sleeps
  and timing-only assertions.
- Test two independent scopes and worker reuse to detect context leakage.
- Test all three ownership-resolution paths and a library call whose
  background work is held pending until after return. Use deterministic test
  coordination rather than assuming maintenance runs slowly.
- Verify global waits include root work and explicit scope executions, and
  reject self-waits from scope hosts, ordinary behaviors, and pinned behaviors.
- Retain handles across completion and runtime restart to catch accidental
  lifetime-based holds. Verify invalid destination scheduling leaves no work
  or reservations behind.
- Exercise registration versus host closure repeatedly, with reproducible
  generated workloads and existing fault-injection facilities where available.
- Verify that counts balance and resources are released after failures, not
  merely that successful results are correct.
- Verify behavior-result exception flags and explicit re-raising through
  existing APIs. A scope output may contain partial results after a behavior
  failure; joins must not add aggregation, poisoning, or rollback.
- Keep deliberate deadlock cases out of the ordinary in-process suite. Any
  such test must run in an isolated subprocess with a hard deadline and cleanup.
- Include supported conventional and free-threaded Python builds, Windows
  portability checks, existing linting, and documentation tests if present.
- Compare existing scheduling benchmarks before and after the change for
  zero-cown and contended-cown workloads. Report overhead; agree an acceptable
  regression threshold with the author rather than inventing one.

## Completion criteria

The work is complete when root scheduling and explicit local scopes coexist,
all implementation decisions above are resolved, and:

- Nested scopes return the right results and restore the enclosing terminator.
- Every scheduling route resolves explicit scope, active scope, then root.
- Root-owned background work can outlive a library call and is included in
  global waiting; retained handles do not prevent completion.
- Scoped completion cannot race publication or allow early result transfer.
- Existing behavior-result exception semantics are preserved; scope joins do
  not add automatic failure aggregation. Pinned progress and cleanup match the
  design.
- Runtime-wide joining remains correct without substituting global completion
  for local scope completion.
- Tests, examples, stubs, documentation, and the chosen migration policy agree.

This document records the implementation sequence and its validation results.
The remaining local verification limitation is the absence of a free-threaded
interpreter; platform-specific behavior still relies on the corresponding CI
jobs. Further design issues should be surfaced to the author rather than
resolved by adding unapproved mechanisms.
