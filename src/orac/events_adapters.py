from __future__ import annotations

from pathlib import Path
from typing import Any

from orac.adapters import Adapter
from orac.events_store import EventsStore
from orac.models import CapabilityRequest
from orac.tooling import ToolResult

EVENT_TOOLS = frozenset(
    {
        "event.create",
        "event.add_participant",
        "event.ask_human",
        "event.wait_for_response",
        "event.advance_round",
        "event.broadcast_update",
        "event.close",
    }
)


class EventsAdapterSet:
    """Group 5 Human Events and session workflow adapters bound to a repository root."""

    def __init__(self, repo_root: Path | str, store: EventsStore | None = None) -> None:
        self.root = Path(repo_root)
        self.store = store or EventsStore(self.root)

    def adapters(self) -> dict[str, Adapter]:
        return {
            "event.create": self.event_create,
            "event.add_participant": self.event_add_participant,
            "event.ask_human": self.event_ask_human,
            "event.wait_for_response": self.event_wait_for_response,
            "event.advance_round": self.event_advance_round,
            "event.broadcast_update": self.event_broadcast_update,
            "event.close": self.event_close,
        }

    def event_create(self, req: CapabilityRequest) -> ToolResult:
        title = str(req.args.get("title") or "")
        if not title:
            raise ValueError("event.create requires a non-empty 'title'.")
        total_rounds = int(req.args.get("total_rounds", 1))
        metadata = req.args.get("metadata")
        event = self.store.create_event(
            title=title,
            total_rounds=total_rounds,
            task_id=req.task_id,
            metadata=metadata if isinstance(metadata, dict) else {},
        )
        return ToolResult(
            "event.create",
            f"Created event session [{event.id}] '{event.title}' with {event.total_rounds} round(s).",
            {"event": event.to_dict()},
        )

    def event_add_participant(self, req: CapabilityRequest) -> ToolResult:
        event_id = str(req.args.get("event_id") or "")
        name = str(req.args.get("name") or "")
        if not event_id or not name:
            raise ValueError("event.add_participant requires 'event_id' and 'name'.")
        role = str(req.args.get("role") or "participant")
        channel = str(req.args.get("channel") or "local")
        participant = self.store.add_participant(
            event_id=event_id,
            name=name,
            role=role,
            channel=channel,
        )
        return ToolResult(
            "event.add_participant",
            f"Added participant [{participant.id}] '{participant.name}' (role: {participant.role}) to event [{event_id}].",
            {"participant": participant.to_dict()},
        )

    def event_ask_human(self, req: CapabilityRequest) -> ToolResult:
        event_id = str(req.args.get("event_id") or "")
        question = str(req.args.get("question") or "")
        if not event_id or not question:
            raise ValueError("event.ask_human requires 'event_id' and 'question'.")
        options = req.args.get("options")
        opts_list = [str(o) for o in options] if isinstance(options, list) else []
        participant_id = req.args.get("participant_id")
        timeout_seconds = int(req.args.get("timeout_seconds", 0))
        human_req = self.store.ask_human(
            event_id=event_id,
            question=question,
            options=opts_list,
            participant_id=str(participant_id) if participant_id else None,
            timeout_seconds=timeout_seconds,
        )
        return ToolResult(
            "event.ask_human",
            f"Created human input request [{human_req.id}] for event [{event_id}]: '{question}'",
            {"request": human_req.to_dict()},
        )

    def event_wait_for_response(self, req: CapabilityRequest) -> ToolResult:
        request_id = str(req.args.get("request_id") or "")
        if not request_id:
            raise ValueError("event.wait_for_response requires 'request_id'.")
        human_req = self.store.get_human_request(request_id)
        if human_req.status == "answered":
            msg = f"Request [{request_id}] answered: {human_req.response}"
        else:
            msg = f"Request [{request_id}] is {human_req.status} (waiting for participant/human)."
        return ToolResult("event.wait_for_response", msg, {"request": human_req.to_dict()})

    def event_advance_round(self, req: CapabilityRequest) -> ToolResult:
        event_id = str(req.args.get("event_id") or "")
        if not event_id:
            raise ValueError("event.advance_round requires 'event_id'.")
        summary = str(req.args.get("summary") or "")
        title = str(req.args.get("title") or "")
        prompt = str(req.args.get("prompt") or "")
        event = self.store.advance_round(event_id, summary=summary, title=title, prompt=prompt)
        status_msg = f"Event [{event.id}] advanced to round {event.current_round}/{event.total_rounds} (status: {event.status})."
        return ToolResult("event.advance_round", status_msg, {"event": event.to_dict()})

    def event_broadcast_update(self, req: CapabilityRequest) -> ToolResult:
        event_id = str(req.args.get("event_id") or "")
        message = str(req.args.get("message") or "")
        if not event_id or not message:
            raise ValueError("event.broadcast_update requires 'event_id' and 'message'.")
        channel = str(req.args.get("channel") or "all")
        broadcast = self.store.broadcast_message(event_id=event_id, message=message, channel=channel)
        return ToolResult(
            "event.broadcast_update",
            f"Broadcast message to channel '{channel}' for event [{event_id}].",
            {"broadcast": broadcast.to_dict()},
        )

    def event_close(self, req: CapabilityRequest) -> ToolResult:
        event_id = str(req.args.get("event_id") or "")
        if not event_id:
            raise ValueError("event.close requires 'event_id'.")
        summary = str(req.args.get("summary") or "")
        event = self.store.close_event(event_id, summary=summary)
        return ToolResult(
            "event.close",
            f"Closed event session [{event.id}] (status: {event.status}).",
            {"event": event.to_dict()},
        )


def events_adapters_for(
    repo_root: Path | str, store: EventsStore | None = None
) -> dict[str, Adapter]:
    return EventsAdapterSet(repo_root, store=store).adapters()
