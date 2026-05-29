---
name: c-extensions-with-bocpy
description: "Write a C extension whose custom types can live inside a bocpy Cown and travel between worker sub-interpreters. Use when: writing a native type (matrix, buffer, GPU handle, opaque C resource) that needs to round-trip through @when, send/receive, or any other bocpy primitive that crosses interpreter boundaries; designing the producer/consumer XIData callbacks; implementing proto-Region ownership semantics; setting up multi-phase init and per-interpreter type registration. Covers the bocpy public C ABI (bocpy.h, xidata.h, BOCPY_NO_OWNER, bocpy_interpid, XIDATA_REGISTERCLASS, XIDATA_GETDATA_FUNC, XIDATA_INIT), the setup.py boilerplate, and the worker-import contract."
---

# Writing C extensions with the bocpy public C ABI

This skill is for writing a **downstream C extension** whose custom
types must travel through bocpy's runtime — placed inside a
`Cown`, scheduled with `@when`, or shipped via `send`/`receive`
across worker sub-interpreters.

> **Read `thinking-in-boc` first.** The C ABI does **not** change the
> BOC mental model. Your C type is still wrapped in a `Cown`,
> behaviors are still scheduled with `@when`, and ordering still
> comes from the cown graph — not from anything you do in C. The C
> ABI only buys you a safe, zero-copy way to *cross* the interpreter
> boundary; the choreography between behaviors is unchanged.

## When you need this skill

You need a C extension that uses the bocpy public C ABI only when
**all three** of the following are true:

1. Your type wraps a **native C resource** (large buffer, matrix, GPU
   handle, file descriptor, FFI pointer, etc.) that you do **not**
   want to copy or pickle every time it crosses interpreter
   boundaries.
2. You want instances of that type to live inside a `Cown` and be
   acquired by `@when` behaviors on worker sub-interpreters.
3. You want **isolation guarantees**: at most one interpreter may
   read or write the resource at a time, and any stale wrapper left
   behind in the previous owner cannot observe the resource any more.

If your type is pure-Python, pickleable, or you are happy with a copy
on each interpreter crossing, **you do not need this skill** —
`Cown(my_obj)` already works. Read the BOC primer in the project
copilot instructions and stop here.

The canonical worked example shipped by bocpy lives in
`templates/c_abi_consumer/` of the bocpy source tree — copy it as the
starting point for a new extension. The `Matrix` type in
`src/bocpy/_math.c` is the in-tree reference implementation.

## The proto-Region mental model

A bocpy-aware C type implements **proto-Region ownership**: a single
atomic `owner` field on the impl identifies the interpreter that may
read or write the payload. The producer-side XIData callback CASes
that field from `bocpy_interpid()` to `BOCPY_NO_OWNER`; the
consumer-side callback CASes it back from `BOCPY_NO_OWNER` to
`bocpy_interpid()`. Any data accessor that reads or writes the
payload first asserts `bocpy_interpid() == atomic_load(&impl->owner)`
and raises `RuntimeError` otherwise.

This is not the full Lungfish region model — there are no nested
regions, no freeze, no merge, no borrow tracking — but it is enough
to turn "a shareable pointer" into "a resource owned by exactly one
interpreter at a time", which is what BOC needs.

What the proto-Region contract gives you in practice:

- A stale wrapper held in a producer interpreter after the handoff
  cannot read or write the impl — every accessor raises
  `RuntimeError` until ownership returns. This catches races that
  pointer-only sharing would miss.
- The CAS in the producer callback fails if the calling interpreter
  is not the current owner. A behavior that tries to send a cown's
  value somewhere it should not go is rejected at the boundary,
  not silently corrupted.

## Required scaffolding

### `setup.py`

Use `bocpy.get_include()` for the header search path and
`bocpy.get_sources()` for the MSVC out-of-line atomics shim (no-op
elsewhere). Copy this verbatim from `templates/c_abi_consumer/setup.py`
and change the module name:

```python
from setuptools import Extension, setup
import bocpy

setup(
    ext_modules=[
        Extension(
            "_your_extension",
            sources=["src/_your_extension.c"] + bocpy.get_sources(),
            include_dirs=[bocpy.get_include()],
        ),
    ],
)
```

### `pyproject.toml`

