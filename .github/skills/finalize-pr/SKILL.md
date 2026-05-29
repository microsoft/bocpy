---
name: finalize-pr
description: "Finalize a feature branch for merge. Use when finishing a feature branch, preparing for merge, releasing a new version, bumping the version, adding a changelog entry, or when asked to finalize, wrap up, or close out a PR. Covers version bump across all files, CHANGELOG entry, Sphinx + README updates, editor-lens pass over the diff, lint, and test verification. Replaces the older version-bump skill."
argument-hint: "Optional: target version (e.g. '0.7.0'); otherwise inferred from scope of changes"
---

# Finalize PR

Prepares a feature branch for merge by bumping the version, writing a
changelog entry, updating documentation, scrubbing comment debt, and
verifying the change with lint + the full test suite. The user reviews
and commits — this skill never commits.

## When to Use

- A feature branch is ready to merge and needs the final release polish
- Bumping the bocpy version (replaces the older `version-bump` skill)
- Adding a CHANGELOG entry
- Pre-merge documentation sweep

For a multi-perspective pre-merge review of the *code itself*, run
`branch-review` first. This skill assumes the code is settled and the
test suite is green.

## Prerequisites

Before invoking this skill:

- All code changes are complete
- The test suite passes locally in the venv you intend to use
- A `branch-review` pass (or equivalent) has been run if the change is
  non-trivial — see `.github/copilot-instructions.md`
- You know which venv this work targets (`.env312`, `.env313d`,
  `.env313t`, `.env314`, `.env315`, `.env315t`). Ask the user if
  unsure; default is `.env314`.

## Procedure

Execute these steps in order. Pause for user review between steps —
this is a wrap-up workflow, not an autonomous one.

### Step 1 — Determine What Changed

Gather context about the branch:

1. Read the current version from `pyproject.toml` (`[project] version`).
2. Run `git log --oneline main..HEAD` to see commits on the branch.
3. Run `git diff main --stat` to see changed files.
4. Read the current `CHANGELOG.md` to see the most recent entry format
   and any existing `## Unreleased` section.
5. If a session plan file exists under `.copilot/plans/` for this
   branch, read it to understand the scope of work.

Summarize the work performed (what was added, what was changed, what
was removed, any breaking changes) and confirm with the user before
proceeding. The summary drives every later step.

### Step 2 — Choose the New Version

bocpy follows semantic versioning. Propose the version bump based on
the Step 1 summary:

| Bump | When |
|------|------|
| **Patch** (`0.6.0` → `0.6.1`) | Bug fixes, internal refactors, test additions, doc-only changes, no public API change |
| **Minor** (`0.6.0` → `0.7.0`) | New public API (new `@when` semantics, new `bocpy.*` symbols, new C ABI surface), new examples, new tunables. No breaking changes |
| **Major** (`0.6.0` → `1.0.0`) | Breaking changes to the Python API, the C ABI (`<bocpy/bocpy.h>` / `<bocpy/xidata.h>`), or `Cown` / `@when` semantics |

The public C ABI is version-gated via the `BOCPY_ABI` macro and the
`bocpy~=MAJOR.MINOR` pin in `templates/c_abi_consumer/pyproject.toml`.
Any incompatible change to `<bocpy/bocpy.h>` or `<bocpy/xidata.h>`
requires a minor bump at minimum and an explicit `BOCPY_ABI` bump in
the header.

Confirm the proposed version with the user before editing any files.

### Step 3 — Bump the Version

Update **all five** files in lock-step. Skipping any one of these
leaves the release inconsistent.

#### 3.1 `pyproject.toml`

```toml
[project]
name = "bocpy"
version = "<NEW_VERSION>"
```

#### 3.2 `sphinx/source/conf.py`

```python
release = '<NEW_VERSION>'
```

#### 3.3 `CITATION.cff`

Update both fields:

```yaml
version: <NEW_VERSION>
date-released: <TODAY YYYY-MM-DD>
```

#### 3.4 `templates/c_abi_consumer/pyproject.toml`

Update both `bocpy` entries to a compatible-release bound on the new
`MAJOR.MINOR`:

```toml
[build-system]
requires = ["setuptools", "wheel", "bocpy~=<MAJOR.MINOR>"]

[project]
dependencies = ["bocpy~=<MAJOR.MINOR>"]
```

The template is the canonical downstream example; its pin signals
which public C ABI it was authored against. Keep it in lock-step with
the root `[project].version`.

#### 3.5 `CHANGELOG.md`

Handled in Step 4 — version is part of the changelog entry header.

### Step 4 — Add a CHANGELOG Entry

Open `CHANGELOG.md`. If a `## Unreleased` section already exists,
re-title it; otherwise prepend a new entry at the top of the file
(below any header).

#### Format

Match the prevailing format in the file. Recent entries follow this
shape:

```markdown
## YYYY-MM-DD - Version X.Y.Z
One-paragraph summary of the headline change.

**New Features**

- **Feature name** — what it is, why it matters, where it lives.
- ...

**Bug Fixes**

- **Short title** — root cause + fix, in past tense.
- ...

**Improvements**

- ...

**Breaking Changes**

- **What broke** — why, and the recommended migration path.

**Documentation**

- New :doc:`xxx` page, expanded :doc:`api`, ...

**Tests**

- ...

**Internal**

- ...
```

#### Rules

- Date is today's date (UTC).
- Use the same bold-noun-phrase + em-dash style as recent entries.
- Reference Sphinx pages with `:doc:` directives (rendered on the
  Sphinx site, which embeds the changelog).
- Group by category in the order shown above. Omit any category with
  no entries.
- **Breaking Changes** must be its own section, never folded into
  another. Each entry must say what to do instead.
- Mention any new console entry points (`bocpy-*`) by name.
- Mention any new files in `templates/c_abi_consumer/` only if they
  change the consumer-facing template.

### Step 5 — Update Documentation

Walk through the docs and update anything the branch made stale. Do
not rewrite prose that is still accurate.

#### 5.1 `README.md` (root)

Check and update:

- **Public API table / feature list** — add new `@when`, `Cown`,
  `send`/`receive`, `noticeboard`, or messaging symbols.
- **Quick-start / examples list** — add any new entry-point example
  (`bocpy-*` console script).
- **Compatibility matrix** — update if Python-version support
  changed (look at `pyproject.toml` classifiers).
- **Sub-interpreter / scheduler description** — update only if the
  architecture changed meaningfully on this branch.

The file is PyPI's project description (after the
`<!-- pypi-skip-start -->...<!-- pypi-skip-end -->` filter in
`setup.py`); keep it presentable.

#### 5.2 `sphinx/source/`

| File | Update if... |
|------|--------------|
| `index.rst` | Architecture overview changed, or a new top-level subsystem was added |
| `api.rst` | New public Python symbol added or removed (Sphinx autodoc picks the docstring up from `__init__.pyi`, but the toctree entry must exist) |
| `c_abi.rst` | Anything in `<bocpy/bocpy.h>` or `<bocpy/xidata.h>` changed, or `BOCPY_ABI` was bumped |
| `messaging.rst` | `send` / `receive` / `set_tags` / `drain` / `TIMEOUT` semantics changed |
| `noticeboard.rst` | `notice_*` / `noticeboard()` / `REMOVED` / snapshot semantics changed |
| `sbom.rst` | SBOM generation, the `audit` extra, or wheel-embedding format changed |

Sphinx autodoc reads docstrings from `src/bocpy/__init__.pyi` and
`src/bocpy/_core.pyi`. If you added a new public symbol, the stub
docstring is the canonical source — update it there, not in
`behaviors.py` (or both, where applicable).

#### 5.3 `templates/c_abi_consumer/README.md`

Update only if the consumer-facing API surface changed (new helpers
in `bocpy.get_include()` / `bocpy.get_sources()`, new headers, new
required compile flags).

### Step 6 — Editor-Lens Pass Over the Diff

Comment debt accumulates while a PR is in flight: review-process
scaffolding (chunk numbers, finding IDs like `H1` / `M5` / `L2`,
plan back-references, sketch IDs, "Round-2 adv#6"), wordy paraphrases
of the code, dated status notes, and "previously / now" archaeology.
None of this serves a future reader. Scrub it as part of finalize.

This step is run via the `review-loop` skill against the **PR diff**
(not the whole repo) using the `editor-lens` agent.

1. Identify the changed in-scope source files:

   ```bash
   mkdir -p .copilot/finalize
   git diff --name-only main -- \
       'src/bocpy/**/*.c' 'src/bocpy/**/*.h' \
       'src/bocpy/**/*.py' 'src/bocpy/**/*.pyi' \
       'examples/**/*.py' \
       'test/**/*.py' \
       'templates/c_abi_consumer/src/**/*.c' \
       'templates/c_abi_consumer/src/**/*.h' \
       'templates/c_abi_consumer/src/**/*.py' \
       'scripts/**/*.py' \
     | tee .copilot/finalize/editor-lens-targets.txt
   ```

   `git diff` (no `..HEAD`) covers both committed and uncommitted
   changes — important during finalize, when you may still be editing.

2. Invoke `review-loop` with `editor-lens` as the reviewer and the
   file list as the target. The lens follows the keep / rewrite / cut
   policy in `.github/agents/editor-lens.agent.md`. Iterate until the
   loop comes back clean.

3. Apply approved cuts and rewrites. Anything the lens flags under
   "Questions for the user" must be resolved before proceeding — do
   not silently delete.

The editor-lens scope explicitly excludes `sphinx/source/`,
`README.md`, `CHANGELOG.md`, the top-level policy docs, and
everything under `.github/` and `.copilot/`. Those have different
rules and are managed by other steps in this skill (or are off-limits
entirely).

