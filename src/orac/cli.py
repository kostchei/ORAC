from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from orac.audio_io import audio_status, speak_text
from orac.agent_registry import (
    AgentProtocolSpec,
    get_agent_protocol,
    get_tool_map,
    load_agent_profiles,
    load_tool_specs,
)
from orac.intent_backbone import SPEC, IntentBackbone, IntentField
from orac.intent_gate import IntentGate
from orac.llm import build_brain
from orac.model_policy import (
    ModelPolicyStore,
    lmstudio_models,
    lmstudio_start,
    lmstudio_status,
    verify_model_slots,
)
from orac.broker_store import BrokerStore, Notification
from orac.code_adapters import code_adapters_for
from orac.models import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    Task,
    TaskStatus,
)
from orac.resources import read_resource_snapshot
from orac.scrum import Scrum
from orac.storage import BoardStore
from orac.task_registry import TaskRegistry
from orac.ui_server import run_ui
from orac.daemon import run_daemon

# Friendly CLI names for the LM Studio model slots in model_policy.DEFAULT_POLICY.
# "small" is the busy-box model and the model the council's LLM lenses run on.
_MODEL_SLOTS = {
    "standard": "lmstudio_standard_model",
    "small": "lmstudio_small_model",
    "code": "lmstudio_code_model",
    "creative": "lmstudio_creative_model",
}


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orac", description="Run a local scrum of agents.")
    parser.add_argument("--root", default=".", help="Project root containing the .orac board.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create an ORAC board in this repo.")

    add = subparsers.add_parser("add", help="Add a backlog task.")
    add.add_argument("title")
    add.add_argument("--desc", default="", help="Task description.")
    add.add_argument("--points", type=int, default=1, help="Estimated effort points.")

    list_cmd = subparsers.add_parser("list", help="List tasks.")
    list_cmd.add_argument("--status", choices=[status.value for status in TaskStatus])
    list_cmd.add_argument("--logs", action="store_true", help="Show task work logs.")

    registry = subparsers.add_parser("registry", help="Task registry operations.")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    registry_sub.add_parser("stats", help="Show task registry stats.")
    base = registry_sub.add_parser("base-request", help="Add a base request.")
    base.add_argument("title")
    base.add_argument("--desc", default="")
    base.add_argument("--points", type=int, default=1)

    intent = subparsers.add_parser("intent", help="Intent backbone operations.")
    intent_sub = intent.add_subparsers(dest="intent_command", required=True)
    intent_sub.add_parser("protocol", help="Show the Intent backbone protocol.")
    inspect = intent_sub.add_parser("inspect", help="Inspect task intent confidence and next action.")
    inspect.add_argument("task_id")
    answer = intent_sub.add_parser("answer", help="Record one clarification answer.")
    answer.add_argument("task_id")
    answer.add_argument("--field", choices=[field.value for field in IntentField], required=True)
    answer.add_argument("--value", required=True)
    lock = intent_sub.add_parser("lock", help="Lock intent after YES-GO.")
    lock.add_argument("task_id")
    blueprint = intent_sub.add_parser("blueprint", help="Show a short pre-build blueprint.")
    blueprint.add_argument("task_id")
    risk = intent_sub.add_parser("risk", help="Show the top intent failure scenarios.")
    risk.add_argument("task_id")
    reset = intent_sub.add_parser("reset", help="Reset a task's intent state.")
    reset.add_argument("task_id")

    agents = subparsers.add_parser("agents", help="Inspect ORAC agent profiles.")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_sub.add_parser("list", help="List configured agents.")
    show_agent = agents_sub.add_parser("show", help="Show one agent prompt and tools.")
    show_agent.add_argument("agent")
    agent_protocol = agents_sub.add_parser("protocol", help="Show one agent protocol.")
    agent_protocol.add_argument("agent")

    tools = subparsers.add_parser("tools", help="Inspect regular-use tool definitions.")
    tools_sub = tools.add_subparsers(dest="tools_command", required=True)
    tools_sub.add_parser("list", help="List configured tools.")
    show_tool = tools_sub.add_parser("show", help="Show one tool definition.")
    show_tool.add_argument("tool")

    resources = subparsers.add_parser("resources", help="Inspect local resources.")
    resources_sub = resources.add_subparsers(dest="resources_command", required=True)
    resources_sub.add_parser("check", help="Print the current resource snapshot.")

    audio = subparsers.add_parser("audio", help="Inspect and use local audio devices.")
    audio_sub = audio.add_subparsers(dest="audio_command", required=True)
    audio_sub.add_parser("devices", help="Print detected microphones and speakers.")
    speak = audio_sub.add_parser("speak", help="Speak text with the local TTS engine.")
    speak.add_argument("text")

    models = subparsers.add_parser("models", help="Model routing and LM Studio operations.")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser("policy", help="Print current model routing decision.")
    set_model = models_sub.add_parser(
        "set", help="Set an LM Studio model slot. Lenses use 'standard' unless 'small' is set."
    )
    set_model.add_argument(
        "--slot", choices=sorted(_MODEL_SLOTS), required=True,
        help="Which slot to set: standard (resident local model, also runs the lenses), "
        "small (optional busy-box/lens override), code, creative.",
    )
    set_model.add_argument("--model", required=True, help="LM Studio model key/name to assign.")
    models_sub.add_parser(
        "verify", help="Check that every configured model slot is loadable in LM Studio."
    )
    models_sub.add_parser("lmstudio-status", help="Check LM Studio local server status.")
    models_sub.add_parser("lmstudio-models", help="List models visible to LM Studio server.")
    start = models_sub.add_parser("lmstudio-start", help="Start LM Studio local server.")
    start.add_argument("--port", type=int, default=1234)

    lenses = subparsers.add_parser("lenses", help="Council LLM-lens operations.")
    lenses_sub = lenses.add_subparsers(dest="lenses_command", required=True)
    lenses_sub.add_parser(
        "eval", help="Score the lenses against curated cases on the local lens model."
    )

    reviews = subparsers.add_parser(
        "reviews",
        help="Show the review queue: pending approvals, unacked actions, lens verdicts.",
    )
    reviews.add_argument(
        "--all", action="store_true",
        help="Include acked notifications and the full lens-verdict history.",
    )
    reviews.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Machine-readable output (e.g. for calibration tooling).",
    )

    approve = subparsers.add_parser(
        "approve", help="Approve a pending request; the loop resumes the parked task."
    )
    approve.add_argument("id", type=int, help="Pending approval id from `orac reviews`.")

    deny = subparsers.add_parser(
        "deny", help="Deny a pending request; the loop blocks the parked task."
    )
    deny.add_argument("id", type=int, help="Pending approval id from `orac reviews`.")

    ack = subparsers.add_parser(
        "ack", help="Acknowledge a reviewed action as ok (it stands as done)."
    )
    ack.add_argument("id", type=int, help="Notification id from `orac reviews`.")

    rollback = subparsers.add_parser(
        "rollback",
        help="Undo a reviewed action: git-revert its recorded commit, then ack it.",
    )
    rollback.add_argument("id", type=int, help="Notification id from `orac reviews`.")
    rollback.add_argument(
        "--push", action="store_true",
        help="Also push the inverse commit to the notification's remote.",
    )

    ui = subparsers.add_parser("ui", help="Run the local ORAC web UI.")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)

    daemon = subparsers.add_parser("daemon", help="Run ORAC continuously.")
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_run = daemon_sub.add_parser("run", help="Run the 24/7 agent loop.")
    daemon_run.add_argument("--interval", type=int, default=60)
    daemon_run.add_argument("--cycles", type=int, default=1)

    sprint = subparsers.add_parser("sprint", help="Sprint operations.")
    sprint_sub = sprint.add_subparsers(dest="sprint_command", required=True)
    plan = sprint_sub.add_parser("plan", help="Select backlog work for the next sprint.")
    plan.add_argument("--capacity", type=int, default=5)
    plan.add_argument("--brain", choices=["rules", "ollama"], default="rules")

    scrum = subparsers.add_parser("scrum", help="Scrum runner operations.")
    scrum_sub = scrum.add_subparsers(dest="scrum_command", required=True)
    run = scrum_sub.add_parser("run", help="Run the local agents.")
    run.add_argument("--cycles", type=int, default=1)
    run.add_argument(
        "--brain",
        choices=["auto", "rules", "ollama", "lmstudio", "foundation"],
        default="auto",
    )
    run.add_argument(
        "--lenses",
        action="store_true",
        help="Enable the council's LLM lenses (local model) on consequential edges.",
    )

    return parser