Declare `bocpy` in both `[build-system].requires` (so an isolated PEP 517
build can satisfy `import bocpy` in `setup.py`) and
`[project].dependencies` (so installing your wheel installs bocpy at
runtime).

Always install with `pip install --no-build-isolation` so the build
resolves headers against the bocpy install actually being tested,
not whatever PyPI happens to publish.

### Header include

```c
#include <bocpy/bocpy.h>

/* Compile-time guard: bocpy.h must not leak Py_BUILD_CORE. */
#ifdef Py_BUILD_CORE
#error "Py_BUILD_CORE leaked from bocpy.h"
#endif
```

Rules:

- `<bocpy/bocpy.h>` includes `<Python.h>` internally. It is
  order-insensitive with respect to `<Python.h>` itself.
- It must appear **before** any system header (`<stdio.h>`,
  `<string.h>`, ...) in the same translation unit, the same way
  `<Python.h>` must — CPython forbids system headers before
  `Python.h`.
- **C only.** `<bocpy/bocpy.h>` is not supported from C++ in this
  release. C++ consumers must wrap the ABI in a thin C translation
  unit.

### Public ABI surface — what you may use

Everything below is exposed by `<bocpy/bocpy.h>`. Treat anything else
under the bocpy package directory as private.

| Symbol | Purpose |
|--------|---------|
| `BOCPY_ABI` | Integer macro. Gate code on `BOCPY_ABI >= N` if you need a minimum revision. |
| `BOCPY_NO_OWNER` | Sentinel `-2` meaning "no interpreter owns this impl right now". Use as initial / in-flight value of the owner field. |
| `bocpy_interpid()` | `static inline int_least64_t`. Returns the running interpreter's ID, pre-typed for the atomic CAS parameter list. Must be called with the GIL held / attached. |
| `atomic_int_least64_t` | 64-bit atomic integer type. Sequentially consistent on every supported target. |
| `atomic_load(p)`, `atomic_store(p, v)`, `atomic_fetch_add(p, v)`, `atomic_compare_exchange_strong(p, &exp, des)` | The four atomic ops you will need. SC on every supported MSVC target (x86, x64, ARM64); plain `<stdatomic.h>` elsewhere. |
| `thread_local` | Macro for thread-local storage. Use to cache the per-interpreter `LOCAL_STATE` so callbacks don't walk `PyModule_GetState` every call. |
| `XIDATA_T` | Opaque struct holding a serialised cross-interpreter handoff. |
| `XIDATA_NEW()`, `XIDATA_GETXIDATA(value, xidata)`, `XIDATA_FREE(xidata)`, `XIDATA_SET_FREE(xidata, fn)` | Lifecycle ops. You normally only call `XIDATA_INIT` from your producer callback; the rest is called by bocpy. |
| `XIDATA_INIT(xidata, interp, data, obj, new_object)` | Initialise an `XIDATA_T`. `interp` must be the interpreter that currently owns `data`. Buffer must be freshly allocated. |
| `XIDATA_NEWOBJECT` | Type alias for the consumer-side reconstruction callback. |
| `XIDATA_REGISTERCLASS(type, cb)` | Register a Python `type` as cross-interpreter shareable with producer callback `cb`. **Per-interpreter** — call from the exec slot. |
| `XIDATA_GETDATA_FUNC(name)` | Macro that declares a producer callback with a portable `(tstate, obj, xidata)` signature. Hides the legacy-CPython signature change. |

Internal headers and surfaces (`boc_compat.h`, `boc_cown.h`,
`boc_sched.h`, `boc_tags.h`, `boc_terminator.h`, `boc_noticeboard.h`,
the typed atomics, the BOC mutex/condvar types, `boc_yield`, `boc_now_*`,
`boc_sleep_ns`, etc.) are **not** public. Do not depend on them.

## The lifecycle: a `Counter`-sized walkthrough

The full annotated source is `templates/c_abi_consumer/src/_bocpy_probe.c`.
The skeleton below shows the five places you must get right.

### 1. The impl struct

A heap-allocated C struct that lives outside any single Python object's
lifetime. Carries its own atomic refcount and its atomic owner field.

