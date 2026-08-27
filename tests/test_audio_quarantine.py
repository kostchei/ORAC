from __future__ import annotations

from orac import audio_io
from orac.storage import BoardStore
from orac.ui_server import _state_payload


def test_audio_status_contains_device_probe_failures(monkeypatch) -> None:
    def broken_devices():
        raise RuntimeError("PortAudio unavailable")

    monkeypatch.setattr(audio_io, "detect_audio_devices", broken_devices)
    monkeypatch.setattr(audio_io, "detect_default_audio_devices", broken_devices)

    status = audio_io.audio_status()

    assert status.microphones == [] and status.speakers == []
    assert status.error is not None
    assert "PortAudio unavailable" in status.error


def test_transcription_rejects_malformed_base64_without_launching_whisper(monkeypatch) -> None:
    monkeypatch.setattr(audio_io, "_module_available", lambda name: True)

    result = audio_io.transcribe_base64_audio("%%%not-base64%%%")

    assert result["ok"] is False
    assert "Invalid base64 audio" in result["error"]


def test_core_state_does_not_enumerate_audio_hardware(tmp_path, monkeypatch) -> None:
    store = BoardStore(tmp_path)
    store.init()

    def forbidden_probe():
        raise AssertionError("core state must not enumerate audio devices")

    monkeypatch.setattr(audio_io, "detect_audio_devices", forbidden_probe)
    monkeypatch.setattr(audio_io, "detect_default_audio_devices", forbidden_probe)

    payload = _state_payload(store)

    assert payload["audio"]["microphones"] == []
    assert payload["audio"]["speakers"] == []
    assert payload["audio"]["error"] is None
