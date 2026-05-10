.. _api:

API
===

.. module:: bocpy

This part of the documentation covers all the interfaces of `bocpy`.

Behaviors
---------

.. autoclass:: Cown
    :members:
    :undoc-members:

.. autodecorator:: when
.. autofunction:: wait
.. autofunction:: start

Cown Groups
^^^^^^^^^^^

In addition to passing individual cowns to ``@when``, you can pass a
**list of cowns** to acquire an entire group atomically. The list is
delivered to the behavior parameter as a ``list[Cown]``::

    from bocpy import Cown, when, wait

    items = [Cown(i) for i in range(5)]

    @when(items)
    def _(items):
        # `items` is a list[Cown] — all five acquired together
        total = sum(c.value for c in items)
        print("Sum:", total)

    wait()

You can mix individual cowns and groups freely::

    summary = Cown(0)
    items = [Cown(i) for i in range(5)]

    @when(summary, items)
    def _(summary, items):
        summary.value = sum(c.value for c in items)

Each argument to ``@when`` becomes one parameter of the decorated function:
a single :class:`Cown` is passed directly, while a list is delivered as a
``list[Cown]``.

Runtime Lifecycle
^^^^^^^^^^^^^^^^^

The bocpy runtime follows a simple lifecycle:

1. **Start** — the first ``@when`` call (or an explicit :func:`start`) spawns
   the worker sub-interpreters and the noticeboard thread.
2. **Schedule** — ``@when`` / :func:`whencall` schedules behaviors against
   cowns. Scheduling and release run on the caller and worker threads; there
   is no central scheduler thread.
3. **Wait** — :func:`wait` blocks until all scheduled behaviors complete, then
   tears down the runtime (joins workers, closes the noticeboard).
4. **Re-start** — after ``wait()`` returns, the next ``@when`` call spins up
   a fresh runtime. The noticeboard is cleared and worker statistics are
   reset; existing :class:`Cown` objects survive and can be scheduled
   against the new runtime.

.. autodata:: WORKER_COUNT


Advanced
^^^^^^^^

.. autofunction:: whencall


Noticeboard
-----------

See the :ref:`noticeboard` guide for a conceptual overview, consistency model,
and worked examples.

.. autofunction:: notice_write
.. autofunction:: notice_update
.. autofunction:: notice_delete
.. autofunction:: noticeboard
.. autofunction:: notice_read
.. autofunction:: notice_sync
.. autodata:: REMOVED


Math
----

.. autoclass:: Matrix
    :members:
    :undoc-members:
    :special-members: __init__


Messaging
---------

See the :ref:`messaging` guide for a conceptual overview, the selective-receive
pattern, timeouts, and a worked calculator example.

.. autofunction:: send
.. autofunction:: receive
.. autofunction:: set_tags
.. autofunction:: drain
.. autodata:: TIMEOUT


C ABI
-----

See :ref:`c-abi` for the full usage contract for downstream C extensions
that want to interoperate with bocpy at the C level.

.. autofunction:: get_include
.. autofunction:: get_sources
