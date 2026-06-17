# Knowledge: persistent memory and self-improving skills

ORAC's doer sessions start fresh by design — a new context window with a single
contract (see `docs/` and `agent_session.py`). That keeps each run focused, but
it means nothing carries forward: every session re-learns the repo's conventions
and re-derives the same working method.

The knowledge layer closes that gap, adapting the headline ideas from
[Nous Research's Hermes Agent](https://hermes-agent.org/) — *memory, skills, and
a real learning loop* — to ORAC's house style: plain Markdown on disk, fully
offline, no new dependencies, and deterministic by default.

It has two halves, both rooted under `.orac/` (local state, gitignored) and both
injected into a doer session's prompt at the start of a run.

## Persistent memory

Two small, character-capped Markdown files the agents curate across sessions:

- `.orac/memory/MEMORY.md` (≤2200 chars) — environment facts, project
  conventions, tool quirks, and techniques that worked.
- `.orac/memory/USER.md` (≤1375 chars) — who the operator is and how they like
  to work.

There is no *read* action: the text is injected into the session prompt as a
frozen snapshot at session start. Writes are `add` / `replace` / `remove`. When
a file is full, the write is refused and the current entries are returned so they
can be consolidated rather than silently truncated (the Hermes capacity contract).

```powershell
orac memory show
orac memory add "Tests run with pytest from the repo root"
orac memory add --target user "Operator prefers concise, plain replies"
orac memory remove "Tests run with pytest from the repo root"
```

## Skills

A skill is a portable `SKILL.md` file — frontmatter plus human-readable sections
(`When to use`, `Procedure`, `Pitfalls`) — stored under `.orac/skills/`. Skills
are matched to a task by keyword overlap and the most relevant few are injected
into the session prompt, so a doer starts with the method an earlier session
already found.

```powershell
orac skills list
orac skills show <name-or-slug>
```

### The learning loop

When a doer session finishes with `done` after at least five tool calls (the
Hermes threshold — below it the procedure is too thin to reuse), ORAC
synthesises a skill from the session's own transcript:

- the working sequence of **allowed** tool calls becomes the `Procedure`
  (immediate repeats collapse to one step, so it reads as a method, not a log);
- **denied** or **errored** steps become `Pitfalls`;
- the task's goal and work kind become the trigger and tags.

This is deterministic — the agent writing from its own experience, with no
speculative second model call. Re-learning a skill of the same name **patches**
it (refreshes the procedure, accumulates pitfalls, bumps the minor version)
rather than creating a duplicate. Matched skills that were injected into a
successful run have their use count incremented, so proven skills rank higher.

Skill capture is best-effort: a knowledge write can never turn a finished task
into a failure — any error is logged and the work stands.

## Where it plugs in

- `src/orac/knowledge.py` — `MemoryStore`, `Skill`, `SkillLibrary`, and the
  `KnowledgeBase` facade with `prompt_preamble` (inject) and
  `capture_from_session` (learn).
- `src/orac/agent_session.py` — an `AgentSession` given a `KnowledgeBase`
  prepends the memory + matched-skill preamble and captures a skill on a
  successful multi-step run.
- `src/orac/work.py` and `src/orac/scrum.py` — thread the project-rooted
  `KnowledgeBase` down into every doer session; it is active whenever there is a
  durable project root (orthogonal to model routing), and absent for rootless
  in-memory test runs so unit tests stay pure.

The layer is intentionally model-agnostic: it works the same on the local
LM Studio / Ollama workhorse, a foundation model, or the deterministic rules
brain, because skills are synthesised from the audited transcript rather than
from a model's self-report.
