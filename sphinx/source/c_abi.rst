.. _c-abi:

C ABI
=====

This page documents the public C ABI shipped with bocpy. Use it when
writing a downstream C extension that needs to participate in
behavior-oriented concurrency at the C level — typically by registering
a custom Python type as cross-interpreter shareable so :class:`Cown`
can hand instances of it across worker interpreters.

When do I need this?
--------------------

You do **not** need this header to use a custom Python type with
:class:`Cown`. Any type that has been registered as cross-interpreter
shareable through CPython's ``_PyXIData_*`` / ``_PyCrossInterpreterData_*``
machinery — by whatever means — is automatically handled by bocpy's
scheduler and message queue. Registration is what makes a type usable
across worker sub-interpreters; bocpy does not impose any additional
requirement on top of that.

The public C ABI is provided as a **convenience** for extension
authors who want to do that registration from C. It exists for two
reasons:

- **Cross-platform atomics.** ``<bocpy/bocpy.h>`` exposes a small
  sequentially-consistent atomics surface
  (``atomic_load`` / ``atomic_store`` / ``atomic_fetch_add`` /
  ``atomic_compare_exchange_strong`` over ``atomic_int_least64_t``,
  plus a ``thread_local`` macro) that compiles on MSVC as well as
  every toolchain that ships ``<stdatomic.h>``. On non-MSVC builds
  the umbrella simply pulls in ``<stdatomic.h>``; on MSVC it provides
  the missing prototypes so the same source compiles unchanged.
- **Portability across Python versions.** The ``_PyXIData_*`` API has
  changed shape several times between CPython 3.12, 3.13, 3.14, and
  3.15 (free-threaded builds included), and differs again on the
  legacy ``BOC_NO_MULTIGIL`` path. ``<bocpy/bocpy.h>`` and
  ``<bocpy/xidata.h>`` paper over those differences with a single
  set of macros, so one source file builds unchanged across every
  supported Python version that bocpy itself supports.

If neither of those concerns applies to your extension, you can ignore
the C ABI entirely and rely on whatever cross-interpreter registration
your type already has.

.. note::

   The bocpy public C ABI is **C only**. Including ``bocpy.h`` from a
   C++ translation unit is not supported in this release. C++ consumers
   must wrap the bocpy ABI in a thin C translation unit and call into
   that from C++.

Quickstart
----------

In your downstream ``setup.py``:

.. code-block:: python

   import bocpy
   from setuptools import setup, Extension

   setup(
       ext_modules=[
           Extension(
               "myext",
               sources=["myext.c"] + bocpy.get_sources(),
               include_dirs=[bocpy.get_include()],
           )
       ],
   )

In your C source:

.. code-block:: c

   #include <bocpy/bocpy.h>

   /* <bocpy/bocpy.h> includes <Python.h> internally and is
    * order-insensitive with respect to <Python.h> itself (which is
    * idempotent). It must still appear *before* any system header
    * (<stdio.h>, <string.h>, ...) in the same translation unit, the
    * same way <Python.h> must — CPython forbids system headers
    * before Python.h. */

ABI versioning
--------------

``bocpy.h`` defines a single integer macro ``BOCPY_ABI``. Compare it
with ``>=`` if you want to gate code on a minimum bocpy ABI revision.
The value is bumped on any incompatible change to ``bocpy.h`` or
``xidata.h``. Wheels are CPython-version-tagged (currently ``cp310``,
``cp311``, ``cp312``, ``cp313``, ``cp314``), so a runtime ABI mismatch
between bocpy and its host CPython cannot occur: each wheel embeds
the ``xidata.h`` ladder arm appropriate for its target CPython.

Atomic surface
--------------

The atomic surface is a minimal, sequentially-consistent shim over
``int_least64_t``. On non-MSVC compilers it is just ``<stdatomic.h>``;
on MSVC the four functions below have out-of-line bodies in
``bocpy_msvc.c``, which downstream extensions pick up automatically
via :func:`bocpy.get_sources`.

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Symbol
     - Description
   * - ``atomic_int_least64_t``
     - Type alias for a 64-bit atomic integer.
   * - ``atomic_load(ptr)``
     - Sequentially-consistent load of ``*ptr``.
   * - ``atomic_store(ptr, value)``
     - Sequentially-consistent store of ``value`` into ``*ptr``.
   * - ``atomic_fetch_add(ptr, value)``
     - Sequentially-consistent ``*ptr += value`` returning the old value.
   * - ``atomic_compare_exchange_strong(ptr, expected, desired)``
     - Sequentially-consistent CAS. Returns ``true`` on success;
       on failure writes the observed value through ``expected``.

