# bocpy — Copilot Instructions

## What bocpy Is

bocpy is a Python library implementing **Behavior-Oriented Concurrency (BOC)** —
a concurrency paradigm that eliminates data races and deadlocks by construction.
Instead of locks and shared mutable state, programmers wrap data in **cowns**
(concurrently-owned objects) and schedule **behaviors** that execute once all
required cowns are available. The scheduler acquires cowns in a deterministic
order (two-phase locking over sorted cown IDs), guaranteeing deadlock freedom.

Workers run in **Python sub-interpreters** — on Python 3.12+ these are truly
parallel (per-interpreter GIL). An Erlang-style **message queue** (`send`/`receive`)
implemented as a lock-free multi-producer single-consumer (MPSC) ring buffer in
C provides cross-interpreter communication.

## Architecture

```
User code (@when)
       │
       ▼
 Python layer (behaviors.py)
   ├── @when decorator: registers the behavior in the marshalled-code
   │     registry and schedules it directly from the caller's thread
   │     (no central queue hop)
   ├── Behaviors: runtime lifecycle, worker pool, terminator
   │     (no scheduler thread; scheduling and release run on the
   │      threads that need them — caller and worker)
   ├── Cown: typed data wrapper with acquire/release semantics
   └── Noticeboard: notice_write/update/delete/read, noticeboard()
         (global key-value store, snapshot-per-behavior; mutators
          are serialized through one dedicated noticeboard thread)
       │
       ▼
 Transpiler (transpiler.py)
   └── AST reduction: reduces __main__ to its bindings (imports,
       classes, functions, constants) for worker import
       │
       ▼
 Worker (worker.py)
   └── Sub-interpreter event loop: receives behavior capsules via
       boc_worker queue, executes them, then releases cowns and
       decrements the terminator on the worker thread itself
       │
       ▼
 C extensions
   ├── _core.c: CownCapsule, BehaviorCapsule, BOCBehavior +
   │            BOCRequest array (two-phase locking), C-level
   │            terminator, lock-free MPSC message queues (16
   │            queues, tag-based), global Noticeboard (mutex-
   │            protected, up to 64 entries with thread-local
   │            snapshot cache + monotonic version counter)
   └── _math.c: dense double-precision Matrix
```

### Key data flow

1. `@when(cown_a, cown_b)` decorates a function → `register_behavior`
   content-addresses the code object into the marshalled-code registry under
   a canonical hex key, and the trailing default-valued parameters are
   snapshotted as captures.
