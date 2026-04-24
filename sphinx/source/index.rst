bocpy documentation
===================

`bocpy <https://github.com/microsoft/bocpy>`_ is a Python library implementing
**Behavior-Oriented Concurrency (BOC)**.  Programmers wrap shared data in
**cowns** (concurrently-owned objects) and schedule **behaviors** with the
``@when`` decorator; the runtime runs each behavior once all of its required
cowns are available, with deadlock freedom guaranteed by construction.  On
Python 3.12 and newer, behaviors execute in parallel across worker
sub-interpreters that each have their own GIL.

For a hands-on introduction, see the
`BOC tutorial <https://microsoft.github.io/bocpy/>`_, the
`project README <https://github.com/microsoft/bocpy#readme>`_, and the
`runnable examples <https://github.com/microsoft/bocpy/tree/main/src/bocpy/examples>`_.
The :ref:`api` page below documents every public symbol.

A taste of BOC
--------------

The snippet below — a trimmed version of
`bocpy-bank <https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/bank.py>`_
— shows the core concepts: data wrapped in :class:`Cown`\s, a behavior
scheduled with :func:`when` that takes exclusive access to two cowns at once,
and :func:`wait` blocking the main thread until all behaviors have completed.
The runtime acquires ``src`` and ``dst`` in a deadlock-free order, so
``transfer`` can safely mutate both accounts.

.. code-block:: python

   from bocpy import Cown, wait, when


   class Account:
       def __init__(self, name, balance):
           self.name = name
           self.balance = balance


   def transfer(src: Cown[Account], dst: Cown[Account], amount: float):
       # `@when` schedules `_` to run once both cowns are available.
       # Inside, src.value and dst.value can be mutated safely —
       # no other behavior can touch either account at the same time.
       @when(src, dst)
       def _(src, dst):
           print(f"  transfer: {src.value.name} -> {dst.value.name} ({amount})")
           if src.value.balance >= amount:
               src.value.balance -= amount
               dst.value.balance += amount

       @when(dst)
       def _(dst):
           print(f"  {dst.value.name} now has {dst.value.balance}")


   alice = Cown(Account("Alice", 100))
   bob = Cown(Account("Bob", 0))

   print("scheduling first transfer")
   transfer(alice, bob, 40)
   print("scheduling second transfer")
   transfer(bob, alice, 10)
   print("main thread reaches wait()")

   wait()  # block until every scheduled behavior has finished
   print("all behaviors complete")

Running it prints something like:

.. code-block:: console

   $ python bank.py
   scheduling first transfer
   scheduling second transfer
   main thread reaches wait()
     transfer: Alice -> Bob (40)
     Bob now has 40
     transfer: Bob -> Alice (10)
     Alice now has 70
   all behaviors complete

Note how the ``scheduling …`` lines all print *before* any behavior body
runs: ``@when`` returns immediately, and the runtime only fires each
behavior once its cowns are free.  The two transfers serialise on the
``Alice``/``Bob`` cowns, so their effects are interleaved in a deadlock-free,
data-race-free order chosen by the runtime.

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   api

Indices and Tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
