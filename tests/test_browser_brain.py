from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import orac.browser_brain as bb
from orac.browser_brain import (
    BrowserFoundationBrain,
    _extract_new_text,
    cdp_reachable,
    ensure_browser_foundation_ready,
)
from orac.llm import build_brain, FallbackBrain
from orac.model_policy import DEFAULT_POLICY, ModelPolicyStore, can_escalate, session_brain_for
from orac.models import Board, Task, TaskStatus
from orac.storage import BoardStore


# ---------------------------------------------------------------------------
# Browser primitive mock helpers
# ---------------------------------------------------------------------------

# Known streaming selectors (absent = streaming finished).
_STREAMING_SELS = {"[data-is-streaming]", ".result-streaming"}


def _make_page(response_el_text: str = "the answer", provider: str = "claude") -> MagicMock:
    """Build a mock browser Page for the two-phase wait flow.

    query_selector_all behaviour:
    - streaming selectors → always [] (streaming already done)
    - response selectors  → [] on the first call (before submission),
                            [response_el] on all subsequent calls
    """
    page = MagicMock()
    response_el = MagicMock()
    response_el.inner_text.return_value = response_el_text
    # Gemini message-content sub-element lookup.
    response_el.query_selector.return_value = None

    qsa_count: list[int] = [0]

    def query_selector_all(selector: str) -> list:
        if selector in _STREAMING_SELS:
            return []  # streaming finished immediately
        # Response selector: first call (Phase 0 count) returns empty.
        qsa_count[0] += 1
        if qsa_count[0] == 1:
            return []
        return [response_el]

    page.query_selector_all.side_effect = query_selector_all
    page.wait_for_selector.return_value = MagicMock()
    return page


def _make_page_cm(page: MagicMock) -> MagicMock:
    """Return a context-manager mock that yields ORAC's local CDP page."""

    @contextmanager
    def cm(*_args: object, **_kwargs: object):
        yield page

    return cm


# ---------------------------------------------------------------------------
# Unit tests for BrowserFoundationBrain
# ---------------------------------------------------------------------------


def test_think_sends_prompt_and_returns_response(monkeypatch) -> None:
    task = Task(title="test-task", status=TaskStatus.IN_PROGRESS)
    page = _make_page(response_el_text="AI result", provider="claude")
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))

    brain = BrowserFoundationBrain(
        provider="claude", stabilise_seconds=0, poll_interval=0
    )
    result = brain.think("builder", "doer", task, "do the thing")

    assert result == "AI result"
    page.goto.assert_called_once_with(
        "https://claude.ai/new", wait_until="networkidle", timeout=30_000
    )
    # Send button was clicked (wait_for_selector was called for the send button).
    assert page.wait_for_selector.call_count >= 2  # input + send button


def test_think_json_appends_schema_instructions(monkeypatch) -> None:
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    captured: list[str] = []

    page = _make_page(response_el_text=json.dumps({"done": True}))

    def fake_type(text: str, delay: int = 0) -> None:
        captured.append(text)

    page.keyboard.type.side_effect = fake_type
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))

    brain = BrowserFoundationBrain(
        provider="claude", stabilise_seconds=0, poll_interval=0
    )
    schema = {"type": "object", "properties": {"done": {"type": "boolean"}}}
    brain.think_json("builder", "doer", task, "do stuff", schema)

    assert captured, "keyboard.type was not called"
    typed = captured[0]
    assert "JSON schema" in typed
    assert '"done"' in typed
    assert "no markdown" in typed.lower()


def test_provider_url_dispatched_correctly(monkeypatch) -> None:
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    for provider, expected_url in [
        ("claude", "https://claude.ai/new"),
        ("gemini", "https://gemini.google.com/app"),
        ("openai", "https://chatgpt.com"),
    ]:
        page = _make_page()
        monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))
        brain = BrowserFoundationBrain(
            provider=provider, stabilise_seconds=0, poll_interval=0
        )
        brain.think("builder", "doer", task, "prompt")
        page.goto.assert_called_once_with(
            expected_url, wait_until="networkidle", timeout=30_000
        )