```c
typedef struct {
    atomic_int_least64_t refcount;   /* number of wrappers holding this impl */
    atomic_int_least64_t owner;      /* interpreter ID, or BOCPY_NO_OWNER in flight */
    /* ... your payload here ... */
} your_impl;

static your_impl *your_impl_new(void) {
    your_impl *impl = PyMem_RawMalloc(sizeof(*impl));
    if (impl == NULL) return NULL;
    atomic_store(&impl->refcount, 1);
    atomic_store(&impl->owner, bocpy_interpid());   /* born owned */
    return impl;
}

static void your_impl_incref(your_impl *impl) {
    atomic_fetch_add(&impl->refcount, 1);
}

static void your_impl_decref(your_impl *impl) {
    if (atomic_fetch_add(&impl->refcount, -1) == 1) {
        /* last holder, free the payload */
        PyMem_RawFree(impl);
    }
}

static bool your_impl_check_acquired(your_impl *impl, bool set_error) {
    if (bocpy_interpid() != atomic_load(&impl->owner)) {
        if (set_error)
            PyErr_SetString(PyExc_RuntimeError,
                            "the current interpreter does not own this type");
        return false;
    }
    return true;
}
```

Refcounting and ownership are **independent**. Any interpreter holding
a wrapper drops its ref on dealloc, regardless of who currently owns
the impl. The proto-Region check guards **data accessors**, not the
lifetime of the impl itself.

### 2. The Python wrapper, as a heap type with per-module state

Use `PyType_FromModuleAndSpec` (not the static-type pattern). Store
the type on per-module state and cache it in a `thread_local`
`LOCAL_STATE` so callbacks can find it without walking module state.

```c
typedef struct {
    PyTypeObject *your_type;
} your_module_state;

static thread_local your_module_state *LOCAL_STATE;

typedef struct {
    PyObject_HEAD
    your_impl *impl;
} YourObject;
```

Data accessors must call `your_impl_check_acquired` before reading
the payload. Identity-only accessors (e.g. an `address` getter that
returns `(uintptr_t)impl`) may skip the check — printing the address
of a Region handle without being inside the Region is allowed.

### 3. The producer callback — declare with `XIDATA_GETDATA_FUNC`

Runs on the interpreter that currently owns the impl, every time
something asks XIData to package one of your objects. CAS the owner
field from this interpreter to `BOCPY_NO_OWNER`, then call
`XIDATA_INIT`. Failing the CAS surfaces as a `RuntimeError` and
aborts the handoff.

```c
XIDATA_GETDATA_FUNC(_your_shared) {
    YourObject *self = (YourObject *)obj;
    your_impl *impl = self->impl;
    if (impl == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "type not initialised");
        return -1;
    }

    int_least64_t expected = bocpy_interpid();
    int_least64_t desired  = BOCPY_NO_OWNER;
    if (!atomic_compare_exchange_strong(&impl->owner, &expected, desired)) {
        PyErr_Format(PyExc_RuntimeError,
                     "cannot share: owned by interpreter %lld",
                     (long long)expected);
        return -1;
    }

    XIDATA_INIT(xidata, tstate->interp, impl, obj, _new_your_object);
    return 0;
}
```

Why `XIDATA_GETDATA_FUNC` and not a hand-written signature? On older
CPython the callback is `(obj, xidata)` only — no `tstate`. The macro
emits a small trampoline so the body is portable across every
supported CPython.

### 4. The consumer callback — `new_object` reconstruction

Runs on the interpreter that is *taking ownership*. CAS the owner
from `BOCPY_NO_OWNER` to `bocpy_interpid()`, allocate a fresh
wrapper from the local heap type, and bump the impl refcount.

```c
static PyObject *_new_your_object(XIDATA_T *xidata) {
    your_impl *impl = (your_impl *)xidata->data;

    int_least64_t expected = BOCPY_NO_OWNER;
    int_least64_t desired  = bocpy_interpid();
    if (!atomic_compare_exchange_strong(&impl->owner, &expected, desired)) {
        PyErr_Format(PyExc_RuntimeError,
                     "cannot acquire (expected BOCPY_NO_OWNER, observed %lld)",
                     (long long)expected);
        return NULL;
    }

    PyTypeObject *type = LOCAL_STATE->your_type;
    YourObject *self = (YourObject *)type->tp_alloc(type, 0);
    if (self == NULL) {
        /* CRITICAL: roll the owner back so a retry can succeed and
         * the impl is not stranded with us as owner without a wrapper. */
        atomic_store(&impl->owner, BOCPY_NO_OWNER);
        return NULL;
    }
    self->impl = impl;
    your_impl_incref(impl);
    return (PyObject *)self;
}
```

