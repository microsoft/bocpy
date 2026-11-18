.. _scoped-concurrency:

Scoped Concurrency
==================

.. py:currentmodule:: bocpy

Use :class:`Scope` when a synchronous function must wait for its own
behaviors without waiting for unrelated application work. A scope joins its
behaviors and their nested scopes, then returns control to the synchronous
caller. It shares the existing worker pool and does not shut down the runtime.
The application still calls :func:`wait` for final global cleanup.

Returning a Local Result
------------------------

``Scope(initial=None)`` creates a single-use context manager with an output
cown, ``output_cown``. Behaviors acquire that cown through :func:`when` and
read or mutate the acquired parameter's ``.value``, just like any other cown.
After normal exit, the host reads the transferred value through ``output_value``.

.. code-block:: python

    from bocpy import Scope, wait, when


    def sum_squares(values):
        with Scope(0) as local:
            for value in values:
                @when(local.output_cown)
                def add_square(total, value=value):
                    total.value += value * value

        return local.output_value


    print(sum_squares([1, 2, 3, 4]))  # 30
    wait()

Entering a scope starts the runtime lazily if necessary. To choose the pool
size or bindings module, call :func:`start` before entering the first scope;
for example, ``start(4)`` creates four workers. Nested scopes reuse that pool.

``output_value`` is a read-only property, but its value may be mutable and belongs
to the host after transfer. It remains available while unrelated work runs.
This is not an ordinary :meth:`Cown.unwrap`: unwrapping an ordinary cown still
requires global quiescence, not merely completion of its producer's scope.
The global completion count must still be zero when unwrapping, with the usual
host-access protections; an earlier quiescence event is not enough if
another producer has since submitted work.

Processing More Than One Batch
------------------------------

Call ``scope.quiesce(timeout=None)`` inside the host body to observe a local
zero-work point without exiting. It returns ``None`` immediately if no work
is outstanding, or waits until the scoped work count next reaches zero.
The scope and workers remain live, so the host can schedule another batch:

.. code-block:: python

    with Scope(0) as local:
        @when(local.output_cown)
        def first_batch(total):
            total.value += 10

        local.quiesce()

        @when(local.output_cown)
        def second_batch(total):
            total.value += 20

        local.quiesce()

    print(local.output_value)  # 30; published only by scope exit

Quiescence is an observed event, not a promise that the count is still zero
on return: concurrent producers can already have submitted more work.
It does not close the scope, transfer its output, or permit host access to
``output_cown``. ``output_value`` remains unavailable until normal exit.

Only the original entering host may call ``quiesce`` while the scope is
``ACTIVE`` and is that host's current scope. A behavior cannot wait on its own
scope or an ancestor that accounts for it. A host inside a nested scope cannot
quiesce its enclosing scope. A pinned behavior cannot enter, quiesce, or join
any scope, even if its pending work is worker-only. A worker may quiesce a new
nested scope it entered, subject to the same blocking hazards as nested scope exit.

A finite nonnegative timeout raises ``TimeoutError`` if no zero is observed
before the deadline; the scope remains active for retry. Only ``None`` waits
indefinitely. Negative values, infinities, and NaN raise ``ValueError``.
Scope exit itself has no public timeout.

Choosing Which Join Owns Work
-----------------------------

Both ``@when(*cowns, scope=destination)`` and
``whencall(func, args, captures, *, scope=destination)`` select ownership in
this order:

1. An explicit ``scope=`` destination, if supplied.
2. Otherwise, the currently active scope.
3. Otherwise, :data:`root_scope`, the runtime's default scheduling destination.

``scope`` is keyword-only and is not passed as an argument to the behavior.
Omit it to inherit; passing ``None`` is not an alternative spelling.
A behavior runs with its selected scope active, so work it schedules inherits
that same owner. It can instead enter a nested scope or choose another
destination explicitly. A behavior whose request set contains a pinned cown
must select :data:`root_scope`; it cannot be owned by a local scope. Its
non-pinned descendants may explicitly select a local scope.

Root-owned work can outlive a library call:

.. code-block:: python

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

The local join includes ``update``, but not ``tidy``. A caller can supply an
open enclosing scope as ``background`` to choose a shorter lifetime.
Destination selection does not bypass cown dependencies: unrelated work that
holds a cown needed locally can still delay local completion.