def configure_output_encoding() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def cmd_init(store: BoardStore) -> int:
    store.init()
    print(f"Initialized ORAC board at {store.board_path}")
    return 0


def cmd_add(store: BoardStore, args: argparse.Namespace) -> int:
    board = store.load()
    task = Task(title=args.title, description=args.desc, points=args.points)
    board.add_task(task)
    store.save(board)
    print(f"Added {task.id}: {task.title}")
    return 0


def cmd_list(store: BoardStore, args: argparse.Namespace) -> int:
    board = store.load()
    status = TaskStatus(args.status) if args.status else None
    tasks = [task for task in board.tasks if status is None or task.status == status]
    if not tasks:
        print("No tasks found.")
        return 0
    for task in tasks:
        assignee = f" @{task.assignee}" if task.assignee else ""
        print(f"{task.id} [{task.status.value}] ({task.points}){assignee} {task.title}")
        if task.description:
            print(f"  {task.description}")
        if task.acceptance_criteria:
            print("  Acceptance criteria:")
            for criterion in task.acceptance_criteria:
                print(f"  - {criterion}")
        if args.logs and task.work_log:
            print("  Work log:")
            for entry in task.work_log:
                print(f"  - {entry.created_at} {entry.agent}: {entry.message}")
    return 0


def cmd_registry_stats(store: BoardStore) -> int:
    stats = TaskRegistry(store.load()).stats()
    print(json.dumps(asdict(stats), indent=2, sort_keys=True))
    return 0


