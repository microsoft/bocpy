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
                      receive, send, wait, when)


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
               # over a message channel (the noticeboard is torn down by
               # wait(), so it cannot carry the result back to main).
               notice_write("done", True)
               send("answer", result)
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

   # Collect the first final answer produced by any worker, then drain.
   answer = receive("answer")[1]
   wait()
   print("final answer:", answer)
   # After wait(), the noticeboard is torn down; "partials" is no longer
   # readable. Snapshot it from inside a behavior before wait() returns
   # if you need to inspect it.

**Key points:**

- ``notice_write("done", True)`` is non-blocking — the worker doesn't wait
  for the write to commit.
- Other workers poll ``notice_read("done", False)`` at the start of each
  batch. They will *eventually* see the flag and stop.
- ``notice_update("partials", append_result, ...)`` shows the read-modify-
  write pattern: ``append_result`` is run atomically against the current
  list, so concurrent appends from different workers don't lose entries.
- The final answer is delivered over the message queue rather than the
  noticeboard, because :func:`wait` tears the noticeboard down before
  control returns to the main thread.
- The pattern is cooperative: there is no hard cancellation. Workers stop
  at the next polling point.

Reading the Noticeboard
-----------------------

From inside a behavior, call :func:`noticeboard` to get a read-only mapping
of all entries, or :func:`notice_read` for a single key::

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

The snapshot is taken once per behavior and cached — multiple calls to
:func:`noticeboard` or :func:`notice_read` within the same behavior return
data from the same point in time.

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

``notice_sync`` (Testing Only)
-------------------------------

:func:`notice_sync` blocks until every mutation the calling thread has
posted so far has been committed by the noticeboard thread. It exists to
make the noticeboard's eventual consistency tractable for **tests** — a
test can write a value, call ``notice_sync()``, and then assert that a
subsequently scheduled behavior observes the write — not as a primitive
for application code.

.. warning::

   Outside of tests, reaching for ``notice_sync`` is almost always an
   anti-pattern. The guarantee it provides is much weaker than it looks:

   - It only orders the **calling thread's prior writes** against the
     **next per-behavior snapshot** taken on any thread. Snapshots are
     captured once per behavior, so a behavior already executing when
     ``notice_sync`` returns will keep seeing its existing snapshot.
   - It does **not** refresh the calling behavior's own snapshot — you
     cannot ``notice_sync`` and then ``notice_read`` to see your write.
   - It establishes no happens-before relationship between unrelated
     behaviors and is not a substitute for cown-mediated ordering.

   If application code needs read-your-writes ordering, model the shared
   state as a :class:`Cown`. If you find yourself wanting
   ``notice_sync`` outside a test, that is a strong signal the noticeboard
   is the wrong primitive for the problem.


API Reference
-------------

.. autofunction:: notice_write
   :no-index:
.. autofunction:: notice_update
   :no-index:
.. autofunction:: notice_delete
   :no-index:
.. autofunction:: noticeboard
   :no-index:
.. autofunction:: notice_read
   :no-index:
.. autofunction:: notice_sync
   :no-index:
.. autodata:: REMOVED
   :no-index:
