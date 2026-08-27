# Corrections Plan

Tracked inaccuracies found in docs vs. codebase and their resolution.

## docs/agentic-harness-patterns.md

- [x] **§3.4 Promoter mapping overstated.** Resolved by implementing `src/orac/promoter.py`, wiring it to successful goal completion, exact opt-in TODO/roadmap reconciliation, the durable promotion spool, and Slack/WhatsApp outbound digests. The harness document now has an implementation-status table with regression-test references rather than presenting M1–M4 as unbuilt targets.
