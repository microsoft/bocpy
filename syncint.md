# Synchronous integration with BOC scopes

The key idea in this work is to provide synchronous integration of cowns into
bocpy through a context-managed BOC scope. A scope creates a boundary between
behavior-oriented concurrency (BOC) and ordinary synchronous Python code.

This is a proposed extension to bocpy. `Scope` groups work that must finish
before a context manager returns. Work outside a context manager belongs to
the runtime's implicit root scope and can be joined with global `wait()`.
Every behavior has an owner, but not every owner needs an explicit `with`
block. A scope can also observe a zero-work point between batches without
exiting; only exit transfers its final output to synchronous code.

## Proposed interface

`Scope[T](initial)` constructs a single-use scope whose `output_cown` holds
an initial value of type `T`. Entering the scope returns the same `Scope`
object and makes it active. The **host body** is the synchronous block or
worker behavior executing that `with` statement. Behaviors scheduled by the
host body, or by behaviors executing with this scope as their active dynamic
context, belong to it unless they explicitly select another destination.

Scheduling selects an owner in this order:

1. The explicit `scope=` destination, if supplied.
2. Otherwise, the currently active scope.
3. Otherwise, the runtime's implicit root scope.

The same rule applies to `@when` and lower-level scheduling. A behavior runs
with its assigned scope active, so further behaviors inherit that owner unless
they select another destination or enter a nested `Scope`. Explicit
destinations must accept new work; scheduling against a completed scope raises
`RuntimeError` rather than silently falling back to the root.

Exiting the scope waits for those behaviors and, transitively, for every
nested scope they create. A **scope subtree** consists of one scope and,
recursively, every scope dynamically entered while its host body or direct
behaviors execute. Work explicitly assigned outside that subtree is not part
of the local join. Once the subtree has no current work and admission is
closed, the context manager moves the value from `output_cown` to the read-only
`output_value` property, where
synchronous continuation code can access it.

```python
from bocpy import Scope, start, wait, when


start(2)
with Scope[list[str]]([]) as outer:
    @when(outer.output_cown)
    def produce_result(results):
        with Scope[list[str]]([]) as inner:
            @when(inner.output_cown)
            def produce_inner_result(inner_results):
                inner_results.value.append("inner")

        # The behavior resumes only after the nested scope has completed.
        results.value.extend(inner.output_value)

# The outer scope has completed; the continuation owns the final value.
print(outer.output_value)  # ["inner"]
wait()  # Drain the runtime and shut down its workers.
```

The active scope is determined by dynamic context. In the example,
`produce_result` belongs directly to `outer`. When that behavior runs, its
scheduling scope becomes active in the worker interpreter. The behavior may
therefore enter `inner`, and `produce_inner_result` belongs to that innermost
active scope. After `inner` completes, `outer` is again the behavior's active
scope. Any further behaviors it schedules belong to `outer`.

This association does not require the programmer to give scopes global names
or pass a scope argument for ordinary inherited work. Scope exit waits for
completion before restoring the previously active scope, including when the
body or a descendant fails. Leaving a top-level scope restores the default
root context, so another `@when` can schedule root-owned work without opening
a new context manager.

The example assumes at least two available workers: `produce_result` blocks
one worker while its nested behavior runs on another. Worker exhaustion is an
accepted blocking hazard, discussed below.

## Work that outlives a library call

The runtime exposes `root_scope`, a scheduling handle for its implicit root
scope. A library can accept a longer-lived destination, defaulting to this
handle, while using a local scope for work required before it returns:

```python
from bocpy import Scope, Cown, root_scope, wait, when


def update_index(index, *, background=root_scope):
    with Scope([]) as local:
        @when(index, local.output_cown)
        def update(data, results):
            data.value["version"] += 1
            results.value.append(data.value["version"])

        @when(index, scope=background)
        def tidy(data):
            data.value.pop("obsolete", None)

    return local.output_value


index = Cown({"version": 0, "obsolete": True})
print(update_index(index))  # [1]; tidying may still be running.
wait()
```

