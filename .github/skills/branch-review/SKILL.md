---
name: branch-review
description: "Multi-perspective code review for a branch before merging. Use when: reviewing a branch, preparing a PR, pre-merge review, auditing a feature branch, or when /branch-review is invoked. Spawns three constructive reviewer subagents (correctness, security, usability), then runs an adversarial gap analysis to find what they missed, and synthesizes all findings into a unified review report."
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

## Procedure

### 1. Gather the Diff

Determine the branch and its merge target (default: `main`). Collect the diff
using one of these methods, in order of preference:

1. `git diff <merge-target>...<branch> -- . ':!*.lock'` — full diff against
   the merge base.
2. `get_changed_files` — if the working tree has uncommitted changes that are
   part of the review.

Also collect the list of changed files:

```
git diff --name-only <merge-target>...<branch>
```

Read the full current content of every changed file so reviewers have both the
diff and the surrounding context.

### 2. Build the Context Block

Assemble a context block that every reviewer will receive. It must include:

- **Diff** — the full unified diff.
- **Changed files** — full current content of each modified file.
- **Related tests** — content of test files that cover the changed code, if
  identifiable.
- **Project conventions** — brief summary of relevant conventions from
  `copilot-instructions.md` (style, commenting, error handling, etc.).

Keep the context block identical across all four reviewers to ensure a fair
comparison.

### 3. Spawn Three Constructive Reviewer Lens Subagents

Launch three subagents **in parallel**, each using a named lens agent operating
in **review mode**. Each receives the context block and must return findings in
the severity-tagged format defined above.

| # | Agent | Focus |
|---|-------|-------|
| 1 | `correctness-lens` | Logic errors, broken invariants, test gaps |
| 2 | `security-lens` | Injection, overflows, trust boundary violations |
| 3 | `usability-lens` | Naming, complexity, conventions, maintainability |

Each subagent prompt must include:

- The shared context block
- An instruction to operate in **review mode**
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

### 4. Adversarial Gap Analysis

After the three constructive reviewers return, spawn a fresh `adversarial-lens`
subagent operating in **review mode**. This step runs **sequentially** — the
adversarial reviewer receives the existing findings so it can focus on what the
others missed.

The adversarial subagent prompt must include:

- The shared context block
- The full list of findings from the three constructive reviewers
- These instructions:

  > You are the adversarial reviewer. The findings below were produced by three
  > constructive reviewers (correctness, security, usability). Your job is to
  > find what they missed.
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
  > If the existing findings are comprehensive and you find no gaps, state
  > explicitly: "No additional issues found."
  > Do NOT duplicate issues already reported. Only report NEW problems.
  > Order findings by severity (critical first).

### 5. Deduplicate and Synthesize

After all four reviewers (three constructive + adversarial) have returned:

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

### 6. Present the Report

Present a single unified review report to the user with these sections, in
order:

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

If a fix is ambiguous or touches architecture, ask the user for guidance.

### 8. Check Exit or Re-review

After all approved fixes are applied:

> All approved fixes have been applied and tests pass. Should I run another
> review pass on the updated diff, or is the branch ready to merge?

- If the user wants another pass → go to **step 1** with the updated diff.
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