A scope handle can be captured in a behavior's default-valued parameters and
used on a worker as a scheduling destination:

.. code-block:: python

    with Scope([]) as outer:
        @when()
        def dispatch(destination=outer):
            @when(destination.output_cown, scope=destination)
            def append_result(output):
                output.value.append("done")

    print(outer.output_value)  # ["done"]

Transferred handles are scheduling-only views: they cannot enter, exit, or
quiesce the original context manager or read its host-side ``output_value``.
Those operations belong to the original host thread and interpreter. The root handle
has neither a local output nor a context-manager interface. Retaining
``root_scope`` or a completed scope does not keep global joins waiting.
The same root handle remains usable after runtime restart.

Scope handles may cross bocpy-managed message, capture, and noticeboard
boundaries. Standalone Python ``pickle.dumps`` is rejected:
ordinary pickle bytes have no durable receiving transfer object that can own
the native reference for exactly the correct lifetime.

Output Ownership and Errors
---------------------------

The scope output cown is separate from the result cown returned by each
``@when``. Only behaviors assigned to the output's owning scope or its nested
scopes may schedule against it. Root-owned, sibling, and other unrelated
behaviors cannot acquire that output, even if scheduled from its host body.

Direct host access to ``output_cown.value``, ``acquire()``, context-manager
entry, and ``unwrap()`` raises ``RuntimeError``. Scheduled behaviors use their
acquired parameter's ``.value`` instead. Scheduling before scope entry is
invalid. Exit permanently closes the output cown, after which neither direct
access nor scheduling is allowed.

A scope cannot be entered twice. Before normal exit, reading ``output_value``
raises ``RuntimeError``. If the host body raises, scope exit still joins its
work and closes the output, but does not publish a result. The host exception
propagates. If exit or transfer fails, ordinary Python exception propagation
and chaining apply. Reading ``output_value`` on a failed scope raises its failure.

If pumping during scope exit is interrupted, scoped cleanup defers propagating
that interruption until the remaining local work has drained and the output
cown can be closed safely. The previously active scope is restored before
control returns or the exception propagates. This drain can wait indefinitely
under the blocking hazards described below. It adds no cancellation or
exception grouping and does not change the existing behavior of global
``wait`` or ``pump``.

A captured **behavior exception** is different: it stays on that behavior's
own result cown. Input cowns are not poisoned, mutations are not rolled back,
and normal scope exit still publishes the output, which can contain partial
results. Neither scope exit nor global :func:`wait` scans result cowns,
aggregates exceptions, or automatically re-raises captured failures. Retain
the behavior's result cown to inspect it using the existing access rules;
``unwrap()`` still requires global quiescence.

Local and Global Completion
---------------------------

Scope exit waits for the host's scheduled behaviors, including work they add
while the join is waiting. Nested scopes are included because their creating
execution cannot finish until they exit. Work explicitly assigned outside
that subtree is excluded.

Local joins do not fence asynchronous noticeboard mutations and do not return
statistics or noticeboard snapshots. Use the existing global
``quiesce(stats=..., noticeboard=...)`` or
``wait(stats=..., noticeboard=...)`` options for those checkpoints.
Global ``quiesce`` observes global completion count zero and leaves the
runtime running. This count includes each open scope's lifetime reservation,
so even an idle scope on another host delays quiescence and can cause a
timeout. ``Scope.quiesce`` instead observes only that scope's work, allowing
repeated batches while it remains open. Global ``wait`` atomically closes
admission at current global count zero before teardown. Snapshot options are sampled
after the observed quiescence; they do not guarantee that the global count is
still zero on return when other producers are active.

If the work-drain phase of global ``wait(timeout=...)`` or
``Behaviors.stop(timeout=...)`` times out, it raises ``TimeoutError`` and
leaves the runtime running and admission open for retry. This applies to
root-only work as well as open scopes. Snapshot flags do not suppress this
timeout. A later noticeboard-shutdown timeout retains its existing
``RuntimeError`` and retry behavior; workers may already be stopped.
Scope exit has no timeout argument.

Global ``wait``, ``quiesce``, and runtime ``stop`` reject calls from any
running behavior or active local scope host body with ``RuntimeError``.
Libraries should use ``scope.quiesce()`` or scope exit and leave global
lifecycle operations to the application.

Internal Lifecycle
------------------