The local scope waits for `update`, not for `tidy`. The caller can supply an
open enclosing scope instead of `root_scope` to give background work a shorter
lifetime. Selecting a destination changes which join waits for the behavior;
exceptions still belong to that behavior's own result cown. The selection does
not bypass cown dependencies. Background work holding a cown required by local
work can still delay the local join indirectly.

`root_scope` is a runtime-owned scheduling handle, not a permanently entered
`Scope`. It has no local result slot, and retaining the handle alone must
not prevent global completion or runtime restart.

## Root lifetime and global waiting

The shared runtime starts lazily when needed and survives individual scope
exits so root-owned work can continue after library calls return. Global
`wait()` joins all outstanding runtime work, including active explicit scopes
and their descendants, before shutting down the workers. It is not merely a
wait for behaviors scheduled outside context managers.

Calling global `wait()` from an active `Scope` host body or from any running
behavior raises `RuntimeError`: that join could wait for its own caller.
Libraries use local scope exit, leaving global shutdown to the application.

Runtime shutdown must account for active scope executions, even when their
behavior counts are temporarily zero. This accounting follows execution
lifetime, not the lifetime of retained scope handles. Shutdown and admission
of new work must be synchronized so no accepted work is stranded.

If the work-drain phase of global `wait(timeout=...)` or
`Behaviors.stop(timeout=...)` times out, it raises `TimeoutError`, leaves the
runtime running, and leaves admission open for retry. This applies to
root-only work as well as open scopes. Snapshot options do not suppress this
timeout. An open scope retains its lifetime reservation through output
extraction or cleanup and restoration of the previous active scope, preventing
worker teardown during a scoped transfer. A later noticeboard-shutdown timeout
retains its existing `RuntimeError` and retry behavior; workers may already
be stopped. Scope exit has no timeout argument. Captured behavior exceptions remain on
their own result cowns, without rethrowing or grouping at global joins.

### Quiescence without closing

`scope.quiesce(timeout=None)` observes local work zero while leaving the scope
active and its worker pool running. If the work count is already zero it
returns immediately. Otherwise it waits for the next positive-to-zero
transition. New work can already be active when it returns: this is an
observation of completion, not an admission fence. The host may schedule
another batch afterward.

```python
with Scope(0) as local:
    @when(local.output_cown)
    def first_batch(total):
        total.value += 10

    local.quiesce()

    @when(local.output_cown)
    def second_batch(total):
        total.value += 20

    local.quiesce()

print(local.output_value)  # 30; only exit publishes the result.
```

Only the original entering host can quiesce its currently active scope.
The operation is unavailable on cross-interpreter scheduling-only handles.
A behavior cannot wait on its own scope or an ancestor that accounts for it;
a worker may quiesce a fresh nested scope that it has entered. The latter
still has the worker-exhaustion and cown-dependency hazards of nested exit.

The method returns `None` on success. A finite nonnegative timeout raises
`TimeoutError` if the wait expires, without closing the scope; retry is
permitted. Only `None` waits indefinitely. Negative values, infinities, and NaN
raise `ValueError`. Quiescence does not publish `output_value`, allow host
access to `output_cown`, or collect behavior exceptions.

Global `quiesce` applies the same zero-count observation to the root, including
all scheduled work and each open scope's lifetime reservation. An idle scope
on another host can therefore delay global quiescence or cause a timeout.
The scope's own count remains work-only, so `scope.quiesce()` can observe
repeated batches without exiting. Global `wait` and runtime `stop` instead
require current root count zero and atomically close admission before teardown.
Global operations remain unavailable inside behaviors or active local scope hosts.

