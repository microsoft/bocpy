---
name: multi-perspective-plan
description: "Multi-perspective planning with rebuttal rounds and adversarial review loop. Use when: planning complex changes, designing architecture, evaluating implementation strategies, drafting implementation plans, or when /plan is invoked. Spawns three planner subagents, runs rebuttals on disagreements, synthesizes their outputs, then iteratively hardens the plan through an adversarial review loop until it passes scrutiny. All intermediate artifacts are persisted to .copilot/ so the process can be restarted from any step."
argument-hint: "Describe the change or feature to plan"
---

# Multi-Perspective Planning

Generate a robust implementation plan by soliciting three competing viewpoints,
synthesizing them, and then hardening the result through an adversarial review
loop.

## When to Use

- Planning non-trivial code changes that touch multiple subsystems
- Evaluating architecture or design trade-offs
- Any time you want a plan stress-tested before implementation

## Persistence and Restart

Every intermediate artifact produced by this skill is written to disk under
`.copilot/plans/<slug>/`, where `<slug>` is a short kebab-case name derived
from the planning task (e.g. `work-stealing-scheduler`). This makes the
process **fully resumable**: if any step fails, is interrupted, or produces
an unsatisfactory result, you can re-run only the affected step using the
on-disk artifacts from prior steps as input.

### Directory layout

```
.copilot/plans/<slug>/
├── 00-context.md                       # Step 1 output
├── 10-plan-speed-lens.md               # Step 2 outputs (one per lens)
├── 10-plan-usability-lens.md
├── 10-plan-conservative-lens.md
├── 20-analysis.md                      # Step 3 output
├── 30-rebuttal-<topic>-<lens>.md       # Step 4 outputs (one per lens per topic)
├── 40-draft-plan.md                    # Step 5 output
├── 50-adversarial-iter1.md             # Step 6a output, iteration 1
├── 50-revisions-iter1.md               # Step 6b notes for iteration 1
├── 50-adversarial-iter2.md
├── 50-revisions-iter2.md
├── ...
└── 99-final-plan.md                    # Step 7 output
```

Numeric prefixes preserve chronological order. The `<slug>` directory is
created at step 1 and reused for the whole run.

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

### 1. Gather Context

Before spawning planners, collect enough context about the target code so each
subagent can work from the same facts. Read the relevant source files and tests.

Write the context block to `.copilot/plans/<slug>/00-context.md`. This file
must be self-contained: any subagent reading it should have everything it
needs without further file lookups. Include:

- The planning task as stated by the user
- A summary of the current state of the relevant code
- Key file paths and line ranges that matter
- Any constraints or invariants the plan must respect
- Pointers to related artifacts (sketches, prior plans, benchmark JSONs)

If a sketch document already exists (e.g. `.copilot/<slug>.md`), reference it
from `00-context.md` rather than duplicating its contents.

### 2. Spawn Three Planner Lens Subagents

Launch three subagents **in parallel**, each using a named lens agent. Each
receives the same context block and must return a concrete, step-by-step
implementation plan (not just commentary).

| # | Agent | Focus |
|---|-------|-------|
| 1 | `speed-lens` | Performance — minimize latency and overhead |
| 2 | `usability-lens` | Clarity — clean, readable, maintainable code |
| 3 | `conservative-lens` | Scope — minimal changeset, surgical edits |

Each subagent prompt must include:

- A directive to read `.copilot/plans/<slug>/00-context.md` as its context
- An instruction to operate in **planning mode**
- A request for a **numbered step-by-step plan** with rationale per step
- A request for **risks and mitigations** specific to their perspective
- A directive to **write the resulting plan to**
  `.copilot/plans/<slug>/10-plan-<lens>.md` and return a brief confirmation
  plus the file path

After the subagents return, verify all three files exist before continuing.

### 3. Review the Three Plans

Read all three `10-plan-*.md` files. Write a brief analysis to
`.copilot/plans/<slug>/20-analysis.md` noting:

- Points of agreement (high-confidence decisions)
- Points of disagreement (trade-offs to resolve), each labelled with a short
  topic slug for use in step 4 filenames
- Any gaps none of the planners addressed

### 4. Rebuttals (If Disagreements Exist)

If `20-analysis.md` lists any disagreements, run a rebuttal round.

For **each disagreement topic**, identify which lenses hold competing
positions. Spawn those lenses **in parallel** as fresh subagents operating
in **rebuttal mode**. Each subagent receives:

- The path to `00-context.md`
- The specific point of disagreement (quoted from `20-analysis.md`)
- The path to its own original plan and the competing plan(s)
- An instruction to argue concisely for why its approach is best and why the
  alternatives are inferior — one turn only
- A directive to write its rebuttal to
  `.copilot/plans/<slug>/30-rebuttal-<topic>-<lens>.md`

If there are **no disagreements**, skip this step. Record that fact in
`20-analysis.md` so a restarted run knows step 4 is intentionally empty.

### 5. Synthesize

Spawn a `synthesis-lens` subagent operating in **planning mode**. Its prompt
must direct it to read:

- `00-context.md`
- All three `10-plan-*.md` files
- `20-analysis.md`
- All `30-rebuttal-*.md` files (if any)

The subagent must produce a numbered step-by-step implementation sequence
with clear rationale, written to `.copilot/plans/<slug>/40-draft-plan.md`.
For each disagreement, it must pick one option and justify the choice by
engaging with the rebuttal arguments — not ignoring or averaging them. Flag
any unresolved risks.

If the synthesis agent reports any **unresolved disagreements** (trade-offs
it could not resolve), **stop and present them to the user**. For each
unresolved item, show:

- The competing options with their lens attribution
- The key argument from each side's rebuttal
- Why the choice matters

Wait for the user to decide before proceeding. Incorporate the user's
decisions into `40-draft-plan.md` directly.

### 6. Adversarial Review Loop

Iteratively harden the draft plan by running adversarial reviews until the
plan passes scrutiny. Each iteration `i` (starting at 1) proceeds as follows:

#### 6a. Spawn Adversarial Reviewer

Launch a fresh `adversarial-lens` subagent operating in **planning mode**.
Its prompt must direct it to read `00-context.md` and the current plan
(initially `40-draft-plan.md`, then the most recently revised version) and
to write its findings to `.copilot/plans/<slug>/50-adversarial-iter<i>.md`
using this structure:

> **Plan reviewed:** <path>
>
> For each issue found, report it in this exact format:
>
>   **[SEVERITY] Short title**
>   - **Location:** plan step number
>   - **Problem:** what is wrong and why it matters
>   - **Suggestion:** concrete fix or remediation
>
>   where SEVERITY is one of: critical, high, medium, low.
>
> If the plan survives scrutiny, the file must contain exactly:
> "LGTM — no issues found."
>
> Do NOT fabricate issues. Order findings by severity (critical first).

#### 6b. Evaluate Findings and Revise

Read `50-adversarial-iter<i>.md`:

- If it contains **"LGTM"**, the plan is final. Proceed to step 7.
- Otherwise, address the findings:
  - **critical** / **high**: revise the plan to fix or mitigate.
  - **medium**: revise if straightforward; otherwise document as a risk.
  - **low**: note and move on.

Update the draft plan **in place** at the same path it was loaded from.
Write a short note to `.copilot/plans/<slug>/50-revisions-iter<i>.md`
summarising which findings were addressed and how, and which were
deliberately deferred or rejected.

#### 6c. Check for Stuck State

If you are unsure how to proceed — e.g. a concern conflicts with a core
requirement, or two mitigations are mutually exclusive — **stop and ask the
user**. Present the dilemma and the options. Save the user's decision into
`50-revisions-iter<i>.md` so a restart can recover it.

#### 6d. Repeat

Increment `i` and go back to step 6a with the revised plan. Use a fresh
subagent each time (no memory of previous passes).

**Bound:** If the loop has run **3 times** (`50-adversarial-iter3.md`
exists and is not LGTM) without reaching LGTM, present the current plan to
the user with all remaining unresolved findings and ask how to proceed.

### 7. Present

Copy the final plan to `.copilot/plans/<slug>/99-final-plan.md` and present
it to the user for approval. Clearly attribute which ideas came from which
perspective where relevant. Note any risks that survived the adversarial
review as known trade-offs, and reference the iteration files that
documented their resolution.
