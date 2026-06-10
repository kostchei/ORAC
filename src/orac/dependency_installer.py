from __future__ import annotations

import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    command: list[str]
    output: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def install_playwright() -> InstallResult:
    """Install the playwright package (no browser binary needed — uses CDP attach)."""
    command = [sys.executable, "-m", "pip", "install", "playwright"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return InstallResult(False, command, str(exc))
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return InstallResult(completed.returncode == 0, command, output[-6000:])


def install_audio_stack() -> InstallResult:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "openai-whisper",
        "sounddevice",
        "pyttsx3",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return InstallResult(False, command, str(exc))
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return InstallResult(completed.returncode == 0, command, output[-6000:])
