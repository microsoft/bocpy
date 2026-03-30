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
   ├── @when decorator: captures closure vars, schedules behavior
   ├── Behaviors: runtime lifecycle, worker pool, scheduler thread
   └── Cown: typed data wrapper with acquire/release semantics
       │
       ▼
 Transpiler (transpiler.py)
   └── AST transforms: extracts @when functions, rewrites closures
       as explicit parameters, exports module for worker import
       │
       ▼
 Worker (worker.py)
   └── Sub-interpreter event loop: receives behavior capsules via
       message queue, executes them, sends release messages
       │
       ▼
 C extensions
   ├── _core.c: CownCapsule, BehaviorCapsule, Request (two-phase
   │            locking), lock-free MPSC message queues (16 queues,
   │            tag-based)
   └── _math.c: dense double-precision Matrix
```

### Key data flow

1. `@when(cown_a, cown_b)` → transpiler extracts the decorated function and its
   captured variables → exported as `__behavior__N` in a generated module.
2. The scheduler enqueues a `Request` that performs two-phase locking (2PL) over
   the sorted cown IDs.
3. When all cowns are acquired, the behavior capsule is sent to a worker via
   `send("boc_worker", capsule)`.
4. The worker executes `__behavior__N` with exclusive access to the cowns, then
   sends `("release", bid)` back to the scheduler.
5. The scheduler releases the cowns, allowing waiting behaviors to proceed. The
   result is stored in the `Cown` returned by `@when`.

## Public API

| Symbol | Purpose |
|--------|---------|
| `Cown[T]` | Typed wrapper for concurrently-owned data |
| `@when(*cowns)` | Schedule a behavior with exclusive access to the listed cowns |
| `send(tag, contents)` | Send a message to a tag (lock-free) |
| `receive(tags, timeout, after)` | Selective receive; blocks or times out |
| `drain(tags)` | Clear all queued messages for the given tag(s) |
| `set_tags(tags)` | Pre-assign tags to queues; clears all messages |
| `TIMEOUT` | Sentinel returned by `receive` on timeout |
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
| `src/bocpy/transpiler.py` | AST transformers for `@when` extraction and module export |
| `src/bocpy/worker.py` | Sub-interpreter worker loop |
| `src/bocpy/_core.c` | Cown/behavior capsules, 2PL requests, MPSC message queues |
| `src/bocpy/_math.c` | Dense matrix implementation |
| `src/bocpy/examples/` | Runnable demos (bank, boids, dining philosophers, …) |
| `test/` | pytest test suite |
| `sphinx/` | Sphinx documentation source and build |
| `setup.py` | C extension build configuration |
| `pyproject.toml` | Project metadata, dependencies, entry points |
| `.flake8` | Linting rules (Google style, 120 chars, double quotes) |

## Build and Test

```bash
pip install -e .[test]       # editable install with test deps
pytest -vv                   # run full suite
pip install -e .[linting]    # linting deps
flake8 src/ test/            # lint check
```

C extensions are compiled by setuptools from `_core.c` and `_math.c` during
`pip install`.

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
