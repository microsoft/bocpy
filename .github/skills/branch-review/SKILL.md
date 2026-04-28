---
name: branch-review
description: "Multi-perspective code review for a branch before merging. Use when: reviewing a branch, preparing a PR, pre-merge review, auditing a feature branch, or when /branch-review is invoked. Spawns three constructive reviewer subagents (correctness, security, usability), then runs an adversarial gap analysis to find what they missed, and synthesizes all findings into a unified review report. All intermediate artifacts are persisted to .copilot/ so the process can be restarted from any step."
argument-hint: "Branch name or merge target (e.g. 'main' or 'feature/foo -> main')"
---

# Branch Review

Perform a thorough multi-perspective code review of a branch before it is
merged. Four independent reviewers examine the diff from competing viewpoints,
and their findings are synthesized into one actionable report.

## When to Use

- A feature branch is ready to merge and needs review
- Preparing a pull request
- Final quality gate before integration

## Severity Levels

Findings use the same severity scale as the **review-loop** skill:

| Severity | Meaning |
|----------|---------|
| **critical** | Correctness bug, security vulnerability, or data loss risk. Must fix. |
| **high** | Likely bug, race condition, or significant design flaw. Should fix. |
| **medium** | Code smell, unclear logic, missing edge case, or maintainability concern. Recommended fix. |
| **low** | Style nit, naming suggestion, minor improvement. Fix at discretion. |

## Persistence and Restart

Every intermediate artifact produced by this skill is written to disk under
`.copilot/reviews/<slug>/`, where `<slug>` is a short kebab-case name derived
from the branch under review (e.g. `work-stealing-scheduler` for a branch
named `feature/work-stealing-scheduler`). This makes the process **fully
resumable**: if any step fails, is interrupted, or produces an unsatisfactory
result, you can re-run only the affected step using the on-disk artifacts
from prior steps as input.

### Directory layout

```
.copilot/reviews/<slug>/
├── 00-context.md                       # Step 2 output (shared context block)
├── 00-diff.patch                       # Step 1 raw diff
├── 00-changed-files.txt                # Step 1 file list
├── 10-review-correctness-lens.md       # Step 3 outputs (one per lens)
├── 10-review-security-lens.md
├── 10-review-usability-lens.md
├── 20-adversarial.md                   # Step 4 output
├── 30-synthesis.md                     # Step 5 output (deduped findings)
├── 40-report.md                        # Step 6 output (final unified report)
├── 50-fixes-iter1.md                   # Step 7 notes (per fix pass, optional)
├── 50-fixes-iter2.md
└── ...
```

Numeric prefixes preserve chronological order. The `<slug>` directory is
created at step 1 and reused for the whole run. If the same branch is
re-reviewed after fixes (step 8 loop-back), append a generation suffix
(e.g. `<slug>-r2/`) rather than overwriting the prior review.

### Restart contract

At the start of every step, **check whether the corresponding output file
already exists**. If it does:

- Either reuse it (skip re-running the step), or
- Explicitly overwrite it (re-run the step from scratch).

Ask the user which to do if the choice is non-obvious. Never silently discard
an existing artifact.

When the user asks to "restart from step N", load all artifacts numbered
below N into context and re-run from step N onward.

## Procedure

### 1. Gather the Diff

Determine the branch and its merge target (default: `main`) and derive the
slug. Create `.copilot/reviews/<slug>/` if it does not already exist.

Collect the diff using one of these methods, in order of preference:

1. `git diff <merge-target>...<branch> -- . ':!*.lock'` — full diff against
   the merge base. Save to `00-diff.patch`.
2. `get_changed_files` — if the working tree has uncommitted changes that are
   part of the review.

Also collect the list of changed files and save to `00-changed-files.txt`:

```
git diff --name-only <merge-target>...<branch>
```

Read the full current content of every changed file so reviewers have both the
diff and the surrounding context.