def test_unknown_provider_raises(monkeypatch) -> None:
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    page = _make_page()
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))

    brain = BrowserFoundationBrain(
        provider="myspace", stabilise_seconds=0, poll_interval=0
    )
    with pytest.raises(ValueError, match="Unknown browser foundation provider"):
        brain.think("builder", "doer", task, "prompt")


def test_cdp_connection_failure_raises_runtime_error(monkeypatch) -> None:
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)

    def fail_open(*_args: object, **_kwargs: object) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(bb, "_open_browser_page", fail_open)

    brain = BrowserFoundationBrain(provider="claude", stabilise_seconds=0, poll_interval=0)
    with pytest.raises(RuntimeError, match="Cannot connect to Chrome"):
        brain.think("builder", "doer", task, "prompt")


def test_send_button_fallback_to_enter_when_not_found(monkeypatch) -> None:
    """If the send button doesn't appear, the brain falls back to pressing Enter."""
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    page = _make_page(response_el_text="ok", provider="claude")

    # Make the send-button wait_for_selector fail for the send button call only.
    call_n: list[int] = [0]
    real_wfs = page.wait_for_selector.side_effect

    def wfs(selector: str, **kw: object) -> object:
        call_n[0] += 1
        if "Send message" in selector or "send-button" in selector:
            raise Exception("element not found")
        return MagicMock()

    page.wait_for_selector.side_effect = wfs
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))
    brain = BrowserFoundationBrain(provider="claude", stabilise_seconds=0, poll_interval=0)
    brain.think("builder", "doer", task, "hi")
    page.keyboard.press.assert_any_call("Enter")


def test_phase2_timeout_when_response_never_appears(monkeypatch) -> None:
    """Timeout fires in Phase 2 when no response element ever appears."""
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    page = MagicMock()
    # query_selector_all always returns [] — response never appears.
    page.query_selector_all.return_value = []
    page.wait_for_selector.return_value = MagicMock()

    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))
    brain = BrowserFoundationBrain(
        provider="claude",
        timeout_seconds=1,
        stabilise_seconds=0,
        poll_interval=0,
    )
    with pytest.raises(TimeoutError, match="timed out"):
        brain.think("builder", "doer", task, "prompt")


def test_gemini_uses_text_stability_not_streaming_selector(monkeypatch) -> None:
    """Gemini has no streaming selector — falls back to text-stability polling."""
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    page = _make_page(response_el_text="gemini answer", provider="gemini")
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))
    brain = BrowserFoundationBrain(
        provider="gemini", stabilise_seconds=0, poll_interval=0
    )
    result = brain.think("builder", "doer", task, "prompt")
    assert result == "gemini answer"


# ---------------------------------------------------------------------------
# _extract_new_text helper
# ---------------------------------------------------------------------------


def test_extract_new_text_removes_before_prefix() -> None:
    before = "header nav sidebar"
    after = "header nav sidebar\nUser: question\nAI: actual answer"
    result = _extract_new_text(before, after)
    assert "actual answer" in result


def test_extract_new_text_short_before() -> None:
    result = _extract_new_text("hi", "hi\nnew stuff")
    assert "new stuff" in result


def test_extract_new_text_no_new_content() -> None:
    result = _extract_new_text("same", "same")
    assert result == "same"


