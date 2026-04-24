---
name: review-loop
description: "Iterative review loop with subagent reviewer. Use when: reviewing code changes, reviewing plans, auditing implementations, validating fixes, running code review, performing quality checks, or when /review is invoked. Spawns a fresh subagent to review a user-specified target, reports severity-tagged findings, applies approved fixes, and repeats until the user is satisfied."
argument-hint: "Describe or reference the target to review (file paths, plan, diff, etc.)"
---

# Review Loop

Iteratively review a target (code or plan) using a fresh subagent reviewer,
present findings, apply fixes, and repeat until the user approves.

## When to Use

- After implementing a non-trivial change
- Validating a plan before execution
- Auditing code for correctness, style, or security issues
- Any time you want an independent second opinion on work in progress

## Severity Levels

Findings are tagged with one of four severities:

| Severity | Meaning |
|----------|---------|
| **critical** | Correctness bug, security vulnerability, or data loss risk. Must fix. |
| **high** | Likely bug, race condition, or significant design flaw. Should fix. |
| **medium** | Code smell, unclear logic, missing edge case, or maintainability concern. Recommended fix. |
| **low** | Style nit, naming suggestion, minor improvement. Fix at discretion. |

## Procedure

### 1. Identify the Review Target

Ask the user what to review if not already specified. The target can be:

- One or more source files (by path)
- A plan (in session memory or conversation)
- A diff or set of changes
- A specific function, class, or module

Gather the full content of the target so it can be passed to the reviewer.

### 2. Spawn Reviewer Subagent

If a specific lens is requested (e.g., `correctness-lens`, `security-lens`,
`adversarial-lens`, etc.), use that named lens agent operating in **review
mode**. Otherwise, use a generic reviewer.

Launch a subagent with the following prompt structure:

> You are a code reviewer performing a thorough review of the following target.
> Your job is to find bugs, design flaws, security issues, and quality problems.
>
> **Review target:**
> {include the full content of the target here}
>
> **Additional context:**
> {include relevant surrounding code, tests, or specifications the reviewer
> needs to understand the target}
>
> **Instructions:**
> - Review the target for correctness, security, performance, readability, and
>   adherence to project conventions.
> - For each issue found, report it in this exact format:
>
>   **[SEVERITY] Short title**
>   - **Location:** file path and line number (or plan step)
>   - **Problem:** what is wrong and why it matters
>   - **Suggestion:** concrete fix or remediation
>
>   where SEVERITY is one of: critical, high, medium, low.
>
> - If you find no issues, state explicitly that the target looks correct.
> - Do NOT fabricate issues. Only report genuine problems.
> - Order findings by severity (critical first).

Use the `Explore` subagent for read-only review of code. If the reviewer needs
to run tests or execute code to verify a finding, note that as an unverified
finding and let the main agent handle verification.

### 3. Present Findings

After the reviewer returns:

1. List all findings grouped by severity.
2. For each finding, include the reviewer's suggested remediation.
3. If the reviewer found no issues, report that explicitly.
4. Ask the user which findings to address. The user may:
   - Approve all fixes
   - Select specific findings to fix
   - Dismiss findings they disagree with
   - Ask for clarification on any finding

### 4. Apply Fixes

For each approved finding:

1. Implement the suggested fix (or an alternative if the user provides one).
2. Briefly confirm each fix as it is applied.

If a fix is non-trivial or ambiguous, ask the user for guidance rather than
guessing.

### 5. Check Exit Condition

After fixes are applied, ask the user:

> All approved fixes have been applied. Should I run another review pass, or
> are we done?

- If the user wants another pass → go to **step 2** with the updated target.
- If the user is satisfied → exit the loop.

## Guidelines

- **Fresh context per pass.** Each reviewer subagent starts with no memory of
  previous passes. This prevents anchoring bias and ensures new eyes on the
  updated code.
- **Do not auto-fix without approval.** Always present findings and wait for
  the user to decide which to address.
- **Verify critical findings.** If the reviewer flags a critical or high issue,
  attempt to verify it (e.g., by running tests or tracing the code) before
  presenting it to the user. Mark unverified findings as such.
- **Keep the loop bounded.** If the reviewer returns only low-severity findings
  on two consecutive passes, suggest exiting the loop.
