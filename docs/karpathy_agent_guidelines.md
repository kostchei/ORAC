# Karpathy-Inspired Agent Guidelines

These notes summarize the guidance ORAC applies to Simples, Optimiser, and Efficiency.

## Working Rules

- Keep agents on a leash: one concrete task, bounded editable scope, clear stop condition.
- Treat generation as cheap and verification as scarce.
- Prefer fast generation-verification loops over broad autonomous wandering.
- Use measurable checks wherever possible; if a check is not cheap and repeatable, reduce autonomy.
- Make prompts, protocol files, and work orders agent-readable.
- Keep humans in the loop for risk, ambiguity, and large autonomy jumps.
- Reject large opaque generated diffs even when they appear to work.
- Prefer constrained loops: one scope, one metric, fixed budget, keep-or-discard decision.

## Applied To Agents

- Optimiser sets budgets, autonomy level, metric, editable scope, and escalation trigger.
- Simples works on one small concrete thing and stops before the diff becomes hard to review.
- Efficiency verifies, checks for silent failures, and blocks or discards work that is too opaque.
