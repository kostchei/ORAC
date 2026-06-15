from __future__ import annotations

import json
import mimetypes
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from orac.audio_io import audio_status, speak_text, transcribe_base64_audio
from orac.broker_store import BrokerStore
from orac.browser_brain import cdp_reachable, ensure_browser_foundation_ready
from orac.chat_processes import ChatProcessRuntime
from orac.chat_signon import (
    allow_sender,
    chat_status,
    connect_slack,
    disconnect_channel,
    disallow_sender,
    prepare_whatsapp,
)
from orac.dependency_installer import install_audio_stack
from orac.llm import build_brain, drain_foundation_spend_usd
from orac.model_policy import (
    ModelPolicyStore,
    ensure_lmstudio_model_loaded,
    lmstudio_loaded_models,
    verify_model_slots,
)
from orac.notify import review_queue_summary
from orac.resources import read_resource_snapshot
from orac.scrum import Scrum
from orac.storage import BoardStore
from orac.task_registry import TaskRegistry


def run_ui(root: Path | str = ".", host: str = "127.0.0.1", port: int = 8765) -> None:
    store = BoardStore(root)
    store.init()
    policy_store = ModelPolicyStore(store)
    runtime = UIRuntime(store)
    chat_runtime = ChatProcessRuntime(store)
    handler = _make_handler(store, runtime, chat_runtime)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"ORAC UI listening on http://{host}:{port}")
    policy = policy_store.load_policy()
    threading.Thread(
        target=_autoload_lmstudio_model,
        args=(policy, policy_store),
        daemon=True,
    ).start()
    if policy.get("browser_foundation_provider"):
        threading.Thread(
            target=_autostart_browser_foundation,
            args=(policy, store.root),
            daemon=True,
        ).start()
    server.serve_forever()


def _autoload_lmstudio_model(policy: dict[str, Any], policy_store: ModelPolicyStore) -> None:
    result = ensure_lmstudio_model_loaded(policy)
    print(f"LM Studio startup: {result.get('action')} {result.get('message', '')}")
    slots = verify_model_slots(policy_store)
    # Interactive surface: warn loudly but keep the UI up so the operator can fix
    # the slot from the settings panel. The daemon, being autonomous, hard-fails.
    prefix = "⚠ Model slots" if slots["missing"] else "Model slots"
    print(f"{prefix}: {slots['message']}")


def _autostart_browser_foundation(policy: dict[str, Any], root: Path) -> None:
    result = ensure_browser_foundation_ready(policy, orac_root=root)
    print(f"Browser foundation startup: {result.get('action')} {result.get('message', '')}")


class UIRuntime:
    def __init__(self, store: BoardStore) -> None:
        self.store = store
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_tick: dict[str, Any] | None = None
        self.last_error: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "last_tick": self.last_tick,
            "last_error": self.last_error,
        }

    def record_tick(self, result: Any, decision: Any) -> dict[str, Any]:
        self.last_tick = {
            "result": asdict(result),
            "model_decision": decision.to_dict(),
            "at": time.time(),
        }
        self.last_error = None
        return self.last_tick

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                policy_store = ModelPolicyStore(self.store)
                policy = policy_store.load_policy()
                decision = policy_store.decide()
                board = self.store.load()
                result = Scrum(
                    build_brain(decision.brain, model=decision.model),
                    root=self.store.root,
                    originate_when_idle=True,
                    route_models=True,
                ).run(board, cycles=int(policy["daemon_cycles"]))
                self.store.save(board)
                spent = drain_foundation_spend_usd()
                if spent > 0:
                    policy_store.record_foundation_spend(spent)
                self.record_tick(result, decision)
                interval = int(policy["daemon_interval_seconds"])
            except Exception as exc:
                self.last_error = str(exc)
                interval = 30
            self.stop_event.wait(interval)