The scope owner uses one shared atomic state machine::

    CREATED -> ENTERING -> ACTIVE -> DRAINING -> EXTRACTING -> CLOSED

Entry claims ``ENTERING`` with compare-and-exchange so only one thread can
enter. Entry reserves one ordinary unit of the root completion count and
retains that root instance on the scope owner. The host initializes its
identity and parent before publishing ``ACTIVE``.

Root and scope terminators share the same fields: ``count``, the zero-transition
generation ``zero_count``, a per-instance mutex and condition variable,
reference count ``refs``, admission flag ``open``, and immutable role flag
``is_root``. The role flag is not a second root pointer. The scope owner holds
the root reference needed to release its lifetime reservation.
There is no separate host counter. A scope's own count tracks only scheduled
work; the root count tracks all scheduled work and open scope lifetime
reservations. Both counts initially start at zero.

Transitioning to ``DRAINING`` ends the host body without decrementing work.
Behaviors retain scheduled access to ``output_cown`` during both ``ACTIVE`` and
``DRAINING``. Exit waits on a condition variable, pumping pinned work where
permitted, until every behavior has released its cowns and reservations.

Work can be admitted from zero while the scope is ``ACTIVE`` or ``DRAINING``.
An event waiter records ``zero_count`` under the counter mutex and succeeds
on initial zero or a changed generation, even if newer work is active.
Only a positive-to-zero count transition advances this generation.
Scope exit instead needs current zero: under the same admission fence, it
claims ``DRAINING -> EXTRACTING`` and prevents further reservations before
acquiring the output cown. This also rejects reentrant host operations during
reconstruction or destruction. After extraction or cleanup,
the host restores the enclosing context and publishes ``CLOSED``, then releases
its root lifetime reservation and root reference. Output
values and exit exceptions are stored separately from this lifecycle state.

Pinned enqueue wakes registered waiters in the main interpreter as a separate
wake reason.
It does not increment ``zero_count`` or broadcast completion to unrelated
counters. Waiters retain their original generation while pumping.

The wait-and-pump loop runs in C. One native waiter lives on the calling
stack throughout the operation, preserving its completion observation across
pinned batches and signal checks. Blocking releases the GIL; pinned Python
behaviors run with it held. Python handles argument validation and public
lifecycle behavior, not a second polling loop or a Python waiter object.
One native exit operation owns draining, output finalization, and restoration
of the host context and reservations. The shared native pump takes numeric
limits directly; only explicit Python ``pump()`` calls construct pump statistics.
Full runtime shutdown retires the root terminator only after all reservations
have drained; the next startup creates
a fresh one. Retained ``root_scope`` handles do not pin a retired root, while
native waiters keep references to the counter they are observing.

These are implementation states, not additional public scope operations.

Blocking and Pinned-Work Hazards
--------------------------------

Cown acquisition remains deadlock-free, but synchronously joining a nested
scope can introduce dependencies that the scheduler cannot resolve:

* A waiting parent behavior keeps its cowns. A child requiring one of those
  cowns cannot run until the parent returns, so joining that child deadlocks.
* A waiting behavior occupies a worker. If all workers wait for descendants,
  none can run those descendants. Size the pool for the nesting pattern and
  leave spare workers; merely using two workers does not make arbitrary
  nesting safe.

For :ref:`pinned-cowns`, scope exit and ``scope.quiesce()`` from a thread in
the main interpreter pump ready pinned behaviors when it is not already inside
a pinned behavior. A worker-side join cannot pump the main-interpreter queue
and needs a thread in the main interpreter to pump concurrently if its
descendants use pinned cowns.
On free-threaded builds, automatic joins wait when another host thread owns
the pump, then retry if work remains. Explicit concurrent ``pump()`` calls
still raise ``RuntimeError``.

Nested pumping in the main interpreter remains prohibited. Scope entry, quiescence, and
exit/join are rejected while a pinned behavior is executing, before scope
state or completion reservations change. Pinned behaviors may still schedule
work into inherited, explicit, or :data:`root_scope` destinations. Scopes do
not release parent cowns or add worker-helping or reentrant pumping to avoid
these hazards.

For a complete runnable example of local output and root-owned background
work, see
`examples/scopes.py <https://github.com/microsoft/bocpy/blob/main/examples/scopes.py>`_.
