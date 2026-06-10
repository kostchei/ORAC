from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orac.agent_registry import load_agent_profiles
from orac.agent_session import AgentSession
from orac.broker import ToolBroker
from orac.llm import Brain
from orac.models import Board, Task, TaskStatus

# The general work model. A goal task carries a work kind; each kind names its
# sole doer (the §4.6 invariant generalised: one writer per category of
# consequence), a contract template, and what "done" means for that kind.
# The governance is identical for all five — broker, council floor, risk
# model, review-after — only the doer and the verification differ.
#
# Kinds without a doer yet are registered, not imagined: originating work for
# them produces a visible BLOCKED task naming the missing doer, so the system
# states what it cannot do instead of pretending.


@dataclass(frozen=True)
class WorkKindSpec:
    kind: str
    doer_slug: str | None          # sole doer role; None = no doer exists yet
    done_means: str                # what verification looks like for this kind
    contract_rules: str            # working rules injected into the contract


CONTRACT_TEMPLATE = """\
GOAL: {goal}

ACCEPTANCE CRITERIA:
{criteria}

CONTEXT:
{context}

WORKING RULES:
{rules}

DONE MEANS: {done_means}
"""

WORK_KINDS: dict[str, WorkKindSpec] = {
    "code": WorkKindSpec(
        kind="code",
        doer_slug="builder",
        done_means="the change is committed on a branch and the tests pass.",
        contract_rules=(
            "- Checkpoint first: create a branch (suggested name: build/{task_id}) "
            "before changing files.\n"
            "- Write only inside the repo root given in CONTEXT.\n"
            "- One logical change per commit, with explicit paths.\n"
            "- Run the tests; you are not done until they pass."
        ),
    ),
    "comms": WorkKindSpec(
        kind="comms",
        doer_slug=None,  # Messenger: sole holder of channel.send (Group 2)
        done_means="a draft exists in the review queue; sending requires human approval.",
        contract_rules="- Draft only. Never send without an approved grant.",
    ),
    "media": WorkKindSpec(
        kind="media",
        doer_slug=None,  # Producer: job-queue media generation (Group 3)
        done_means="an artifact exists in the asset store awaiting review; publish is gated.",
        contract_rules="- Queue jobs; never block. Publish requires approval.",
    ),
    "physical": WorkKindSpec(
        kind="physical",
        doer_slug=None,  # Operator: sole holder of execute_action (Group 4)
        done_means="the device state read back confirms the prepared action took effect.",
        contract_rules=(
            "- read_state, prepare_action, execute_action in that order.\n"
            "- Execution requires approval or a standing grant; honour cooldowns."
        ),
    ),
    "event": WorkKindSpec(
        kind="event",
        doer_slug=None,  # Host: human sessions/games/workshops (Group 5)
        done_means="the session reached its closing state with all rounds resolved.",
        contract_rules="- Advance rounds only on human input; never answer for a participant.",
    ),
}


def run_goal_task(
    board: Board,
    parent: Task,
    goal: str,
    acceptance_criteria: tuple[str, ...],
    work_kind: str,
    brain: Brain,
    broker: ToolBroker,
    context: dict[str, Any],
    max_steps: int = 16,
) -> Task:
    """Spawn the kind's doer against a goal; the model decides how.

    The child inherits no parent history — only the contract. Summary-up on
    completion; a kind with no registered doer blocks visibly.
    """
    try:
        spec = WORK_KINDS[work_kind]
    except KeyError as exc:
        raise ValueError(f"Unknown work kind {work_kind!r}.") from exc

    child = Task(
        title=f"[{spec.kind}] {goal}",
        description=goal,
        parent_id=parent.id,
        status=TaskStatus.IN_PROGRESS,
        acceptance_criteria=list(acceptance_criteria),
        metadata={"contract": {"goal": goal, "kind": spec.kind}},
    )
    board.add_task(child)

    if spec.doer_slug is None:
        child.assignee = None
        child.add_log(
            "system",
            f"No doer agent exists yet for work kind {spec.kind!r}; "
            "task blocked until that capability group is built.",
        )
        child.transition(TaskStatus.BLOCKED)
        parent.add_log(
            "Orchestrator",
            f"Subtask {child.id} blocked: no {spec.kind!r} doer registered yet.",
        )
        parent.transition(TaskStatus.BLOCKED)
        return child

    profiles = {profile.slug: profile for profile in load_agent_profiles()}
    doer = profiles[spec.doer_slug]
    child.assignee = doer.name
    parent.add_log("Orchestrator", f"Spawned {spec.kind} subtask {child.id}: {goal}")

    contract = CONTRACT_TEMPLATE.format(
        goal=goal,
        criteria="\n".join(f"- {c}" for c in acceptance_criteria) or "- (none given)",
        context="\n".join(f"- {k}: {v}" for k, v in context.items()) or "- (none)",
        rules=spec.contract_rules.format(task_id=child.id),
        done_means=spec.done_means,
    )
    session = AgentSession(profile=doer, brain=brain, broker=broker, max_steps=max_steps)
    result = session.run(child, contract)

    if result.status == "done":
        child.transition(TaskStatus.DONE)
        parent.add_log("Orchestrator", f"{spec.kind} subtask {child.id} done: {result.summary}")
    elif result.status == "pending":
        child.park_for_approval(result.pending_id, TaskStatus.IN_PROGRESS)
        parent.add_log(
            "Orchestrator",
            f"{spec.kind} subtask {child.id} parked for approval (pending {result.pending_id}).",
        )
    else:
        child.transition(TaskStatus.BLOCKED)
        parent.transition(TaskStatus.BLOCKED)
        parent.add_log("Orchestrator", f"{spec.kind} subtask {child.id} blocked: {result.summary}")
    return child