**Why this lives in finalize, not pre-commit:** during PR work the
review tags and scaffold comments are useful — they let the author
and reviewer cross-reference findings. They become noise only after
the review documents are deleted. Finalize is the right place to
scrub.

### Step 7 — Lint and Test

Activate the chosen venv (default `.env314` — confirm with the user)
and run the full local mirror of the PR gate. Pipe long-running
output to `.copilot/finalize/<name>.log` so the chat stays readable.

```bash
source .env314/bin/activate
```

#### 7.1 Lint

Mirrors the `.github/workflows/pr_gate.yml` linting job. The
`--filename` flag opts the walker into `.pyi` stubs (it would
otherwise skip them silently):

```bash
flake8 --filename='*.py,*.pyi' src/bocpy test examples scripts \
  2>&1 | tee .copilot/finalize/flake8.log
```

Must exit zero.

#### 7.2 Clang-format

Mirrors the `cpp-format` job. Run for both C trees touched by the
branch:

```bash
clang-format-18 --dry-run -Werror \
    src/bocpy/*.c src/bocpy/*.h src/bocpy/include/bocpy/*.h \
  2>&1 | tee .copilot/finalize/clang-format-src.log

clang-format-18 --dry-run -Werror \
    templates/c_abi_consumer/src/**/*.{c,h} \
  2>&1 | tee .copilot/finalize/clang-format-template.log
```

Both must exit zero. If `clang-format-18` is not installed, install
it (`apt install clang-format-18`) or skip with explicit user
approval; the CI version is pinned to 18.

#### 7.3 Reinstall bocpy in the venv

A version bump touches `pyproject.toml` and (often) C sources. Force
a fresh editable install so the test suite sees the new build. Use
the `BOCPY_BUILD_INTERNAL_TESTS=1` opt-in so the
`_internal_test_*` extensions are built and the
`test_internal_*` / `test_compat_atomics.py` modules run instead
of skipping:

```bash
BOCPY_BUILD_INTERNAL_TESTS=1 pip install -e .[test] --no-build-isolation \
  2>&1 | tee .copilot/finalize/install.log
```

#### 7.4 Full test suite

```bash
pytest -vv 2>&1 | tee .copilot/finalize/pytest.log | tail -40
```

Must exit zero. If pre-existing skips are present (e.g.
version-gated tests), confirm they match the baseline recorded in
the session plan; new skips warrant investigation.

#### 7.5 Downstream consumer (if touched)

If the branch changed anything under `src/bocpy/include/bocpy/`,
`src/bocpy/boc_*.{c,h}`, `templates/c_abi_consumer/`, or the
`bocpy.get_include()` / `bocpy.get_sources()` helpers, also run:

```bash
pip install --no-build-isolation ./templates/c_abi_consumer \
  2>&1 | tee .copilot/finalize/consumer-install.log

pytest -vv templates/c_abi_consumer/test \
  2>&1 | tee .copilot/finalize/consumer-pytest.log | tail -20
```

Both must exit zero.

#### 7.6 Cross-version spot check (optional)

If the branch touched anything version-gated (`xidata.h`,
`PY_VERSION_HEX` ladders, free-threaded code paths,
`#if Py_GIL_DISABLED` branches), re-run 7.3 + 7.4 in at least one
additional venv covering the affected versions
(e.g. `.env312`, `.env313t`, `.env315t`). Confirm with the user
which extra venvs to exercise.

### Step 8 — Summarize

Present a single summary to the user with:

- **Version:** old → new
- **Files changed by this skill:** `pyproject.toml`,
  `sphinx/source/conf.py`, `CITATION.cff`,
  `templates/c_abi_consumer/pyproject.toml`, `CHANGELOG.md`,
  any Sphinx pages updated, any README sections updated, and the
  list of source files edited by the editor-lens pass.
- **Verification:** `flake8`, `clang-format`, `pytest`, downstream
  consumer (if run), cross-version (if run) — all pass / fail with
  log paths under `.copilot/finalize/`.
- **Open questions:** anything `editor-lens` flagged as ambiguous
  and any pre-existing test skips that warrant attention.

**Do not commit.** All git operations belong to the user.

## Guardrails

- **Never commit, push, tag, or create a release.** The user owns
  every git operation.
- **Never skip a file listed in Step 3.** All five (`pyproject.toml`,
  `conf.py`, `CITATION.cff`, the template `pyproject.toml`, and
  `CHANGELOG.md`) must move in lock-step.
- **Never silently widen scope.** If the editor-lens pass surfaces a
  bug, stop and raise it as a finding; do not fix it in the finalize
  pass.
- **Never edit prose under `sphinx/source/` to mask a missing public
  symbol.** If the docs reference a symbol that does not exist, the
  bug is in the code or in `__init__.pyi`, not the docs.
- **Always confirm the venv** at Step 7 before running `pip install`.
  Installing into the wrong venv silently rebuilds the wrong
  interpreter's wheel.