Global statistics and noticeboard snapshot options remain available. They are
sampled after observed quiescence, not atomically with the zero-count transition;
concurrent producers may already have submitted more work. Ordinary
`Cown.unwrap()` still requires current global completion count zero and the existing
host-access protections. A quiescence event alone cannot authorize an unwrap
under concurrent scheduling.

## Nested-scope semantics

All scopes share the runtime's worker pool. Entering a scope ensures that the
runtime is available; nested scopes do not create separate pools. Completing
one scope does not tear down the shared pool or stop root-owned background
work. The application can drain and stop the runtime with global `wait()`.

A scope may exit only after both of the following conditions hold:

1. the execution that entered the scope has left its `with` body; and
2. every behavior scheduled directly in the scope has completed.

Structured scope nesting carries descendant completion transitively. A
synchronous caller cannot leave a child `with` statement until that child has
completed. Similarly, a behavior cannot complete while a nested scope in its
body is still active. Waiting for its direct behaviors therefore also waits
for every nested scope in those behaviors.

Waiting for an inner scope does not wait for unrelated work in its parent or
elsewhere in the process. Waiting for an outer scope includes descendant work
because the execution that created each descendant remains incomplete until
the descendant exits.

While scope exit waits, behaviors already scheduled in that scope may still
schedule further work in it or open nested scopes. Scope exit waits for this
work too, but not for work explicitly assigned outside its subtree. A
completed scope cannot accept new work.

Allowing a behavior to wait synchronously for a nested scope introduces
blocking hazards that ordinary BOC scheduling avoids. The parent behavior
continues to hold its cowns while it waits. The program can therefore deadlock
if a child requests one of those cowns, or if waiting behaviors occupy every
worker needed to run their descendants. This proposal accepts that risk rather
than temporarily releasing parent cowns or adding a helping scheduler.

Pinned work adds another progress requirement. Scope exit and quiescence on
the main interpreter pump ready pinned behaviors while waiting. Existing pinned
exception and interrupt handling is preserved; scopes do not add a
failure-collecting pump or grouped exceptions. A worker-side scope cannot pump
the main-interpreter queue. If its subtree contains pinned work, the main
interpreter must pump concurrently; otherwise the scoped wait may deadlock.
This proposal also accepts that risk.

## Scope output and behavior exceptions

There are two distinct cowns:

- The **behavior result cown**, returned by `@when`, holds that behavior's
  return value or captured exception, as in the existing library.
- The **scope output cown**, `scope.output_cown`, holds the value that behaviors
  explicitly store for the synchronous continuation. It is not an exception
  collector.

The scope output cown belongs to its scope subtree. The scheduler rejects an
attempt to schedule unrelated work over that cown. This ownership rule
guarantees that scoped quiescence accounts for every request that could access
the output. Validation uses the selected destination, not the context that
initiated scheduling. In particular, a root-owned background behavior cannot
acquire a local scope's output cown: that cown will be consumed when the local
call exits.

While an output cown is scope-owned, direct `acquire()`, context-manager entry,
`.value` access, and `unwrap()` are rejected with `RuntimeError` in every
interpreter. Behaviors access its payload only through scheduled exclusive
access. After the cown is closed by successful or failed scope exit, all
scheduling and direct-access operations on it raise `RuntimeError`. These
restrictions prevent synchronous access from bypassing the scope's completion
state. They do not change the access rules for ordinary behavior result cowns.

After current work reaches zero and exit fences admission, the context manager
uses a scoped-transfer operation to consume the payload from `output_cown` and materialize it in the
interpreter that exits the scope. This cannot use the current `Cown.unwrap()`,
which requires global quiescence. The operation does not retain an alias in
the cown. It may reconstruct ordinary Python values when crossing interpreter
boundaries, while move-aware native values may transfer their backing storage.

The observable states of an explicit `Scope` are:

- **Created:** the scope has not been entered. `output_value` is unavailable, and
  scheduling over `output_cown` raises `RuntimeError`.
- **Active:** `output_cown` is available only to the scope subtree. Reading
  `output_value` raises `RuntimeError` because no value has been published.
