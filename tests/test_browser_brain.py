from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, call

import pytest

import orac.browser_brain as bb
from orac.browser_brain import BrowserFoundationBrain, _extract_new_text
from orac.llm import build_brain, FallbackBrain
from orac.model_policy import DEFAULT_POLICY, ModelPolicyStore, can_escalate, session_brain_for
from orac.models import Board, Task, TaskStatus
from orac.storage import BoardStore


# ---------------------------------------------------------------------------
# Playwright mock helpers
# ---------------------------------------------------------------------------


def _make_page(
    before_text: str = "page header",
    after_text: str = "page header\n\nUser: hello\n\nAI: the answer",
    response_el_text: str = "the answer",
) -> MagicMock:
    """Build a mock playwright Page.

    inner_text("body") alternates: first call = before_text, subsequent = after_text.
    query_selector_all returns a single element whose inner_text() = response_el_text.
    """
    page = MagicMock()
    call_counts: dict[str, int] = {"inner_text": 0}

    def inner_text(selector: str) -> str:
        call_counts["inner_text"] += 1
        if call_counts["inner_text"] == 1:
            return before_text
        return after_text

    page.inner_text.side_effect = inner_text
    response_el = MagicMock()
    response_el.inner_text.return_value = response_el_text
    page.query_selector_all.return_value = [response_el]
    page.wait_for_selector.return_value = MagicMock()
    return page


def _make_playwright_cm(page: MagicMock) -> MagicMock:
    """Return a context-manager mock that yields a playwright-like object."""
    browser = MagicMock()
    context = MagicMock()
    context.new_page.return_value = page
    browser.contexts = [context]

    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value = browser

    @contextmanager
    def cm():
        yield pw

    return cm


# ---------------------------------------------------------------------------
# Unit tests for BrowserFoundationBrain
# ---------------------------------------------------------------------------


def test_think_sends_prompt_and_returns_response(monkeypatch) -> None:
    task = Task(title="test-task", status=TaskStatus.IN_PROGRESS)
    page = _make_page(response_el_text="AI result")
    monkeypatch.setattr(bb, "_sync_playwright", _make_playwright_cm(page))

    brain = BrowserFoundationBrain(
        provider="claude", stabilise_seconds=0, poll_interval=0
    )
    result = brain.think("builder", "doer", task, "do the thing")

    assert result == "AI result"
    page.goto.assert_called_once_with(
        "https://claude.ai/new", wait_until="networkidle", timeout=30_000
    )
    page.keyboard.press.assert_any_call("Enter")


def test_think_json_appends_schema_instructions(monkeypatch) -> None:
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    captured: list[str] = []

    page = _make_page(response_el_text=json.dumps({"done": True}))

    def fake_type(text: str, delay: int = 0) -> None:
        captured.append(text)

    page.keyboard.type.side_effect = fake_type
    monkeypatch.setattr(bb, "_sync_playwright", _make_playwright_cm(page))

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
        monkeypatch.setattr(bb, "_sync_playwright", _make_playwright_cm(page))
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
    monkeypatch.setattr(bb, "_sync_playwright", _make_playwright_cm(page))

    brain = BrowserFoundationBrain(
        provider="myspace", stabilise_seconds=0, poll_interval=0
    )
    with pytest.raises(ValueError, match="Unknown browser foundation provider"):
        brain.think("builder", "doer", task, "prompt")


def test_cdp_connection_failure_raises_runtime_error(monkeypatch) -> None:
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)

    pw = MagicMock()
    pw.chromium.connect_over_cdp.side_effect = OSError("connection refused")

    @contextmanager
    def cm():
        yield pw

    monkeypatch.setattr(bb, "_sync_playwright", cm)

    brain = BrowserFoundationBrain(provider="claude", stabilise_seconds=0, poll_interval=0)
    with pytest.raises(RuntimeError, match="Cannot connect to Chrome"):
        brain.think("builder", "doer", task, "prompt")


def test_response_fallback_to_text_diff_when_no_selector_match(monkeypatch) -> None:
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    page = _make_page(
        before_text="before",
        after_text="before\nUser: hi\nAI: fallback response",
    )
    page.query_selector_all.return_value = []  # no matching elements

    monkeypatch.setattr(bb, "_sync_playwright", _make_playwright_cm(page))
    brain = BrowserFoundationBrain(
        provider="claude", stabilise_seconds=0, poll_interval=0
    )
    result = brain.think("builder", "doer", task, "hi")
    assert "fallback response" in result


def test_timeout_raises(monkeypatch) -> None:
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)
    page = _make_page(
        before_text="before",
        after_text="before still typing...",  # text never stabilises
    )
    # Always return changing text so stability is never reached
    call_n: list[int] = [0]

    def evolving_text(sel: str) -> str:
        call_n[0] += 1
        return f"body text version {call_n[0]}"

    page.inner_text.side_effect = evolving_text

    monkeypatch.setattr(bb, "_sync_playwright", _make_playwright_cm(page))
    brain = BrowserFoundationBrain(
        provider="claude",
        timeout_seconds=1,
        stabilise_seconds=5,  # longer than timeout
        poll_interval=0,
    )
    with pytest.raises(TimeoutError, match="timed out"):
        brain.think("builder", "doer", task, "prompt")


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


def test_decide_returns_browser_brain_when_no_api_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ORAC_FOUNDATION_API_KEY", raising=False)
    monkeypatch.setenv("ORAC_BROWSER_FOUNDATION", "claude")
    store = BoardStore(tmp_path)
    store.init()
    decision = ModelPolicyStore(store).decide()
    assert decision.brain == "browser"
    assert decision.model == "claude"
    assert "browser foundation" in decision.reason


def test_decide_prefers_api_key_over_browser(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORAC_FOUNDATION_API_KEY", "sk-test")
    monkeypatch.setenv("ORAC_BROWSER_FOUNDATION", "claude")
    store = BoardStore(tmp_path)
    store.init()
    decision = ModelPolicyStore(store).decide()
    assert decision.brain == "foundation"
