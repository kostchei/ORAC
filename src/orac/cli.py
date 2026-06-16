from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
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
from orac.llm import build_brain, drain_foundation_spend_usd
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

    board = subparsers.add_parser("board", help="Board state operations.")
    board_sub = board.add_subparsers(dest="board_command", required=True)
    board_sub.add_parser(
        "recover", help="Restore board.json from the last-good backup."
    )
    board_events = board_sub.add_parser(
        "events", help="Show the append-only board event log (commit history)."
    )
    board_events.add_argument(
        "--limit", type=int, default=20, help="Show the most recent N events (0 = all)."
    )
    board_sub.add_parser(
        "rebuild", help="Rebuild board.json from the event log's latest snapshot."
    )

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

    metrics = subparsers.add_parser(
        "metrics",
        help="Show governance metrics: brokered calls, lens escalations, queue depth.",
    )
    metrics.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Machine-readable output (e.g. for lens calibration).",
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

    standing = subparsers.add_parser(
        "standing",
        help="Standing grants: pre-authorise a recurring action, rate-capped per day.",
    )
    standing_sub = standing.add_subparsers(dest="standing_command", required=True)
    standing_sub.add_parser("list", help="List active standing grants.")
    sg_add = standing_sub.add_parser(
        "add", help="Pre-authorise (agent, tool) to run without parking, up to a daily cap."
    )
    sg_add.add_argument("--agent", required=True, help="Agent the grant applies to.")
    sg_add.add_argument("--tool", required=True, help="Tool the grant pre-authorises.")
    sg_add.add_argument(
        "--daily-cap", type=int, required=True,
        help="Max auto-approved runs per day; over the cap, the action parks for a human.",
    )
    sg_add.add_argument("--reason", required=True, help="Why this is pre-authorised.")
    sg_add.add_argument(
        "--args-json", default=None,
        help="Optional canonical args JSON to pin the grant to one exact call.",
    )
    sg_revoke = standing_sub.add_parser("revoke", help="Revoke an active standing grant.")
    sg_revoke.add_argument("id", type=int, help="Standing grant id from `orac standing list`.")

    ui = subparsers.add_parser("ui", help="Run the local ORAC web UI.")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)

    chat = subparsers.add_parser("chat", help="Chat control plane (WhatsApp/Slack) sign-on.")
    chat_sub = chat.add_subparsers(dest="chat_command", required=True)
    chat_sub.add_parser("status", help="Show channel connection + allowlist state.")
    c_slack = chat_sub.add_parser("connect-slack", help="Store Slack tokens and enable the channel.")
    c_slack.add_argument("--bot-token", required=True, help="Slack bot token (xoxb-…).")
    c_slack.add_argument("--app-token", required=True, help="Slack app-level token (xapp-…, Socket Mode).")
    chat_sub.add_parser(
        "connect-whatsapp",
        help="Begin WhatsApp pairing (QR via the local bridge; see chat-control-plane.md).",
    )
    c_allow = chat_sub.add_parser("allow", help="Add a sender to a channel's allowlist.")
    c_allow.add_argument("channel", choices=["slack", "whatsapp"])
    c_allow.add_argument("sender", help="Slack user id (U…) or E.164 phone number.")
    c_disallow = chat_sub.add_parser("disallow", help="Remove a sender from a channel's allowlist.")
    c_disallow.add_argument("channel", choices=["slack", "whatsapp"])
    c_disallow.add_argument("sender")
    c_disc = chat_sub.add_parser("disconnect", help="Disable a channel and delete its stored secrets.")
    c_disc.add_argument("channel", choices=["slack", "whatsapp"])
    chat_run = chat_sub.add_parser("run", help="Run the live Slack/WhatsApp chat connectors.")
    chat_run.add_argument("--no-slack", action="store_true", help="Do not start Slack Socket Mode.")
    chat_run.add_argument("--no-whatsapp", action="store_true", help="Do not start the WhatsApp bridge client.")
    chat_run.add_argument("--poll-interval", type=float, default=3.0)
    chat_sub.add_parser(
        "whatsapp-bridge",
        help="Run the local Node WhatsApp bridge (installs bridge dependencies on first run).",
    )

    browser = subparsers.add_parser("browser", help="Browser-foundation operations.")
    browser_sub = browser.add_subparsers(dest="browser_command", required=True)
    doctor = browser_sub.add_parser(
        "doctor", help="Check provider chat-UI selectors against the live DOM."
    )
    doctor.add_argument(
        "--provider", default=None,
        help="Provider to check (claude/gemini/openai); default: all configured.",
    )
    doctor.add_argument("--cdp-url", default="http://localhost:9222")
    doctor.add_argument(
        "--no-probe", action="store_true",
        help="Only report selector matches; skip the live one-word round trip.",
    )

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