All operations are sequentially consistent on every supported MSVC
target (x86, x64, ARM64). The MSVC shim implements ``atomic_load``
via ``InterlockedOr64(ptr, 0)`` and ``atomic_store`` via
``InterlockedExchange64`` on x64/ARM64 (full barriers); on x86 both
go through ``InterlockedCompareExchange64``. The RMW ops are
``InterlockedExchangeAdd64`` / ``InterlockedCompareExchange64`` on
x64/ARM64 and CAS-loops on x86 — all already full barriers. The shim
deliberately does not expose ``_explicit`` variants or weaker memory
orders.

Ownership helpers
-----------------

The XIData callbacks shown in the worked example below flip a single
``atomic_int_least64_t`` owner field as the resource crosses
interpreter boundaries. ``bocpy.h`` exposes two helpers for that
pattern so downstream code does not have to redefine them:

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Symbol
     - Description
   * - ``BOCPY_NO_OWNER``
     - Sentinel value (``-2``) meaning "no interpreter currently owns
       this resource". Use it as the initial value of an owner field
       and as the CAS target during the producer-side
       ``XIDATA_GETDATA_FUNC`` callback. Negative so it never collides
       with a real ``PyInterpreterState_GetID()`` return value.
   * - ``bocpy_interpid()``
     - ``static inline int_least64_t``: returns the running
       interpreter's ID, pre-typed for the
       ``atomic_compare_exchange_strong`` parameter list. Must be
       called with the GIL held (or while attached to an interpreter
       on free-threaded builds) — same contract as the underlying
       ``PyInterpreterState_GetID(PyInterpreterState_Get())``.
   * - ``bocpy_main_interpid()``
     - ``static inline int_least64_t``: returns the *main*
       interpreter's ID, pre-typed to match ``bocpy_interpid()`` for
       owner-field equality checks. Wraps
       ``PyInterpreterState_GetID(PyInterpreterState_Main())``, which
       returns the process's main interpreter regardless of which
       interpreter the caller is currently attached to, so this
       helper is safe to call from a worker sub-interpreter for
       diagnostic / assert use (under the GIL or equivalent
       attachment, same as ``bocpy_interpid()``). Used by bocpy's
       own main-pinned-cown call sites to assert that the running
       interpreter is the permanent owner of a pinned cown's value.

The two are designed to be used together: producer-side, CAS the
owner from ``bocpy_interpid()`` to ``BOCPY_NO_OWNER`` before calling
``XIDATA_INIT``; consumer-side, CAS it back from ``BOCPY_NO_OWNER`` to
``bocpy_interpid()`` inside the ``new_object`` callback. See the
worked example below.

Proto-Region semantics
----------------------

The ownership pattern shown in the worked example is a deliberately
narrow approximation of the **Region** discipline from Pyrona's
*Lungfish* model (Stoldt et al., `Dynamic Region Ownership for
Concurrency Safety
<https://www.microsoft.com/en-us/research/publication/dynamic-region-ownership-for-concurrency-safety/>`__,
PLDI 2025). Lungfish is a dynamic ownership model for Python in which mutable state is grouped into
*regions*: at any point in time at most one thread has access to a
region, transferring a region into a `cown` makes it sharable
between threads, and acquiring the cown moves the region into the
acquiring thread for the duration of the behavior. The bocpy public
ABI does not implement Lungfish — there is no region graph, no
freeze, no merge, no borrow tracking — but the ``BOCPY_NO_OWNER`` /
``bocpy_interpid()`` pair gives downstream C extensions enough
machinery to model the *single most important* invariant a region
provides: **a mutable C resource is owned by exactly one interpreter
at a time, and any other interpreter that still holds a wrapper
around it cannot read or write its contents.**

What proto-Region buys you
~~~~~~~~~~~~~~~~~~~~~~~~~~

Wrapping a C struct in a refcounted Python type and registering it
through ``XIDATA_REGISTERCLASS`` already moves the *pointer* between
sub-interpreters efficiently — no copy, no pickle. But pointers alone
are unsafe: nothing stops a worker from racing the previous owner on
the same impl after the handoff, exactly the unrestricted-shared-
mutable-state hazard regions are designed to eliminate (see Pyrona §1
and Fig. 1: ``share(x, T2)`` between threads is unsafe). The owner
field, flipped atomically as the impl crosses the XIData boundary,
turns that hazard into a deterministic ``RuntimeError`` rather than a
data race.

The contract
~~~~~~~~~~~~

A custom C resource opting into proto-Region semantics commits to all
of the following:

1. **Single owner field.** The resource carries one
   ``atomic_int_least64_t owner`` field, initialised to
   ``bocpy_interpid()`` of the constructing interpreter. ``Matrix``'s
   ``matrix_impl`` and the consumer template's ``counter_impl`` are
   the canonical examples.
2. **CAS in the producer callback.** ``XIDATA_GETDATA_FUNC`` CASes
   the owner from ``bocpy_interpid()`` to ``BOCPY_NO_OWNER`` before
   calling ``XIDATA_INIT``. A failed CAS surfaces as a
   ``RuntimeError`` and aborts the handoff: the resource is not
   transferred.
3. **CAS in the consumer callback.** The ``new_object`` callback
   CASes the owner from ``BOCPY_NO_OWNER`` to ``bocpy_interpid()``
   before constructing the new wrapper. If wrapper allocation fails
   after the CAS succeeds, the callback must store the owner back to
   ``BOCPY_NO_OWNER`` so a future retry of the handoff can succeed.
4. **Ownership check on data accessors.** Any method or getter that
   reads or writes the resource's payload must verify
   ``bocpy_interpid() == atomic_load(&impl->owner)`` and raise
   ``RuntimeError`` otherwise. ``Matrix``'s
   ``impl_check_acquired`` and the consumer template's
   ``counter_impl_check_acquired`` are the canonical helpers.
   Identity-only accessors (e.g. ``Counter.address``,
   ``Counter.refcount``) are allowed to skip the check — the same way
   you may print the address of a Lungfish bridge object without
   acquiring its region.
5. **No raw send of the cown's value.** Inside a ``@when``,
   ``send("tag", c.value)`` is *not* the right primitive for shipping
   a proto-Region resource to another behavior: it would atomically
   move the impl out of the cown mid-behavior and leave the worker
   unable to release the cown afterwards. Send a copy
   (``c.value.copy()``, like ``examples/boids.py``) or send primitive
   summary data (``c.value.address``, ``c.value.count``). The cown
   itself is the right primitive for handing the resource to a
   different behavior — schedule a downstream ``@when`` on the same
   cown.

What proto-Region does **not** give you
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The bocpy ABI does **not** implement these parts of Lungfish, and a
downstream extension that wants them must build them on top:

* **Transitive closure.** Lungfish regions enforce isolation across a
  whole object graph; proto-Region tracks ownership of a single
  ``impl`` pointer. If your resource holds further heap-allocated
  state, you are responsible for keeping that state private (no
  outgoing references) or applying the same owner-CAS pattern to
  each piece. ``Matrix`` does this implicitly: ``matrix_impl``
  encapsulates ``data`` and ``row_ptrs``, both private to the impl.
* **Freezing and immutability.** There is no equivalent of
  ``freeze(b)``. Resources are either owned-and-mutable or in flight.
* **Merge / region trees.** There is no nesting; one resource = one
  owner field.
* **Borrowed references from a "local region".** The bocpy worker
  loop is the closest equivalent — a worker behavior is the period
  during which the impl is owned by that interpreter — but there is
  no first-class borrow type, and a borrowed-style reference held
  past the end of a behavior is undefined behavior.

In short: proto-Region is the **smallest** thing that turns a
shareable C struct into a Region-like resource, and it slots into
the existing ``XIDATA_REGISTERCLASS`` lifecycle without any
additional machinery beyond the two ABI symbols
``BOCPY_NO_OWNER`` and ``bocpy_interpid()``.

XIData ladder
-------------

The cross-interpreter data ("XIData") API is a thin macro ladder over
CPython's internal cross-interpreter data primitives, smoothing over
the rename from ``_PyCrossInterpreterData`` (3.12, 3.13) to
``_PyXIData`` (3.14+). All macros and the typedef below are exposed
by ``bocpy.h`` via its ``#include "xidata.h"``.

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Symbol
     - Description
   * - ``XIDATA_T``
     - Opaque struct holding a serialised cross-interpreter handoff.
   * - ``XIDATA_NEW()``
     - Allocate and zero a fresh ``XIDATA_T *``.
   * - ``XIDATA_INIT(xidata, interp, data, obj, new_object)``
     - Initialise an allocated ``XIDATA_T``. See safety contract.
   * - ``XIDATA_GETXIDATA(value, xidata)``
     - Ask CPython to populate ``xidata`` from the producer-side
       Python object ``value``.
   * - ``XIDATA_NEWOBJECT``
     - Field type used for the consumer-side reconstruction callback.
   * - ``XIDATA_FREE(xidata)``
     - Release any resources owned by ``xidata`` and free the buffer.
   * - ``XIDATA_SET_FREE(xidata, fn)``
     - Install a custom free callback into ``xidata``.
   * - ``XIDATA_REGISTERCLASS(type, cb)``
     - Register a Python ``type`` as cross-interpreter shareable with
       producer-side callback ``cb``. See safety contract.

