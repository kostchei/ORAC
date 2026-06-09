from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AudioDevice:
    name: str
    kind: str
    source: str


@dataclass(frozen=True)
class AudioStatus:
    microphones: list[AudioDevice]
    speakers: list[AudioDevice]
    default_microphone: str | None
    default_speaker: str | None
    whisper_available: bool
    tts_available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "microphones": [asdict(device) for device in self.microphones],
            "speakers": [asdict(device) for device in self.speakers],
            "default_microphone": self.default_microphone,
            "default_speaker": self.default_speaker,
            "whisper_available": self.whisper_available,
            "tts_available": self.tts_available,
        }


def audio_status() -> AudioStatus:
    microphones, speakers = detect_audio_devices()
    default_microphone, default_speaker = detect_default_audio_devices()
    return AudioStatus(
        microphones=microphones,
        speakers=speakers,
        default_microphone=default_microphone,
        default_speaker=default_speaker,
        whisper_available=_module_available("whisper"),
        tts_available=_module_available("pyttsx3") or os.name == "nt",
    )


def detect_audio_devices() -> tuple[list[AudioDevice], list[AudioDevice]]:
    sounddevice_result = _sounddevice_devices()
    if sounddevice_result:
        return sounddevice_result
    if os.name == "nt":
        return _windows_audio_devices()
    return [], []


def detect_default_audio_devices() -> tuple[str | None, str | None]:
    sounddevice_result = _sounddevice_default_devices()
    if sounddevice_result != (None, None):
        return sounddevice_result
    if os.name == "nt":
        return _windows_default_audio_devices()
    return None, None


def transcribe_base64_audio(audio_base64: str, suffix: str = ".webm") -> dict[str, Any]:
    if not _module_available("whisper"):
        return {
            "ok": False,
            "text": "",
            "error": "Python package `openai-whisper` is not installed.",
        }
    model_name = os.environ.get("ORAC_WHISPER_MODEL", "base")
    audio_bytes = base64.b64decode(audio_base64)
    suffix = _safe_audio_suffix(suffix)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        audio_path = temp_path / f"recording{suffix}"
        audio_path.write_bytes(audio_bytes)
        command = [
            sys.executable,
            "-m",
            "whisper",
            str(audio_path),
            "--model",
            model_name,
            "--output_format",
            "json",
            "--output_dir",
            str(temp_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=900,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "text": "", "error": str(exc)}
        if completed.returncode != 0:
            return {
                "ok": False,
                "text": "",
                "error": (completed.stdout + "\n" + completed.stderr).strip()[-4000:],
            }
        output_path = temp_path / "recording.json"
        if not output_path.exists():
            return {"ok": False, "text": "", "error": "Whisper did not produce JSON output."}
        data = json.loads(output_path.read_text(encoding="utf-8"))
        return {"ok": True, "text": str(data.get("text", "")).strip(), "model": model_name}


def speak_text(text: str) -> dict[str, Any]:
    clean = text.strip()
    if not clean:
        return {"ok": False, "error": "No text supplied."}
    if _module_available("pyttsx3"):
        try:
            import pyttsx3  # type: ignore

            engine = pyttsx3.init()
            engine.say(clean)
            engine.runAndWait()
            return {"ok": True, "engine": "pyttsx3"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    if os.name == "nt":
        return _windows_sapi_speak(clean)
    return {"ok": False, "error": "No local text-to-speech engine is available."}


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _sounddevice_devices() -> tuple[list[AudioDevice], list[AudioDevice]] | None:
    try:
        import sounddevice as sd  # type: ignore

        devices = sd.query_devices()
    except Exception:
        return None
    microphones: list[AudioDevice] = []
    speakers: list[AudioDevice] = []
    for device in devices:
        name = str(device.get("name", "Unknown"))
        if int(device.get("max_input_channels", 0)) > 0:
            microphones.append(AudioDevice(name=name, kind="microphone", source="sounddevice"))
        if int(device.get("max_output_channels", 0)) > 0:
            speakers.append(AudioDevice(name=name, kind="speaker", source="sounddevice"))
    return microphones, speakers


def _sounddevice_default_devices() -> tuple[str | None, str | None]:
    try:
        import sounddevice as sd  # type: ignore

        devices = sd.query_devices()
        default_input, default_output = sd.default.device
    except Exception:
        return None, None

    def name_for(index: int | None) -> str | None:
        if index is None or index < 0:
            return None
        try:
            return str(devices[index].get("name", "Unknown"))
        except Exception:
            return None

    return name_for(default_input), name_for(default_output)


def _windows_audio_devices() -> tuple[list[AudioDevice], list[AudioDevice]]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-PnpDevice -Class AudioEndpoint -Status OK | Select-Object -ExpandProperty FriendlyName",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return [], []
    microphones: list[AudioDevice] = []
    speakers: list[AudioDevice] = []
    for raw in completed.stdout.splitlines():
        name = raw.strip()
        if not name:
            continue
        lower = name.lower()
        if any(token in lower for token in ["microphone", "mic", "input"]):
            microphones.append(AudioDevice(name=name, kind="microphone", source="windows"))
        elif any(token in lower for token in ["speaker", "headphone", "output", "audio"]):
            speakers.append(AudioDevice(name=name, kind="speaker", source="windows"))
    return microphones, speakers


def _windows_default_audio_devices() -> tuple[str | None, str | None]:
    # PowerShell exposes the friendly endpoint names, but not a built-in default marker
    # without extra modules. Prefer the most human-useful active endpoint names.
    microphones, speakers = _windows_audio_devices()
    return _first_device_name(microphones), _first_device_name(speakers)


def _first_device_name(devices: list[AudioDevice]) -> str | None:
    if not devices:
        return None
    preferred = [
        device
        for device in devices
        if "mapper" not in device.name.lower() and "primary" not in device.name.lower()
    ]
    return (preferred or devices)[0].name


def _windows_sapi_speak(text: str) -> dict[str, Any]:
    if shutil.which("powershell") is None:
        return {"ok": False, "error": "PowerShell was not found."}
    escaped = text.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Speak('{escaped}')"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    if completed.returncode != 0:
        return {"ok": False, "error": completed.stderr.strip()}
    return {"ok": True, "engine": "windows_sapi"}


def _safe_audio_suffix(suffix: str) -> str:
    suffix = suffix.lower().strip()
    allowed = {".webm", ".wav", ".mp3", ".m4a", ".ogg", ".flac"}
    return suffix if suffix in allowed else ".webm"
