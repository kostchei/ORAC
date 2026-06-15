from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orac.agent_registry import load_agent_profiles
from orac.agent_session import AgentSession
from orac.broker import ToolBroker
from orac.broker_store import MAX_SUBAGENTS
from orac.dispatch import ACTIVE_SLICE_CEILING, both_agree
from orac.intent_ledger import (
    SLICE_BLOCKED,
    SLICE_SATISFIED,
    attach_child,
    coverage_report,
    is_blocked,
    is_covered,
    open_ledger,
    mark,
    slices,
)
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
    # How the kind's done-means is independently confirmed before DONE. Empty =
    # no verifier yet; a kind cannot have a doer without at least one (the doer
    # can claim done, so something other than the doer must check it). Every
    # listed verifier must pass; a verifier that does not apply to a given goal
    # (e.g. a UI check on a backend-only change) passes as a no-op. Enforced below.
    verifiers: tuple[str, ...] = ()


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
            "- Run the tests; you are not done until they pass.\n"
            "- For a UI change, leave the local app running and name its URL so "
            "the change can be verified in a browser."
        ),
        # run_tests always re-runs the suite on the built branch (red => not done);
        # verify_local_app additionally drives the running app when the goal is a
        # UI change (context carries app_url), and is a no-op pass otherwise.
        verifiers=("run_tests", "verify_local_app"),
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


