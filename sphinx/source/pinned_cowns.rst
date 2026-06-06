.. _pinned-cowns:

Pinned Cowns
============

.. module:: bocpy
   :noindex:

A :class:`PinnedCown` is a :class:`Cown` whose value never leaves the
**main interpreter**. Behaviors whose request set contains *any*
pinned cown run on the main thread, drained by :func:`pump` (called
from your event loop) or implicitly by :func:`wait`. Use a pinned
cown when the underlying value cannot survive an XIData round-trip:
pyglet shapes, Tk widgets, open file handles, ctypes pointers, GPU
contexts, asyncio loops.

When to Use a Pinned Cown
-------------------------

Reach for :class:`PinnedCown` when the value:

- has a ``__reduce__`` that raises or silently reconstructs a
  broken object on the other side of the worker boundary,
- is a handle into a library loaded only by ``__main__``
  (pyglet GL state, a Tk root, an open SQLite connection, a CUDA
  context),
- must observe identity across acquires (``id(value)`` stable
  between behaviors).

A regular :class:`Cown` stores its value as cross-interpreter data
and the same Python object is never observed twice in a worker. A
:class:`PinnedCown` holds its value as a plain ``PyObject``
reference in the main interpreter; every acquire sees the same
object.

Pump Contract
-------------

:func:`pump` drains the main-thread queue of pinned-aware
behaviors. Each behavior runs to completion before the next starts.
The pump is non-preemptive: ``deadline_ms`` gates *starting* the
next behavior, not interrupting one already running.

.. code-block:: python

    from bocpy import pump

    result = pump()                       # drain to empty
    result = pump(deadline_ms=4)          # wall-clock budget
    result = pump(max_behaviors=8)        # hard count
    result = pump(raise_on_error=True)    # re-raise first body exception

The result is a :class:`PumpResult` ``NamedTuple``:

- ``executed`` — pinned behaviors whose lifecycle (acquire attempt
  → optional body → release) ran to completion.
- ``deadline_reached`` — ``True`` iff the loop exited because
  ``deadline_ms`` tripped before the queue drained.
- ``raised`` — pinned behaviors whose body raised an
  ``Exception`` captured to the result cown's ``.exception``.

Script-mode programs need not call :func:`pump` explicitly —
:func:`wait` pumps internally when any :class:`PinnedCown` exists
in the process.

.. _pinned-coarse-grained:

The Coarse-Grained Dispatch Pattern
-----------------------------------

The pinned arm is **single-consumer**: only the main thread drains
the pump queue. If you schedule a pinned behavior per item, those
behaviors serialise on the main thread and you lose worker
parallelism.

Schedule pinned behaviors **coarsely** — one per logical frame or
batch, not per item:

1. Wrap per-item state in regular :class:`Cown`\s.
2. Schedule worker ``@when``\s that compute per-item physics and
   **return** a result :class:`Cown`.
3. Schedule **one** pinned ``@when`` per frame that captures all
   the result cowns together with the main-thread handle, and
   performs the batched write-back.

The
`boids example <https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/boids.py>`_
follows this pattern: worker behaviors compute per-cell flock
physics in parallel; one pinned behavior per frame writes the
results back into the global pyglet-visible position and velocity
matrices. Dispatch through the pump queue is ~1 per frame, not N.

Integrating with an Event Loop
------------------------------

Pyglet
^^^^^^

Call :func:`pump` at the top of each scheduled tick so the prior
frame's write-back drains before new work is scheduled:

.. code-block:: python

    import pyglet
    from bocpy import PinnedCown, pump, start, when

    start()
    canvas = PinnedCown(MyCanvas())   # holds a pyglet handle

    def update(dt):
        pump()                         # drain prior frame's write-back
        # ... schedule worker behaviors that return result cowns ...
        results = [worker_compute(i) for i in range(num_items)]

        @when(*results, canvas)
        def _writeback(*args):
            *cells, canvas = args
            for cell in cells:
                canvas.value.draw(cell.value)

    pyglet.clock.schedule_interval(update, 1 / 60)
    pyglet.app.run()

Tk / asyncio
^^^^^^^^^^^^

Drive :func:`pump` from a periodic callback (``root.after(ms,
...)`` for Tk, ``loop.call_later(...)`` or an ``asyncio``-friendly
periodic task for asyncio). The same coarse-grained pattern
applies: keep dispatch rate at one pinned behavior per logical
batch.

Starvation and the Watchdog
---------------------------

**The watchdog is disabled until you call** :func:`set_pump_watchdog`.
No call means no warnings — the runtime stays silent regardless of
how long the pinned queue has been non-empty.

Once enabled, if pinned work piles up because the host event loop
is wedged or not calling :func:`pump` often enough, the watchdog
logs a warning carrying the queue's age and depth. The threshold
gates on **queue-non-empty time**: a program that runs only
unpinned work indefinitely never trips it.

.. code-block:: python

    from bocpy import set_pump_watchdog

    set_pump_watchdog(warn_ms=1000)   # enable warn-at-1s (matches the kwarg default)
    set_pump_watchdog(warn_ms=None)   # disable

- **No call ⇒ no watchdog.** The runtime ships with the warn
  threshold unset; you opt in by calling
  :func:`set_pump_watchdog` at least once.
- ``warn_ms`` (default ``1000`` when the kwarg is omitted) logs a
  warning carrying the queue's non-empty duration (ms) and current
  depth. Pass ``None`` to turn the warning off.
