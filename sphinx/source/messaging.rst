.. _messaging:

Messaging
=========

.. module:: bocpy
   :noindex:

bocpy includes an Erlang-style **message-passing** subsystem built on top of
lock-free multi-producer single-consumer (MPSC) ring buffers implemented in C.
Messages can be sent from any thread or sub-interpreter and received by any
other — they are the primary mechanism for communication that does *not*
require shared ownership of a cown.

.. note::

   Messaging is a **lower-level** facility than ``@when`` / :class:`Cown`.
   Most programs should model coordination through cowns and behaviors; reach
   for ``send`` / ``receive`` when you need a channel-like pattern (producer–
   consumer queues, heartbeat loops, event buses) or need to communicate with
   code running outside the behavior runtime (plain threads, the main thread
   before ``wait()``).

Concepts
--------

Tags
^^^^

Every message carries a **tag** — a short string label (max 63 UTF-8 bytes)
that acts as a routing key. The runtime maintains 16 internal queues; each
tag is assigned to the first free slot the first time it is used. Receivers
specify one or more tags and only dequeue messages whose tag matches.

There is no declaration step: the first ``send("my-tag", ...)`` auto-assigns
the tag to a queue. If you want deterministic queue assignment (useful for
benchmarks or when you need to isolate traffic), call :func:`set_tags` before
any sends.

Selective Receive
^^^^^^^^^^^^^^^^^

:func:`receive` blocks the calling thread until a message with a matching tag
arrives. You can pass a single tag or a sequence of tags to listen on multiple
channels simultaneously::

    # Wait for whichever arrives first
    msg = receive(["order-ready", "order-cancelled"])

The return value is a two-element list ``[tag, contents]``.

Timeouts and the ``after`` Callback
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

:func:`receive` accepts an optional **timeout** (in seconds). When the timeout
fires:

- If an ``after`` callback is provided, the runtime calls ``after()`` and
  returns its result **directly** as the value of ``receive(...)``. By
  convention the callback returns a ``(tag, contents)`` tuple so the caller
  can pattern-match it the same way as a normal message, but the runtime
  itself does not interpret the value — nothing is enqueued and no other
  receiver sees it.
- If no ``after`` is provided, ``receive`` returns ``(TIMEOUT, None)``.
  :data:`TIMEOUT` is the *tag* slot of the synthetic two-element result;
  compare ``msg[0]`` against it.

::

    from bocpy import receive, TIMEOUT

    msg = receive("heartbeat", timeout=2.0)
    if msg[0] == TIMEOUT:
        print("No heartbeat in 2 seconds")

With an ``after`` callback::

    def after():
        return "heartbeat", "self-tick"

    msg = receive("heartbeat", timeout=1.0, after=after)
    # msg == ("heartbeat", "self-tick") if nothing arrived in 1 s

The ``after`` function may return any value; the ``(tag, contents)``
shape is purely a convention so the caller can pattern-match it
uniformly with normal ``receive`` results.

Worked Example: Calculator Service
-----------------------------------

The following example (adapted from
`examples/calculator.py <https://github.com/microsoft/bocpy/tree/main/examples/calculator.py>`_)
demonstrates a concurrent calculator service. Multiple client threads send
arithmetic operations to a server thread via the ``"calculator"`` tag. The
server uses selective receive with a timeout to detect when clients have gone
silent.

.. code-block:: python

   """Concurrent calculator using message-passing channels."""

   import random
   from threading import Thread
   import time

   from bocpy import receive, send


   def client(num_operations: int):
       """Send random arithmetic operations to the calculator channel."""
       actions = ["+", "-", "/", "*"]
       for _ in range(num_operations):
           time.sleep(random.random() * 0.1)
           action = random.choice(actions)
           value = random.random() * 10 - 5
           send("calculator", (action, value))


   def server(timeout):
       """Receive and process arithmetic operations until stopped."""
       value = 0
       num_operations = 0
       running = True

       def after():
           return "calculator", ("print", True)

       while running:
           match receive("calculator", timeout, after):
               case [_, ("+", x)]:
                   num_operations += 1
                   value += x

               case [_, ("-", x)]:
                   num_operations += 1
                   value -= x

               case [_, ("*", x)]:
                   num_operations += 1
                   value *= x

               case [_, ("/", x)]:
                   num_operations += 1
                   value /= x

               case [_, ("print", _)]:
                   print("Total operations:", num_operations)
                   print("Final value:", value)
                   running = False


   # Start the server with a 2-second idle timeout
   server_thread = Thread(target=server, args=(2.0,))
   server_thread.start()

   # Spawn 4 clients, each sending 5 operations
   clients = [Thread(target=client, args=(5,)) for _ in range(4)]
   for c in clients:
       c.start()
   for c in clients:
       c.join()

   # Once clients finish, send a shutdown signal (or let the timeout fire)
   send("calculator", ("print", False))
   server_thread.join()

