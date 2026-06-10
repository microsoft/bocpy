# Downstream BOC project — agent bootstrap prompt

This file is a **prompt you give to a coding agent in a downstream project that
uses `bocpy`**. The agent will install the same multi-lens planning and review
agent structure used by bocpy itself, plus skills and copilot instructions
that teach the agent how to write Behavior-Oriented Concurrency code
correctly.

The downstream agent does NOT need to know anything about bocpy's internals —
just how to consume the public API (`@when`, `Cown`, `send`/`receive`,
`noticeboard`).

---

## How to use this file

1. Open your downstream project (the one that depends on `bocpy`) in VS Code
   with Copilot or any other agent that can run shell, fetch URLs, and create
   files.
2. Copy **everything between the `=== BEGIN PROMPT ===` and `=== END PROMPT ===`
   markers below** into the agent's chat.
3. Before sending, fill in the three placeholders at the top of the prompt:
   - `{{PROJECT_NAME}}` — the name of your downstream library/application
   - `{{PROJECT_ONE_LINER}}` — one sentence describing what it does
   - `{{BOCPY_REPO_URL}}` — the public git URL of the bocpy repo
     (e.g. `https://github.com/your-org/bocpy.git`)
4. Send the prompt. The agent will ask a couple of clarifying questions, then
   create everything.

If your project is a brand-new workspace with no `.github/` directory, the
agent will create one. If a `.github/copilot-instructions.md` already exists,
the agent will ask before overwriting.

---

## === BEGIN PROMPT ===

You are bootstrapping the Copilot/agent customization layer for
**{{PROJECT_NAME}}** ({{PROJECT_ONE_LINER}}), a Python project that depends on
the `bocpy` Behavior-Oriented Concurrency (BOC) library.

Your job is to install a multi-perspective planning and review system plus
BOC-aware skills and instructions, modeled on bocpy's own `.github/` layout.

### Step 0 — Ask the user

Before creating any files, ask the user the following questions and wait for
answers:

1. **Existing `.github/copilot-instructions.md`?** If one exists, should you
   (a) replace it with the BOC-aware template from this prompt, (b) merge the
   relevant sections into the existing file, or (c) leave it alone and only
   create the new agents and skills?
2. **Does this project contain its own C extensions?** If yes, you will
   install two additional skills: `commenting-c-and-python` (C/Python doc
   conventions) and `c-extensions-with-bocpy` (how to write native types
   that can live inside a `Cown` and travel between worker sub-interpreters
   via the bocpy public C ABI). If no, you will skip both.
3. **Python virtual environments.** What venv directory or directories does
   this project use (e.g. `.venv`, `.env312`)? You will record these in the
   copilot instructions so future agent sessions know which to activate.
4. **Default test command.** Is the test runner `pytest` (most common), or
   something else? Include any standard flags (e.g. `-vv`).

Wait for the answers before proceeding.

### Step 1 — Fetch bocpy's agent and skill assets

Most of the files you will install are **verbatim copies** of files from the
bocpy repo. Fetch them with a shallow clone into a temporary location:

```bash
mkdir -p .copilot
git clone --depth 1 {{BOCPY_REPO_URL}} .copilot/_bocpy_bootstrap
```

If the clone fails (no network, private repo, etc.), stop and ask the user
either to provide a local path to a bocpy checkout or to attach the relevant
files manually. Do not invent file contents.

### Step 2 — Create the directory layout

Create these directories at the repo root (use `mkdir -p`; they may already
exist):

```
.github/agents/
.github/skills/multi-perspective-plan/
.github/skills/branch-review/
.github/skills/review-loop/
.github/skills/thinking-in-boc/
.github/skills/testing-with-boc/
.copilot/
```

Only create `.github/skills/commenting-c-and-python/` and
`.github/skills/c-extensions-with-bocpy/` if the user answered "yes" to
question 2 in Step 0.

### Step 3 — Install the lens agents (verbatim copies)

Copy all seven `.agent.md` files from the bocpy clone into your `.github/agents/`
directory **without modification**:

```bash
cp .copilot/_bocpy_bootstrap/.github/agents/adversarial-lens.agent.md  .github/agents/
cp .copilot/_bocpy_bootstrap/.github/agents/conservative-lens.agent.md .github/agents/
cp .copilot/_bocpy_bootstrap/.github/agents/correctness-lens.agent.md  .github/agents/
cp .copilot/_bocpy_bootstrap/.github/agents/security-lens.agent.md     .github/agents/
cp .copilot/_bocpy_bootstrap/.github/agents/speed-lens.agent.md        .github/agents/
cp .copilot/_bocpy_bootstrap/.github/agents/synthesis-lens.agent.md    .github/agents/
cp .copilot/_bocpy_bootstrap/.github/agents/usability-lens.agent.md    .github/agents/
```

These define the perspectives that the planning and review skills spawn as
subagents. Do not edit them — the skills reference them by exact name.

### Step 4 — Install the orchestration skills (verbatim copies)

These three skills coordinate the lens agents. Copy them verbatim:

```bash
cp .copilot/_bocpy_bootstrap/.github/skills/multi-perspective-plan/SKILL.md \
   .github/skills/multi-perspective-plan/SKILL.md

cp .copilot/_bocpy_bootstrap/.github/skills/branch-review/SKILL.md \
   .github/skills/branch-review/SKILL.md

cp .copilot/_bocpy_bootstrap/.github/skills/review-loop/SKILL.md \
   .github/skills/review-loop/SKILL.md
```

What they do:

- **`multi-perspective-plan`** — Spawns the speed, usability, and conservative
  lenses in parallel to draft a plan, runs rebuttals on disagreements, has
  `synthesis-lens` reconcile them, then iteratively hardens the plan via an
  `adversarial-lens` review loop. Use for non-trivial design work.
- **`branch-review`** — Pre-merge code review. Runs correctness, security,
  usability lenses on the branch diff in parallel, then an adversarial gap
  analysis, then synthesis. Produces a unified report and remediation plan.
- **`review-loop`** — Lightweight single-pass review with any chosen lens.
  Use after non-trivial implementations.

All three persist intermediate artifacts under `.copilot/plans/<slug>/` or
`.copilot/reviews/<slug>/` so runs can be resumed if interrupted.

### Step 5 — Install the BOC-knowledge skills (verbatim copies)

These two skills teach the agent how to write BOC code correctly. They apply
unchanged to downstream consumers of bocpy:

```bash
cp .copilot/_bocpy_bootstrap/.github/skills/thinking-in-boc/SKILL.md \
   .github/skills/thinking-in-boc/SKILL.md

cp .copilot/_bocpy_bootstrap/.github/skills/testing-with-boc/SKILL.md \
   .github/skills/testing-with-boc/SKILL.md
```

What they do:

- **`thinking-in-boc`** — A corrective. Catches the reflex to reach for
  `time.sleep`, `threading.Event`, polling loops, `Future`, atomic counters,
  etc. when the correct BOC answer is "make it a cown and schedule a behavior
  on it". This is the single most important skill for someone learning BOC.
- **`testing-with-boc`** — How to write pytest tests against `@when`, `Cown`,
  `send`/`receive`, the noticeboard, exception propagation, and cown grouping.
  Covers the parameter-count rule, the module-level class requirement, and
  the `send`/`receive` assertion pattern.

If the user answered "yes" to C extensions in Step 0, also copy these two:

```bash
cp .copilot/_bocpy_bootstrap/.github/skills/commenting-c-and-python/SKILL.md \
   .github/skills/commenting-c-and-python/SKILL.md

cp .copilot/_bocpy_bootstrap/.github/skills/c-extensions-with-bocpy/SKILL.md \
   .github/skills/c-extensions-with-bocpy/SKILL.md
```

- **`commenting-c-and-python`** — the doc-comment conventions used across
  bocpy's own C and Python sources. Apply when your project mixes the two.
- **`c-extensions-with-bocpy`** — how to write a downstream C extension
  whose custom types (matrices, buffers, GPU handles, opaque C resources)
  can live inside a `Cown` and travel between worker sub-interpreters via
  the bocpy public C ABI. Covers `setup.py` boilerplate, multi-phase init,
  `XIDATA_REGISTERCLASS`, the producer/consumer callback pair, and the
  proto-Region ownership discipline (`BOCPY_NO_OWNER` / `bocpy_interpid()`).
  Skip this skill only if the project's C extensions do **not** define any
  types that need to cross interpreter boundaries through bocpy.

Do **not** copy these bocpy-internal skills — they do not apply to downstream
consumers:

- `testing-message-queue` (tests bocpy's internal MPSC implementation, not
  the public `send`/`receive` API; that is already covered by
  `testing-with-boc`)
- `finalize-pr` (bumps bocpy's own version files and runs the release
  polish workflow)

### Step 6 — Create `.github/copilot-instructions.md`

This is the file that requires customization. Honor the user's answer to
Step 0 question 1:

- If they said **replace**: write the template below as the new file.
- If they said **merge**: insert the "BOC programming primer" and "How to
  work on this project" sections from the template into the existing file,
  preserving everything project-specific that is already there. Ask the user
  to review your merge before saving.
- If they said **leave alone**: skip this step entirely.

Use this template, filling in the placeholders with the answers from Step 0:

````markdown
# {{PROJECT_NAME}} — Copilot Instructions

## What {{PROJECT_NAME}} Is

{{PROJECT_ONE_LINER}}

This project depends on [`bocpy`](https://pypi.org/project/bocpy/), a Python
library implementing **Behavior-Oriented Concurrency (BOC)**. BOC eliminates
data races and deadlocks by construction: data lives inside **cowns**
(concurrently-owned wrappers), and code runs as **behaviors** that the
scheduler dispatches once all required cowns are available. Workers run in
sub-interpreters and are truly parallel on Python 3.12+.

Before writing or reviewing concurrency code in this project, read the
`thinking-in-boc` skill at `.github/skills/thinking-in-boc/SKILL.md`. It is
short, opinionated, and prevents the most common class of mistake: reaching
for threads-and-locks primitives instead of expressing the dependency through
the cown graph.

## BOC programming primer

The full reference is the `thinking-in-boc` skill. The short version:

| You wrote | What you almost certainly meant |
|-----------|---------------------------------|
| `time.sleep(...)` in a polling loop | Schedule a behavior on the cown the predicate depends on. |
| `while not <flag>: ...` busy-wait | Make `<flag>` a cown and `@when(flag)` a behavior on it. |
| `threading.Event` / `Condition` / `Lock` | A cown plus a behavior chain. |
| `Future` / `Queue.get()` to ferry a value out | `return` the value from a behavior; `@when(behavior)` reads it. |
| Loop inside one behavior to process many items | A **behavior loop**: process one chunk, then `@when(state)` again. |
| `wait_for_*` helpers / polling | Replace with `@when(downstream_cowns)`; let the cown graph do the ordering. |

The five replacement patterns you should know cold:

1. **Sequencing on data** — `@when(x)` runs after any prior `@when(x)`
   completes. That is the entire ordering mechanism.
2. **Multi-cown / barrier** — `@when(a, b, c)` when you know the cowns at
   write-time; `@when(cowns)` (a single list arg) when the set is dynamic.
3. **Happens-after across unrelated data** — chain on the prior behavior's
   result cown: `@when(x, prior_behavior)`.
4. **Run when any worker is free** — `@when()` (no args) for fire-and-forget
   follow-ups that should not block the current behavior.
5. **Behavior loops** — to process work in chunks, schedule the next iteration
   from inside the current one with `@when(state)`. Never write a `while`
   loop inside a single behavior.

### Public bocpy API (the only surface you should touch)

| Symbol | Purpose |
|--------|---------|
| `Cown[T]` | Typed wrapper for concurrently-owned data. Read/write `.value` only inside an `@when` that holds the cown. `.exception` is `True` if the behavior that produced this cown's value raised. |
| `@when(*cowns)` | Decorator. Schedules the function as a behavior with exclusive access to the listed cowns. The decorated function must take **exactly** as many parameters as `@when` got arguments. Default args count — do not use them. |
| `send(tag, contents)` | Cross-interpreter message send. Non-blocking. |
| `receive(tags, timeout, after)` | Selective receive. Returns `(TIMEOUT, None)` on timeout. |
| `drain(tags)` | Clear all queued messages for the given tag(s). |
| `set_tags(tags)` | Pre-assign tags to queues; clears all messages. |
| `TIMEOUT` | Sentinel returned by `receive` on timeout. |
| `noticeboard()` | Read a per-behavior snapshot of the global key-value store. |
| `notice_read(key, default)` | Read a single key from the snapshot. |
| `notice_write(key, value)` | Non-blocking write. |
| `notice_update(key, fn, default)` | Atomic read-modify-write. `fn` must be picklable (module-level function or `functools.partial`). Return `REMOVED` to delete. |
| `notice_delete(key)` | Non-blocking delete. |
| `REMOVED` | Sentinel for deleting via `notice_update`. |
| `wait(timeout)` | Block until all scheduled behaviors complete; stops the runtime. |
| `start(workers, export_dir, module)` | Manually start the runtime (auto-called on first `@when`). |

### Hard rules

- **Classes and functions used inside a behavior must be defined at module
  level.** Behaviors run in sub-interpreters that import your module; locally-
  defined classes inside a test method or function cannot be resolved.
- **Parameter count must match `@when` argument count exactly.** A mismatch
  crashes the worker silently and the behavior never completes — your test
  will hang unless `receive` has a timeout.
- **Do not use `def _(c, x=x)` to snapshot a loop variable.** The transpiler
  already snapshots captures by value at schedule time. Adding `x=x`
  introduces an extra parameter that breaks the call.
- **No `time.sleep`, `threading.*`, atomics, or polling inside a behavior or
  in code that drives one.** Those primitives are only correct (a) outside
  the runtime when talking to it (e.g. a test thread blocking on `receive`
  for an assertion), (b) inside `wait()` itself, or (c) inside bocpy's own
  C internals. If you are not in one of those three places, re-derive the
  design through the cown graph.

## Project layout

Fill this section in with the structure of {{PROJECT_NAME}}. At minimum,
list:

- where the source lives (e.g. `src/{{PROJECT_NAME}}/`)
- where tests live (e.g. `test/`)
- where examples or scripts live, if any
- any extension modules or generated files

## Scratch and temporary files

Use the `.copilot/` directory at the repo root for **all** temporary files:
diffs saved for review, scratch scripts, generated transpiler output, ad-hoc
notes, intermediate command output, plan/review artifacts. The directory is
gitignored.

- **Do not use `/tmp`** or any other system temp location. Keeping scratch
  files inside the repo means they survive across tool calls in the same
  session and are easy to find again.
- **Look in `.copilot/` first** when searching for prior scratch artifacts.
  Standard search tools respect `.gitignore`, so you may need to pass
  include-ignored flags to see these files.
- Create the directory with `mkdir -p .copilot` if it does not yet exist.

## Build and test

Always activate the project virtual environment first. This project uses:

{{LIST_OF_VENVS_FROM_USER}}

If the user does not specify a venv at the start of a session, suggest the
default and wait for confirmation. Never run `pip`, `pytest`, `python`, or
any project command outside the activated venv.

Typical workflow:

```bash
source {{DEFAULT_VENV}}/bin/activate
pip install -e .[test]       # editable install with test deps
{{DEFAULT_TEST_COMMAND}}     # run the test suite
```

Re-installing in a fresh venv triggers a rebuild of any C extensions against
that interpreter's headers.

---

## How to work on this project

### Move slow to go fast

Break every task into small, testable steps. Do not attempt to fix or implement
multiple things in a single pass. Each step should be independently verifiable
before moving on.

### Plan before you act

Before making changes:

1. **Write a plan** — outline the steps you intend to take. Save it in a
   session memory plan file so it stays in context throughout the task.
2. **Get the plan approved** — present the plan and wait for explicit approval
   before writing any code.
3. **Update the plan as you go** — record progress, findings, and any
   deviations in the plan file so context is never lost.

### Baseline the tests

Before modifying any code:

1. Run the full test suite and record the results in your plan file.
2. Note any pre-existing failures so you can distinguish them from regressions
   you introduce.
3. Keep this baseline in context for the duration of the task.

### Review every non-trivial change

After implementing a change, run the **review-loop** skill to get an
independent review. This may be skipped for trivial changesets, but you do
not decide what qualifies as trivial — ask for approval first.

For a complete pre-merge audit, use **branch-review** instead, which runs
three constructive reviewer lenses plus an adversarial gap-analysis pass over
the branch diff.

For non-trivial design work (multi-subsystem changes, architecture
decisions), use **multi-perspective-plan** to draft and stress-test the plan
with competing lens subagents before any code is written.

Other skills available in `.github/skills/`:

- **thinking-in-boc** — the BOC mental model. Read this any time you catch
  yourself reaching for a classical synchronization primitive.
- **testing-with-boc** — how to write pytest tests against `@when`, `Cown`,
  noticeboard, exception propagation, and cown grouping, including the
  `send`/`receive` assertion pattern.
- **c-extensions-with-bocpy** *(only if this project has C extensions)* —
  how to write a native type whose instances can live inside a `Cown` and
  cross worker sub-interpreters via the bocpy public C ABI. Covers
  `XIDATA_REGISTERCLASS`, multi-phase init, the producer/consumer callback
  pair, and the proto-Region ownership discipline. Read this **before**
  designing any C type that will be wrapped in a `Cown`.
- **commenting-c-and-python** *(only if this project has C extensions)* —
  the doc-comment conventions used across bocpy's own C and Python sources.

The user is your collaborator. If you are unsure how to address a reviewer's
comment, ask rather than guessing.

### Fix root causes, not symptoms

When diagnosing a bug or unexpected behavior, trace the problem to its
origin. Do not apply surface-level patches that silence an error or make a
test pass without understanding **why** the failure occurred. A fix that
papers over a symptom often hides a deeper defect that will resurface later
in a harder-to-debug form.

Before writing a fix:

1. **Reproduce** — confirm the failure with a minimal case.
2. **Trace** — follow the control and data flow back to where things first go
   wrong, not where they are first observed.
3. **Understand** — articulate the root cause in the plan file before
   proposing a change.
4. **Verify** — ensure the fix addresses the root cause and does not merely
   suppress the symptom.

If the root cause is ambiguous or spans multiple subsystems, surface that
uncertainty rather than guessing. Ask for guidance.

For concurrency bugs specifically: if you find yourself reaching for a
classical synchronization primitive to "fix" the issue, stop and re-read
`thinking-in-boc`. The root cause is almost always a missing cown
dependency, not a missing lock.

### You do not commit code

All git operations (commit, push, branch management) are the user's
responsibility. Do not run `git commit` or `git push`.

### Test your changes

Every change must be tested:

- If test coverage already exists, run the relevant tests and confirm they
  pass.
- If coverage does not exist, **add tests** before considering the change
  done. Follow the patterns in `testing-with-boc`.
- Where appropriate, make tests **fuzzable** — parameterize over random or
  generated inputs so they surface bugs that hand-picked cases might miss.
````

### Step 7 — Add `.copilot/` to `.gitignore`

Ensure the repo's `.gitignore` ignores the scratch directory:

```bash
if ! grep -q '^\.copilot/' .gitignore 2>/dev/null; then
    printf '\n# Agent scratch directory\n.copilot/\n' >> .gitignore
fi
```

### Step 8 — Clean up the bootstrap clone

```bash
rm -rf .copilot/_bocpy_bootstrap
```

### Step 9 — Verify and report

Confirm that all of these files exist and are non-empty:

```
.github/agents/adversarial-lens.agent.md
.github/agents/conservative-lens.agent.md
.github/agents/correctness-lens.agent.md
.github/agents/security-lens.agent.md
.github/agents/speed-lens.agent.md
.github/agents/synthesis-lens.agent.md
.github/agents/usability-lens.agent.md
.github/skills/multi-perspective-plan/SKILL.md
.github/skills/branch-review/SKILL.md
.github/skills/review-loop/SKILL.md
.github/skills/thinking-in-boc/SKILL.md
.github/skills/testing-with-boc/SKILL.md
.github/copilot-instructions.md   (unless the user opted out)
```

Plus, if the user answered "yes" to C extensions:

```
.github/skills/commenting-c-and-python/SKILL.md
.github/skills/c-extensions-with-bocpy/SKILL.md
```

Print a short summary listing each file that was created, modified, or
intentionally skipped, then stop. Do **not** start any other work in the
same session — the user will drive the next task with the new agent
configuration loaded.

## === END PROMPT ===

---

## What the installed structure gives the downstream project

After running the prompt above, the downstream project has:

- **Seven lens subagents** in `.github/agents/` (adversarial, conservative,
  correctness, security, speed, synthesis, usability) that competing
  perspectives invoke during planning and review.
- **Three orchestration skills** in `.github/skills/`:
  - `multi-perspective-plan` — design-time planning with rebuttals and an
    adversarial hardening loop.
  - `branch-review` — pre-merge multi-lens code review with synthesis and
    adversarial gap analysis.
  - `review-loop` — lightweight single-pass review for any chosen lens.
- **Two BOC-knowledge skills** in `.github/skills/`:
  - `thinking-in-boc` — the mental model corrective that prevents
    threads-and-locks reflexes.
  - `testing-with-boc` — how to write pytest tests against the bocpy API.
- **Optionally, two C-extension skills** (installed only if the project has
  its own C extensions):
  - `commenting-c-and-python` — doc-comment conventions for mixed C/Python
    codebases.
  - `c-extensions-with-bocpy` — how to write native types whose instances
    can live inside a `Cown` and travel between workers via the public C
    ABI (`bocpy.h`, `XIDATA_REGISTERCLASS`, proto-Region ownership).
- **A customized `copilot-instructions.md`** containing the same working
  philosophy as bocpy (move slow, plan, baseline, review, fix root causes,
  test changes, you do not commit) plus a BOC programming primer that points
  back to `thinking-in-boc` for every common concurrency-pattern question.
- **`.copilot/`** gitignored as the canonical scratch directory.

The downstream project does **not** get bocpy-internal skills
(`testing-message-queue`, `finalize-pr`, the bocpy-specific
architecture/transpiler sections). Those are not useful unless you are
hacking on bocpy itself.

## Keeping in sync with bocpy upstream

The agent files and skill files are verbatim copies. To pick up changes from
bocpy upstream later, re-run Steps 1–5 of the prompt — they will overwrite
the verbatim files but leave `copilot-instructions.md` alone (Step 6 is
gated on the user's answer to Step 0 question 1).
