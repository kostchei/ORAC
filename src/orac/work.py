from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orac.agent_registry import load_agent_profiles
from orac.agent_session import AgentSession
from orac.broker import ToolBroker
from orac.llm import Brain
from orac.models import (
    Board,
    CapabilityRequest,
    CapabilityStatus,
    Task,
    TaskStatus,
)

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
    # How the kind's done-means is independently confirmed before DONE. None =
    # no verifier yet; a kind cannot have a doer without one (the doer can claim
    # done, so something other than the doer must check it). Enforced below.
    verifier: str | None = None


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
        verifier="run_tests",  # re-run the suite on the built branch; red => not done
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

    # A kind with a doer must also declare how its done-means is verified: the
    # doer claims done, so something other than the doer must confirm it. A doer
    # without a verifier is a design hole, surfaced loudly rather than trusted.
    if spec.doer_slug is not None and spec.verifier is None:
        raise ValueError(
            f"work kind {spec.kind!r} has a doer but no verifier; a doer that can "
            "claim done needs an independent check before DONE is granted."
        )

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
        # Generation is cheap, verification is scarce: the doer's self-reported
        # "done" is a claim, not proof. Independently confirm the kind's
        # done-means before granting DONE; a failed or unrunnable check blocks
        # the task rather than trusting the summary (most likely live-fire
        # failure: a local model declaring victory with red or unrun tests).
        ok, detail = verify_goal_done(spec, child, broker, context)
        if ok:
            child.transition(TaskStatus.DONE)
            parent.add_log(
                "Orchestrator",
                f"{spec.kind} subtask {child.id} done (verified): {result.summary}",
            )
        else:
            child.add_log(
                doer.name, f"Claimed done, but verification failed: {detail}"
            )
            child.transition(TaskStatus.BLOCKED)
            parent.transition(TaskStatus.BLOCKED)
            parent.add_log(
                "Orchestrator",
                f"{spec.kind} subtask {child.id} blocked: claimed done but "
                f"verification failed: {detail}",
            )
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


# Verifier name -> how it confirms a kind's done-means. The check runs through
# the broker (audited, no privileged path) as the doer agent.
def verify_goal_done(
    spec: WorkKindSpec,
    child: Task,
    broker: ToolBroker,
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Independently confirm the kind's done-means. Returns (ok, detail).

    Only invoked when a doer session claimed done, so ``spec.verifier`` is set
    (the doer/verifier invariant is enforced at spawn). An unknown verifier name
    is a configuration fault, not a silent pass.
    """
    if spec.verifier == "run_tests":
        return _verify_run_tests(spec, child, broker, context)
    raise ValueError(
        f"work kind {spec.kind!r} names unknown verifier {spec.verifier!r}."
    )


def _verify_run_tests(
    spec: WorkKindSpec,
    child: Task,
    broker: ToolBroker,
    context: dict[str, Any],
) -> tuple[bool, str]:
    repo_root = context.get("repo_root")
    if not repo_root:
        return False, "no repo_root in context to run the suite against"
    doer = WORK_KINDS[spec.kind].doer_slug
    profiles = {profile.slug: profile for profile in load_agent_profiles()}
    agent_name = profiles[doer].name if doer in profiles else "Builder"
    result = broker.request(
        CapabilityRequest(
            agent=agent_name,
            tool="repo.run_tests",
            task_id=child.id,
            args={"root": str(repo_root)},
        ),
        child,
    )
    if result.status is not CapabilityStatus.ALLOWED:
        return False, f"could not run tests ({result.status.value}): {result.message}"
    passed = bool(result.data.get("passed"))
    summary = str(result.data.get("summary", "")).strip()[-500:]
    if passed:
        return True, "tests passed"
    return False, summary or "tests failed"