### 2. Build the Context Block

Assemble a context block that every reviewer will receive and write it to
`.copilot/reviews/<slug>/00-context.md`. This file must be self-contained:
any subagent reading it should have everything it needs without further file
lookups (beyond the diff/changed-files artifacts referenced by path). Include:

- **Branch and merge target** — branch name, base, commit range.
- **Diff** — the full unified diff (or a reference to `00-diff.patch` if
  large, with key hunks inlined).
- **Changed files** — list from `00-changed-files.txt` plus full current
  content of each modified file (or excerpts with line ranges if very large).
- **Related tests** — content of test files that cover the changed code, if
  identifiable.
- **Project conventions** — brief summary of relevant conventions from
  `copilot-instructions.md` (style, commenting, error handling, etc.).
- **Prior audits** — pointers to any prior review artifacts the user has
  flagged as already-covered (so reviewers know what is in/out of scope).

Keep the context block identical across all four reviewers to ensure a fair
comparison.

### 3. Spawn Three Constructive Reviewer Lens Subagents

Launch three subagents **in parallel**, each using a named lens agent operating
in **review mode**. Each receives the context block (by path) and must return
findings in the severity-tagged format defined above.

| # | Agent | Focus |
|---|-------|-------|
| 1 | `correctness-lens` | Logic errors, broken invariants, test gaps |
| 2 | `security-lens` | Injection, overflows, trust boundary violations |
| 3 | `usability-lens` | Naming, complexity, conventions, maintainability |

Each subagent prompt must include:

- A directive to read `.copilot/reviews/<slug>/00-context.md` as its context
- An instruction to operate in **review mode**
- A directive to **write the resulting findings to**
  `.copilot/reviews/<slug>/10-review-<lens>.md` and return a brief
  confirmation plus the file path
- These instructions:

  > Review the diff and changed files from the perspective described above.
  > For each issue found, report it in this exact format:
  >
  >   **[SEVERITY] Short title**
  >   - **Location:** file path and line number(s)
  >   - **Problem:** what is wrong and why it matters
  >   - **Suggestion:** concrete fix or remediation
  >
  >   where SEVERITY is one of: critical, high, medium, low.
  >
  > If you find no issues from your perspective, state that explicitly.
  > Do NOT fabricate issues. Only report genuine problems.
  > Order findings by severity (critical first).

After the subagents return, verify all three `10-review-*.md` files exist
before continuing.

### 4. Adversarial Gap Analysis

After the three constructive reviewers return, spawn a fresh `adversarial-lens`
subagent operating in **review mode**. This step runs **sequentially** — the
adversarial reviewer receives the existing findings so it can focus on what the
others missed.

The adversarial subagent prompt must include:

- A directive to read `.copilot/reviews/<slug>/00-context.md` and all three
  `.copilot/reviews/<slug>/10-review-*.md` files
- A directive to write its findings to
  `.copilot/reviews/<slug>/20-adversarial.md`
- These instructions:

  > You are the adversarial reviewer. The findings in the `10-review-*.md`
  > files were produced by three constructive reviewers (correctness,
  > security, usability). Your job is to find what they missed.
  >
  > Focus on:
  > - Code sections covered by NO existing finding (overlooked areas)
  > - Issue categories not represented in the existing findings
  > - Cross-component interactions no single lens would catch
  > - Unchecked assumptions and untested preconditions
  > - Silent divergences with no test coverage
  > - Fragile coupling where changing one thing silently breaks another
  >
  > For each issue found, report it in this exact format:
  >
  >   **[SEVERITY] Short title**
  >   - **Location:** file path and line number(s)
  >   - **Problem:** what is wrong and why it matters
  >   - **Suggestion:** concrete fix or remediation
  >
  >   where SEVERITY is one of: critical, high, medium, low.
  >
  > If the existing findings are comprehensive and you find no gaps, the
  > file must contain exactly: "No additional issues found."
  > Do NOT duplicate issues already reported. Only report NEW problems.
  > Order findings by severity (critical first).