def cmd_registry_base_request(store: BoardStore, args: argparse.Namespace) -> int:
    board = store.load()
    task = TaskRegistry(board).add_base_request(args.title, args.desc, args.points)
    store.save(board)
    print(f"Added base request {task.id}: {task.title}")
    return 0


def print_intent_assessment(task: Task) -> None:
    assessment = IntentBackbone().assess(task)
    print(f"{task.id} [{task.status.value}] {task.title}")
    print(f"Confidence: {assessment.confidence}%")
    print(f"Locked: {assessment.locked}")
    if assessment.missing_fields:
        print("Missing fields:")
        for field in assessment.missing_fields:
            print(f"- {field.value}")
    if assessment.next_question:
        print(f"Next question: {assessment.next_question}")
    print(f"Echo check: {assessment.echo_check}")


def print_protocol(spec: AgentProtocolSpec) -> None:
    print(spec.title)
    print(spec.mission)
    print(f"Response prompt: {spec.response_prompt}")
    for step in spec.steps:
        print(f"{step.step}. {step.name}: {step.description}")
        if step.details:
            print(f"   Details: {step.details}")


def cmd_intent_protocol() -> int:
    print_protocol(SPEC)
    return 0


def cmd_intent_inspect(store: BoardStore, args: argparse.Namespace) -> int:
    board = store.load()
    print_intent_assessment(board.get_task(args.task_id))
    return 0


def cmd_intent_answer(store: BoardStore, args: argparse.Namespace) -> int:
    board = store.load()
    task = board.get_task(args.task_id)
    IntentBackbone().answer(task, args.field, args.value)
    store.save(board)
    print_intent_assessment(task)
    return 0


def cmd_intent_lock(store: BoardStore, args: argparse.Namespace) -> int:
    board = store.load()
    task = board.get_task(args.task_id)
    try:
        IntentBackbone().lock(task)
    except ValueError as exc:
        print(str(exc))
        print_intent_assessment(task)
        return 1
    IntentGate().release(task)
    store.save(board)
    print_intent_assessment(task)
    return 0


def cmd_intent_blueprint(store: BoardStore, args: argparse.Namespace) -> int:
    task = store.load().get_task(args.task_id)
    for step in IntentBackbone().blueprint(task):
        print(f"- {step}")
    return 0


def cmd_intent_risk(store: BoardStore, args: argparse.Namespace) -> int:
    task = store.load().get_task(args.task_id)
    for risk in IntentBackbone().risk_report(task):
        print(f"- {risk}")
    return 0


def cmd_intent_reset(store: BoardStore, args: argparse.Namespace) -> int:
    board = store.load()
    task = board.get_task(args.task_id)
    IntentBackbone().reset(task)
    store.save(board)
    print_intent_assessment(task)
    return 0


def cmd_agents_list() -> int:
    for profile in load_agent_profiles():
        print(f"{profile.order:02d} {profile.name} [{profile.slug}]")
        print(f"  {profile.purpose}")
    return 0


