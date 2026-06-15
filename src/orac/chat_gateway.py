from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from orac.broker_store import BrokerStore, Notification
from orac.chat_config import CHANNELS, is_authorized_sender, load_chat_config
from orac.chat_logs import CommsLog
from orac.code_adapters import code_adapters_for
from orac.llm import LMStudioBrain
from orac.model_policy import ModelPolicyStore
from orac.models import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    Task,
    TaskStatus,
)
from orac.notify import review_queue_summary
from orac.storage import BoardStore
from orac.task_registry import TaskRegistry

HELP_TEXT = """ORAC chat commands:
goal: <text>
status
reviews
approve <pending-id>
deny <pending-id>
ack <notification-id>
rollback <notification-id>
help"""


@dataclass(frozen=True)
class InboundMessage:
    channel: str
    sender: str
    text: str
    reply_to: str | None = None


@dataclass(frozen=True)
class OutboundMessage:
    channel: str
    target: str
    text: str


class ChatRateLimiter:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, channel: str, sender: str, limit_per_minute: int) -> bool:
        if limit_per_minute <= 0:
            return False
        now = time.monotonic()
        key = (channel, sender)
        events = self._events[key]
        cutoff = now - 60.0
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= limit_per_minute:
            return False
        events.append(now)
        return True


class ChatGateway:
    def __init__(self, root: Path | str = ".") -> None:
        self.store = BoardStore(root)
        self.rate_limiter = ChatRateLimiter()
        self.log = CommsLog(self.store.root)
        self._last_push_digest: str | None = None

    def handle(self, message: InboundMessage) -> list[OutboundMessage]:
        cfg = load_chat_config(self.store)
        if message.channel not in CHANNELS:
            self.log.record(
                "inbound_ignored",
                reason="unknown_channel",
                channel=message.channel,
                sender=message.sender,
            )
            return []
        if not is_authorized_sender(cfg, message.channel, message.sender):
            self.log.record(
                "inbound_ignored",
                reason="unauthorized_sender",
                channel=message.channel,
                sender=message.sender,
            )
            return []
        self.log.record(
            "inbound",
            channel=message.channel,
            sender=message.sender,
            text=message.text,
        )
        if not self.rate_limiter.allow(
            message.channel,
            message.sender,
            int(cfg.get("inbound_rate_per_min", 10)),
        ):
            reply = self._reply(message, "Rate limit hit. Wait a minute, then retry.")
            self._log_outbound(reply, reason="rate_limit")
            return [reply]

        try:
            text = self._dispatch(message)
        except Exception as exc:
            text = f"Command failed: {exc}"
        reply = self._reply(message, text)
        self._log_outbound(reply, reason="reply")
        return [reply]

    def poll_outbound(self) -> list[OutboundMessage]:
        cfg = load_chat_config(self.store)
        if not cfg.get("enabled"):
            self._last_push_digest = None
            return []

        bstore = BrokerStore(self.store.root).init()
        pending = bstore.list_pending()
        notes = bstore.list_notifications(unacked_only=True)
        digest = json.dumps(
            {
                "pending": [p.id for p in pending],
                "notifications": [n.id for n in notes],
            },
            sort_keys=True,
        )
        if digest == self._last_push_digest:
            return []
        had_previous = self._last_push_digest is not None
        self._last_push_digest = digest
        if not pending and not notes and not had_previous:
            return []

        text = self._reviews_text(pending_limit=4, notification_limit=4)
        out: list[OutboundMessage] = []
        for channel in CHANNELS:
            spec = cfg["channels"][channel]
            if not spec.get("enabled"):
                continue
            for sender in spec.get("authorized_senders", []):
                outbound = OutboundMessage(channel=channel, target=sender, text=text)
                self._log_outbound(outbound, reason="review_push")
                out.append(outbound)
        return out

    def _reply(self, message: InboundMessage, text: str) -> OutboundMessage:
        return OutboundMessage(
            channel=message.channel,
            target=message.reply_to or message.sender,
            text=text,
        )

    def _log_outbound(self, message: OutboundMessage, reason: str) -> None:
        self.log.record(
            "outbound",
            reason=reason,
            channel=message.channel,
            target=message.target,
            text=message.text,
        )

    def _dispatch(self, message: InboundMessage) -> str:
        raw = message.text.strip()
        lower = raw.lower()
        if not raw or lower == "help":
            return HELP_TEXT
        if lower.startswith("/goal "):
            return self._add_goal(raw[6:].strip(), message)
        if lower.startswith("goal:"):
            return self._add_goal(raw.split(":", 1)[1].strip(), message)
        if lower == "status":
            return self._status_text()
        if lower == "reviews":
            return self._reviews_text()
        if lower.startswith("approve "):
            return self._resolve_pending(raw, "approved")
        if lower.startswith("deny "):
            return self._resolve_pending(raw, "denied")
        if lower.startswith("ack "):
            return self._ack(raw)
        if lower.startswith("rollback "):
            return self._rollback(raw)
        return self._freeform_reply(raw, message)

    def _load_or_init_board(self):
        try:
            return self.store.load()
        except FileNotFoundError:
            return self.store.init()

    def _add_goal(self, goal: str, message: InboundMessage) -> str:
        if not goal:
            return "Usage: goal: <text>"
        board = self._load_or_init_board()
        title = goal if len(goal) <= 80 else goal[:77].rstrip() + "..."
        task = Task(
            title=title,
            description=goal,
            status=TaskStatus.READY,
            work_kind="code",
            acceptance_criteria=[goal],
            metadata={
                "request_type": "chat_goal",
                "goal": goal,
                "source_channel": message.channel,
                "source_sender": message.sender,
            },
        )
        task.add_log("User", f"Chat goal added from {message.channel}.", kind="user")
        board.add_task(task)
        self.store.save(board)
        return f"Added goal {task.id}: {task.title}"

    def _status_text(self) -> str:
        board = self._load_or_init_board()
        stats = TaskRegistry(board).stats()
        summary = review_queue_summary(BrokerStore(self.store.root).init())
        return (
            f"ORAC status: {stats.active} active, {stats.blocked} blocked, "
            f"{stats.done} done, {stats.backlog} backlog. {summary.message()}"
        )

    def _freeform_reply(self, raw: str, message: InboundMessage) -> str:
        lower = raw.lower()
        if _mentions_any(lower, {"blocked", "stuck", "relevant", "relevance"}):
            return self._naturalize(raw, self._blocked_text(), intent="blocked review")
        if _mentions_any(lower, {"review", "approval", "pending"}):
            return self._naturalize(raw, self._reviews_text(), intent="review queue")
        if _mentions_any(lower, {"status", "state", "progress", "what now"}):
            return self._naturalize(raw, self._status_text(), intent="status")
        if _looks_like_goal_request(lower):
            goal = _strip_goal_request(raw)
            if goal:
                return self._add_goal(goal, message)

        context = "\n".join(
            [
                self._status_text(),
                self._blocked_text(limit=3),
                self._reviews_text(pending_limit=3, notification_limit=3),
            ]
        )
        fallback = (
            "I can answer status, reviews, and blocked-work questions in normal "
            "language. To create work, say `goal: <what you want>` or `create task <what you want>`."
        )
        return self._naturalize(raw, fallback, intent="general operator question", context=context)

    def _blocked_text(self, limit: int = 8) -> str:
        board = self._load_or_init_board()
        blocked = [task for task in board.tasks if task.status == TaskStatus.BLOCKED]
        if not blocked:
            return "No blocked tasks right now."

        lines = ["Blocked tasks:"]
        for task in blocked[:limit]:
            last_log = task.work_log[-1].message if task.work_log else "No work log yet."
            lines.append(f"[{task.id}] {task.title} - {last_log}")
        if len(blocked) > limit:
            lines.append(f"... {len(blocked) - limit} more blocked task(s)")
        lines.append("I cannot decide relevance automatically; tell me which IDs to revive or ignore.")
        return "\n".join(lines)

    def _naturalize(
        self,
        user_text: str,
        factual_reply: str,
        *,
        intent: str,
        context: str | None = None,
    ) -> str:
        prompt = (
            "Write the exact chat reply ORAC should send to the operator.\n"
            "Use only the facts below. Do not invent task state. Do not approve, deny, "
            "ack, rollback, delete, or change anything. Preserve ids exactly. Keep it under "
            "90 words unless there is a list of ids.\n\n"
            f"Operator message: {user_text}\n"
            f"Intent: {intent}\n"
            f"Facts:\n{context or factual_reply}\n\n"
            f"Fallback reply if the facts are enough:\n{factual_reply}"
        )
        policy = ModelPolicyStore(self.store).load_policy()
        base_url = str(policy.get("lmstudio_url") or "http://localhost:1234/v1")
        task = Task(title="Chat operator reply", description=user_text, work_kind="comms")
        errors: list[str] = []
        for model in _chat_model_candidates(policy):
            try:
                brain = LMStudioBrain(
                    base_url=base_url,
                    model=model,
                    timeout_seconds=60,
                    max_tokens=512,
                )
                reply = brain.think("ORAC", "chat assistant", task, prompt).strip()
            except Exception as exc:
                errors.append(f"{model}: {exc}")
                continue
            if reply:
                self.log.record(
                    "llm_reply",
                    channel="chat",
                    intent=intent,
                    model=model,
                )
                return _trim_reply(reply)

        self.log.record(
            "llm_unavailable",
            channel="chat",
            reason="; ".join(errors) or "empty reply",
            intent=intent,
        )
        return factual_reply

    def _reviews_text(
        self, pending_limit: int = 8, notification_limit: int = 8
    ) -> str:
        bstore = BrokerStore(self.store.root).init()
        pending = bstore.list_pending()
        notes = bstore.list_notifications(unacked_only=True)
        if not pending and not notes:
            return "Review queue clear."

        lines = ["Review queue:"]
        for p in pending[:pending_limit]:
            arg_text = f" args={json.dumps(p.args, sort_keys=True)}" if p.args else ""
            lines.append(f"pending [{p.id}] {p.agent}/{p.tool} task={p.task_id}{arg_text}")
        if len(pending) > pending_limit:
            lines.append(f"... {len(pending) - pending_limit} more pending approval(s)")
        for n in notes[:notification_limit]:
            lines.append(f"review [{n.id}] {n.agent}/{n.tool} task={n.task_id}: {n.message}")
        if len(notes) > notification_limit:
            lines.append(f"... {len(notes) - notification_limit} more review notification(s)")
        return "\n".join(lines)

    def _parse_id(self, raw: str, verb: str) -> int:
        parts = raw.split()
        if len(parts) != 2:
            raise ValueError(f"Usage: {verb} <id>")
        return int(parts[1])

    def _resolve_pending(self, raw: str, status: str) -> str:
        pending_id = self._parse_id(raw, "approve" if status == "approved" else "deny")
        bstore = BrokerStore(self.store.root).init()
        bstore.resolve_pending(pending_id, status)
        pending = bstore.get_pending(pending_id)
        outcome = "will resume" if status == "approved" else "will block"
        return f"{status.capitalize()} [{pending_id}] {pending.agent}/{pending.tool}; loop {outcome} the parked task."

    def _ack(self, raw: str) -> str:
        note_id = self._parse_id(raw, "ack")
        bstore = BrokerStore(self.store.root).init()
        bstore.ack_notification(note_id)
        note = bstore.get_notification(note_id)
        return f"Acked [{note_id}] {note.agent}/{note.tool}: {note.message}"

    def _rollback(self, raw: str) -> str:
        note_id = self._parse_id(raw, "rollback")
        bstore = BrokerStore(self.store.root).init()
        note = bstore.get_notification(note_id)
        sha = note.data.get("sha")
        if not sha:
            return (
                f"Notification [{note.id}] has no recorded commit sha; "
                "nothing to revert automatically."
            )
        root = str(note.data.get("root") or self.store.root.resolve())
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
        if not note.acked:
            bstore.ack_notification(note.id)
        return f"{result.message}\nRolled back and acked [{note.id}] {note.agent}/{note.tool}."