2. `whencall` (caller's thread) increments the C terminator and calls
   `_core.behavior_schedule`, which performs two-phase locking (2PL) over
   the sorted cown IDs in C, releasing the GIL across the lock-free link
   loops.
3. When all cowns are acquired, `behavior_resolve_one` enqueues the
   `BehaviorCapsule` directly to the `boc_worker` queue — no central
   scheduler hop.
4. A worker pops the capsule, resolves the behavior function from the
   registry by its hex key (via the per-interpreter `Resolver`), executes it
   with exclusive access to the cowns, then on the same worker thread calls
   `behavior_release_all` (MCS unlink + handoff to the next behavior
   waiting on each cown) and `terminator_dec`.
5. Releasing a cown may resolve the next waiting behavior, which is
   dispatched directly to a worker without touching any central queue.
   The result is stored in the `Cown` returned by `@when`.
6. `wait()` blocks on `terminator_wait` until the C-level count reaches
   zero; `stop()` then drains the workers and the noticeboard thread.

## Public API

| Symbol | Purpose |
|--------|---------|
| `Cown[T]` | Typed wrapper for concurrently-owned data (with `.value` and `.exception` properties) |
| `@when(*cowns)` | Schedule a behavior with exclusive access to the listed cowns |
| `whencall(func, args, captures)` | Lower-level form of `@when`; registers and schedules a behavior function against cowns and capture values |
| `send(tag, contents)` | Send a message to a tag (lock-free) |
| `receive(tags, timeout, after)` | Selective receive; blocks or times out |
| `drain(tags)` | Clear all queued messages for the given tag(s) |
| `set_tags(tags)` | Pre-assign tags to queues; clears all messages |
| `TIMEOUT` | Sentinel returned by `receive` on timeout |
| `noticeboard()` | Read a per-behavior cached snapshot of the global key-value store |
| `notice_read(key, default)` | Convenience: read a single key from the snapshot |
| `notice_write(key, value)` | Non-blocking write to the noticeboard |
| `notice_update(key, fn, default)` | Atomic read-modify-write; returning `REMOVED` deletes the entry |
| `notice_delete(key)` | Non-blocking delete of a single noticeboard entry |
| `REMOVED` | Sentinel returned by a `notice_update` fn to delete the entry |
| `wait(timeout)` | Block until all behaviors complete; stops the runtime |
| `start(workers, export_dir, module)` | Manually start the runtime (auto-called on first `@when`) |
| `Matrix` | Dense 2D matrix of doubles with C-backed arithmetic |
| `WORKER_COUNT` | Default worker count (CPU count − 1) |

## Project Layout

| Path | Contents |
|------|----------|
| `src/bocpy/__init__.py` | Public re-exports |
| `src/bocpy/__init__.pyi` | Type stubs (Sphinx-style docstrings) |
| `src/bocpy/behaviors.py` | `Cown`, `@when`, `Behaviors` scheduler, runtime lifecycle |
| `src/bocpy/transpiler.py` | AST reduction of `__main__` to its bindings module for worker import |
| `src/bocpy/worker.py` | Sub-interpreter worker loop |
| `src/bocpy/_core.c` | Cown/behavior capsules, 2PL requests, MPSC message queues |
| `src/bocpy/_math.c` | Dense matrix implementation |
| `src/bocpy/examples/` | Runnable demos (bank, boids, dining philosophers, …) |
| `test/` | pytest test suite |
| `sphinx/` | Sphinx documentation source and build |
| `setup.py` | C extension build configuration |
| `pyproject.toml` | Project metadata, dependencies, entry points |
| `.flake8` | Linting rules (Google style, 120 chars, double quotes) |
| `.copilot/` | Scratch directory for temporary files (gitignored) |

## Scratch and Temporary Files

Use the `.copilot/` directory at the repo root for **all** temporary files:
diffs saved for review, scratch scripts, generated transpiler output, ad-hoc
notes, intermediate command output, etc. The directory is gitignored.

- **Do not use `/tmp`** or any other system temp location. Keeping scratch
  files inside the repo means they survive across tool calls in the same
  session and are easy for you to find again.
- **Look in `.copilot/` first** when searching for prior scratch artifacts
  (saved diffs, exported modules, plan notes). Standard search tools
  respect `.gitignore`, so you may need to pass include flags to see
  these files.
- Create the directory with `mkdir -p .copilot` if it does not yet exist.

## Build and Test

**Always activate a project virtual environment first.** The repository keeps
several side-by-side venvs (one per Python version / build flavor) because the
underlying `XIData` API changes between Python releases and the C extension
needs version-specific testing. At the time of writing the following venvs
exist at the repo root:

| venv | Python flavor |
|------|---------------|
| `.env312` | CPython 3.12 |
| `.env313d` | CPython 3.13 (debug build) |
| `.env313t` | CPython 3.13 (free-threaded) |
| `.env314` | CPython 3.14 (default for most work) |
| `.env315` | CPython 3.15 |
| `.env315t` | CPython 3.15 (free-threaded) |

**At the start of a session, ask the user which venv to use** before running
any `pip`, `pytest`, `python`, or other project command. Do not assume
`.env314`; the user may be debugging a version-specific issue and have a
different venv in mind. If the user does not specify, suggest `.env314` as the
default but wait for confirmation.

```bash
source .env314/bin/activate  # or whichever venv the user picked
pip install -e .[test]       # editable install with test deps
pytest -vv                   # run full suite
pip install -e .[linting]    # linting deps
flake8 src/ test/ examples/  # lint check
```

The private `bocpy._internal_test` C extension (used by
`test_internal_mpmcq.py`, `test_internal_wsq.py`, and
`test_compat_atomics.py`) is **not** built by default — it is gated off
in [setup.py](../setup.py) so it never ships in distributed wheels. To
run those test files locally, opt in at install time:

```bash
BOCPY_BUILD_INTERNAL_TESTS=1 pip install -e .[test]
```

Without the env var, the affected tests skip cleanly via
`pytest.importorskip`. CI sets the variable at the workflow level in
`.github/workflows/pr_gate.yml`.

Never run `pip`, `pytest`, `python`, or any project command outside the
activated venv. If you need to validate a fix against more than one Python
version, re-install and re-run the suite in each relevant venv.

C extensions are compiled by setuptools from `_core.c` and `_math.c` during
`pip install`. Re-installing in a fresh venv triggers a rebuild against that
interpreter's headers.

## Inspecting the Worker Bindings Module

`@when` is a **runtime decorator** (`src/bocpy/behaviors.py`). It content-
addresses the decorated function's code object into the marshalled-code
registry and dispatches it to workers by hex key — there is no
`__behavior__N` extraction or call-site rewriting any more.

Captures are the decorated function's **trailing parameters**: every
parameter beyond the cown count must carry a default value, and those
defaults are snapshotted at schedule time (so `def b(c, i=i)` captures the
loop value). A capture-count mismatch raises `TypeError` at decoration.

The transpiler (`src/bocpy/transpiler.py`) now only reduces the `__main__`
module to a **bindings module** — its imports, classes, module-level
functions, and UPPERCASE constants — which worker sub-interpreters import so
a behavior's free globals resolve. It no longer rewrites call sites or
extracts behaviors.

To inspect that bindings module, call `bind_file` in a REPL:

```python
from bocpy.transpiler import bind_file
print(bind_file("path/to/your_module.py").code)
```

The bindings module is exactly what worker interpreters import. Reach for it
whenever a behavior's global, class, or constant fails to resolve on a
worker in a way that is not explained by the source as written.

---

## How to Work on This Project

### Move slow to go fast

Break every task into small, testable steps. Do not attempt to fix or implement
multiple things in a single pass. Each step should be independently verifiable
before moving on.

### Plan before you act

Before making changes:

1. **Write a plan** — outline the steps you intend to take. Save this in a
   session memory plan file so it stays in context throughout the task.
2. **Get the plan approved** — present the plan and wait for explicit approval
   before writing any code.
3. **Update the plan as you go** — record progress, findings, and any deviations
   in the plan file so context is never lost.

### Baseline the tests

Before modifying any code:

1. Run the full test suite and record the results in your plan file.
2. Note any pre-existing failures so you can distinguish them from regressions
   you introduce.
3. Keep this baseline in context for the duration of the task.

### Review every non-trivial change

After implementing a change, run the **review-loop** skill to get an independent
review. This may be skipped for trivial changesets, but you do not decide what
qualifies as trivial — ask for approval first.

For a complete pre-merge audit, use **branch-review** instead, which runs three
constructive reviewer lenses plus an adversarial gap-analysis pass over the
branch diff.

For non-trivial design work (multi-subsystem changes, architecture decisions),
use **multi-perspective-plan** to draft and stress-test the plan with competing
lens subagents before any code is written.

When a branch is ready to merge, use the **finalize-pr** skill to bump the
version across all required files, add a CHANGELOG entry, update the Sphinx
and README docs, scrub comment debt with the **editor-lens** agent, and run
the local mirror of the PR-gate lint and test jobs. This skill replaces the
older `version-bump` skill.

Other skills available in `.github/skills/`:

- **commenting-c-and-python** — the C and Python doc/comment conventions used
  across `_core.c`, `_math.c`, `behaviors.py`, `transpiler.py`, and the
  `__init__.pyi` stub.
- **testing-with-boc** — how to write pytest tests against `@when`, `Cown`,
  noticeboard, and `Cown.exception`, including the `send`/`receive` assertion
  pattern.
- **testing-message-queue** — how to write tests for the lock-free MPSC queue
  (`send`/`receive`/`set_tags`/`drain`).
- **c-extensions-with-bocpy** — how to write a downstream C extension whose
  custom types can live inside a `Cown` and travel between worker
  sub-interpreters via the public C ABI. Use when touching `_math.c`,
  `templates/c_abi_consumer/`, or any code that goes through
  `XIDATA_REGISTERCLASS` / `XIDATA_GETDATA_FUNC` / the proto-Region
  ownership pattern.

The **editor-lens** agent (`.github/agents/editor-lens.agent.md`) is the
counterweight to `usability-lens`: it ruthlessly cuts review-process
scaffolding, archaeology, and stale comments from the source tree. It is
normally invoked as a step of `finalize-pr`, but you can run it standalone
via `review-loop` whenever comment debt has built up between PRs.

I am still your collaborator. If you are unsure how to address a reviewer's
comment, ask me rather than guessing.

### Fix root causes, not symptoms

When diagnosing a bug or unexpected behavior, trace the problem to its origin.
Do not apply surface-level patches that silence an error or make a test pass
without understanding **why** the failure occurred. A fix that papers over a
symptom often hides a deeper defect that will resurface later in a harder-to-debug
form.

Before writing a fix:

1. **Reproduce** — confirm the failure with a minimal case.
2. **Trace** — follow the control and data flow back to where things first go
   wrong, not where they are first observed.
3. **Understand** — articulate the root cause in the plan file before proposing
   a change.
4. **Verify** — ensure the fix addresses the root cause and does not merely
   suppress the symptom.

If the root cause is ambiguous or spans multiple subsystems, surface that
uncertainty rather than guessing. Ask for guidance.

### You do not commit code

All git operations (commit, push, branch management) are my responsibility. Do
not run git commit or git push.

### Test your changes

Every change must be tested:

- If test coverage already exists, run the relevant tests and confirm they pass.
- If coverage does not exist, **add tests** before considering the change done.
- Where appropriate, make tests **fuzzable** — parameterize over random or
  generated inputs so they surface bugs that hand-picked cases might miss.