def cmd_agents_show(args: argparse.Namespace) -> int:
    profiles = {profile.slug: profile for profile in load_agent_profiles()}
    profile = profiles.get(args.agent.lower())
    if profile is None:
        print(f"No agent found for {args.agent!r}.")
        return 1
    print(f"{profile.name} [{profile.slug}]")
    print(profile.purpose)
    print("\nPrompt:")
    print(profile.system_prompt)
    print("\nRegular tools:")
    tool_map = get_tool_map()
    for tool_name in profile.tools:
        tool = tool_map[tool_name]
        print(f"- {tool.name}: {tool.regular_use}")
    return 0


def cmd_agents_protocol(args: argparse.Namespace) -> int:
    try:
        protocol = get_agent_protocol(args.agent.lower())
    except KeyError as exc:
        print(str(exc))
        return 1
    print_protocol(protocol)
    return 0


def cmd_tools_list() -> int:
    for tool in load_tool_specs():
        print(f"{tool.name}")
        print(f"  {tool.description}")
    return 0


def cmd_tools_show(args: argparse.Namespace) -> int:
    tools = get_tool_map()
    tool = tools.get(args.tool)
    if tool is None:
        print(f"No tool found for {args.tool!r}.")
        return 1
    print(tool.name)
    print(tool.description)
    print(f"Regular use: {tool.regular_use}")
    print("Inputs:")
    for input_name in tool.inputs:
        print(f"- {input_name}")
    return 0


