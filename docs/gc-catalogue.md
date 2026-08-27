# Garbage Collection & Entropy Catalogue

A structured checklist of system entropy categories for ORAC's idle self-improvement driver (`src/orac/driver.py`) and maintenance routines.

When ORAC is idle with an empty review queue and free compute, the driver's origination loop selects bounded, deterministic maintenance tasks to counteract entropy across code, board state, documentation, and operational storage.

---

## 1. Board & Subagent Roster Entropy

| Entropy Pattern | Symptoms & Indicators | Detection Mechanism | Safe Remediation |
| :--- | :--- | :--- | :--- |
| **Stale Subagent Reservations** | `subagents` table in `broker.db` retains rows with status `proposed` or `active` long after the parent task completed or timed out. | `SELECT COUNT(*) FROM subagents WHERE status IN ('proposed', 'active') AND created_at < cutoff` | Invoke `BrokerStore.reap_stale_subagents(older_than_seconds=...)` to free roster slots. |
| **Superseded Blocked Tasks** | Tasks marked `blocked` from previous failures whose root causes were subsequently resolved or superseded by later commits. | Board tasks with `status == TaskStatus.BLOCKED` and missing or obsolete error contexts. | Tag `metadata["superseded"] = True` using `BoardStore.update` so `goal_outcomes` ignores them. |
| **Orphaned Child Tasks** | Subtasks whose `parent_id` points to a non-existent or long-closed task. | Traversal of `board.tasks` checking `task.parent_id` validity. | Reconcile or retire orphaned subtasks to prevent unanchored execution. |

---

## 2. Documentation & Spec Staleness

| Entropy Pattern | Symptoms & Indicators | Detection Mechanism | Safe Remediation |
| :--- | :--- | :--- | :--- |
| **Unsynchronized TODO Items** | `TODO.md` or `roadmap.md` marks capabilities as pending (`[ ]`) when implementation and tests have already landed. | Grep for feature symbols and test files corresponding to unchecked TODO items. | Update checkboxes to `[x]` with reference to test suites and commit history. |
| **Doc/Code Constant Drift** | Architecture docs cite outdated defaults (e.g. rate limits, timeout seconds, lens names). | Compare constants in `src/orac/*.py` against documentation references in `docs/`. | Update doc tables to match code of record. |
| **Divergent CLI Documentation** | README.md or help docs omit new CLI commands (e.g. `orac status`, `orac estimate`). | Compare `make_parser()` subcommands in `cli.py` against user documentation. | Add concise usage examples to documentation. |

---

## 3. Codebase & Test Entropy

| Entropy Pattern | Symptoms & Indicators | Detection Mechanism | Safe Remediation |
| :--- | :--- | :--- | :--- |
| **Unreferenced / Dead Code** | Deprecated helper functions, orphaned adapter shims, or unused imports. | Static analysis and test coverage reports. | Remove dead code cleanly under test verification; commit on dedicated branch. |
| **Orphaned Test Fixtures** | Temporary test directories or test files that are no longer referenced by active pytest test paths. | Glob comparison between `tests/` and test runners. | Prune obsolete test utilities while maintaining full test coverage. |
| **Deprecated Config Keys** | `.orac/config.json` contains unused legacy fields replaced by newer subsystems. | Schema validation against active dataclasses in `model_policy.py` and `chat_config.py`. | Clean up obsolete keys using atomic JSON save. |

---

## 4. Operational & Runtime Storage Entropy

| Entropy Pattern | Symptoms & Indicators | Detection Mechanism | Safe Remediation |
| :--- | :--- | :--- | :--- |
| **Stale Session Files** | `.orac/sessions/*.json` files corresponding to terminated PIDs or older than TTL. | `live_sessions()` check against active OS process table. | Automatically prune dead session files on read. |
| **Bloated Connectors Log** | `.orac/*.log` files growing unbounded on long-running daemons. | File size inspection (`Path.stat().st_size > 10MB`). | Rotate or truncate oldest log entries while preserving recent tail. |
| **SQLite DB Fragmentation** | High fragmentation or unused space in `broker.db` after high-volume test runs. | SQLite page count / freelist check. | Issue `VACUUM` during maintenance idle windows. |

---

## 5. Governance & Review Queue Drift

| Entropy Pattern | Symptoms & Indicators | Detection Mechanism | Safe Remediation |
| :--- | :--- | :--- | :--- |
| **Accumulating Unacked Reviews** | Large number of completed `Notification` records with `acked = 0` remaining unreviewed for days. | `SELECT COUNT(*) FROM notifications WHERE acked = 0` | Emit summary reminder via `review_queue_summary`; provide batch review CLI. |
| **Stale Standing Grants** | Standing grants whose daily caps are never utilized or whose associated tools were removed. | Compare `grants` table against available tools and historical usage. | Suggest revoking unused standing grants during operator review. |