def _rollback_request(
    note: Notification, root: str, tool: str, call_args: dict[str, object]
) -> CapabilityRequest:
    return CapabilityRequest(agent="human", tool=tool, task_id=note.task_id, args=call_args)


def _mentions_any(text: str, words: set[str]) -> bool:
    return any(word in text for word in words)


def _looks_like_goal_request(text: str) -> bool:
    prefixes = (
        "add goal ",
        "add task ",
        "create goal ",
        "create task ",
        "new goal ",
        "new task ",
        "make ",
        "build ",
    )
    return text.startswith(prefixes)


def _strip_goal_request(text: str) -> str:
    lowered = text.lower()
    for prefix in (
        "add goal ",
        "add task ",
        "create goal ",
        "create task ",
        "new goal ",
        "new task ",
        "make ",
        "build ",
    ):
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    return text.strip()


def _trim_reply(text: str, limit: int = 1800) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3].rstrip() + "..."


def _chat_model_candidates(policy: dict[str, Any]) -> list[str]:
    preferred: Iterable[str] = (
        str(policy.get("lmstudio_code_model") or ""),
        str(policy.get("lmstudio_small_model") or ""),
        str(policy.get("lmstudio_standard_model") or ""),
        "qwen3.6-35b-a3b",
        "google/gemma-4-12b",
    )
    candidates: list[str] = []
    for model in preferred:
        model = model.strip()
        if not model or model in {"local", "small_local"} or model in candidates:
            continue
        candidates.append(model)
    return candidates or ["local-model"]
