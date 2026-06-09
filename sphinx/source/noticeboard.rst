.. _noticeboard:

Noticeboard
===========

.. module:: bocpy
   :noindex:

The **noticeboard** is a global key-value store (up to 64 entries) designed for
cross-behavior data sharing that does not warrant a dedicated :class:`Cown`.
It is eventually consistent: writes are fire-and-forget, and readers see a
snapshot that may lag behind the latest committed state.

When to Use the Noticeboard
---------------------------

Use the noticeboard when:

- Multiple behaviors need to observe shared configuration or summary data
  without taking exclusive ownership.
- You want to broadcast a value (*e.g.,* "stop" flag, running totals, discovered
  results) that many independent behaviors can poll.
- The data does not need strict read-your-writes ordering between behaviors.

If you need strict sequencing or exclusive access, use a :class:`Cown` instead.

Consistency Model
-----------------

All mutations (``notice_write``, ``notice_update``, ``notice_delete``) are
serialized through a dedicated **noticeboard thread**. The calling behavior (or
thread) hands off the mutation and returns immediately — this is the
"fire-and-forget" property.

Readers call :func:`noticeboard` or :func:`notice_read` to take a **snapshot**
that is cached for the lifetime of the behavior. The snapshot is consistent
(all entries come from the same committed version), but may not reflect writes
that were posted after the snapshot was taken — or even writes posted *before*
it, if the noticeboard thread has not yet committed them.

.. important::

   The noticeboard is **not** a synchronization channel. Do not rely on a
   subsequent behavior seeing a prior behavior's write just because the two
   are chained through a cown. If you need read-your-writes ordering, model
   the shared state as a :class:`Cown` instead.

Worked Example: Early Termination
----------------------------------

The
`prime_factor example <https://github.com/microsoft/bocpy/tree/main/examples/prime_factor.py>`_
uses the noticeboard to coordinate early termination across parallel worker
behaviors. A simplified version of the pattern:

.. note::

   ``expensive_computation``, ``is_final_answer`` and ``get_work`` below are
   placeholders for application logic; substitute your own when adapting
   the example.

.. code-block:: python

   from functools import partial
   from bocpy import (Cown, notice_read, notice_update, notice_write,
                      wait, when)


   def append_result(existing, new_item):
       """Append new_item to the shared partials list (used with notice_update)."""
       return existing + [new_item]


   class WorkerState:
       def __init__(self, worker_id, items):
           self.worker_id = worker_id
           self.items = items


   def process_batch(state: Cown[WorkerState]):
       @when(state)
       def _(state):
           # Check if another worker already signalled completion.
           if notice_read("done", False):
               return

           item = state.value.items.pop(0)
           result = expensive_computation(item)

           if is_final_answer(result):
               # Signal all other workers to stop and publish the answer
               # to the noticeboard. wait(noticeboard=True) will lift the
               # final state back across runtime shutdown for us.
               notice_write("done", True)
               notice_write("answer", result)
           else:
               # Record the partial result for diagnostics, then continue.
               notice_update("partials", partial(append_result, new_item=result),
                             default=[])
               if state.value.items:
                   process_batch(state)


   # Launch parallel workers.
   workers = [Cown(WorkerState(i, get_work(i))) for i in range(4)]
   for w in workers:
       process_batch(w)

   # Drain the runtime and lift the final noticeboard state back. See
   # "Reading the Final State at Shutdown" below for the API contract.
   final = wait(noticeboard=True)
   print("final answer:", final.get("answer"))
   print("partials:    ", final.get("partials", []))

**Key points:**

- ``notice_write("done", True)`` is non-blocking — the worker doesn't wait
  for the write to commit.
- Other workers poll ``notice_read("done", False)`` at the start of each
  batch. They will *eventually* see the flag and stop.
- ``notice_update("partials", append_result, ...)`` shows the read-modify-
  write pattern: ``append_result`` is run atomically against the current
  list, so concurrent appends from different workers don't lose entries.
- The final answer is lifted back through :func:`wait` with
  ``noticeboard=True`` — :func:`wait` snapshots the noticeboard between
  joining the noticeboard thread and clearing the C-side entries, so the
  caller gets a plain ``dict`` of the last committed state.
- The pattern is cooperative: there is no hard cancellation. Workers stop
  at the next polling point.

Reading the Final State at Shutdown
------------------------------------

The noticeboard is torn down at the end of :func:`wait`: the dedicated
mutator thread is joined and the C-side entries are freed before control
returns to the main thread. To carry data back across that boundary, pass
``noticeboard=True``::

    from bocpy import wait

    snap = wait(noticeboard=True)        # plain dict[str, Any]
    print(snap.get("answer"))

The snapshot is taken on the main thread between joining the noticeboard
thread and clearing the entries, so every mutation enqueued by a behavior
that completed before :func:`wait` returns is visible.

The combined form returns a :class:`WaitResult` ``NamedTuple`` carrying
the scheduler-stats snapshot alongside the noticeboard::

    result = wait(stats=True, noticeboard=True)
    print(result.noticeboard.get("answer"))
    print(result.stats[0]["popped_local"])

Edge cases:

- If the runtime was never started (or already torn down), the
  noticeboard snapshot is the empty dict ``{}`` rather than ``None``.
- If a key your code expects might not have been written before
  quiescence, use ``snap.get(key)`` (or check ``key in snap``) rather
  than indexing — :func:`wait` quiesces as soon as every behavior
  completes, with no guarantee any particular write happened.
