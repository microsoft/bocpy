.. _api:

API
===

.. module:: bocpy

This part of the documentation covers all the interfaces of `bocpy`.

.. autodata:: __version__

Behaviors
---------

.. autoclass:: Cown
    :members:
    :undoc-members:

.. autodecorator:: when
.. autofunction:: wait
.. autoclass:: WaitResult
    :members:
.. autofunction:: quiesce
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
   For a non-tearing-down checkpoint (e.g. parallel-search inspection
   between rounds), use :func:`quiesce` instead — it blocks until
   the runtime is quiescent, returns optional ``stats`` /
   ``noticeboard`` snapshots, and leaves workers and the noticeboard
   thread running so further ``@when`` calls work immediately.
4. **Re-start** — after ``wait()`` returns, the next ``@when`` call spins up
   a fresh runtime. The noticeboard is cleared and worker statistics are
   reset; existing :class:`Cown` objects survive and can be scheduled
   against the new runtime.

.. autodata:: WORKER_COUNT


Advanced
^^^^^^^^

.. autofunction:: whencall


Pinned Cowns
------------

See :ref:`pinned-cowns` for the conceptual overview, the
coarse-grained dispatch pattern, event-loop integration recipes,
and the free-threaded support trajectory.

.. autoclass:: PinnedCown
    :members:
    :undoc-members:

.. autofunction:: pump
.. autoclass:: PumpResult
    :members:
.. autofunction:: set_pump_watchdog
.. autofunction:: set_wait_pump_poll


Noticeboard
-----------

See the :ref:`noticeboard` guide for a conceptual overview, consistency model,
and worked examples.

.. autofunction:: notice_write
.. autofunction:: notice_seed
.. autofunction:: notice_update
.. autofunction:: notice_delete
.. autofunction:: noticeboard
.. autofunction:: notice_read
.. autodata:: REMOVED


Math
----

.. autoclass:: Matrix
    :members:
    :undoc-members:
    :special-members: __init__, __eq__, __ne__, __lt__, __le__, __gt__, __ge__, __getitem__, __setitem__


Matrix views
^^^^^^^^^^^^

:meth:`Matrix.row_view`, :meth:`Matrix.column_view`, and the ``m.view[...]``
accessor return a :class:`MatrixView` — a read/write, 1-D window that
**shares** the matrix's storage instead of copying it. This is the
borrow-by-view counterpart to plain subscripting (``m[i]`` / ``m[i, j]`` /
``m[rows, cols]``), which always **copies**::

    m = Matrix(3, 4, [float(i) for i in range(12)])

    row = m.row_view(1)      # borrow: aliases m's storage
    row[0] = 99.0            # writes THROUGH to m[1, 0]
    col = m.view[:, 2]       # slice-notation borrow of column 2
    snap = m[1]              # copy: an independent 1 x 4 Matrix

Key semantics:

* **Aliasing / write-through.** Reading or writing a view element touches the
  source matrix directly (``view[i] = x``, ``view[:] = scalar``,
  ``view[:] = sequence``). Sub-views (``view[1:3]``) and the free transpose
  (``view.T``, an O(1) orientation flip) alias the same storage.
* **Copy vs borrow.** ``m[...]`` copies; ``m.row_view`` / ``m.column_view`` /
  ``m.view[...]`` borrow. Use :meth:`MatrixView.copy` (or
  :meth:`Matrix.from_view`) to materialise an independent owned
  :class:`Matrix`.
* **Iterating views.** :meth:`Matrix.row_views` / :meth:`Matrix.column_views`
  yield a fresh borrow over each row / column in turn — a cleaner spelling of
  ``for i in range(m.rows): m.row_view(i)``. Each yielded view is a distinct
  object (never a reused cursor), so ``list(m.row_views())`` behaves like any
  other iterator. Plain ``for row in m`` still **copies** each row.
* **Whole-buffer lifetime pin.** A view keeps the *entire* backing matrix
  alive for as long as the view exists, even a one-element span.
* **Not shippable.** A view cannot be pickled, sent, or placed in a
  :class:`Cown`; doing so raises :class:`TypeError` naming the ``.copy()``
  remedy. ``.copy()`` yields an owned, shippable matrix.
* **Arithmetic materialises.** ``+``, ``-``, ``*``, ``/``, ``@``, unary ``-``,
  and ``abs()`` on a view return a fresh owned :class:`Matrix`; augmented
  ``view += x`` therefore rebinds the name to a new matrix rather than writing
  through.
* **Column views are strided.** A column view materialises (for arithmetic or
  ``.copy()``) by gathering strided elements, so it costs a little more than a
  contiguous row view.
* **Ownership.** Every value access requires the current interpreter to own
  the source matrix; a released base raises :class:`RuntimeError`.

.. autoclass:: MatrixView
    :members:
    :undoc-members:
    :special-members: __len__, __getitem__, __setitem__


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