- **Completed:** the transfer closes `output_cown` and leaves its payload as
  `None`. `output_value` returns the transferred value. Completion does not imply
  that every behavior succeeded.
- **Failed:** the scope's own exit or transfer failed, or its host body raised.
  After safe cleanup, `output_cown` is closed without publication. Reading
  `output_value` raises the scope's failure. A captured behavior exception alone
  does not put the scope in this state.

Scopes are single-use. `__enter__` succeeds only in the **Created** state;
entering an active, completed, or failed scope raises `RuntimeError`.

### Preserve existing exception semantics

If a behavior raises, the existing execution path stores the exception in
that behavior's result cown and marks it as an exception. Cleanup releases
the acquired cowns and scheduling requests, then completes the behavior's
terminator reservation. Input cowns are not poisoned or blackholed, and
subsequent behaviors can run.

Mutations made before the exception are not rolled back. This includes any
writes already made to `scope.output_cown`. On normal scope exit, the output
is transferred even if one or more behaviors failed; it may therefore contain
partial results. The exception is not automatically written into the scope
output cown.

Neither scope exit nor global `wait()` scans behavior results, aggregates
exceptions, or re-raises captured behavior failures. A caller that needs to
observe a failure retains the behavior's result cown and uses the existing
inspection or `unwrap()` interface, subject to its existing access and global
quiescence requirements. Local scope completion alone does not relax those
requirements.

An exception raised directly by the `with` body is different: scope exit
still joins its work and performs cleanup, then lets the original exception
propagate. If result transfer or cleanup itself raises, it follows ordinary
Python exception propagation and chaining, not a new exception-group policy.
Previous terminator state must still be restored. Catching such an exception
does not leave ancestor scopes permanently marked as failed.

Root-owned background behaviors use the same result-cown failure path.
Assigning work to `root_scope` changes how long it may run, not how its
exceptions are reported. This work adds no scope-level failure records,
ancestor failure propagation, automatic cancellation, or new treatment of
`BaseException` in existing execution and cleanup paths.

## Implementation: terminator inheritance

The thread-local current `Terminator` is an implementation detail, not part of
the public interface. Each explicit scope owns a `Terminator` that tracks
scheduled work. The runtime also owns a root terminator that counts all runtime
work and one ordinary reservation for each open explicit scope.
Each thread holds a reference to its current terminator; an absent reference
selects the runtime root. The terminator itself can be shared by behaviors on
different workers; only the selection of the current terminator is
thread-local.

The shared scope owner has one atomic lifecycle state:

```text
CREATED -> ENTERING -> ACTIVE -> DRAINING -> EXTRACTING -> CLOSED
```

Entry claims `CREATED -> ENTERING` with compare-and-exchange, rejecting a
competing or repeated entry. It then reserves one unit of the ordinary root
count and retains that root instance on the scope owner. It saves the current
terminator, initializes the parent and host identity, and sets the
local work count to zero. Only then does it publish
`ACTIVE` and permit scheduling. If the runtime refuses admission, the
unpublished entry returns to `CREATED` without a reservation.

Root and scope terminators use the same `BOCTerminator` fields: `count`,
the zero-transition generation `zero_count`, a per-instance mutex and condition
variable, reference count `refs`, admission flag `open`, and immutable role
flag `is_root`. The role flag distinguishes root from explicit-scope counters;
it is not another root pointer. The scope owner retains the root reference
needed to release its lifetime reservation. There is no separate host counter
or host-count API. Root startup creates no enclosing lifetime reservation.
The scope's own count tracks only work, while the root count also includes
scope lifetime reservations. The shared lifecycle determines admission and
finalization; there is no duplicate scope lifecycle state.