**Key observations:**

- ``send("calculator", ...)`` is non-blocking and thread-safe — all four
  clients fire concurrently.
- The server's ``match`` on ``receive(...)`` is a *selective receive*: it
  pattern-matches on the message contents, not just the tag.
- The ``after`` callback fires when no message arrives within ``timeout``
  seconds, causing the server to print results and exit gracefully.
- No locks, no shared mutable state — the only coordination is the message
  queue.

Pre-assigning Tags
^^^^^^^^^^^^^^^^^^

For performance-sensitive applications where you know your tag set ahead of
time, :func:`set_tags` pins tags to specific internal queues, avoiding hash
collisions::

    from bocpy import set_tags

    set_tags(["orders", "heartbeat", "shutdown"])

Calling ``set_tags`` **clears all queued messages** and reassigns the queue
layout. Call it once at startup, before any sends.

Draining Queues
^^^^^^^^^^^^^^^

:func:`drain` discards all pending messages for one or more tags::

    from bocpy import drain

    drain("calculator")           # clear one tag
    drain(["orders", "events"])   # clear multiple tags

This is useful for cleanup between test runs or when resetting a subsystem.

.. warning::

   If new messages are arriving faster than they can be drained, ``drain``
   may not return promptly.

Sending Custom Types Across Sub-interpreters
--------------------------------------------

Messages cross sub-interpreter boundaries through CPython's
**cross-interpreter data** (XIData) machinery, with a **pickle fallback**
when no XIData handler is registered for the payload's type. The runtime
makes no attempt to ship class definitions along with the message — the
receiver must already be able to resolve the type by its fully qualified
name.

In practice this means:

- **Builtins and stdlib containers just work.** Numbers, strings, bytes,
  ``tuple``, ``list``, ``dict``, ``set``, ``frozenset`` and similar types
  either have a native XIData handler or pickle cleanly to types every
  interpreter already knows about.
- **C extension types can register a custom XIData handler** to transfer
  ownership directly without going through pickle. :class:`Cown` and
  :class:`Matrix` use this path; see :ref:`c-abi` for how to expose your
  own type through the same mechanism.
- **Pure-Python custom classes fall back to pickle.** Unpickling only
  succeeds if the receiving interpreter can already import the class by
  its fully qualified name. If a worker has never executed
  ``import my_pkg.my_module``, then receiving an instance of
  ``my_pkg.my_module.MyClass`` will fail with a ``ModuleNotFoundError`` or
  ``AttributeError`` raised from inside ``receive``.
- **Closures, lambdas, and locally-defined classes cannot be sent at all**
  — the pickle fallback cannot resolve them by qualified name from any
  interpreter, and they have no XIData handler.

Inside ``@when`` behaviors the :ref:`transpiler <api>` handles the
import-side of this automatically: it rewrites the decorated module so
each worker imports the same set of names the caller had in scope, and
any class referenced by a behavior is therefore resolvable on the worker
side. When you use ``send`` / ``receive`` from a *plain* thread, a
sub-interpreter spawned outside the behavior runtime, or from inside a
behavior body but with a type that was not part of the captured
environment, **you are responsible for ensuring the class is importable
on the receiver** (or for registering an XIData handler that bypasses
pickle entirely).

The simplest way to satisfy the pickle path is to define message payload
types at module scope in a module that every participating interpreter
imports at startup — for example, a shared ``messages.py`` that the main
program, the worker bootstrap, and any auxiliary threads all import
before the first ``send``.


API Reference
-------------

.. autofunction:: send
   :no-index:
.. autofunction:: receive
   :no-index:
.. autofunction:: set_tags
   :no-index:
.. autofunction:: drain
   :no-index:
.. autodata:: TIMEOUT
   :no-index:
