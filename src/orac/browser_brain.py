from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
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

_PROVIDER_URLS: dict[str, str] = {
    "claude": "https://claude.ai/new",
    "gemini": "https://gemini.google.com/app",
    "openai": "https://chatgpt.com",
}

# CSS selectors for the text-entry area.  Multiple candidates separated by
# commas are tried in order by playwright's wait_for_selector.
_INPUT_SELECTORS: dict[str, str] = {
    "claude": 'div[contenteditable="true"]',
    "gemini": 'rich-textarea div[contenteditable], div[contenteditable][aria-label]',
    "openai": '#prompt-textarea, div[contenteditable][placeholder]',
}

# CSS selectors for the last AI-turn response element.
_RESPONSE_SELECTORS: dict[str, str] = {
    "claude": '.font-claude-message, [data-testid="chat-message-content"]',
    "gemini": 'model-response .markdown, .response-content',
    "openai": '[data-message-author-role="assistant"] .markdown, [data-message-author-role="assistant"]',
}


# The playwright entry-point is imported lazily so the rest of the module
# loads even when playwright is not installed.  Tests patch this function.
def _sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed — run: pip install playwright"
        ) from exc
    return sync_playwright()


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
    2. ``pip install playwright``  (no browser binary download needed).
    3. The browser must already be logged in to the chosen provider.

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
        ctx = _sync_playwright()
        with ctx as pw:
            try:
                browser = pw.chromium.connect_over_cdp(self.cdp_url)
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot connect to Chrome at {self.cdp_url}. "
                    "Start Chrome with: chrome --remote-debugging-port=9222 --no-first-run"
                ) from exc

            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                return self._chat(page, text)
            finally:
                page.close()

    def _chat(self, page: Any, text: str) -> str:
        url = _PROVIDER_URLS.get(self.provider)
        if url is None:
            raise ValueError(
                f"Unknown browser foundation provider: {self.provider!r}. "
                f"Expected one of: {', '.join(_PROVIDER_URLS)}"
            )

        page.goto(url, wait_until="networkidle", timeout=30_000)

        # Snapshot of the page before submission — used to extract new text.
        before_text = page.inner_text("body")

        # Focus the input, clear any draft, type the prompt.
        input_sel = _INPUT_SELECTORS[self.provider]
        inp = page.wait_for_selector(input_sel, timeout=15_000)
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type(text, delay=5)
        page.keyboard.press("Enter")

        # Poll until the response stabilises (no body-text change for
        # stabilise_seconds) or the hard timeout is hit.
        deadline = time.monotonic() + self.timeout_seconds
        last_body = ""
        last_change_at = time.monotonic()
        timed_out = True

        while time.monotonic() < deadline:
            if self.poll_interval > 0:
                time.sleep(self.poll_interval)
            body = page.inner_text("body")
            if body != last_body:
                last_change_at = time.monotonic()
                last_body = body
            elif time.monotonic() - last_change_at >= self.stabilise_seconds:
                timed_out = False
                break

        if timed_out:
            raise TimeoutError(
                f"Browser foundation timed out after {self.timeout_seconds}s "
                f"waiting for {self.provider!r} response to stabilise."
            )

        # Try provider-specific response element first.
        response_sel = _RESPONSE_SELECTORS[self.provider]
        try:
            els = page.query_selector_all(response_sel)
            if els:
                return els[-1].inner_text().strip()
        except Exception:  # noqa: BLE001
            pass

        # Fallback: extract all text that appeared after the snapshot.
        return _extract_new_text(before_text, page.inner_text("body"))


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

    The process is detached so it outlives the ORAC daemon.
    """
    args = [
        chrome_path,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
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
    """Ensure playwright is installed and Chrome is reachable via CDP.

    Called once at daemon/UI startup when ``browser_foundation_provider`` is
    set.  Launches Chrome into a persistent ORAC-owned profile
    (``{orac_root}/.orac/chrome-profile``) so that provider logins survive
    restarts.  The user needs to log in once in the opened browser window;
    subsequent starts reconnect automatically.

    Returns a status dict with ``ok``, ``action``, and ``message`` keys.
    """
    # 1. Ensure playwright is importable; install if missing.
    try:
        import playwright  # noqa: F401
    except ImportError:
        from orac.dependency_installer import install_playwright  # noqa: PLC0415

        result = install_playwright()
        if not result.ok:
            return {
                "ok": False,
                "action": "playwright_install_failed",
                "message": result.output[-500:],
            }

    # 2. CDP already running — nothing to do.
    cdp_url = str(policy.get("browser_cdp_url", "http://localhost:9222"))
    if cdp_reachable(cdp_url):
        return {
            "ok": True,
            "action": "already_running",
            "message": "Chrome CDP already reachable.",
        }

    # 3. Find the Chrome / Edge executable.
    chrome = find_chrome()
    if not chrome:
        return {
            "ok": False,
            "action": "chrome_not_found",
            "message": (
                "No Chrome or Edge executable found.  Install Chrome, or add it to PATH."
            ),
        }

    # 4. Build the profile directory (persists logins between restarts).
    root = Path(orac_root) if orac_root else Path(".")
    profile_dir = root / ".orac" / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        cdp_port = _port_from_cdp_url(cdp_url)
        launch_chrome(chrome, cdp_port, profile_dir)
    except OSError as exc:
        return {"ok": False, "action": "launch_failed", "message": str(exc)}

    # 5. Wait up to 15 s for the CDP endpoint to come up.
    for _ in range(15):
        time.sleep(1.0)
        if cdp_reachable(cdp_url):
            provider = str(policy.get("browser_foundation_provider", ""))
            return {
                "ok": True,
                "action": "launched",
                "message": (
                    f"Chrome launched (provider={provider}, port={cdp_port}).  "
                    f"Log in at the opened browser window if this is the first run.  "
                    f"Profile: {profile_dir}"
                ),
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