def cmd_board_recover(store: BoardStore) -> int:
    board = store.recover()
    print(
        f"Restored {store.board_path} from {store.backup_path} "
        f"({len(board.tasks)} task(s), revision {board.revision})."
    )
    return 0


def cmd_board_events(store: BoardStore, args: argparse.Namespace) -> int:
    events = store.read_events()
    if not events:
        print(f"No board events at {store.events_path}.")
        return 0
    limit = getattr(args, "limit", 20)
    shown = events if limit in (0, None) else events[-limit:]
    print(f"{len(events)} board event(s) at {store.events_path}:")
    for event in shown:
        changes = event.get("changes", {})
        delta = ", ".join(
            f"{k}={len(changes.get(k, []))}"
            for k in ("added", "updated", "removed")
            if changes.get(k)
        )
        print(
            f"  rev {event.get('revision'):>4}  {event.get('ts', '')}  "
            f"{event.get('tasks', 0)} task(s)" + (f"  [{delta}]" if delta else "")
        )
    return 0


def cmd_board_rebuild(store: BoardStore) -> int:
    board = store.restore_from_events()
    print(
        f"Rebuilt {store.board_path} from {store.events_path} "
        f"({len(board.tasks)} task(s), revision {board.revision})."
    )
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


def cmd_metrics(store: BoardStore, args: argparse.Namespace) -> int:
    from orac.metrics import compute_metrics, render_metrics

    bstore = BrokerStore(store.root).init()
    m = compute_metrics(bstore)
    if args.as_json:
        print(json.dumps(m, indent=2, sort_keys=True))
    else:
        print(render_metrics(m))
    return 0


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


def _rollback_via_contract(
    bstore: BrokerStore, store: BoardStore, note: Notification, contract: dict
) -> int:
    """Roll back a non-git action by applying its RollbackContract's inverse. If
    the inverse is not automatable, fall to the human-in-the-loop path: name the
    target and the manual step rather than guessing."""
    from orac.rollback_contract import RollbackContractError, apply_rollback

    try:
        message = apply_rollback(contract)
    except RollbackContractError as exc:
        target = contract.get("target_resource", "?")
        print(
            f"Notification [{note.id}] ({note.tool}) cannot be rolled back "
            f"automatically: {exc}\n"
            f"Manual undo required for {target}, then `orac ack {note.id}`."
        )
        return 1
    req = _rollback_request(note, str(store.root.resolve()), note.tool, {})
    bstore.record_audit(
        req,
        CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool="rollback.contract",
            message=message,
            data={"target_resource": contract.get("target_resource")},
        ),
    )
    print(message)
    if not note.acked:
        bstore.ack_notification(note.id)
    print(f"Rolled back and acked [{note.id}] {note.agent} {note.tool}.")
    return 0