- The returned dict is a plain mutable ``dict``; mutating it locally
  does not affect the (now-freed) noticeboard.

Reading the State Between Rounds
---------------------------------

When you want a noticeboard snapshot at a synchronization point
*without* tearing the runtime down — e.g. a parallel search that
inspects its best-so-far state between rounds and then keeps
working — use :func:`quiesce` with ``noticeboard=True``::

    from bocpy import quiesce

    snap = quiesce(noticeboard=True)  # plain dict[str, Any]
    print("best so far:", snap.get("best"))
    # ... next batch of @when calls runs immediately ...

:func:`quiesce` blocks until every in-flight behavior completes,
captures the snapshot the same way :func:`wait` does (by cycling
the dedicated mutator thread, which guarantees every prior
``notice_write`` / ``notice_update`` / ``notice_delete`` has been
committed before the read), and then leaves the workers and the
noticeboard thread running. The combined ``stats=True,
noticeboard=True`` form returns a :class:`WaitResult` just like
:func:`wait`.

Reading the Noticeboard
-----------------------

The noticeboard is a **behavior-scope read surface**. Inside a behavior,
call :func:`noticeboard` to get a read-only mapping of all entries, or
:func:`notice_read` for a single key::

    from bocpy import noticeboard, notice_read, when, Cown

    c = Cown(None)

    @when(c)
    def _(c):
        # Full snapshot
        snap = noticeboard()
        for key, value in snap.items():
            print(f"{key} = {value}")

        # Single key with a default
        threshold = notice_read("threshold", 0.5)

The snapshot is taken once per behavior and cached -- multiple calls to
:func:`noticeboard` or :func:`notice_read` within the same behavior return
data from the same point in time.

Cowns embedded in a noticeboard entry remain valid for the lifetime of
the entry; they survive as long as the entry has not been overwritten or
deleted, regardless of how many readers have observed the entry.

.. warning::

   Calling :func:`noticeboard` or :func:`notice_read` from the main
   thread *outside* a behavior is **undefined behavior**. The only
   supported ways to read the noticeboard from the main thread are
   :func:`wait` with ``noticeboard=True`` (see "Reading the Final
   State at Shutdown" above) and :func:`quiesce` with
   ``noticeboard=True`` (see "Reading the State Between Rounds"
   above). To install read-mostly configuration from the main thread
   *before* scheduling behaviors, use :func:`notice_seed` (see
   "Seeding Before Scheduling" below), which commits synchronously.

Writing and Updating
--------------------

:func:`notice_write` sets a key unconditionally::

    from bocpy import notice_write

    notice_write("config.max_retries", 3)
    notice_write("status", "running")

:func:`notice_update` performs an atomic read-modify-write. The function
``fn`` receives the current value (or ``default`` if the key is absent) and
returns the new value::

    from functools import partial
    from operator import add
    from bocpy import notice_update

    # Increment a counter
    notice_update("counter", partial(add, 1), default=0)

    # Append to a list
    def append_item(lst, item):
        return lst + [item]

    notice_update("results", partial(append_item, item="found!"), default=[])

.. warning::

   ``fn`` must be **picklable** — lambdas and closures are not.
   Use ``functools.partial`` with module-level functions, or ``operator``
   functions.

If ``fn`` returns :data:`REMOVED`, the entry is deleted::

    from bocpy import notice_update, REMOVED

    def clear_if_empty(value):
        return REMOVED if not value else value

    notice_update("buffer", clear_if_empty, default=[])

Deleting Entries
----------------

:func:`notice_delete` removes a single key (no-op if absent)::

    from bocpy import notice_delete

    notice_delete("temporary_flag")


Seeding Before Scheduling
-------------------------

:func:`notice_write` is fire-and-forget: it hands the write to the
noticeboard thread and returns before the value commits, so a behavior
scheduled immediately afterwards is *not* guaranteed to observe it. To
install read-mostly configuration on the main thread *before* scheduling
the behaviors that read it, use :func:`notice_seed`, which commits
synchronously under the noticeboard mutex and returns only once the entry
is live::

    from bocpy import notice_seed, notice_read, when, Cown

    notice_seed("config.threshold", 0.5)   # committed before it returns

    work = Cown(load_work())

    @when(work)
    def _(work):
        threshold = notice_read("config.threshold")   # always observes 0.5
        ...

:func:`notice_seed` may be called only from the primary interpreter — never
from inside a ``@when`` body (use :func:`notice_write` there). If the runtime
is not yet running it starts it, so seeding can be the first bocpy call a
program makes, with no explicit :func:`start`.

.. note::

   :func:`notice_seed` is a plain overwrite intended for one-shot seeding
   *before* concurrent noticeboard mutations are in flight. It does **not**
   provide the read-modify-write atomicity of :func:`notice_update`, and a
   seed that races an in-flight :func:`notice_update` on the same key may be
   lost. Seed once, up front, rather than interleaving seeds with concurrent
   updates.


API Reference
-------------

.. autofunction:: notice_write
   :no-index:
.. autofunction:: notice_seed
   :no-index:
.. autofunction:: notice_update
   :no-index:
.. autofunction:: notice_delete
   :no-index:
.. autofunction:: noticeboard
   :no-index:
.. autofunction:: notice_read
   :no-index:
.. autodata:: REMOVED
   :no-index:
