---
name: multi-perspective-plan
description: "Multi-perspective planning with adversarial review. Use when: planning complex changes, designing architecture, evaluating implementation strategies, drafting implementation plans, or when /plan is invoked. Spawns four subagents with competing priorities (speed, usability, conservatism, adversarial) then synthesizes their outputs into a balanced final plan."
argument-hint: "Describe the change or feature to plan"
---

# Multi-Perspective Planning

Generate a robust implementation plan by soliciting four competing viewpoints
and synthesizing them into a single balanced proposal.

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

### 2. Spawn Four Planner Subagents

Launch four subagents **in parallel**. Each receives the same context block plus
a persona directive. Each must return a concrete, step-by-step implementation
plan (not just commentary).

| # | Persona | Directive |
|---|---------|-----------|
| 1 | **Speed** | Obsessed with performance. Minimize latency and overhead at all costs. Inline aggressively, avoid abstractions, prefer lock-free and wait-free primitives. Tolerate complexity if it buys speed. |
| 2 | **Usability** | Prioritize clean, readable, maintainable code. Favor clear abstractions, good naming, and small functions. Accept modest performance cost for clarity. |
| 3 | **Conservative** | Minimize the changeset. Touch as few lines as possible. Prefer surgical edits over refactors. Reuse existing patterns. Resist new dependencies or abstractions. |
| 4 | **Adversarial** | Assume the plan is wrong. Actively try to break the design. Look for race conditions, deadlocks, ABA problems, platform bugs, edge cases, and failure modes. Start from skepticism and only endorse what survives scrutiny. |

Each subagent prompt must include:

- The shared context block
- The persona directive (from the table above)
- A request for a **numbered step-by-step plan** with rationale per step
- A request for **risks and mitigations** specific to their perspective

### 3. Review the Four Plans

After all four subagents return, review their outputs yourself. Write a brief
analysis noting:

- Points of agreement (high-confidence decisions)
- Points of disagreement (trade-offs to resolve)
- Risks raised by the adversarial planner that others missed
- Any gaps none of the planners addressed

### 4. Synthesize

Send all four plans **plus your analysis** to a fifth subagent with the
directive:

> You are a senior engineer synthesizing four competing implementation plans
> into one final plan. Preserve the strongest ideas from each perspective.
> Where planners disagree, make an explicit trade-off decision and justify it.
> The final plan must be a numbered step-by-step implementation sequence with
> clear rationale. Flag any unresolved risks.

### 5. Present

Present the synthesized plan to the user for approval. Clearly attribute which
ideas came from which perspective where relevant.
