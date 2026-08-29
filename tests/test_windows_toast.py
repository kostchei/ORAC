from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus
from orac.broker_store import BrokerStore
from orac.notify import (
    _build_powershell_toast_command,
    notify_review_queue,
    send_windows_toast,
)


def test_build_powershell_toast_command() -> None:
    cmd = _build_powershell_toast_command("Test 'Title'", "Test 'Message'\nLine 2", "App's ID")
    assert "Test ''Title''" in cmd
    assert "Test ''Message'' Line 2" in cmd
    assert "App''s ID" in cmd
    assert "ToastGeneric" in cmd


def test_send_windows_toast_sync_success() -> None:
    with patch("os.name", "nt"), patch("subprocess.run") as mock_run:
        res = send_windows_toast("Title", "Message", async_spawn=False)
        assert res is True
        assert mock_run.called
        args = mock_run.call_args[0][0]
        assert args[0] == "powershell"


def test_send_windows_toast_non_windows() -> None:
    with patch("os.name", "posix"):
        res = send_windows_toast("Title", "Message", async_spawn=False)
        assert res is False


def test_notify_review_queue_triggers_toast(tmp_path) -> None:
    store = BrokerStore(tmp_path)
    store.init()
    req = CapabilityRequest(agent="builder", tool="repo.write_file", task_id="t1", args={})
    res = CapabilityResult(status=CapabilityStatus.ALLOWED, tool="repo.write_file", message="ok")
    store.record_notification(req, res)

    with patch("orac.notify.send_windows_toast") as mock_toast:
        summary = notify_review_queue(store, send_toast=True)
        assert summary.unacked_notifications == 1
        assert mock_toast.called
        title, msg = mock_toast.call_args[0][:2]
        assert title == "ORAC Review Queue"
        assert "1 action(s) awaiting review" in msg