The lifecycle is **register once, then per-handoff init/get/free**:
register each shareable type at module init; on the producer side,
allocate an ``XIDATA_T``, populate it with ``XIDATA_GETXIDATA`` (which
internally calls the registered callback that will end up calling
``XIDATA_INIT``), hand the buffer to the consumer interpreter; on the
consumer side, call the ``new_object`` callback recorded during init
to reconstruct the Python object, then ``XIDATA_FREE`` to release.

Safety contract
---------------

These contracts are mirrored from the canonical doc-comments in
``xidata.h``; the C header is the single source of truth.

* ``XIDATA_INIT(xidata, interp, …)`` — ``interp`` must be the
  interpreter that currently owns ``data``. Passing the wrong
  ``interp`` produces a use-after-free across the worker handoff.
  The ``xidata`` buffer must be freshly allocated (or zeroed) and
  must not have been initialised before; double-init is undefined.

* ``XIDATA_REGISTERCLASS(type, cb)`` — must be called once per
  ``(type, cb)`` pair **per interpreter**, from inside that
  interpreter's module-exec slot (or equivalent module-init code that
  runs on every import in every interpreter). The standard idiom is
  to call it from a ``Py_mod_exec`` slot — see ``_core.c`` /
  ``_math.c`` for the canonical pattern, mirrored by the consumer
  template under ``templates/c_abi_consumer/``. Registering the same type
  twice with different callbacks in the same interpreter is
  undefined.

* ``BOC_NO_MULTIGIL`` — internal marker. Defined only on CPython
  <3.12, where the host interpreter has no per-interpreter GIL and
  the bocpy runtime falls back to a single-interpreter mode. The
  ``XIDATA_*`` ladder still exposes the same macros on these
  versions; downstream consumers do not need to special-case this
  macro themselves. (The ``XIDATA_GETDATA_FUNC`` macro — see below —
  hides the only consumer-visible signature change for you.)

Worked example: ``bocpy.Matrix``
--------------------------------

The bocpy-shipped :class:`Matrix` type uses every entry in the table
above. The annotated extracts below come from ``src/bocpy/_math.c``.

**1. Module init: register the type from the exec slot.** This runs
once per interpreter, on every import — main interpreter and every
worker sub-interpreter that imports the module. Use multi-phase
initialisation (``Py_mod_exec``) and declare
``Py_MOD_PER_INTERPRETER_GIL_SUPPORTED`` so the bocpy runtime can
load the module inside worker sub-interpreters:

.. code-block:: c

   /* _math_module_exec */
   if (XIDATA_REGISTERCLASS(state->matrix_type, _matrix_shared)) {
       Py_FatalError(
           "could not register MatrixObject for cross-interpreter sharing");
       return -1;
   }

A single-phase ``PyModule_Create`` module that registers from
``PyInit`` will load fine in the main interpreter but cannot satisfy
``Py_MOD_PER_INTERPRETER_GIL_SUPPORTED``; if a transpiled ``@when``
body imports it, the worker sub-interpreter import will not run the
registration and the consumer callback will see no type registered
in its registry. See ``templates/c_abi_consumer/src/_bocpy_probe.c`` for
the full multi-phase template.

**2. Producer side: prepare the underlying C matrix for handoff.**
``_matrix_shared`` is the ``cb`` registered above; CPython invokes it
when ``XIDATA_GETXIDATA`` is called against a ``MatrixObject`` from
the producer interpreter. Declare the callback with
``XIDATA_GETDATA_FUNC`` so the body has the same
``(tstate, obj, xidata)`` parameter list on every supported CPython —
the macro emits a small trampoline on Python <3.12 (where the runtime
calls the callback with ``(obj, xidata)`` only) so the body never has
to special-case ``BOC_NO_MULTIGIL``:

.. code-block:: c

   XIDATA_GETDATA_FUNC(_matrix_shared) {
       MatrixObject *matrix = (MatrixObject *)obj;
       matrix_impl *impl = matrix->impl;

       /* Atomically transfer ownership: this interpreter -> BOCPY_NO_OWNER. */
       int_least64_t expected = bocpy_interpid();
       int_least64_t desired = BOCPY_NO_OWNER;
       if (!atomic_compare_exchange_strong(&impl->owner,
                                           &expected, desired)) {
           PyErr_Format(PyExc_RuntimeError, /* … */);
           return -1;
       }

       XIDATA_INIT(xidata, tstate->interp, impl, obj, _new_matrix_object);
       return 0;
   }