# A subagent's default share of the 60% resource band. 0.25 => up to four run
# concurrently before the band is full. Optimise's allocator may pass a tuned
# slice; this is the standing default.
DEFAULT_RESOURCE_SLICE = 0.25


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
    intent: str | None = None,
    resource_slice: float = DEFAULT_RESOURCE_SLICE,
    contract_metadata: dict[str, Any] | None = None,
    max_repairs: int = 0,
    review_return: bool = False,
) -> Task:
    """Spawn the kind's doer against a goal; the model decides how.

    The child inherits no parent history — only the contract. Summary-up on
    completion; a kind with no registered doer blocks visibly. When the broker
    has a store, the spawned doer is admitted to the subagent register (the
    roster) and its final state is written back, so the live free-count stays
    truthful. ``intent`` records what slice of the parent intent this child
    carries (defaults to the goal).

    ``contract_metadata`` carries the slice's scope (allowed/forbidden tools,
    owned paths) onto the child's contract so the broker can enforce it. With
    ``max_repairs`` > 0, a verification failure is not the end: the doer is
    re-run up to that many times with the failure detail injected ("fix exactly
    this"), a bounded repair loop rather than a broad retry. It is opt-in
    (default 0) — the decomposition path enables it; a bare single-goal build
    keeps the original block-on-first-failure behaviour.
    """
    try:
        spec = WORK_KINDS[work_kind]
    except KeyError as exc:
        raise ValueError(f"Unknown work kind {work_kind!r}.") from exc

    # A kind with a doer must also declare how its done-means is verified: the
    # doer claims done, so something other than the doer must confirm it. A doer
    # without a verifier is a design hole, surfaced loudly rather than trusted.
    if spec.doer_slug is not None and not spec.verifiers:
        raise ValueError(
            f"work kind {spec.kind!r} has a doer but no verifier; a doer that can "
            "claim done needs an independent check before DONE is granted."
        )

    contract_data: dict[str, Any] = {"goal": goal, "kind": spec.kind}
    if contract_metadata:
        contract_data.update(contract_metadata)

    child = Task(
        title=f"[{spec.kind}] {goal}",
        description=goal,
        parent_id=parent.id,
        status=TaskStatus.IN_PROGRESS,
        acceptance_criteria=list(acceptance_criteria),
        metadata={"contract": contract_data},
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

    # Admit the doer to the roster (the register), reserving its resource slice.
    # Recorded so the live free-count behind the Orchestrator's frame is honest.
    subagent_id: int | None = None
    if broker.store is not None:
        subagent_id = broker.store.admit_subagent(
            parent_task_id=parent.id,
            profile_slug=spec.doer_slug,
            instruction=contract,
            intent=intent or goal,
            resource_slice=resource_slice,
        )

    def _retire(status: str) -> None:
        if broker.store is not None and subagent_id is not None:
            broker.store.set_subagent_status(subagent_id, status)

    session = AgentSession(profile=doer, brain=brain, broker=broker, max_steps=max_steps)
    result = session.run(child, contract)

    if result.status == "done":
        # Generation is cheap, verification is scarce: the doer's self-reported
        # "done" is a claim, not proof. Independently confirm the kind's done-means
        # before granting DONE; a failed or unrunnable check does not trust the
        # summary (most likely live-fire failure: a local model declaring victory
        # with red or unrun tests).
        ok, detail = verify_goal_done(spec, child, broker, context)
        if ok:
            # The deterministic verifier proved the work RUNS. When enabled (the
            # orchestrated fan-out turns it on), promote the RETURN edge to a full
            # council review: three lenses judge whether what came back is on-goal,
            # minimally shaped, and waste-free before it integrates (rugged
            # decomposition §13). A rejected return blocks the slice with the lens
            # reasons rather than integrating questionable work on green tests alone.
            review = None
            if review_return:
                from orac.plan_review import review_return as _review_return_edge  # noqa: PLC0415

                review = _review_return_edge(
                    goal, acceptance_criteria, result.summary, brain, task=child
                )
            if review is not None and review.status is not CapabilityStatus.ALLOWED:
                child.add_log(
                    "Council",
                    f"RETURN review did not accept the slice "
                    f"({review.status.value}): {review.reason}",
                )
                child.transition(TaskStatus.BLOCKED)
                _retire("blocked")
                parent.transition(TaskStatus.BLOCKED)
                parent.add_log(
                    "Orchestrator",
                    f"{spec.kind} subtask {child.id} blocked by RETURN review: {review.reason}",
                )
            else:
                child.transition(TaskStatus.DONE)
                _retire("done")
                reviewed = " + RETURN review" if review is not None else ""
                parent.add_log(
                    "Orchestrator",
                    f"{spec.kind} subtask {child.id} done (verified{reviewed}): {result.summary}",
                )
        elif max_repairs > 0:
            # Repair is a NEW focused slice (rugged decomposition §13): a child of
            # this one, visible on the board and independently verified in its own
            # right — not an invisible in-place re-run. It carries the exact failure
            # and inherits this slice's scope; a repair that itself fails verification
            # chains one more bounded repair (max_repairs - 1) before giving up.
            child.add_log(
                doer.name,
                f"Claimed done, but verification failed: {detail}. "
                f"Spawning a repair slice (repair budget {max_repairs}).",
            )
            repair = run_goal_task(
                board,
                child,
                goal=_repair_goal(goal, detail),
                acceptance_criteria=acceptance_criteria,
                work_kind=work_kind,
                brain=brain,
                broker=broker,
                context={**context, "verification_failure": detail},
                max_steps=max_steps,
                intent=f"repair: {intent or goal}",
                resource_slice=resource_slice,
                contract_metadata=contract_metadata,
                max_repairs=max_repairs - 1,
                review_return=review_return,
            )
            if repair.status is TaskStatus.DONE:
                # The repair verified, so this slice's goal is now met on the branch.
                child.transition(TaskStatus.DONE)
                _retire("done")
                parent.add_log(
                    "Orchestrator",
                    f"{spec.kind} subtask {child.id} done via repair slice {repair.id}.",
                )
            else:
                child.transition(TaskStatus.BLOCKED)
                _retire("blocked")
                parent.transition(TaskStatus.BLOCKED)
                parent.add_log(
                    "Orchestrator",
                    f"{spec.kind} subtask {child.id} blocked: repair slice "
                    f"{repair.id} did not verify ({detail}).",
                )
        else:
            child.add_log(doer.name, f"Claimed done, but verification failed: {detail}")
            child.transition(TaskStatus.BLOCKED)
            _retire("blocked")
            parent.transition(TaskStatus.BLOCKED)
            parent.add_log(
                "Orchestrator",
                f"{spec.kind} subtask {child.id} blocked: claimed done but "
                f"verification failed: {detail}",
            )
    elif result.status == "pending":
        # Parked for approval: still live (holds its roster slot) until resolved.
        child.park_for_approval(result.pending_id, TaskStatus.IN_PROGRESS)
        parent.add_log(
            "Orchestrator",
            f"{spec.kind} subtask {child.id} parked for approval (pending {result.pending_id}).",
        )
    else:
        child.transition(TaskStatus.BLOCKED)
        _retire("blocked")
        parent.transition(TaskStatus.BLOCKED)
        parent.add_log("Orchestrator", f"{spec.kind} subtask {child.id} blocked: {result.summary}")
    return child


def _repair_goal(goal: str, detail: str) -> str:
    """The focused goal for a repair slice: the original goal plus the exact
    verification failure to fix, with an explicit 'no unrelated edits' bound."""
    return (
        f"{goal}\n\nA prior attempt failed verification. Fix EXACTLY this failure "
        f"and make no unrelated edits:\n{detail}"
    )


def run_decomposed_goal(
    board: Board,
    parent: Task,
    intent: str,
    decomposition: list[dict[str, Any]],
    work_kind: str,
    brain: Brain,
    broker: ToolBroker,
    context: dict[str, Any],
    max_steps: int = 16,
    resource_slice: float = DEFAULT_RESOURCE_SLICE,
    band: float = ACTIVE_SLICE_CEILING,
    max_repairs: int = 0,
    review_return: bool = False,
    plan_brain: Brain | None = None,
    depth: int = 0,
    max_depth: int = 2,
) -> list[Task]:
    """Fan a parent intent out across one child per declared slice, tracking the
    intent ledger so the parent cannot be called done until every slice is.

    ``decomposition`` is the Orchestrator's plan: a list of slice contracts. Each
    slice carries its piece of ``intent`` faithfully into its child contract
    (instruction-down), its scope (allowed/forbidden tools, owned paths) onto the
    child for the broker to enforce, and its ``inputs`` into the child's context;
    the ledger is the deterministic guarantee that no slice is dropped and that
    the parent's status reflects true coverage. ``max_repairs`` enables the doer
    repair loop per slice (rugged decomposition §4.5–4.8) — opt-in, default 0;
    ``run_orchestrated_goal`` (the full fan-out) turns it on.
    """
    open_ledger(parent, intent, decomposition)
    children: list[Task] = []
    for index, slice_ in enumerate(slices(parent)):
        # (e) both-must-agree gate: the slice is from an approved plan
        # (Orchestrator proposed), but Optimise must also have a free slot and
        # band room. A refused spawn defers the slice (stays open), not an error.
        if broker.store is not None:
            decision = both_agree(
                broker.store,
                orchestrator_proposed=True,
                resource_slice=resource_slice,
                band=band,
            )
            if not decision.agreed:
                parent.add_log(
                    "Optimise",
                    f"Spawn of {slice_['sub_intent']!r} deferred: {decision.reason}",
                )
                continue
        # Carry the slice's scope onto the child (the broker enforces it) and its
        # inputs into the child's context.
        contract_metadata = {
            key: slice_[key]
            for key in ("allowed_tools", "forbidden_tools", "owned_paths_or_resources")
            if key in slice_
        }
        slice_context = {**context, **dict(slice_.get("inputs", {}) or {})}

        # Subagent recursion (rugged decomposition §13): a slice flagged
        # ``decompose`` is itself large enough to fan out again rather than run as a
        # single doer. Bounded two ways — by ``max_depth`` and by the global roster
        # cap (subagent_free_slots), so a full roster simply runs the slice as one
        # doer instead of nesting deeper. The sub-fan-out plans on the foundation
        # brain (plan_brain) and runs its own children on the local brain.
        recurse = (
            bool(slice_.get("decompose"))
            and depth < max_depth
            and broker.store is not None
            and broker.store.subagent_free_slots(MAX_SUBAGENTS) > 1
        )
        if recurse:
            child = Task(
                title=f"[decompose] {slice_['goal']}",
                description=slice_["goal"],
                parent_id=parent.id,
                status=TaskStatus.IN_PROGRESS,
                acceptance_criteria=list(slice_.get("acceptance_criteria", [])),
                metadata={"goal": slice_["goal"]},
            )
            child.work_kind = work_kind
            board.add_task(child)
            parent.add_log(
                "Orchestrator",
                f"Slice {slice_['sub_intent']!r} re-decomposed (depth {depth + 1}).",
            )
            run_orchestrated_goal(
                board=board,
                parent=child,
                goal=slice_["goal"],
                intent=slice_["sub_intent"],
                work_kind=work_kind,
                brain=plan_brain or brain,
                broker=broker,
                context=slice_context,
                max_steps=max_steps,
                band=band,
                max_repairs=max_repairs,
                child_brain=brain,
                depth=depth + 1,
                max_depth=max_depth,
            )
        else:
            child = run_goal_task(
                board=board,
                parent=parent,
                goal=slice_["goal"],
                acceptance_criteria=tuple(slice_["acceptance_criteria"]),
                work_kind=work_kind,
                brain=brain,
                broker=broker,
                context=slice_context,
                max_steps=max_steps,
                intent=slice_["sub_intent"],
                resource_slice=resource_slice,
                contract_metadata=contract_metadata or None,
                max_repairs=max_repairs,
                review_return=review_return,
            )
        attach_child(parent, index, child.id)
        if child.status is TaskStatus.DONE:
            mark(parent, child.id, SLICE_SATISFIED)
        elif child.status is TaskStatus.PENDING_APPROVAL:
            pass  # slice stays open; it resolves when the approval does
        else:
            mark(parent, child.id, SLICE_BLOCKED)
        children.append(child)

    settle_parent_against_ledger(parent)
    return children


def run_orchestrated_goal(
    board: Board,
    parent: Task,
    goal: str,
    intent: str,
    work_kind: str,
    brain: Brain,
    broker: ToolBroker,
    context: dict[str, Any],
    max_steps: int = 16,
    cap: int = MAX_SUBAGENTS,
    band: float = ACTIVE_SLICE_CEILING,
    max_repairs: int = 2,
    child_brain: Brain | None = None,
    depth: int = 0,
    max_depth: int = 2,
) -> list[Task]:
    """The full fan-out: propose a decomposition (with the abundance frame),
    review the plan (the counterweight), then dispatch each slice through the
    both-agree gate, tracking the intent ledger.

    Ties (c) the frame, (d) plan-review, (e) the dispatch gate, and (b) the
    ledger into one entry. A plan the review rejects does not spawn anything —
    the parent blocks with the lens reasons, a visible signal to re-plan, rather
    than running an unreviewed fan-out.

    Two brains by design (docs/model-selection.md): ``brain`` is the orchestrator
    that PROPOSES the decomposition — a high-leverage call that runs on a frontier
    foundation model (rotated). ``child_brain`` is what the fan-out doers run on —
    the local workhorse; the agent fans the planned subtasks OUT to local. When
    ``child_brain`` is omitted both are the same brain (single-model callers/tests).
    """
    from orac.orchestrator import propose_decomposition  # noqa: PLC0415 (cycle)
    from orac.plan_review import review_decomposition  # noqa: PLC0415
    from orac.decomposition import score_decomposition, validate_decomposition  # noqa: PLC0415

    if broker.store is None:
        raise ValueError("run_orchestrated_goal needs a store-backed broker.")
    # The fan-out doers run local; only the proposal uses the foundation brain.
    child_brain = child_brain or brain

    spec = WORK_KINDS[work_kind]
    slices_plan = propose_decomposition(
        goal, intent, broker.store, brain, cap=cap, task=parent,
        work_kind=work_kind, default_verifiers=spec.verifiers,
    )

    # The deterministic floor (doc §4.3): reject structurally broken plans BEFORE
    # spending model tokens on plan review. A missing verifier, a placeholder
    # goal, or two slices owning the same resource are problems ORAC can see
    # without judgment — the parent blocks with the reasons and spawns nothing.
    free = broker.store.subagent_free_slots(cap)
    errors = validate_decomposition(
        parent,
        slices_plan,
        work_kind=work_kind,
        known_work_kinds=set(WORK_KINDS),
        allowed_verifiers=spec.verifiers,
        doer_available=spec.doer_slug is not None,
        max_slices=free,
    )
    if errors:
        parent.add_log(
            "Orchestrator",
            f"Decomposition failed structural validation: {'; '.join(errors)}",
        )
        parent.transition(TaskStatus.BLOCKED)
        return []

    # Telemetry only (§10): the floor above is the gate; the score's recommendation
    # is an operator-facing signal, not a second veto.
    score = score_decomposition(
        parent,
        slices_plan,
        work_kind=work_kind,
        known_work_kinds=set(WORK_KINDS),
        allowed_verifiers=spec.verifiers,
        doer_available=spec.doer_slug is not None,
        resource_slice=DEFAULT_RESOURCE_SLICE,
        max_slices=free,
    )
    parent.add_log(
        "Optimise",
        f"Decomposition scored {score.recommendation} "
        f"({score.slice_count} slice(s), est cost {score.estimated_cost:.2f}).",
    )

    # Plan review is the counterweight lens layer (ROUTING['lens'] == 'local'):
    # judge the orchestrator's plan on the cheap local brain, not the foundation
    # model that proposed it — an independent check, not the proposer grading itself.
    verdict = review_decomposition(intent, slices_plan, child_brain, task=parent)
    if verdict.status is not CapabilityStatus.ALLOWED:
        parent.add_log(
            "Orchestrator",
            f"Decomposition not accepted by plan review ({verdict.status.value}): "
            f"{verdict.reason}",
        )
        parent.transition(TaskStatus.BLOCKED)
        return []

    parent.add_log(
        "Orchestrator",
        f"Decomposed into {len(slices_plan)} slice(s); plan review passed.",
    )
    # The fan-out promotes the per-slice RETURN edge to a full council review on
    # the local child brain: the deterministic verifier proves it runs, the lenses
    # judge that what came back is on-goal and waste-free before it integrates.
    return run_decomposed_goal(
        board, parent, intent, slices_plan, work_kind, child_brain, broker, context,
        max_steps=max_steps, band=band, max_repairs=max_repairs, review_return=True,
        plan_brain=brain, depth=depth, max_depth=max_depth,
    )


def settle_parent_against_ledger(parent: Task) -> None:
    """Set the parent's status from its intent ledger (the authoritative gate).

    Covered -> DONE. Any slice blocked -> BLOCKED (the intent cannot be fully met
    as decomposed). Otherwise slices remain open -> the parent stays IN_PROGRESS
    and Intent logs the reminder of what is still owed. Re-callable after a parked
    slice resolves, so the parent closes only when the whole intent is satisfied.
    """
    report = coverage_report(parent)
    if is_covered(parent):
        parent.transition(TaskStatus.DONE)
        parent.add_log("Intent", f"Parent done — {report}")
    elif is_blocked(parent):
        parent.transition(TaskStatus.BLOCKED)
        parent.add_log("Intent", f"Parent blocked — {report}")
    else:
        if parent.status is not TaskStatus.IN_PROGRESS:
            parent.transition(TaskStatus.IN_PROGRESS)
        parent.add_log(
            "Intent",
            f"Orchestrator not finished — {report}",
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


def _verify_local_app(
    spec: WorkKindSpec,
    child: Task,
    broker: ToolBroker,
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Drive the running local app and confirm it renders, for a UI change.

    Applicability is the presence of ``app_url`` in context: a UI goal leaves
    the app running and names its URL, a backend-only goal does not. With no
    ``app_url`` this is a no-op pass — the run_tests verifier still gates the
    change. When it does apply, the browser tool navigates to the app through the
    local CDP primitive and checks the page loaded with non-empty content; a
    blank or unreachable app blocks the task rather than reaching DONE on the
    doer's word.
    """
    app_url = context.get("app_url")
    if not app_url:
        return True, "no app_url in context (not a UI change); skipped"
    doer = WORK_KINDS[spec.kind].doer_slug
    profiles = {profile.slug: profile for profile in load_agent_profiles()}
    agent_name = profiles[doer].name if doer in profiles else "Builder"
    args: dict[str, Any] = {"app_url": str(app_url)}
    if context.get("cdp_url"):
        args["cdp_url"] = str(context["cdp_url"])
    result = broker.request(
        CapabilityRequest(
            agent=agent_name,
            tool="browser.verify_local_app",
            task_id=child.id,
            args=args,
        ),
        child,
    )
    if result.status is not CapabilityStatus.ALLOWED:
        return False, f"could not verify app ({result.status.value}): {result.message}"
    if bool(result.data.get("verified")):
        return True, str(result.data.get("summary", "app verified"))
    return False, str(result.data.get("summary", "")) or "app did not verify"


# Verifier name -> how it confirms a kind's done-means. The check runs through
# the broker (audited, no privileged path) as the doer agent. Defined after the
# helpers so the registry binds real callables at import time.
_VERIFIERS = {
    "run_tests": _verify_run_tests,
    "verify_local_app": _verify_local_app,
}


def verify_goal_done(
    spec: WorkKindSpec,
    child: Task,
    broker: ToolBroker,
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Independently confirm the kind's done-means. Returns (ok, detail).

    Only invoked when a doer session claimed done, so ``spec.verifiers`` is
    non-empty (the doer/verifier invariant is enforced at spawn). Every verifier
    must pass; the first failure short-circuits with its detail. A verifier that
    does not apply to this goal (e.g. the UI check on a backend-only change)
    returns a no-op pass. An unknown verifier name is a configuration fault, not
    a silent pass.
    """
    details: list[str] = []
    for name in spec.verifiers:
        try:
            run = _VERIFIERS[name]
        except KeyError as exc:
            raise ValueError(
                f"work kind {spec.kind!r} names unknown verifier {name!r}."
            ) from exc
        ok, detail = run(spec, child, broker, context)
        if not ok:
            return False, detail
        details.append(f"{name}: {detail}")
    return True, "; ".join(details)
