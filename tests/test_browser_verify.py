from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import orac.browser_adapters as ba
from orac.broker import ToolBroker
from orac.browser_adapters import verify_local_app
from orac.models import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    Task,
    TaskStatus,
)
from orac.policy import ApprovalMode, approval_mode_for
from orac.work import WORK_KINDS, verify_goal_done, _verify_local_app


# --- fake CDP page -----------------------------------------------------------


class _FakeSession:
    def __init__(self, body_len: int) -> None:
        self._body_len = body_len

    def evaluate(self, _expr: str) -> int:
        return self._body_len


class _FakePage:
    def __init__(self, body_len: int, goto_error: Exception | None = None) -> None:
        self._session = _FakeSession(body_len)
        self._goto_error = goto_error
        self.visited: list[str] = []

    def goto(self, url: str) -> None:
        if self._goto_error is not None:
            raise self._goto_error
        self.visited.append(url)


class _FakeConn:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    def __enter__(self) -> _FakePage:
        return self._page

    def __exit__(self, *_exc: object) -> None:
        return None


def _patch_browser(monkeypatch, page: _FakePage, *, reachable: bool = True) -> None:
    monkeypatch.setattr(ba, "cdp_reachable", lambda _url: reachable)
    monkeypatch.setattr(ba, "open_cdp_page", lambda _url: _FakeConn(page))


def _req(**args) -> CapabilityRequest:
    return CapabilityRequest(
        agent="Builder", tool="browser.verify_local_app", task_id="t1", args=args
    )


# --- the adapter -------------------------------------------------------------


def test_loaded_page_with_content_verifies(monkeypatch) -> None:
    page = _FakePage(body_len=128)
    _patch_browser(monkeypatch, page)

    result = verify_local_app(_req(app_url="http://127.0.0.1:8765"))

    assert result.data["verified"] is True
    assert result.data["body_chars"] == 128
    assert page.visited == ["http://127.0.0.1:8765"]


def test_empty_body_does_not_verify(monkeypatch) -> None:
    _patch_browser(monkeypatch, _FakePage(body_len=0))

    result = verify_local_app(_req(app_url="http://127.0.0.1:8765"))

    assert result.data["verified"] is False
    assert "empty" in result.data["summary"].lower()


def test_unreachable_cdp_does_not_verify(monkeypatch) -> None:
    _patch_browser(monkeypatch, _FakePage(body_len=10), reachable=False)

    result = verify_local_app(_req(app_url="http://127.0.0.1:8765"))

    assert result.data["verified"] is False
    assert "unreachable" in result.data["summary"].lower()


def test_navigation_failure_does_not_verify(monkeypatch) -> None:
    page = _FakePage(body_len=10, goto_error=TimeoutError("navigation timed out"))
    _patch_browser(monkeypatch, page)

    result = verify_local_app(_req(app_url="http://127.0.0.1:8765"))

    assert result.data["verified"] is False
    assert "timed out" in result.data["summary"].lower()


def test_missing_app_url_raises() -> None:
    with pytest.raises(ValueError, match="app_url"):
        verify_local_app(_req())


# --- the verifier wired into verify_goal_done --------------------------------


@dataclass
class _FakeBroker:
    """A broker stub returning canned data per tool, for verifier chaining."""

    responses: dict[str, dict]
    seen: list[str] = field(default_factory=list)

    def request(self, req: CapabilityRequest, _task: Task) -> CapabilityResult:
        self.seen.append(req.tool)
        return CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool=req.tool,
            message="",
            data=self.responses[req.tool],
        )


def _child() -> Task:
    return Task(title="[code] ui change", status=TaskStatus.IN_PROGRESS)


def test_local_app_verifier_skips_when_no_app_url() -> None:
    # A backend-only goal carries no app_url: the UI check is a no-op pass so the
    # browser tool is never even requested.
    broker = _FakeBroker(responses={})
    ok, detail = _verify_local_app(WORK_KINDS["code"], _child(), broker, {})

    assert ok is True
    assert broker.seen == []
    assert "not a UI change" in detail


def test_code_goal_runs_both_verifiers_in_order() -> None:
    broker = _FakeBroker(
        responses={
            "repo.run_tests": {"passed": True, "summary": "5 passed"},
            "browser.verify_local_app": {"verified": True, "summary": "loaded"},
        }
    )
    context = {"repo_root": "/repo", "app_url": "http://127.0.0.1:8765"}

    ok, detail = verify_goal_done(WORK_KINDS["code"], _child(), broker, context)

    assert ok is True
    assert broker.seen == ["repo.run_tests", "browser.verify_local_app"]
    assert "run_tests" in detail and "verify_local_app" in detail


def test_ui_goal_blocks_when_app_does_not_render() -> None:
    # Tests are green, but the app came up blank: the UI goal must not reach DONE.
    broker = _FakeBroker(
        responses={
            "repo.run_tests": {"passed": True, "summary": "5 passed"},
            "browser.verify_local_app": {"verified": False, "summary": "page loaded but body was empty"},
        }
    )
    context = {"repo_root": "/repo", "app_url": "http://127.0.0.1:8765"}

    ok, detail = verify_goal_done(WORK_KINDS["code"], _child(), broker, context)

    assert ok is False
    assert "empty" in detail.lower()


# --- governance wiring -------------------------------------------------------


def test_tool_is_known_granted_and_classified() -> None:
    broker = ToolBroker.from_manifests()
    assert "browser.verify_local_app" in broker.known_tools
    assert "browser.verify_local_app" in broker.grants["Builder"]
    assert "browser.verify_local_app" in broker.adapters
    # Read-only/local: dispatches immediately, never parks for approval.
    assert approval_mode_for("browser.verify_local_app") is ApprovalMode.AUTO