Note the use of ``atomic_compare_exchange_strong`` from the atomic
surface to flip the matrix's owner field, and ``XIDATA_INIT`` to wire
``xidata`` to the C-level ``impl``, the original Python ``obj``, and
the consumer-side reconstruction callback ``_new_matrix_object``.

**3. Consumer side: reconstruct a Python wrapper for the C matrix.**
``_new_matrix_object`` is the ``new_object`` callback recorded by
``XIDATA_INIT``; CPython invokes it on the consumer interpreter:

.. code-block:: c

   static PyObject *_new_matrix_object(XIDATA_T *xidata) {
       matrix_impl *impl = (matrix_impl *)xidata->data;

       /* Atomically take ownership: BOCPY_NO_OWNER -> this interpreter. */
       int_least64_t expected = BOCPY_NO_OWNER;
       int_least64_t desired = bocpy_interpid();
       if (!atomic_compare_exchange_strong(&impl->owner,
                                           &expected, desired)) {
           PyErr_Format(PyExc_RuntimeError, /* … */);
           return NULL;
       }

       PyTypeObject *type = LOCAL_STATE->matrix_type;
       MatrixObject *matrix = (MatrixObject *)type->tp_alloc(type, 0);
       /* … wrap and return … */
   }

Consumer modules and worker sub-interpreters
--------------------------------------------

Workers always run in sub-interpreters, on every supported CPython.
What varies across versions is whether each sub-interpreter owns its
own GIL (3.12+) or shares the legacy global GIL (3.10/3.11, marked
internally by ``BOC_NO_MULTIGIL``). The execution model —
per-interpreter module state, multi-phase init, the per-interpreter
``XIDATA_REGISTERCLASS`` registry — is identical across versions.

Because every ``XIDATA_REGISTERCLASS`` ladder lives in a per-interpreter
exec slot (see step 1 above), a consumer extension's ``Matrix``-like
type is only registered in interpreters that actually imported the
extension. Any consumer module whose XIData wrappers will travel into
a ``@when`` body must be **imported at module scope** in the file the
worker exec'd from.

The transpiler propagates module-scope ``import`` statements into the
exported per-worker module, but it does **not** see runtime imports
(``importlib.import_module(...)``, ``pytest.importorskip(...)``,
``__import__(...)`` from inside a function, …). A worker that imports
the transpiled body without the consumer extension will load the
shared object via the OS loader but skip the per-interpreter exec
slot, leaving its ``LOCAL_STATE`` (or equivalent module-state cache)
NULL. The consumer callback will then segfault on the first reconstruction.

Practical rule for downstream authors:

* Use a top-level ``import _your_extension`` in any test or example
  file that schedules ``@when`` bodies which observe your extension's
  types. ``pytest.importorskip`` is not transpiler-visible.
* Mirror the per-interpreter state pattern (heap-allocated type from
  ``PyType_FromModuleAndSpec``, per-module state, ``thread_local``
  cache primed in the exec slot) shown in
  ``templates/c_abi_consumer/src/_bocpy_probe.c``.

What is NOT public
------------------

The wheel ships only ``bocpy.h``, ``xidata.h``, and ``bocpy_msvc.c``
under the package directory. The following internal headers and
surfaces are **not** part of the public C ABI; do not depend on them:

* Internal headers: ``boc_compat.h``, ``boc_cown.h``, ``boc_sched.h``, ``boc_tags.h``,
  ``boc_terminator.h``, ``boc_noticeboard.h``. As a general rule,
  every ``boc_*`` file in the package directory is private — only
  ``bocpy.h``, ``xidata.h``, and ``bocpy_msvc.c`` are public.
* Ordered atomics (``boc_atomic_*_explicit`` and the typed
  ``boc_atomic_*_u64`` / ``_intptr`` API).
* BOC mutex / condition-variable types (``BOCMutex``, ``BOCCond``)
  and the ``boc_mtx_*`` / ``boc_cnd_*`` helpers.
* ``boc_yield``, ``boc_now_s``, ``boc_now_ns``, ``boc_sleep_ns``, the
  physical-CPU helpers, and any other ``boc_``-prefixed function not
  exposed via ``bocpy.h``.

C++ consumer support is also a non-goal for this release.

CPython version skew
--------------------

bocpy wheels are tagged cp310 / cp311 / cp312 / cp313 / cp314.
Downstream extensions ship the same per-CPython matrix and thereby
pick up the matching ``bocpy.h`` view of the cross-interpreter data
ladder. ``BOCPY_ABI`` is bumped on any incompatible change to
``bocpy.h`` or ``xidata.h``.