def test_launch_chrome_passes_absolute_user_data_dir(monkeypatch) -> None:
    # Regression: a detached Chrome resolves a relative --user-data-dir against
    # its own cwd, not ORAC's, so a relative profile lands on the wrong (often
    # default) profile, hands off to a running Chrome, and never binds the debug
    # port. launch_chrome must always emit an absolute path.
    from pathlib import Path

    captured: dict[str, list[str]] = {}

    def fake_popen(args, **_kwargs):
        captured["args"] = args
        return MagicMock()

    monkeypatch.setattr(bb.subprocess, "Popen", fake_popen)
    bb.launch_chrome("chrome", 9222, Path(".orac/chrome-profile"))

    udd = next(a for a in captured["args"] if a.startswith("--user-data-dir="))
    path = udd.split("=", 1)[1]
    assert Path(path).is_absolute(), f"expected absolute user-data-dir, got {path!r}"
    assert path.endswith("chrome-profile")
    # The profile is pinned so Chrome never shows the "Who's using Chrome?" picker
    # (a picker means it reached the user's real profiles).
    assert "--profile-directory=Default" in captured["args"]


# ---------------------------------------------------------------------------
# llm.build_brain integration
# ---------------------------------------------------------------------------


def test_build_brain_browser_returns_fallback_with_browser_primary() -> None:
    brain = build_brain("browser", model="claude")
    assert isinstance(brain, FallbackBrain)
    assert isinstance(brain.primary, BrowserFoundationBrain)
    assert brain.primary.provider == "claude"


def test_build_brain_browser_default_provider() -> None:
    brain = build_brain("browser")
    assert isinstance(brain, FallbackBrain)
    assert isinstance(brain.primary, BrowserFoundationBrain)


def test_build_brain_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown brain"):
        build_brain("telepathy")


# ---------------------------------------------------------------------------
# model_policy integration
# ---------------------------------------------------------------------------


def test_can_escalate_true_with_browser_foundation(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ORAC_FOUNDATION_API_KEY", raising=False)
    monkeypatch.setenv("ORAC_BROWSER_FOUNDATION", "claude")
    store = BoardStore(tmp_path)
    store.init()
    assert can_escalate(ModelPolicyStore(store)) is True


def test_can_escalate_false_with_no_key_and_no_browser(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ORAC_FOUNDATION_API_KEY", raising=False)
    monkeypatch.delenv("ORAC_BROWSER_FOUNDATION", raising=False)
    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)
    policy_store.save_policy({"browser_foundation_provider": ""})
    assert can_escalate(policy_store) is False


def test_can_escalate_true_via_policy_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ORAC_FOUNDATION_API_KEY", raising=False)
    monkeypatch.delenv("ORAC_BROWSER_FOUNDATION", raising=False)
    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)
    policy_store.save_policy({"browser_foundation_provider": "gemini"})
    assert can_escalate(policy_store) is True


def test_session_brain_for_escalated_uses_browser(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORAC_BROWSER_FOUNDATION", "openai")
    monkeypatch.delenv("ORAC_FOUNDATION_API_KEY", raising=False)
    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)
    task = Task(title="t", work_kind="code", metadata={"escalated": True})
    brain = session_brain_for(policy_store, task)
    assert isinstance(brain, FallbackBrain)
    assert isinstance(brain.primary, BrowserFoundationBrain)
    assert brain.primary.provider == "openai"


def _idle_resources() -> object:
    """Return a ResourceSnapshot that looks like a non-busy system."""
    from orac.resources import ResourceSnapshot
    return ResourceSnapshot(
        cpu_percent=10.0, memory_percent=30.0,
        memory_total_gb=16.0, memory_available_gb=12.0,
        gpu_percent=0.0, vram_percent=0.0,
        disk_free_gb=100.0, busy=False,
        recommended_tier="local", reason="resources within policy",
    )


def test_decide_returns_browser_brain_when_no_api_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ORAC_FOUNDATION_API_KEY", raising=False)
    monkeypatch.setenv("ORAC_BROWSER_FOUNDATION", "claude")
    monkeypatch.setattr("orac.model_policy.read_resource_snapshot", lambda _pct: _idle_resources())
    store = BoardStore(tmp_path)
    store.init()
    decision = ModelPolicyStore(store).decide()
    assert decision.brain == "browser"
    assert decision.model == "claude"
    assert "browser foundation" in decision.reason