def cmd_rollback(store: BoardStore, args: argparse.Namespace) -> int:
    bstore = BrokerStore(store.root).init()
    try:
        note = bstore.get_notification(args.id)
    except KeyError as exc:
        print(str(exc))
        return 1
    sha = note.data.get("sha")
    contract = note.data.get("rollback_contract")
    if not sha:
        # Non-git action: roll back via its RollbackContract if it recorded one;
        # otherwise there is nothing to undo automatically (human-in-the-loop).
        if contract:
            return _rollback_via_contract(bstore, store, note, contract)
        print(
            f"Notification [{note.id}] ({note.tool}) has no recorded commit sha or "
            "rollback contract; nothing to undo automatically. Undo it manually, "
            f"then `orac ack {note.id}`."
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


def cmd_standing_list(store: BoardStore) -> int:
    bstore = BrokerStore(store.root).init()
    grants = bstore.list_standing_grants()
    if not grants:
        print("No active standing grants.")
        return 0
    print("Active standing grants — `orac standing revoke <id>`:")
    for g in grants:
        scope = "any args" if g.args_pattern is None else f"args={g.args_pattern}"
        print(f"  [{g.id}] {g.agent} {g.tool} ({scope}) cap={g.daily_cap}/day — {g.reason}")
    return 0


def cmd_standing_add(store: BoardStore, args: argparse.Namespace) -> int:
    bstore = BrokerStore(store.root).init()
    parsed_args = json.loads(args.args_json) if args.args_json else None
    grant_id = bstore.create_standing_grant(
        agent=args.agent,
        tool=args.tool,
        daily_cap=args.daily_cap,
        reason=args.reason,
        args=parsed_args,
    )
    print(
        f"Added standing grant [{grant_id}]: {args.agent} may run {args.tool} "
        f"up to {args.daily_cap}x/day without parking."
    )
    return 0


def cmd_standing_revoke(store: BoardStore, args: argparse.Namespace) -> int:
    bstore = BrokerStore(store.root).init()
    try:
        bstore.revoke_standing_grant(args.id)
    except KeyError as exc:
        print(str(exc))
        return 1
    print(f"Revoked standing grant [{args.id}].")
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


def cmd_chat(store: BoardStore, args: argparse.Namespace) -> int:
    from orac.chat_config import CHANNELS, load_chat_config, save_chat_config
    from orac.credentials import CredentialStore

    creds = CredentialStore(store.root)
    cfg = load_chat_config(store)
    cmd = args.chat_command

    if cmd == "status":
        print(f"Chat control plane: {'ENABLED' if cfg['enabled'] else 'disabled'}")
        for channel in CHANNELS:
            spec = cfg["channels"][channel]
            ref_keys = [k for k in spec if k.endswith("_ref")]
            have = [k for k in ref_keys if creds.has(str(spec[k]))]
            senders = spec.get("authorized_senders", [])
            print(
                f"  {channel:9}: {'on' if spec.get('enabled') else 'off'}; "
                f"secrets {len(have)}/{len(ref_keys)} stored; "
                f"{len(senders)} allowed sender(s){': ' + ', '.join(senders) if senders else ''}"
            )
        return 0

    if cmd == "connect-slack":
        spec = cfg["channels"]["slack"]
        creds.set(str(spec["bot_token_ref"]), args.bot_token)
        creds.set(str(spec["app_token_ref"]), args.app_token)
        spec["enabled"] = True
        cfg["enabled"] = True
        save_chat_config(store, cfg)
        print(
            "Slack tokens stored (sealed) and channel enabled. Live validation runs "
            "when the Socket Mode connector lands (Phase 3). Add yourself: "
            "`orac chat allow slack <your-user-id>`."
        )
        return 0

    if cmd == "connect-whatsapp":
        spec = cfg["channels"]["whatsapp"]
        from orac.chat_signon import prepare_whatsapp

        status = prepare_whatsapp(store, bridge_url=str(spec["bridge_url"]))
        bridge = status["channels"]["whatsapp"]["bridge"]
        if bridge.get("connected"):
            print("WhatsApp bridge is paired and channel enabled.")
        elif bridge.get("qr"):
            print(f"WhatsApp bridge has a QR ready at {spec['bridge_url']}; scan it in the UI.")
        else:
            print(
                f"WhatsApp bridge is not paired yet. Start it with "
                "`orac chat whatsapp-bridge`, then open the local sign-on box."
            )
        return 0

    if cmd == "run":
        from orac.chat_runner import run_chat_connectors

        try:
            run_chat_connectors(
                root=store.root,
                slack=not args.no_slack,
                whatsapp=not args.no_whatsapp,
                poll_interval=float(args.poll_interval),
            )
        except Exception as exc:
            print(exc)
            return 1
        return 0

    if cmd == "whatsapp-bridge":
        return cmd_chat_whatsapp_bridge(store)

    if cmd == "allow":
        spec = cfg["channels"][args.channel]
        senders = list(spec.get("authorized_senders", []))
        if args.sender not in senders:
            senders.append(args.sender)
        spec["authorized_senders"] = senders
        save_chat_config(store, cfg)
        print(f"Allowed {args.sender!r} on {args.channel}. Allowlist: {senders}")
        return 0

    if cmd == "disallow":
        spec = cfg["channels"][args.channel]
        senders = [s for s in spec.get("authorized_senders", []) if s != args.sender]
        spec["authorized_senders"] = senders
        save_chat_config(store, cfg)
        print(f"Removed {args.sender!r} from {args.channel}. Allowlist: {senders}")
        return 0

    if cmd == "disconnect":
        spec = cfg["channels"][args.channel]
        for key in [k for k in spec if k.endswith("_ref")]:
            creds.delete(str(spec[key]))
        spec["enabled"] = False
        save_chat_config(store, cfg)
        print(f"Disconnected {args.channel}: channel disabled and stored secrets deleted.")
        return 0

    raise ValueError(f"Unknown chat command {cmd!r}.")


def cmd_chat_whatsapp_bridge(store: BoardStore) -> int:
    bridge_dir = Path(__file__).resolve().parents[2] / "bridges" / "whatsapp"
    if not bridge_dir.exists():
        print(f"WhatsApp bridge directory not found at {bridge_dir}.")
        return 1
    env = dict(os.environ)
    env["ORAC_ROOT"] = str(store.root.resolve())
    node_modules = bridge_dir / "node_modules"
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    node = shutil.which("node.exe") or shutil.which("node")
    if node is None:
        print("Node.js was not found on PATH; install Node.js before starting WhatsApp.")
        return 1
    if not node_modules.exists():
        if npm is None:
            print("npm was not found on PATH; install bridge dependencies manually first.")
            return 1
        install = subprocess.run([npm, "--prefix", str(bridge_dir), "install"], env=env)
        if install.returncode != 0:
            return install.returncode
    return subprocess.run([node, str(bridge_dir / "index.mjs")], cwd=store.root, env=env).returncode


def cmd_browser_doctor(args: argparse.Namespace) -> int:
    from orac.browser_brain import browser_doctor, format_doctor_report
    from orac.browser_selectors import load_provider_selectors

    if args.provider:
        providers = [args.provider]
    else:
        providers = list(load_provider_selectors())

    stale = False
    for provider in providers:
        report = browser_doctor(
            provider, args.cdp_url, probe=not args.no_probe
        )
        print(format_doctor_report(report))
        print()
        # The probe is the authority: send/response/streaming/stop don't exist on
        # an idle composer, so only a logged-in provider whose live round trip
        # FAILS is a stale-selector fault worth a non-zero exit for a cron.
        # Not-logged-in / unreachable / probe-skipped is not "stale".
        probe = report.get("probe")
        if report.get("login_ready") and probe is not None and not probe.get("ok"):
            stale = True
    return 1 if stale else 0


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
    spent = drain_foundation_spend_usd()
    if spent > 0:
        policy_store.record_foundation_spend(spent)
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
    if args.command == "board" and args.board_command == "recover":
        return cmd_board_recover(store)
    if args.command == "board" and args.board_command == "events":
        return cmd_board_events(store, args)
    if args.command == "board" and args.board_command == "rebuild":
        return cmd_board_rebuild(store)
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
    if args.command == "metrics":
        return cmd_metrics(store, args)
    if args.command == "approve":
        return cmd_approve(store, args, "approved")
    if args.command == "deny":
        return cmd_approve(store, args, "denied")
    if args.command == "ack":
        return cmd_ack(store, args)
    if args.command == "rollback":
        return cmd_rollback(store, args)
    if args.command == "standing" and args.standing_command == "list":
        return cmd_standing_list(store)
    if args.command == "standing" and args.standing_command == "add":
        return cmd_standing_add(store, args)
    if args.command == "standing" and args.standing_command == "revoke":
        return cmd_standing_revoke(store, args)
    if args.command == "models" and args.models_command == "lmstudio-status":
        return cmd_lmstudio_status()
    if args.command == "models" and args.models_command == "lmstudio-models":
        return cmd_lmstudio_models(store)
    if args.command == "models" and args.models_command == "lmstudio-start":
        return cmd_lmstudio_start(args)
    if args.command == "ui":
        run_ui(root=Path(args.root), host=args.host, port=args.port)
        return 0
    if args.command == "chat":
        return cmd_chat(store, args)
    if args.command == "browser" and args.browser_command == "doctor":
        return cmd_browser_doctor(args)
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