An event waiter records `zero_count` under the counter mutex. It succeeds if
the count is initially zero or that generation later changes, even if newer work
has since made the count positive. Only a positive-to-zero count transition
advances `zero_count`; unrelated or spurious wakeups do not indicate completion.
Pinned enqueue signals registered main-thread waiters as a separate wake
reason, without broadcasting completion to unrelated counters. A waiter
retains its original generation across pumping and subsequent waits.

The native driver owns this waiter on its C call stack. It releases the GIL
while blocking, reacquires it for signal checks and bounded pinned execution,
and applies one overall deadline. Python performs argument normalization and
public lifecycle handling; no Python waiter wrapper or wait-and-pump loop is
needed. Scope draining defers interruptions until finalization is safe, while
ordinary quiescence propagates them without closing the scope.

Scheduling resolves an explicit scope handle first, then the current
terminator, then the root terminator. The new behavior stores the resolved
terminator in a field and reserves one unit of its completion count before
becoming visible to the scheduler. Public scope handles, including
`root_scope`, resolve to native owners; the `Terminator` itself remains private.

Before executing a behavior, a worker saves its previous current terminator
and installs the terminator from the behavior's field. Behaviors scheduled by
that execution therefore inherit the same terminator unless they specify
another destination or the execution enters a new `Scope`. On behavior
completion, the worker releases the reservation on the behavior's stored
terminator and restores its previous thread-local value,
including on failure. The same save/install/restore rule applies to pinned
behavior execution, so pumping unrelated work cannot change the waiting
scope's current terminator.

The host's `ACTIVE -> DRAINING` transition ends the host body without changing
the work count. The host waits on the counter's condition variable until
current work reaches zero, waking to pump pinned work where appropriate rather than busy-spinning.
Behaviors can still acquire `output_cown` and schedule further counted work
while the scope is `ACTIVE` or `DRAINING`.

Each behavior releases its cowns and requests before dropping its reservation.
The count can revive from zero during both `ACTIVE` and `DRAINING`. At current
zero, the host atomically fences further admission and claims
`DRAINING -> EXTRACTING` before acquiring the output cown, so reconstruction
and destructor callbacks cannot recursively finalize it. It extracts or
discards the payload, restores the saved terminator, publishes `CLOSED`, and
releases the ordinary root lifetime reservation and the retained root reference.
Extraction failure takes the same closing path.

The Python facade reads this native lifecycle rather than maintaining another
state machine. It separately retains the output value or failure needed by
`output_value`; successful completion and failed exit both end in `CLOSED`.
For a top-level scope, restoration returns to the root-default context.
Structured nesting already prevents a creator from completing before a nested
scope, so per-scope joins need no separate child counter. Runtime-wide joining
must additionally account for active top-level scopes and root-owned work;
their execution holds end at completion rather than at handle destruction.
Keeping `root_scope` or a completed `Scope` alive must not pin global waiting.
Full shutdown retires the root terminator only with no outstanding reservations,
and startup creates a fresh root.
A retained `root_scope` handle resolves the current runtime root rather than
pinning the retired one. Native event waiters retain references to their
observed counter until they finish, so a restart cannot redirect an old wait.

### Scheduling and completion races

Scheduling checks the published lifecycle and output-cown ownership, then
reserves work before publication. Admission and finalization share a fence:
if reservation wins, the final join includes that work; if finalization wins,
the reservation fails. Merely observing zero does not close admission.
Reservations may increment a zero count while the owner still accepts work.
Global shutdown similarly requires current root count zero, including scope
lifetime reservations, before atomically closing root admission.

Publication has one atomic commit point. Before that point, the registering
execution owns the reservation and releases it if capsule construction or
another pre-publication step fails. After that point, the published behavior
owns the reservation and releases it exactly once when execution completes.

These changes generalize the process-wide terminator into a root owner and
per-scope terminators. Global joining remains available, while local joining
uses only the selected scope's completion. The distributed scheduling algorithm
and shared worker pool are retained. Completion and scope output publication
become scope-aware; behavior exception semantics remain unchanged.