def test_decide_prefers_api_key_over_browser(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORAC_FOUNDATION_API_KEY", "sk-test")
    monkeypatch.setenv("ORAC_BROWSER_FOUNDATION", "claude")
    monkeypatch.setattr("orac.model_policy.read_resource_snapshot", lambda _pct: _idle_resources())
    store = BoardStore(tmp_path)
    store.init()
    decision = ModelPolicyStore(store).decide()
    assert decision.brain == "foundation"


# ---------------------------------------------------------------------------
# ensure_browser_foundation_ready startup logic
# ---------------------------------------------------------------------------


def test_ensure_ready_already_running(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bb, "cdp_reachable", lambda _url: True)
    policy = {"browser_cdp_url": "http://localhost:9222", "browser_foundation_provider": "claude"}
    result = ensure_browser_foundation_ready(policy, orac_root=tmp_path)
    assert result["ok"] is True
    assert result["action"] == "already_running"


def test_ensure_ready_chrome_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bb, "cdp_reachable", lambda _url: False)
    monkeypatch.setattr(bb, "find_chrome", lambda: None)
    policy = {"browser_cdp_url": "http://localhost:9222", "browser_foundation_provider": "claude"}
    result = ensure_browser_foundation_ready(policy, orac_root=tmp_path)
    assert result["ok"] is False
    assert result["action"] == "chrome_not_found"


def test_ensure_ready_launches_chrome(tmp_path, monkeypatch) -> None:
    # First call (before launch): unreachable. Subsequent calls: reachable.
    call_count: list[int] = [0]

    def _reachable(_url: str) -> bool:
        call_count[0] += 1
        return call_count[0] > 1

    launched: list[tuple] = []

    def _launch(chrome_path: str, port: int, profile_dir: object) -> None:
        launched.append((chrome_path, port))

    monkeypatch.setattr(bb, "cdp_reachable", _reachable)
    monkeypatch.setattr(bb, "find_chrome", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(bb, "launch_chrome", _launch)
    monkeypatch.setattr(bb.time, "sleep", lambda _n: None)

    policy = {"browser_cdp_url": "http://localhost:9222", "browser_foundation_provider": "claude"}
    result = ensure_browser_foundation_ready(policy, orac_root=tmp_path)

    assert result["ok"] is True
    assert result["action"] == "launched"
    assert launched[0] == ("/usr/bin/chromium", 9222)
    assert (tmp_path / ".orac" / "chrome-profile").is_dir()


def test_ensure_ready_launch_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bb, "cdp_reachable", lambda _url: False)
    monkeypatch.setattr(bb, "find_chrome", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(bb, "launch_chrome", lambda *_a, **_kw: None)
    monkeypatch.setattr(bb.time, "sleep", lambda _n: None)

    policy = {"browser_cdp_url": "http://localhost:9222", "browser_foundation_provider": "claude"}
    result = ensure_browser_foundation_ready(policy, orac_root=tmp_path)
    assert result["ok"] is False
    assert result["action"] == "launch_timeout"


def test_ensure_ready_does_not_require_playwright(tmp_path, monkeypatch) -> None:
    # The browser foundation now uses ORAC's local CDP primitive, so startup should
    # not import or install playwright before checking CDP.
    import builtins
    real_import = builtins.__import__

    def mock_import(name: str, *args, **kwargs):  # type: ignore[override]
        if name == "playwright":
            raise AssertionError("playwright should not be imported")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    monkeypatch.setattr(bb, "cdp_reachable", lambda _url: True)

    policy = {"browser_cdp_url": "http://localhost:9222", "browser_foundation_provider": "claude"}
    result = ensure_browser_foundation_ready(policy, orac_root=tmp_path)
    assert result["ok"] is True
    assert result["action"] == "already_running"
