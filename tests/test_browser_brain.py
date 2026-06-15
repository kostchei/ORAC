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

# Selectors that signal "still generating"; absent = the turn has finished.
# Claude's must match the value: the attribute persists as "false" when done.
_STREAMING_SELS = {'[data-is-streaming="true"]', ".result-streaming"}
# Stop-generating buttons: absent = no longer generating (the stop->send toggle).
_STOP_SELS = {
    'button[aria-label="Stop response"]',
    'button[data-testid="stop-button"]',
}
# Both classes of "in progress" indicator read as absent in the default mock,
# i.e. the model has finished — completion is gated by these plus text stability.
_DONE_SELS = _STREAMING_SELS | _STOP_SELS


def _make_page(response_el_text: str = "the answer", provider: str = "claude") -> MagicMock:
    """Build a mock browser Page for the two-phase wait flow.

    query_selector_all behaviour:
    - in-progress selectors (streaming / stop button) → always [] (already done)
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
        if selector in _DONE_SELS:
            return []  # not streaming, no stop button -> generation finished
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


def test_prompt_does_not_disclose_agent_identity(monkeypatch) -> None:
    # Providers must not be told the request is from an automated agent: no
    # ORAC/agent/role/task header that could change rate-weighting or treatment.
    task = Task(title="secret-task-title", status=TaskStatus.IN_PROGRESS)
    captured: list[str] = []
    page = _make_page(response_el_text="OK", provider="claude")
    page.keyboard.type.side_effect = lambda text, delay=0: captured.append(text)
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))

    brain = BrowserFoundationBrain(provider="claude", stabilise_seconds=0, poll_interval=0)
    brain.think("builder", "doer", task, "summarise this")

    typed = captured[0]
    assert typed == "summarise this"
    for tell in ("ORAC", "agent=", "Role:", "secret-task-title"):
        assert tell not in typed, f"prompt leaked agent identity: {tell!r}"


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
# Tier 1 ruggedness: text injection, provider errors, completion signal
# ---------------------------------------------------------------------------


def test_type_uses_cdp_insert_text() -> None:
    # ProseMirror/Quill ignore DOM mutations they didn't initiate, so the prompt
    # must go in via CDP Input.insertText (IME-style native insertion), NOT a
    # textContent assignment that the editor silently drops.
    from orac.browser_primitive import BrowserPage

    session = MagicMock()
    page = BrowserPage(session)
    page.keyboard.type("hello world")
    session.call.assert_any_call("Input.insertText", {"text": "hello world"})


def test_rate_limit_banner_raises_provider_rate_limited(monkeypatch) -> None:
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    page = _make_page(
        response_el_text="You've reached your usage limit for Claude. Try again later.",
        provider="claude",
    )
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))
    brain = BrowserFoundationBrain(provider="claude", stabilise_seconds=0, poll_interval=0)
    with pytest.raises(bb.ProviderRateLimited):
        brain.think("builder", "doer", task, "hi")


def test_empty_response_raises_provider_unavailable(monkeypatch) -> None:
    # An empty turn after completion is a non-answer, not the model's reply.
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    page = _make_page(response_el_text="", provider="claude")
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))
    brain = BrowserFoundationBrain(provider="claude", stabilise_seconds=0, poll_interval=0)
    with pytest.raises(bb.ProviderUnavailable):
        brain.think("builder", "doer", task, "hi")


def test_completion_waits_for_stop_button_to_disappear(monkeypatch) -> None:
    # The turn is not "done" while the stop-generating button is still present,
    # even when the text has momentarily stopped changing.
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    page = MagicMock()
    resp = MagicMock()
    resp.inner_text.return_value = "answer"
    resp.query_selector.return_value = None
    state = {"resp": 0, "stop": 0}

    def qsa(selector: str) -> list:
        if selector in _STREAMING_SELS:
            return []
        if selector in _STOP_SELS:
            state["stop"] += 1
            # Stop button present for the first two polls, gone afterwards.
            return [MagicMock()] if state["stop"] <= 2 else []
        state["resp"] += 1
        if state["resp"] == 1:
            return []  # Phase 0 count before submission
        return [resp]

    page.query_selector_all.side_effect = qsa
    page.wait_for_selector.return_value = MagicMock()
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))

    brain = BrowserFoundationBrain(
        provider="claude", stabilise_seconds=0, poll_interval=0, timeout_seconds=5
    )
    result = brain.think("builder", "doer", task, "hi")
    assert result == "answer"
    assert state["stop"] >= 3, "completion did not wait for the stop button to vanish"


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


def test_logged_out_provider_raises_login_required(monkeypatch) -> None:
    # No chat input ever appears (login wall) -> an explicit, actionable signal,
    # not an opaque timeout the loop would retry forever.
    page = _make_page(provider="claude")
    page.wait_for_selector.side_effect = TimeoutError("no input box")
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))

    brain = BrowserFoundationBrain(provider="claude", stabilise_seconds=0, poll_interval=0)
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)

    with pytest.raises(bb.BrowserLoginRequired) as excinfo:
        brain.think("builder", "doer", task, "hi")

    assert excinfo.value.provider == "claude"
    assert "login" in str(excinfo.value).lower()
    # The login tab was brought to the front (popped) for the operator.
    page._session.call.assert_any_call("Page.bringToFront")


def test_provider_login_ready_reflects_input_presence(monkeypatch) -> None:
    page = MagicMock()
    page._session.evaluate.return_value = True
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))
    assert bb.provider_login_ready("openai", "http://localhost:9222", settle=0) is True

    page._session.evaluate.return_value = False
    assert bb.provider_login_ready("claude", "http://localhost:9222", settle=0) is False


def test_pop_provider_login_returns_true_when_already_in(monkeypatch) -> None:
    page = MagicMock()
    page._session.evaluate.return_value = True  # input present => logged in
    monkeypatch.setattr(bb, "_open_browser_page", _make_page_cm(page))
    assert bb.pop_provider_login("gemini", "http://localhost:9222", settle=0) is True


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
