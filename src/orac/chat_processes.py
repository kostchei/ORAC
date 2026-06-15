from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from orac.chat_config import load_chat_config
from orac.chat_signon import whatsapp_bridge_status
from orac.storage import BoardStore


class ChatProcessRuntime:
    """UI-owned local chat processes.

    The cockpit should be able to start/stop the pieces it depends on. This
    runtime owns only processes started by this UI server instance; existing
    external bridge state is still visible through the chat bridge probe.
    """

    def __init__(self, store: BoardStore) -> None:
        self.store = store
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._logs: dict[str, Any] = {}

    def status(self) -> dict[str, Any]:
        cfg = load_chat_config(self.store)
        bridge_url = str(cfg["channels"]["whatsapp"].get("bridge_url", "http://localhost:8788"))
        bridge = whatsapp_bridge_status(bridge_url)
        whatsapp_status = self._process_status("whatsapp_bridge")
        whatsapp_status["external_running"] = bool(
            bridge.get("reachable") and not whatsapp_status["running"]
        )
        whatsapp_status["bridge"] = bridge
        return {
            "whatsapp_bridge": whatsapp_status,
            "connectors": self._process_status("connectors"),
        }

    def start_whatsapp_bridge(self) -> dict[str, Any]:
        cfg = load_chat_config(self.store)
        bridge_url = str(cfg["channels"]["whatsapp"].get("bridge_url", "http://localhost:8788"))
        bridge = whatsapp_bridge_status(bridge_url)
        if bridge.get("reachable"):
            return {
                **self._process_status("whatsapp_bridge"),
                "external_running": True,
                "bridge": bridge,
            }
        return self._start(
            "whatsapp_bridge",
            [sys.executable, "-m", "orac.cli", "--root", str(self.store.root), "chat", "whatsapp-bridge"],
        )

    def stop_whatsapp_bridge(self) -> dict[str, Any]:
        return self._stop("whatsapp_bridge")

    def restart_whatsapp_bridge(self) -> dict[str, Any]:
        self._stop("whatsapp_bridge")
        return self._start(
            "whatsapp_bridge",
            [sys.executable, "-m", "orac.cli", "--root", str(self.store.root), "chat", "whatsapp-bridge"],
        )

    def start_connectors(self) -> dict[str, Any]:
        return self._start(
            "connectors",
            [sys.executable, "-m", "orac.cli", "--root", str(self.store.root), "chat", "run"],
        )

    def stop_connectors(self) -> dict[str, Any]:
        return self._stop("connectors")

    def restart_connectors(self) -> dict[str, Any]:
        self._stop("connectors")
        return self._start(
            "connectors",
            [sys.executable, "-m", "orac.cli", "--root", str(self.store.root), "chat", "run"],
        )

    @property
    def log_dir(self) -> Path:
        return self.store.state_dir / "comms_logs"

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1])
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
        env["ORAC_ROOT"] = str(self.store.root.resolve())
        return env

    def _start(self, name: str, args: list[str]) -> dict[str, Any]:
        proc = self._procs.get(name)
        if proc and proc.poll() is None:
            return self._process_status(name)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{name}.log"
        log = open(log_path, "wb")
        self._logs[name] = log
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._procs[name] = subprocess.Popen(
            args,
            cwd=self.store.root,
            env=self._env(),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        return self._process_status(name)

    def _stop(self, name: str) -> dict[str, Any]:
        proc = self._procs.get(name)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log = self._logs.pop(name, None)
        if log:
            log.close()
        return self._process_status(name)

    def _process_status(self, name: str) -> dict[str, Any]:
        proc = self._procs.get(name)
        log_path = self.log_dir / f"{name}.log"
        return {
            "running": bool(proc and proc.poll() is None),
            "pid": proc.pid if proc and proc.poll() is None else None,
            "returncode": proc.poll() if proc else None,
            "log_path": str(log_path),
            "log_dir": str(self.log_dir),
            "log_tail": _tail(log_path),
        }


def _tail(path: Path, limit: int = 1600) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()[-limit:]
    return data.decode("utf-8", errors="replace").strip()
