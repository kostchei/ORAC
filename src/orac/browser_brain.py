from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from importlib.util import find_spec
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from orac.models import Task

# Windows chrome/edge candidates (checked in order when PATH lookup fails).
_CHROME_WIN_CANDIDATES: list[str] = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"
    ),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Edge\Application\msedge.exe"
    ),
]

# Provider configuration -------------------------------------------------------
# Selectors verified against live DOM 2026-06-10.

_PROVIDER_URLS: dict[str, str] = {
    "claude": "https://claude.ai/new",
    "gemini": "https://gemini.google.com/app",
    "openai": "https://chatgpt.com",
}

# The editable text-entry area for each provider.
_INPUT_SELECTORS: dict[str, str] = {
    # ProseMirror div with role=textbox; class "tiptap ProseMirror" confirmed live.
    "claude": 'div.ProseMirror[role="textbox"]',
    # Quill editor inside Google's <rich-textarea> custom element.
    "gemini": 'rich-textarea .ql-editor[contenteditable="true"]',
    # ProseMirror div with stable id; also has class "ProseMirror".
    "openai": '#prompt-textarea',
}

# The submit button for each provider.
# Claude and Gemini both use aria-label="Send message".
# ChatGPT uses data-testid="send-button".
_SEND_BUTTON_SELECTORS: dict[str, str] = {
    "claude": 'button[aria-label="Send message"]',
    "gemini": 'button[aria-label="Send message"]',
    "openai": '[data-testid="send-button"]',
}

# The container element for the last AI response turn.
_RESPONSE_SELECTORS: dict[str, str] = {
    # Class confirmed: "font-claude-response …"
    "claude": '[class*="font-claude-response"]',
    # Angular custom element; contains <message-content> for the actual prose.
    "gemini": 'model-response',
    # data-message-author-role="assistant" on each turn div.
    "openai": '[data-message-author-role="assistant"]',
}

# Selector that is present WHILE streaming and absent when the response is
# complete.  None = fall back to text-stability polling.
_STREAMING_SELECTORS: dict[str, str | None] = {
    # data-is-streaming attribute on the response element while Claude is typing.
    "claude": '[data-is-streaming]',
    # Gemini has no reliable streaming attribute — use text stability.
    "gemini": None,
    # ChatGPT adds class "result-streaming" to the prose div while generating.
    "openai": '.result-streaming',
}


def _open_browser_page(cdp_url: str, *, timeout: float = 30.0) -> Any:
    """Return ORAC's dependency-free CDP page primitive.

    Kept as a small seam for tests and for future browser primitives; it avoids
    importing Playwright or any third-party websocket package on the hot path.
    """
    from orac.browser_primitive import open_cdp_page  # noqa: PLC0415

    return open_cdp_page(cdp_url, timeout=timeout)


# Brain implementation ---------------------------------------------------------