The wrapper-allocation rollback is easy to forget and impossible to
recover from at runtime. Write it the same time you write the success
path.

### 5. Module init — multi-phase, per-interpreter-GIL aware

`XIDATA_REGISTERCLASS` registers into a **per-interpreter** registry.
It must run in every interpreter that will reconstruct one of your
objects — which means every worker sub-interpreter, not just the main
one. Single-phase `PyModule_Create` modules load in the main
interpreter but cannot satisfy `Py_MOD_PER_INTERPRETER_GIL_SUPPORTED`,
and the registration never runs in worker interpreters; the consumer
callback then dereferences a NULL `LOCAL_STATE` and segfaults.

Use multi-phase init with a `Py_mod_exec` slot:

```c
static int _your_module_exec(PyObject *module) {
    your_module_state *state =
        (your_module_state *)PyModule_GetState(module);

    state->your_type = (PyTypeObject *)PyType_FromModuleAndSpec(
        module, &YourType_Spec, NULL);
    if (state->your_type == NULL) return -1;
    if (PyModule_AddType(module, state->your_type) < 0) return -1;

    if (XIDATA_REGISTERCLASS(state->your_type, _your_shared)) {
        PyErr_SetString(PyExc_RuntimeError,
                        "could not register type for cross-interpreter sharing");
        return -1;
    }

    LOCAL_STATE = state;   /* prime the thread-local cache */
    return 0;
}

static PyModuleDef_Slot _your_module_slots[] = {
    {Py_mod_exec, (void *)_your_module_exec},
#if PY_VERSION_HEX >= 0x030C0000
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
#endif
#if PY_VERSION_HEX >= 0x030D0000
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
#endif
    {0, NULL},
};
```

Also implement `m_traverse`, `m_clear`, and `m_free` to manage the
type stored on per-module state — this is standard multi-phase
hygiene, not bocpy-specific.

## Hard rules

1. **Top-level `import` of your extension in every Python file that
   schedules `@when` bodies which observe your types.** The
   transpiler propagates module-scope `import` statements into the
   exported per-worker module. Runtime helpers like
   `importlib.import_module(...)`, `__import__(...)`, or
   `pytest.importorskip(...)` are invisible to the transpiler — a
   worker without your extension loaded will skip the exec slot and
   the consumer callback will dereference a NULL `LOCAL_STATE`.
2. **`XIDATA_REGISTERCLASS` belongs in `Py_mod_exec`, never in `PyInit`.**
   The registry is per-interpreter.
3. **Always pair the producer CAS with the consumer CAS.** Producer:
   `bocpy_interpid() -> BOCPY_NO_OWNER`. Consumer:
   `BOCPY_NO_OWNER -> bocpy_interpid()`. Anything else strands the
   impl.
4. **Roll back the owner field on consumer-side wrapper allocation
   failure.** Otherwise the impl is owned-but-unreferenced and no
   future handoff can succeed.
5. **Inside an `@when`, never `send("tag", c.value)` of a proto-Region
   resource.** `send` would atomically move the impl out of the cown
   mid-behavior and leave the worker unable to release the cown
   afterwards. Send a copy (`c.value.copy()`) or send primitive
   summary data (`c.value.address`, a hash, a slice). The cown itself
   is the right primitive for handing the resource to another
   behavior — schedule a downstream `@when` on the same cown.
6. **The cown graph still orders your work.** The C ABI gives you
   safe transport; ordering between behaviors still comes from
   `@when`. If you find yourself reaching for a `threading.Event`,
   atomic flag, or polling loop to coordinate two behaviors on the
   same C type, re-read `thinking-in-boc`.
7. **No `Py_BUILD_CORE` leakage.** Guard for it with `#error` at the
   top of your translation unit; a future bocpy refactor that forgets
   the `#undef` should fail your build loudly, not at runtime.
8. **No C++.** Wrap the ABI in a thin C TU if your project needs C++.