- ``on_starve`` lets the host replace the default ``logging``
  sink. Use it to escalate (``on_starve=lambda s, m: pytest.fail(m)``
  in tests, a counter / alert hook in production).

The watchdog deliberately never raises on its own: the pinned queue
is bounded by the live :class:`PinnedCown` count by construction,
so there is no back-pressure threat the library can defend against
without lying about it. Fail-fast policy belongs in the host's
``on_starve`` callback, where the calling code can record the right
context and pick the right exception class.

Hosts that need to tune :func:`wait`'s internal pump cadence call
:func:`set_wait_pump_poll`. The default cadence is **50 ms**, which
is the upper bound on how long the auto-pump loop will park between
checks when no broadcast wakes it.

Main-thread Direct Reads
------------------------

The pinned-cown contract refuses worker-side reads of the underlying
value (the owner CAS rejects them). The symmetric question — "may
the main thread read the value directly, outside a pinned ``@when``
body?" — has a narrower answer:

- Reading the underlying object from the **main thread** is safe
  **iff** no pinned ``@when`` is currently executing against that
  cown. Pinned bodies run synchronously inside :func:`pump`; once
  ``pump()`` (or :func:`wait`'s auto-pump) returns, no body holds
  the cown, and an immediate main-thread read sees a consistent
  value.
- Reading from the main thread **while** ``pump()`` is dispatching
  a body that targets the same cown is **undefined**. Do not
  alternate between "I'm pumping" and "I'm reading directly"
  inside the same callback.
- The safe pattern is: stash any main-thread alias for read-only
  rendering / event-loop integration, but treat the pinned ``@when``
  body as the only writer.

In the boids example, the ``Simulation`` object aliases the same
``Matrix`` under ``self.positions`` (for pyglet rendering) and
``self.positions_cown = PinnedCown(positions)`` (for the per-frame
write-back). The render path runs on the main thread between
``pump()`` calls; the write-back runs inside ``pump()``. They never
overlap.

Thread Affinity and Free-Threaded Builds
-----------------------------------------

- :class:`PinnedCown` may only be **constructed** from the main
  interpreter; a worker that calls ``PinnedCown(x)`` raises
  :class:`RuntimeError`.
- :func:`pump` must run on the main interpreter. On classic
  CPython, any thread within the main interpreter may pump (the
  per-interpreter GIL serialises).
- On free-threaded builds (``Py_GIL_DISABLED``) only **one thread
  at a time** may pump, enforced by a CAS on pump entry that
  raises :class:`RuntimeError` if a second thread tries to enter
  concurrently. The CAS is cleared on every exit path, including
  ``BaseException`` propagation from a pinned body.
- :func:`pump` is **not reentrant**. Calling :func:`pump` from
  inside a pinned-behavior body raises :class:`RuntimeError`.

Handle vs. Value
----------------

A :class:`PinnedCown` *handle* (the Python wrapper and its C
capsule) is a normal cross-interpreter shareable. It travels via
the same XIData mechanism as a regular :class:`Cown` and may be:

- shipped as a captured variable to a worker behavior,
- embedded in any value graph stored in a regular :class:`Cown`
  (``Cown(PinnedCown(x))`` is supported),
- placed in a noticeboard entry via :func:`notice_write` or
  :func:`notice_update`.

What never crosses interpreter boundaries is the *value*. A worker
that ends up holding a pinned-cown handle can do exactly one
useful thing with it: schedule pinned ``@when``\s against it,
which the runtime auto-routes to the main pump queue. Any attempt
to acquire the value from a worker is rejected by the C-level
owner CAS.

Mixed Request Sets
------------------

A behavior may freely combine pinned and unpinned cowns; the 2PL
acquisition order is unchanged. As soon as the request set contains
any pinned cown, the body runs on the main thread. Unpinned cowns
in the set still travel through XIData into the main interpreter
for the body's duration.

Exception Model
---------------

Body exceptions follow the same rules as worker behaviors:
captured on the result :class:`Cown` and surfaced through
``cown.exception``. The default :func:`pump` does **not** re-raise;
pass ``raise_on_error=True`` to opt into fail-fast propagation.
``BaseException`` (``KeyboardInterrupt``, ``SystemExit``,
``GeneratorExit``) propagates from :func:`pump` immediately after
the offending behavior's per-iteration cleanup completes; any
behaviors still queued remain in the pinned queue and resume on
the next :func:`pump` (or :func:`wait`-driven auto-pump) call.

Free-Threaded Trajectory
------------------------

On free-threaded CPython (the ``3.13t`` and ``3.15t`` builds),
:class:`PinnedCown` works identically to classic CPython — the
sub-interpreter boundary still exists for FT workers, and
free-threaded support is "experimental" across all of bocpy.
:class:`PinnedCown` inherits that label. The single-pumper CAS
prevents silent data races from concurrent pumpers, raising
:class:`RuntimeError` instead.

Long term, bocpy will fork into a classic-CPython build (using
sub-interpreters — where :class:`PinnedCown` is meaningful) and a
free-threaded build (running workers as plain main-interpreter
threads — where every cown is effectively pinned and
:class:`PinnedCown` becomes a no-op). In that future, the
single-pumper CAS is removed. Out of scope for v1.

API Reference
-------------

See :ref:`api` for the autodoc-generated reference for
:class:`PinnedCown`, :func:`pump`, :class:`PumpResult`,
:func:`set_pump_watchdog`, and :func:`set_wait_pump_poll`.
