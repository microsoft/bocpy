# bocpy C-ABI consumer smoke test

This directory is **both** a CI smoke test for the bocpy public C ABI
and the canonical downstream template for an extension that wants to
build against it.

## Files

| File                       | Purpose                                                                 |
|----------------------------|-------------------------------------------------------------------------|
| `src/_bocpy_probe.c`       | Tiny C extension; `#include "bocpy.h"` only; exercises the atomic surface and the XIData allocator; calls `XIDATA_REGISTERCLASS` once at module init. |
| `setup.py`                 | Uses `bocpy.get_include()` and `bocpy.get_sources()` — copy this verbatim into your own project and change the module name. |
| `pyproject.toml`           | PEP 517 metadata; declares `bocpy` as a build- and run-time dependency. |
| `test/test_consumer.py`    | pytest module that imports `_bocpy_probe` and asserts the documented behaviour. |

## Running locally

Run all commands from the bocpy repo root:

```bash
pip install -e .[test]                                        # install bocpy itself
pip install --no-build-isolation ./templates/c_abi_consumer   # build and install the consumer
pytest templates/c_abi_consumer/test                          # run the consumer's tests
```

``--no-build-isolation`` is required so the consumer is built
against the same `bocpy` install you import at test time, rather
than whatever PyPI happens to publish.

CI runs the same three commands on every supported (Python, OS) cell;
if anything in the public C ABI silently regresses (a leaked
`Py_BUILD_CORE`, a renamed atomic op, a `bocpy.get_include()` that no
longer points at `bocpy.h`, …) one of those three steps fails loudly.

## Using this as a template

Drop `setup.py` and `pyproject.toml` into your own project, change
`_bocpy_probe` to your module name in **all three** of:

* `pyproject.toml` (the `[project].name` field, plus the
  `[build-system].requires` list if you keep it),
* `setup.py` (the first argument to `Extension(...)`),
* `src/_bocpy_probe.c` (the `PyInit__bocpy_probe` function name and
  the `_bocpy_probe_module*` identifiers — they must match the
  module name CPython looks up).

Then replace `src/_bocpy_probe.c` with your own sources. The
`bocpy.get_sources()` call appends the MSVC out-of-line bodies on
Windows and is a no-op elsewhere, so the same build script works on
every platform.

## Pinning `bocpy`

Both `[build-system].requires` and `[project].dependencies` use a
PEP 440 compatible-release bound (``bocpy~=0.7``). Bump this version
specifier in lock-step with the root ``pyproject.toml`` whenever the
public C ABI changes — the ``finalize-pr`` skill lists every file
that must move together.

### Per-interpreter requirements

`_bocpy_probe.c` uses multi-phase initialisation (`Py_mod_exec`) and
declares `Py_MOD_PER_INTERPRETER_GIL_SUPPORTED`. bocpy workers run in
sub-interpreters on every supported CPython, and
`XIDATA_REGISTERCLASS` registers types into a per-interpreter
registry, so a single-phase `PyModule_Create` module that registers
from `PyInit` will load in the main interpreter but segfault when a
worker reconstructs one of your types.

Two corollaries for downstream code:

1. The `Counter` type is heap-allocated via `PyType_FromModuleAndSpec`
   and stored on per-module state, with a `thread_local` cache primed
   in the exec slot. Mirror this pattern for your own types.
2. Any test or `@when`-scheduling code that reconstructs your types
   in a worker must contain a top-level `import` of your extension.
   The transpiler propagates module-scope imports into worker
   interpreters; runtime helpers like `pytest.importorskip` are
   invisible to it.

See the bocpy C-ABI documentation ("Consumer modules and worker
sub-interpreters") for the full contract.