def _make_handler(
    store: BoardStore, runtime: UIRuntime, chat_runtime: ChatProcessRuntime
) -> type[BaseHTTPRequestHandler]:
    class ORACHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                self._send_static("index.html", "text/html; charset=utf-8")
                return
            if self.path == "/static/styles.css":
                self._send_static("styles.css", "text/css; charset=utf-8")
                return
            if self.path == "/static/app.js":
                self._send_static("app.js", "text/javascript; charset=utf-8")
                return
            if self.path == "/api/state":
                self._send_json(_state_payload(store))
                return
            if self.path == "/api/resources":
                self._send_json(read_resource_snapshot().to_dict())
                return
            if self.path == "/api/model-policy":
                self._send_json(ModelPolicyStore(store).decide().to_dict())
                return
            if self.path == "/api/models/loaded":
                self._send_json({"models": lmstudio_loaded_models()})
                return
            if self.path == "/api/browser/status":
                policy = ModelPolicyStore(store).load_policy()
                cdp_url = str(policy.get("browser_cdp_url", "http://localhost:9222"))
                self._send_json({
                    "provider": str(policy.get("browser_foundation_provider", "")),
                    "cdp_url": cdp_url,
                    "connected": cdp_reachable(cdp_url),
                })
                return
            if self.path == "/api/audio/devices":
                self._send_json(audio_status().to_dict())
                return
            if self.path == "/api/settings":
                self._send_json(ModelPolicyStore(store).load_policy())
                return
            if self.path == "/api/chat":
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/loop/status":
                self._send_json(runtime.status())
                return
            if self.path == "/api/reviews":
                self._send_json(_reviews_payload(store))
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if self.path == "/api/requests":
                payload = self._read_json()
                board = store.load()
                task = TaskRegistry(board).add_base_request(
                    title=str(payload.get("title", "")),
                    description=str(payload.get("description", "")),
                    points=int(payload.get("points", 1)),
                )
                store.save(board)
                self._send_json(task.to_dict(), status=201)
                return
            if self.path == "/api/run":
                payload = self._read_json()
                cycles = int(payload.get("cycles", 1))
                policy_store = ModelPolicyStore(store)
                decision = policy_store.decide()
                board = store.load()
                result = Scrum(
                    build_brain(decision.brain, model=decision.model), root=store.root
                ).run(board, cycles=cycles)
                store.save(board)
                spent = drain_foundation_spend_usd()
                if spent > 0:
                    policy_store.record_foundation_spend(spent)
                runtime.record_tick(result, decision)
                self._send_json({"result": asdict(result), "model_decision": decision.to_dict()})
                return
            if self.path == "/api/audio/transcribe":
                payload = self._read_json()
                suffix = mimetypes.guess_extension(str(payload.get("mime", ""))) or ".webm"
                result = transcribe_base64_audio(str(payload.get("audio_base64", "")), suffix=suffix)
                self._send_json(result, status=200 if result.get("ok") else 400)
                return
            if self.path == "/api/audio/speak":
                payload = self._read_json()
                result = speak_text(str(payload.get("text", "")))
                self._send_json(result, status=200 if result.get("ok") else 400)
                return
            if self.path == "/api/audio/install":
                result = install_audio_stack()
                self._send_json(result.to_dict(), status=200 if result.ok else 500)
                return
            if self.path == "/api/settings":
                payload = self._read_json()
                policy_store = ModelPolicyStore(store)
                current = policy_store.load_policy()
                current.update(payload)
                policy_store.save_policy(current)
                self._send_json(policy_store.load_policy())
                return
            if self.path == "/api/chat/slack/connect":
                payload = self._read_json()
                connect_slack(
                    store,
                    payload.get("bot_token", ""),
                    payload.get("app_token", ""),
                )
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/whatsapp/connect":
                payload = self._read_json()
                prepare_whatsapp(
                    store,
                    bridge_url=payload.get("bridge_url", ""),
                    session=payload.get("session", ""),
                )
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/allow":
                payload = self._read_json()
                allow_sender(
                    store,
                    payload.get("channel", ""),
                    payload.get("sender", ""),
                )
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/disallow":
                payload = self._read_json()
                disallow_sender(
                    store,
                    payload.get("channel", ""),
                    payload.get("sender", ""),
                )
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/disconnect":
                payload = self._read_json()
                disconnect_channel(store, payload.get("channel", ""))
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/runtime/whatsapp/start":
                chat_runtime.start_whatsapp_bridge()
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/runtime/whatsapp/stop":
                chat_runtime.stop_whatsapp_bridge()
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/runtime/whatsapp/restart":
                chat_runtime.restart_whatsapp_bridge()
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/runtime/connectors/start":
                chat_runtime.start_connectors()
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/runtime/connectors/stop":
                chat_runtime.stop_connectors()
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/chat/runtime/connectors/restart":
                chat_runtime.restart_connectors()
                self._send_json(_chat_payload(store, chat_runtime))
                return
            if self.path == "/api/browser/launch":
                policy = ModelPolicyStore(store).load_policy()
                result = ensure_browser_foundation_ready(policy, orac_root=store.root)
                self._send_json(result, status=200 if result.get("ok") else 500)
                return
            if self.path == "/api/loop/start":
                runtime.start()
                self._send_json(runtime.status())
                return
            if self.path == "/api/loop/stop":
                runtime.stop()
                self._send_json(runtime.status())
                return
            self.send_error(404)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_static(self, name: str, content_type: str) -> None:
            data = files("orac").joinpath(f"ui/{name}").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ORACHandler


def _state_payload(store: BoardStore) -> dict[str, Any]:
    board = store.load()
    registry = TaskRegistry(board)
    decision = ModelPolicyStore(store).decide()
    return {
        "stats": asdict(registry.stats()),
        "tasks": [task.to_dict() for task in board.tasks],
        "interactions": registry.interactions(),
        "resources": decision.resources.to_dict(),
        "model_policy": decision.to_dict(),
        "loaded_models": lmstudio_loaded_models(),
        "settings": ModelPolicyStore(store).load_policy(),
        "audio": audio_status().to_dict(),
        # Notify transport (P6): the review-queue pressure, so the UI can badge
        # the unacked count without a separate poll.
        "review_queue": review_queue_summary(BrokerStore(store.root).init()).to_dict(),
    }


def _chat_payload(store: BoardStore, chat_runtime: ChatProcessRuntime) -> dict[str, Any]:
    payload = chat_status(store)
    payload["runtime"] = chat_runtime.status()
    return payload


def _reviews_payload(store: BoardStore) -> dict[str, Any]:
    """The full review queue for the UI cockpit (read-only mirror of the CLI)."""
    bstore = BrokerStore(store.root).init()
    return {
        "summary": review_queue_summary(bstore).to_dict(),
        "pending_approvals": [asdict(p) for p in bstore.list_pending()],
        "notifications": [asdict(n) for n in bstore.list_notifications(unacked_only=True)],
        "standing_grants": [asdict(g) for g in bstore.list_standing_grants()],
    }
