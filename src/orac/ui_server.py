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
from orac.dependency_installer import install_audio_stack
from orac.llm import build_brain
from orac.model_policy import ModelPolicyStore, ensure_lmstudio_model_loaded, lmstudio_loaded_models
from orac.resources import read_resource_snapshot
from orac.scrum import Scrum
from orac.storage import BoardStore
from orac.task_registry import TaskRegistry


def run_ui(root: Path | str = ".", host: str = "127.0.0.1", port: int = 8765) -> None:
    store = BoardStore(root)
    store.init()
    policy_store = ModelPolicyStore(store)
    runtime = UIRuntime(store)
    handler = _make_handler(store, runtime)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"ORAC UI listening on http://{host}:{port}")
    threading.Thread(
        target=_autoload_lmstudio_model,
        args=(policy_store.load_policy(),),
        daemon=True,
    ).start()
    server.serve_forever()


def _autoload_lmstudio_model(policy: dict[str, Any]) -> None:
    result = ensure_lmstudio_model_loaded(policy)
    print(f"LM Studio startup: {result.get('action')} {result.get('message', '')}")


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
                result = Scrum(build_brain(decision.brain, model=decision.model)).run(
                    board, cycles=int(policy["daemon_cycles"])
                )
                self.store.save(board)
                if decision.brain == "foundation" and result.touched_tasks:
                    policy_store.record_foundation_spend(policy_store.estimated_cycle_spend())
                self.last_tick = {
                    "result": asdict(result),
                    "model_decision": decision.to_dict(),
                    "at": time.time(),
                }
                self.last_error = None
                interval = int(policy["daemon_interval_seconds"])
            except Exception as exc:
                self.last_error = str(exc)
                interval = 30
            self.stop_event.wait(interval)


def _make_handler(store: BoardStore, runtime: UIRuntime) -> type[BaseHTTPRequestHandler]:
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
            if self.path == "/api/audio/devices":
                self._send_json(audio_status().to_dict())
                return
            if self.path == "/api/settings":
                self._send_json(ModelPolicyStore(store).load_policy())
                return
            if self.path == "/api/loop/status":
                self._send_json(runtime.status())
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
                result = Scrum(build_brain(decision.brain, model=decision.model)).run(
                    board, cycles=cycles
                )
                store.save(board)
                if decision.brain == "foundation" and result.touched_tasks:
                    policy_store.record_foundation_spend(policy_store.estimated_cycle_spend())
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
    }
