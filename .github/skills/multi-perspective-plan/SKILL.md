---
name: multi-perspective-plan
description: "Multi-perspective planning with rebuttal rounds and adversarial review loop. Use when: planning complex changes, designing architecture, evaluating implementation strategies, drafting implementation plans, or when /plan is invoked. Spawns three planner subagents, runs rebuttals on disagreements, synthesizes their outputs, then iteratively hardens the plan through an adversarial review loop until it passes scrutiny."
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

## Procedure

### 1. Gather Context

Before spawning planners, collect enough context about the target code so each
subagent can work from the same facts. Read the relevant source files and tests.
Summarize the current state in a brief context block that will be included in
every subagent prompt.

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

- The shared context block
- An instruction to operate in **planning mode**
- A request for a **numbered step-by-step plan** with rationale per step
- A request for **risks and mitigations** specific to their perspective

### 3. Review the Three Plans

After all three subagents return, review their outputs yourself. Write a brief
analysis noting:

- Points of agreement (high-confidence decisions)
- Points of disagreement (trade-offs to resolve)
- Any gaps none of the planners addressed

### 4. Rebuttals (If Disagreements Exist)

If step 3 identified points of disagreement, run a rebuttal round.

For **each disagreement**, identify which lenses hold competing positions. Then
spawn those lenses **in parallel** as fresh subagents operating in **rebuttal
mode**. Each subagent receives:

- The specific point of disagreement
- Its own original recommendation
- The competing recommendation(s) from the other lens(es)
- An instruction to argue concisely for why its approach is best and why the
  alternatives are inferior — one turn only

Collect the rebuttals. If there are **no disagreements**, skip this step
entirely.

### 5. Synthesize

Send all three original plans, **your analysis from step 3**, and **any
rebuttals from step 4** to a `synthesis-lens` subagent operating in **planning
mode**.

The subagent must produce a numbered step-by-step implementation sequence with
clear rationale. For each disagreement, it must pick one option and justify the
choice by engaging with the rebuttal arguments — not ignoring or averaging
them. Flag any unresolved risks.

If the synthesis agent reports any **unresolved disagreements** (trade-offs it
could not resolve), **stop and present them to the user**. For each unresolved
item, show:

- The competing options with their lens attribution
- The key argument from each side's rebuttal
- Why the choice matters

Wait for the user to decide before proceeding. Incorporate the user's decisions
into the plan.

The output of this step is the **draft plan**.

### 6. Adversarial Review Loop

Iteratively harden the draft plan by running adversarial reviews until the plan
passes scrutiny. Each iteration proceeds as follows:

#### 6a. Spawn Adversarial Reviewer

Launch a fresh `adversarial-lens` subagent operating in **planning mode** with
the following prompt structure:

> **Plan to review:**
> {include the full draft plan}
>
> **Codebase context:**
> {include the shared context block from step 1}
>
> **Instructions:**
> - For each issue found, report it in this exact format:
>
>   **[SEVERITY] Short title**
>   - **Location:** plan step number
>   - **Problem:** what is wrong and why it matters
>   - **Suggestion:** concrete fix or remediation
>
>   where SEVERITY is one of: critical, high, medium, low.
>
> - If the plan survives your scrutiny, state explicitly: "LGTM — no issues
>   found."
> - Do NOT fabricate issues. Only report genuine problems.
> - Order findings by severity (critical first).

#### 6b. Evaluate Findings

After the adversarial reviewer returns:

- If the reviewer reports **"LGTM"** (no issues found), the plan is final.
  Proceed to step 7.
- If the reviewer reports findings, address them:
  - For **critical** and **high** findings: revise the plan to fix or mitigate
    each issue. Update the draft plan in-place.
  - For **medium** findings: revise if the fix is straightforward; otherwise
    add as a documented risk in the plan.
  - For **low** findings: note and move on.

#### 6c. Check for Stuck State

If after addressing findings you are **unsure how to proceed** — for example,
the adversarial reviewer raises a concern that conflicts with a core
requirement, or two mitigations are mutually exclusive — **stop and ask the
user** for guidance. Present the specific dilemma and the options you see.

#### 6d. Repeat

Go back to step 6a with the revised plan. Use a fresh subagent each time (no
memory of previous passes).

**Bound:** If the loop has run **3 times** without reaching LGTM, present the
current plan to the user with all remaining unresolved findings and ask how to
proceed.

### 7. Present

Present the final plan to the user for approval. Clearly attribute which ideas
came from which perspective where relevant. Note any risks that survived the
adversarial review as known trade-offs.