@dataclass
class BrowserFoundationBrain:
    """Foundation-class brain that drives a logged-in browser tab.

    Instead of calling an API this brain connects to an existing Chrome/Edge
    process via the Chrome DevTools Protocol, opens the provider's chat UI in
    a new tab, types the prompt, waits for the reply to stabilise, and reads
    the response text back.

    Prerequisites
    -------------
    1. Chrome (or Edge) started with --remote-debugging-port=9222.
       Shortcut: ``chrome --remote-debugging-port=9222 --no-first-run``
    2. The browser must already be logged in to the chosen provider.

    ORAC talks to Chrome directly through a tiny local CDP primitive
    (``orac.browser_primitive``); no Playwright or browser automation package is
    required.

    The CDP URL and provider can also be set via env vars ORAC_CDP_URL and
    ORAC_BROWSER_FOUNDATION.
    """

    provider: str = field(
        default_factory=lambda: os.environ.get("ORAC_BROWSER_FOUNDATION", "claude")
    )
    cdp_url: str = field(
        default_factory=lambda: os.environ.get("ORAC_CDP_URL", "http://localhost:9222")
    )
    timeout_seconds: int = 120
    stabilise_seconds: float = 3.0
    poll_interval: float = 1.0  # set to 0 in tests

    # Brain interface ----------------------------------------------------------

    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        return self._send_and_receive(self._build_prompt(agent_name, role, task, prompt))

    def think_json(
        self,
        agent_name: str,
        role: str,
        task: Task,
        prompt: str,
        schema: dict,
    ) -> str:
        """Include schema instructions in the prompt.

        Browser UIs don't support server-side schema enforcement, so the schema
        is added as a plain-text constraint.  The caller's strict parser
        (``parse_decision`` in AgentSession) is still the gate.
        """
        schema_note = (
            "\n\n---\n"
            "IMPORTANT: Your reply must be a single valid JSON object and nothing else. "
            "No prose, no markdown fences, no explanation before or after the JSON.\n"
            f"Required JSON schema:\n{json.dumps(schema, indent=2)}"
        )
        return self._send_and_receive(
            self._build_prompt(agent_name, role, task, prompt) + schema_note
        )

    # Internal helpers ---------------------------------------------------------

    def _build_prompt(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        return (
            f"[ORAC · agent={agent_name} · task={task.title!r}]\n"
            f"Role: {role}\n\n"
            f"{prompt}"
        )

    def _send_and_receive(self, text: str) -> str:
        try:
            ctx = _open_browser_page(self.cdp_url, timeout=30.0)
            with ctx as page:
                try:
                    return self._chat(page, text)
                finally:
                    page.close()
        except Exception as exc:
            if isinstance(exc, (TimeoutError, ValueError)):
                raise
            raise RuntimeError(
                f"Cannot connect to Chrome at {self.cdp_url}. "
                "Start Chrome with: chrome --remote-debugging-port=9222 --no-first-run"
            ) from exc

    def _chat(self, page: Any, text: str) -> str:
        url = _PROVIDER_URLS.get(self.provider)
        if url is None:
            raise ValueError(
                f"Unknown browser foundation provider: {self.provider!r}. "
                f"Expected one of: {', '.join(_PROVIDER_URLS)}"
            )

        page.goto(url, wait_until="networkidle", timeout=30_000)

        # --- Phase 0: count existing response elements before submission ------
        response_sel = _RESPONSE_SELECTORS[self.provider]
        before_count = len(page.query_selector_all(response_sel))

        # --- Phase 1: type and submit -----------------------------------------
        input_sel = _INPUT_SELECTORS[self.provider]
        inp = page.wait_for_selector(input_sel, timeout=15_000)
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type(text, delay=5)

        # Click the send button if visible; fall back to Enter.
        send_sel = _SEND_BUTTON_SELECTORS[self.provider]
        try:
            send_btn = page.wait_for_selector(send_sel, timeout=5_000)
            send_btn.click()
        except Exception:  # noqa: BLE001
            page.keyboard.press("Enter")

        # --- Phase 2: wait for a new response element to appear ---------------
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self.poll_interval > 0:
                time.sleep(self.poll_interval)
            if len(page.query_selector_all(response_sel)) > before_count:
                break
        else:
            raise TimeoutError(
                f"Browser foundation timed out after {self.timeout_seconds}s "
                f"waiting for {self.provider!r} to start responding."
            )

        # --- Phase 3: wait for streaming to finish ----------------------------
        streaming_sel = _STREAMING_SELECTORS[self.provider]
        if streaming_sel:
            # Stream-indicator approach: wait until the element disappears.
            while time.monotonic() < deadline:
                if self.poll_interval > 0:
                    time.sleep(self.poll_interval)
                if not page.query_selector_all(streaming_sel):
                    break
            else:
                raise TimeoutError(
                    f"Browser foundation timed out after {self.timeout_seconds}s "
                    f"waiting for {self.provider!r} streaming to complete."
                )
        else:
            # Text-stability fallback (Gemini): no change for stabilise_seconds.
            last_text = ""
            last_change_at = time.monotonic()
            while time.monotonic() < deadline:
                if self.poll_interval > 0:
                    time.sleep(self.poll_interval)
                els = page.query_selector_all(response_sel)
                current = els[-1].inner_text() if els else ""
                if current != last_text:
                    last_change_at = time.monotonic()
                    last_text = current
                elif time.monotonic() - last_change_at >= self.stabilise_seconds:
                    break
            else:
                raise TimeoutError(
                    f"Browser foundation timed out after {self.timeout_seconds}s "
                    f"waiting for {self.provider!r} response to stabilise."
                )

        # --- Phase 4: extract the response text -------------------------------
        els = page.query_selector_all(response_sel)
        if not els:
            raise RuntimeError(
                f"Browser foundation: response element disappeared after streaming "
                f"({self.provider!r}, selector={response_sel!r})."
            )
        last_el = els[-1]

        # Gemini: the actual prose lives inside <message-content>.
        if self.provider == "gemini":
            inner = last_el.query_selector("message-content")
            if inner:
                return inner.inner_text().strip()

        return last_el.inner_text().strip()


def _extract_new_text(before: str, after: str) -> str:
    """Return the portion of ``after`` that was not present in ``before``.

    Uses a tail-anchor heuristic: find the last ~100 chars of ``before``
    in ``after``, then return everything that follows.  Works regardless of
    provider UI structure.
    """
    if len(after) <= len(before):
        return after.strip()
    anchor = before[-100:].strip() if len(before) > 100 else before.strip()
    idx = after.rfind(anchor)
    if idx >= 0:
        tail = after[idx + len(anchor):]
        return tail.strip()
    return after[len(before):].strip()


# ---------------------------------------------------------------------------
# Application startup helpers
# ---------------------------------------------------------------------------


def cdp_reachable(cdp_url: str = "http://localhost:9222") -> bool:
    """Return True if a Chrome DevTools endpoint is listening at *cdp_url*."""
    try:
        req = Request(f"{cdp_url.rstrip('/')}/json/version")
        with urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except (OSError, URLError):
        return False


def find_chrome() -> str | None:
    """Return the path to a Chrome or Edge executable, or None if not found."""
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome", "msedge"):
        path = shutil.which(name)
        if path:
            return path
    if sys.platform == "win32":
        for candidate in _CHROME_WIN_CANDIDATES:
            expanded = os.path.expandvars(candidate)
            if os.path.exists(expanded):
                return expanded
    return None


def launch_chrome(chrome_path: str, cdp_port: int, profile_dir: Path) -> None:
    """Launch Chrome/Edge with the CDP debug port and a dedicated ORAC profile.

    The process is detached so it outlives the ORAC daemon. The profile path is
    resolved to absolute first: a detached Chrome resolves a relative
    ``--user-data-dir`` against its own working directory, not ORAC's, so a
    relative path silently lands on the wrong (often the default) profile —
    Chrome then hands off to any already-running instance and exits without ever
    binding the debug port. Absolute is the only correct form here.
    """
    args = [
        chrome_path,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={Path(profile_dir).resolve()}",
        # Pin the single ORAC profile so Chrome opens straight into it and never
        # shows the "Who's using Chrome?" picker — belt-and-suspenders with the
        # absolute user-data-dir above (a picker means Chrome reached the user's
        # real profiles, which must never happen here).
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def ensure_browser_foundation_ready(
    policy: dict[str, Any],
    orac_root: Path | str | None = None,
) -> dict[str, Any]:
    """Ensure Chrome is reachable via CDP for the local browser primitive.

    Called once at daemon/UI startup when ``browser_foundation_provider`` is
    set.  Launches Chrome into a persistent ORAC-owned profile
    (``{orac_root}/.orac/chrome-profile``) so that provider logins survive
    restarts.  The user needs to log in once in the opened browser window;
    subsequent starts reconnect automatically.

    Returns a status dict with ``ok``, ``action``, and ``message`` keys.
    """
    def ensure_playwright() -> str | None:
        """Best-effort Playwright bootstrap; return a warning when unavailable."""
        # Browser startup status should still report the actionable Chrome/CDP
        # state when package installation is blocked by the environment. The
        # actual BrowserFoundationBrain import path still raises if Playwright is
        # unavailable when a browser-backed request is executed.
        # 1. Ensure playwright is discoverable; install if missing.
        if find_spec("playwright") is None:
            from orac.dependency_installer import install_playwright  # noqa: PLC0415

            result = install_playwright()
            if not result.ok:
                return f"Playwright install failed: {result.output[-500:]}"
        return None

    cdp_url = str(policy.get("browser_cdp_url", "http://localhost:9222"))

    # 1. CDP already running — nothing to do beyond a best-effort dependency
    # check for the eventual BrowserFoundationBrain connection.
    if cdp_reachable(cdp_url):
        warning = ensure_playwright()
        message = "Chrome CDP already reachable."
        if warning:
            message = f"{message} {warning}"
        return {
            "ok": True,
            "action": "already_running",
            "message": message,
        }

    # 2. Find the Chrome / Edge executable before attempting network-backed
    # dependency installation; without Chrome there is nothing useful to launch.
    chrome = find_chrome()
    if not chrome:
        return {
            "ok": False,
            "action": "chrome_not_found",
            "message": (
                "No Chrome or Edge executable found.  Install Chrome, or add it to PATH."
            ),
        }

    warning = ensure_playwright()
    # 3. Build the profile directory (persists logins between restarts).
    root = Path(orac_root) if orac_root else Path(".")
    profile_dir = root / ".orac" / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        cdp_port = _port_from_cdp_url(cdp_url)
        launch_chrome(chrome, cdp_port, profile_dir)
    except OSError as exc:
        return {"ok": False, "action": "launch_failed", "message": str(exc)}

    # 4. Wait up to 15 s for the CDP endpoint to come up.
    for _ in range(15):
        time.sleep(1.0)
        if cdp_reachable(cdp_url):
            provider = str(policy.get("browser_foundation_provider", ""))
            message = (
                f"Chrome launched (provider={provider}, port={cdp_port}).  "
                f"Log in at the opened browser window if this is the first run.  "
                f"Profile: {profile_dir}"
            )
            if warning:
                message = f"{message} {warning}"
            return {
                "ok": True,
                "action": "launched",
                "message": message,
                "profile_dir": str(profile_dir),
            }

    return {
        "ok": False,
        "action": "launch_timeout",
        "message": (
            f"Chrome was launched but CDP did not come up on {cdp_url} within 15 s.  "
            "Check that no other Chrome instance owns that port."
        ),
    }


def _port_from_cdp_url(url: str) -> int:
    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        return int(urlparse(url).port or 9222)
    except Exception:  # noqa: BLE001
        return 9222