### 5. Deduplicate and Synthesize

Read all four reviewer outputs (`10-review-*.md` and `20-adversarial.md`)
and write a synthesized findings list to
`.copilot/reviews/<slug>/30-synthesis.md`:

1. **Merge duplicates.** If multiple reviewers flag the same issue, keep the
   most detailed version and note which perspectives flagged it (higher
   confidence).
2. **Resolve conflicts.** If reviewers disagree (e.g., correctness wants
   inlining but maintainability wants extraction), note both sides and flag
   the trade-off for the user.
3. **Verify critical/high findings.** Before presenting critical or high
   findings, attempt to verify them — trace the code path, run relevant tests,
   or construct a minimal reproduction. Mark any finding you cannot verify as
   **[unverified]**.

Each synthesized finding should retain its severity tag and a "Flagged by"
attribution listing the contributing lenses.

### 6. Present the Report

Assemble the final report at `.copilot/reviews/<slug>/40-report.md` and
present it to the user. The report must contain these sections, in order:

1. **Summary** — one-paragraph overview: number of findings by severity, overall
   assessment (e.g., "ready to merge with minor fixes" or "has blocking issues").

2. **Positive observations** — bullet list of things the reviewers agreed were
   done well (design choices, test quality, documentation, etc.). Keep it brief
   but genuine — this provides signal about what to preserve during remediation.

3. **Findings — Critical / High** — a Markdown table with columns:
   `#`, `Severity`, `Title`, `Location`, `Flagged by`, `Status`.
   Below the table, expand each row with the full problem description and
   suggested fix.

4. **Findings — Medium** — same table + expansion format.

5. **Findings — Low** — same table + expansion format.

6. **Trade-offs** — any unresolved disagreements between reviewers, with both
   sides stated.

7. **Remediation plan** — a numbered, ordered list of concrete steps to address
   the findings. Group related fixes into a single step where sensible. Each
   step should name the finding(s) it addresses and briefly describe what to
   do. Order by priority: blocking issues first, then medium, then low.

8. **Action prompt** — ask the user which findings to address. Options:
   - Fix all (follow the remediation plan)
   - Select specific findings by number
   - Dismiss specific findings
   - Ask for clarification

### 7. Apply Fixes

For each approved finding:

1. Implement the fix (or the user's alternative if provided).
2. Confirm each fix briefly as it is applied.
3. Run relevant tests after each fix to verify no regressions.

Record a short summary of the pass to
`.copilot/reviews/<slug>/50-fixes-iter<i>.md` (incrementing `i` for each
re-review pass) noting which findings were addressed, which were deferred,
and any test results. This makes it possible to resume mid-remediation if
the session is interrupted.

If a fix is ambiguous or touches architecture, ask the user for guidance and
record the decision in the same `50-fixes-iter<i>.md` file.

### 8. Check Exit or Re-review

After all approved fixes are applied:

> All approved fixes have been applied and tests pass. Should I run another
> review pass on the updated diff, or is the branch ready to merge?

- If the user wants another pass → create a new generation directory
  (e.g. `<slug>-r2/`) and go to **step 1** with the updated diff. The prior
  review's artifacts remain on disk for reference.
- If the user is satisfied → exit.

## Guidelines

- **Fresh context per pass.** Each review pass spawns new subagents with no
  memory of prior passes, preventing anchoring bias.
- **Do not auto-fix without approval.** Always present findings and wait for
  the user to decide.
- **Scope to the diff.** Reviewers should focus on changed code. Pre-existing
  issues in unchanged code are out of scope unless the change makes them worse.
- **Keep it bounded.** If a re-review returns only low-severity findings,
  suggest exiting the loop.
- **Test after fixing.** Run the test suite (or at minimum the relevant subset)
  after applying fixes to catch regressions early.