## Common pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| Single-phase `PyModule_Create` | Loads in main interpreter; segfaults in worker. | Switch to multi-phase init with `Py_mod_exec` and declare `Py_MOD_PER_INTERPRETER_GIL_SUPPORTED`. |
| `XIDATA_REGISTERCLASS` in `PyInit` | Consumer callback sees no type registered in its registry. | Move to the exec slot. |
| Forgetting to roll back owner on alloc failure | Impl stranded with no wrapper; future handoffs fail their CAS. | Add `atomic_store(&impl->owner, BOCPY_NO_OWNER);` before returning `NULL` from the consumer callback. |
| `pytest.importorskip("_your_ext")` instead of top-level `import` | Workers skip the exec slot; consumer dereferences NULL `LOCAL_STATE`. | Use a plain top-level `import _your_ext` in any file that schedules `@when` bodies. |
| `send("tag", c.value)` of a proto-Region object | Worker cannot release the cown afterwards; runtime stalls. | Send a copy or summary data. |
| Reading the payload outside a `@when` | `RuntimeError: the current interpreter does not own this type`. | Read inside a `@when` that holds the cown, or use an identity-only accessor (`.address`). |
| Identity-only getter calling the ownership check | `.address` raises `RuntimeError` from interpreters that hold a stale wrapper. | Skip the ownership check on identity/lifetime accessors. |
| Static-type pattern (`PyTypeObject MyType = {...}`) | Type cannot be per-interpreter; `LOCAL_STATE` cache pattern won't fit. | Use `PyType_FromModuleAndSpec` and store the type on per-module state. |
| Including `<stdio.h>` before `<bocpy/bocpy.h>` | Compile error on some toolchains; `Python.h` ordering rule. | Move `<bocpy/bocpy.h>` to the very top of the translation unit. |
| Hand-written `(obj, xidata)` producer callback | Compile error on CPython <3.12 (legacy `(tstate, obj, xidata)`) or vice versa. | Declare the callback with `XIDATA_GETDATA_FUNC(name)`. |
| Building without `--no-build-isolation` | Wheel resolves against PyPI bocpy headers instead of your local install. | Always `pip install --no-build-isolation .`. |
| Refcount and ownership conflated | `__dealloc__` raises `RuntimeError` from interpreters that hold a stale wrapper. | Keep refcount independent of ownership — drop refs on dealloc regardless of who owns the impl. |

## Verification checklist

Before declaring a bocpy-aware C extension done, verify all of:

- [ ] `setup.py` uses `bocpy.get_include()` and `bocpy.get_sources()`.
- [ ] `pyproject.toml` lists `bocpy` in both `[build-system].requires`
      and `[project].dependencies`.
- [ ] Translation unit includes `<bocpy/bocpy.h>` at the top, with an
      `#error` guard against `Py_BUILD_CORE` leakage.
- [ ] Module uses multi-phase init and declares
      `Py_MOD_PER_INTERPRETER_GIL_SUPPORTED`.
- [ ] Type is heap-allocated via `PyType_FromModuleAndSpec` and stored
      on per-module state.
- [ ] `XIDATA_REGISTERCLASS` is called from the `Py_mod_exec` slot.
- [ ] Producer callback declared with `XIDATA_GETDATA_FUNC`.
- [ ] Producer callback CASes owner from `bocpy_interpid()` to
      `BOCPY_NO_OWNER` before `XIDATA_INIT`.
- [ ] Consumer callback CASes owner from `BOCPY_NO_OWNER` to
      `bocpy_interpid()` before allocating the wrapper.
- [ ] Consumer callback rolls owner back to `BOCPY_NO_OWNER` if
      wrapper allocation fails.
- [ ] Data-reading accessors call an `_check_acquired`-style helper
      and raise `RuntimeError` on mismatch.
- [ ] Identity/lifetime accessors do **not** call the check.
- [ ] A pytest test imports the extension at module scope (not via
      `importorskip`) and exercises a `@when` chain that round-trips
      a cown carrying one of your types through at least one worker.
      Use the `testing-with-boc` `send`/`receive` assertion pattern.
- [ ] Tests pass when installed with `pip install --no-build-isolation`.

See `templates/c_abi_consumer/test/test_consumer.py` in the bocpy
source tree for the canonical test pattern — a tail-recursive `@when`
chain that ships a counter cown between workers, with assertions
fired back via `send` / `receive`.