def cmd_resources_check() -> int:
    print(json.dumps(read_resource_snapshot().to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_audio_devices() -> int:
    print(json.dumps(audio_status().to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_audio_speak(args: argparse.Namespace) -> int:
    result = speak_text(args.text)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


def cmd_models_policy(store: BoardStore) -> int:
    decision = ModelPolicyStore(store).decide()
    print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_models_set(store: BoardStore, args: argparse.Namespace) -> int:
    policy_store = ModelPolicyStore(store)
    policy = policy_store.load_policy()  # full current policy; mutate one slot, keep the rest
    key = _MODEL_SLOTS[args.slot]
    policy[key] = args.model
    policy_store.save_policy(policy)
    notes = {
        "standard": " — the resident local model; the council's lenses run on it too",
        "small": " — optional busy-box override; the lenses use this when set",
    }
    print(f"Set {key} = {args.model!r}{notes.get(args.slot, '')}")
    return 0


def cmd_models_verify(store: BoardStore) -> int:
    report = verify_model_slots(ModelPolicyStore(store))
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["checked"]:
        return 0  # could not reach LM Studio; nothing to validate against
    return 0 if report["ok"] else 1


def cmd_lenses_eval(store: BoardStore) -> int:
    from orac.lens_eval import print_scorecard, run_lens_eval
    from orac.model_policy import lens_brain

    brain = lens_brain(ModelPolicyStore(store))
    print(f"Evaluating lenses on local model {brain.model!r} ...")
    return print_scorecard(run_lens_eval(brain))


# How many lens verdicts the default `orac reviews` view shows.
_RECENT_VERDICTS = 20


def cmd_reviews(store: BoardStore, args: argparse.Namespace) -> int:
    bstore = BrokerStore(store.root).init()
    pending = bstore.list_pending()
    notifications = bstore.list_notifications(unacked_only=not args.all)
    verdicts = (
        bstore.list_reviews()
        if args.all
        else bstore.list_reviews(limit=_RECENT_VERDICTS)
    )

    if args.as_json:
        payload = {
            "pending_approvals": [asdict(p) for p in pending],
            "notifications": [asdict(n) for n in notifications],
            "lens_verdicts": verdicts,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not pending and not notifications and not verdicts:
        print("Review queue is clear.")
        return 0

    # The cause behind each parked request: the latest non-pass lens verdict for
    # the same (agent, tool, task) edge, so the human reviews a reason, not just
    # a tool name.
    causes: dict[tuple[str, str, str], str] = {}
    for row in bstore.list_reviews():
        if row["decision"] != "pass":
            key = (row["agent"], row["tool"], row["task_id"])
            causes[key] = f"{row['lens']}[{row['decision']}]: {row['reason']}"

    if pending:
        print("Pending approvals — `orac approve <id>` / `orac deny <id>`:")
        for p in pending:
            print(f"  [{p.id}] {p.created_at} {p.agent} {p.tool} task={p.task_id}")
            if p.args:
                print(f"      args: {json.dumps(p.args, sort_keys=True)}")
            cause = causes.get((p.agent, p.tool, p.task_id))
            if cause:
                print(f"      cause: {cause}")
    if notifications:
        print(
            "Completed actions awaiting review — `orac ack <id>` to accept, "
            "`orac rollback <id>` to undo:"
        )
        for n in notifications:
            acked = " (acked)" if n.acked else ""
            print(f"  [{n.id}]{acked} {n.created_at} {n.agent} {n.tool} task={n.task_id}")
            print(f"      {n.message}")
            if n.args:
                print(f"      args: {json.dumps(n.args, sort_keys=True)}")
    if verdicts:
        scope = "all" if args.all else f"latest {len(verdicts)}"
        print(f"Lens verdicts ({scope}):")
        for row in verdicts:
            print(
                f"  {row['created_at']} {row['lens']}[{row['decision']}] "
                f"{row['agent']}/{row['tool']} task={row['task_id']}: {row['reason']}"
            )
    return 0


def cmd_approve(store: BoardStore, args: argparse.Namespace, status: str) -> int:
    bstore = BrokerStore(store.root).init()
    try:
        bstore.resolve_pending(args.id, status)
    except KeyError as exc:
        print(str(exc))
        return 1
    pending = bstore.get_pending(args.id)
    outcome = (
        "the loop will resume the parked task"
        if status == "approved"
        else "the loop will block the parked task"
    )
    print(f"{status.capitalize()} [{args.id}] {pending.agent} {pending.tool}; {outcome}.")
    return 0


def cmd_ack(store: BoardStore, args: argparse.Namespace) -> int:
    bstore = BrokerStore(store.root).init()
    try:
        bstore.ack_notification(args.id)
    except KeyError as exc:
        print(str(exc))
        return 1
    note = bstore.get_notification(args.id)
    print(f"Acked [{args.id}] {note.agent} {note.tool}: {note.message}")
    return 0


def _rollback_request(
    note: Notification, root: str, tool: str, call_args: dict[str, object]
) -> CapabilityRequest:
    # Rollback is the human acting on the review queue, so it is recorded under
    # the "human" principal — distinct from any agent — in the same audit log.
    return CapabilityRequest(agent="human", tool=tool, task_id=note.task_id, args=call_args)


def cmd_rollback(store: BoardStore, args: argparse.Namespace) -> int:
    bstore = BrokerStore(store.root).init()
    try:
        note = bstore.get_notification(args.id)
    except KeyError as exc:
        print(str(exc))
        return 1
    sha = note.data.get("sha")
    if not sha:
        print(
            f"Notification [{note.id}] ({note.tool}) has no recorded commit sha; "
            "nothing to revert automatically. Revert manually, then `orac ack`."
        )
        return 1
    root = str(note.data.get("root") or store.root.resolve())
    adapters = code_adapters_for((root,))

    req = _rollback_request(note, root, "git.revert", {"root": root, "sha": sha})
    result = adapters["git.revert"](req)
    bstore.record_audit(
        req,
        CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool=result.name,
            message=result.message,
            data=result.data,
        ),
    )
    print(result.message)

    if args.push:
        remote = note.data.get("remote", "origin")
        push_args: dict[str, object] = {"root": root, "remote": remote}
        branch = note.data.get("branch")
        if branch:
            push_args["branch"] = branch
        push_req = _rollback_request(note, root, "git.push", push_args)
        push_result = adapters["git.push"](push_req)
        bstore.record_audit(
            push_req,
            CapabilityResult(
                status=CapabilityStatus.ALLOWED,
                tool=push_result.name,
                message=push_result.message,
                data=push_result.data,
            ),
        )
        print(push_result.message)

    if not note.acked:
        bstore.ack_notification(note.id)
    print(f"Rolled back and acked [{note.id}] {note.agent} {note.tool}.")
    return 0


def cmd_lmstudio_status() -> int:
    print(json.dumps(lmstudio_status(), indent=2, sort_keys=True))
    return 0


def cmd_lmstudio_models(store: BoardStore) -> int:
    policy = ModelPolicyStore(store).load_policy()
    models = lmstudio_models(str(policy["lmstudio_url"]))
    print(json.dumps({"models": models}, indent=2, sort_keys=True))
    return 0


def cmd_lmstudio_start(args: argparse.Namespace) -> int:
    ok, output = lmstudio_start(args.port)
    print(output)
    return 0 if ok else 1


def cmd_sprint_plan(store: BoardStore, args: argparse.Namespace) -> int:
    board = store.load()
    scrum = Scrum(build_brain(args.brain))
    planned = scrum.plan_sprint(board, args.capacity)
    store.save(board)
    if not planned:
        print("No backlog tasks fit the sprint capacity.")
        return 0
    print(f"Planned {len(planned)} task(s):")
    for task in planned:
        print(f"- {task.id} {task.title} ({task.points})")
    return 0


def cmd_scrum_run(store: BoardStore, args: argparse.Namespace) -> int:
    board = store.load()
    if args.brain == "auto":
        policy_store = ModelPolicyStore(store)
        decision = policy_store.decide()
        brain_name = decision.brain
        model = decision.model
    else:
        policy_store = ModelPolicyStore(store)
        decision = None
        brain_name = args.brain
        model = None
    scrum = Scrum(build_brain(brain_name, model=model), root=store.root, llm_lenses=args.lenses)
    result = scrum.run(board, cycles=args.cycles)
    store.save(board)
    if decision and decision.brain == "foundation" and result.touched_tasks:
        policy_store.record_foundation_spend(policy_store.estimated_cycle_spend())
    print(
        f"Ran {result.cycles} cycle(s); touched {result.touched_tasks} task(s); "
        f"{result.done_tasks} task(s) done."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_output_encoding()
    parser = make_parser()
    args = parser.parse_args(argv)
    store = BoardStore(Path(args.root))

    if args.command == "init":
        return cmd_init(store)
    if args.command == "add":
        return cmd_add(store, args)
    if args.command == "list":
        return cmd_list(store, args)
    if args.command == "registry" and args.registry_command == "stats":
        return cmd_registry_stats(store)
    if args.command == "registry" and args.registry_command == "base-request":
        return cmd_registry_base_request(store, args)
    if args.command == "intent" and args.intent_command == "protocol":
        return cmd_intent_protocol()
    if args.command == "intent" and args.intent_command == "inspect":
        return cmd_intent_inspect(store, args)
    if args.command == "intent" and args.intent_command == "answer":
        return cmd_intent_answer(store, args)
    if args.command == "intent" and args.intent_command == "lock":
        return cmd_intent_lock(store, args)
    if args.command == "intent" and args.intent_command == "blueprint":
        return cmd_intent_blueprint(store, args)
    if args.command == "intent" and args.intent_command == "risk":
        return cmd_intent_risk(store, args)
    if args.command == "intent" and args.intent_command == "reset":
        return cmd_intent_reset(store, args)
    if args.command == "agents" and args.agents_command == "list":
        return cmd_agents_list()
    if args.command == "agents" and args.agents_command == "show":
        return cmd_agents_show(args)
    if args.command == "agents" and args.agents_command == "protocol":
        return cmd_agents_protocol(args)
    if args.command == "tools" and args.tools_command == "list":
        return cmd_tools_list()
    if args.command == "tools" and args.tools_command == "show":
        return cmd_tools_show(args)
    if args.command == "resources" and args.resources_command == "check":
        return cmd_resources_check()
    if args.command == "audio" and args.audio_command == "devices":
        return cmd_audio_devices()
    if args.command == "audio" and args.audio_command == "speak":
        return cmd_audio_speak(args)
    if args.command == "models" and args.models_command == "policy":
        return cmd_models_policy(store)
    if args.command == "models" and args.models_command == "set":
        return cmd_models_set(store, args)
    if args.command == "models" and args.models_command == "verify":
        return cmd_models_verify(store)
    if args.command == "lenses" and args.lenses_command == "eval":
        return cmd_lenses_eval(store)
    if args.command == "reviews":
        return cmd_reviews(store, args)
    if args.command == "approve":
        return cmd_approve(store, args, "approved")
    if args.command == "deny":
        return cmd_approve(store, args, "denied")
    if args.command == "ack":
        return cmd_ack(store, args)
    if args.command == "rollback":
        return cmd_rollback(store, args)
    if args.command == "models" and args.models_command == "lmstudio-status":
        return cmd_lmstudio_status()
    if args.command == "models" and args.models_command == "lmstudio-models":
        return cmd_lmstudio_models(store)
    if args.command == "models" and args.models_command == "lmstudio-start":
        return cmd_lmstudio_start(args)
    if args.command == "ui":
        run_ui(root=Path(args.root), host=args.host, port=args.port)
        return 0
    if args.command == "daemon" and args.daemon_command == "run":
        run_daemon(root=Path(args.root), interval_seconds=args.interval, cycles=args.cycles)
        return 0
    if args.command == "sprint" and args.sprint_command == "plan":
        return cmd_sprint_plan(store, args)
    if args.command == "scrum" and args.scrum_command == "run":
        return cmd_scrum_run(store, args)

    parser.error("Unknown command.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
