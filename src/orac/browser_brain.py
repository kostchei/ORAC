from __future__ import annotations

import json
import os
import re
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

from orac.browser_primitive import BrowserPrimitiveError
from orac.browser_selectors import ProviderSelectors, load_provider_selectors
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

class BrowserLoginRequired(RuntimeError):
    """A browser foundation provider has no logged-in session.

    Raised instead of a generic timeout when the provider's chat input never
    appears because the page is sitting on a login wall. Carries the provider and
    the URL now open in the ORAC browser, so the operator can sign in there and
    retry — a human-action signal, not a retryable model failure.
    """

    def __init__(self, provider: str, url: str) -> None:
        self.provider = provider
        self.url = url
        super().__init__(
            f"{provider} is not logged in. Its login page is open in the ORAC "
            f"browser ({url}); sign in there, then retry."
        )


class BrowserProviderError(RuntimeError):
    """A browser foundation provider returned a non-answer (rate cap, outage).

    Distinct from ``BrowserLoginRequired`` (sign-in needed) and from a model's own
    content refusal (which is a *valid* answer). Carries the provider and the
    matched detail so the loop can treat the result as a provider-side failure
    instead of trusting it as the model's reply — the same review-after philosophy
    that makes a missing-login an explicit signal rather than an opaque timeout.
    """

    def __init__(self, provider: str, detail: str) -> None:
        self.provider = provider
        self.detail = detail
        super().__init__(
            f"{provider} returned a provider error, not an answer: {detail}"
        )


class ProviderRateLimited(BrowserProviderError):
    """The provider's usage / message cap was hit (a non-answer, retry later)."""


class ProviderUnavailable(BrowserProviderError):
    """The provider produced no usable answer (empty turn / transient outage)."""


# Rate-cap banners that some providers render as a response turn. Deliberately
# narrow and high-confidence: a model's normal answer is very unlikely to contain
# these exact phrasings, and a content *refusal* (a valid answer) is NOT matched.
_PROVIDER_ERROR_PATTERNS: tuple[tuple[re.Pattern[str], type[BrowserProviderError]], ...] = (
    (re.compile(r"you('?ve| have)?\s*reached your\s+\w*\s*(usage|message|daily|plan)\s+limit", re.I), ProviderRateLimited),
    (re.compile(r"reached the current usage cap", re.I), ProviderRateLimited),
    (re.compile(r"usage (cap|limit) (has been )?reached", re.I), ProviderRateLimited),
    (re.compile(r"message limit (reached|hit)", re.I), ProviderRateLimited),
    (re.compile(r"you('?ve| have)?\s*hit (the|your)\s+\w*\s*limit", re.I), ProviderRateLimited),
)


def _check_provider_error(provider: str, text: str) -> None:
    """Raise a typed ``BrowserProviderError`` if *text* is a known non-answer.

    A rate-cap banner sitting in the response region is provider chrome, not the
    model's reply; surfaced typed so the loop never integrates it as the answer.
    """
    for pattern, error_cls in _PROVIDER_ERROR_PATTERNS:
        match = pattern.search(text)
        if match:
            raise error_cls(provider, match.group(0))


# Selectors are externalized to browser_selectors.json (a provider redesign is a
# data edit, not a code change). Each of input/send/stop is a priority-ordered
# list of candidates; the first that matches the live DOM wins. `orac browser
# doctor` reports which field has rotted.


def _provider(name: str) -> ProviderSelectors:
    providers = load_provider_selectors()
    sel = providers.get(name)
    if sel is None:
        raise ValueError(
            f"Unknown browser foundation provider: {name!r}. "
            f"Expected one of: {', '.join(providers)}"
        )
    return sel


def _wait_for_first(page: Any, candidates: tuple[str, ...], timeout_ms: int) -> Any:
    """Return the first candidate selector's element to appear within the budget.

    Tries the candidates in priority order, splitting the budget across them. If
    NONE appear it re-raises the last TimeoutError — a loud miss the caller turns
    into BrowserLoginRequired or a diagnostic, never a silent skip.
    """
    per = max(1_000, timeout_ms // max(1, len(candidates)))
    last_exc: Exception = TimeoutError(f"none of {candidates} appeared")
    for candidate in candidates:
        try:
            return page.wait_for_selector(candidate, timeout=per)
        except (TimeoutError, Exception) as exc:  # noqa: BLE001 — try next candidate
            last_exc = exc
    raise last_exc


def _any_present(page: Any, candidates: tuple[str, ...]) -> bool:
    """True if any presence-check candidate (streaming / stop) currently matches."""
    return any(page.query_selector_all(candidate) for candidate in candidates)


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
    # When set, a screenshot + DOM snapshot is written here on a locator miss
    # (no input / no response / incomplete turn) so an unattended failure is
    # diagnosable after the fact. None disables capture (the default; tests and
    # callers that don't want files on disk leave it unset).
    diagnostics_dir: str | None = field(
        default_factory=lambda: os.environ.get("ORAC_BROWSER_DIAGNOSTICS") or None
    )

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
        # Send the prompt as an ordinary human message. We deliberately do NOT
        # prepend any "[ORAC · agent=… · task=…] / Role:" header: announcing to a
        # provider's web UI that the request comes from an automated agent could
        # change how it is rate-weighted or how the model treats the prompt. The
        # substantive instruction already lives in `prompt`; the identity metadata
        # is for ORAC's own logs, not the provider.
        del agent_name, role, task  # intentionally not surfaced to the provider
        return prompt

    def _send_and_receive(self, text: str) -> str:
        try:
            ctx = _open_browser_page(self.cdp_url, timeout=30.0)
            with ctx as page:
                try:
                    return self._chat(page, text)
                finally:
                    page.close()
        except Exception as exc:
            if isinstance(
                exc,
                (TimeoutError, ValueError, BrowserLoginRequired, BrowserProviderError),
            ):
                raise
            raise RuntimeError(
                f"Cannot connect to Chrome at {self.cdp_url}. "
                "Start Chrome with: chrome --remote-debugging-port=9222 --no-first-run"
            ) from exc

    def _capture_diagnostics(self, page: Any, label: str) -> str | None:
        """Best-effort screenshot + DOM snapshot of a failing page.

        A locator miss on an unattended run is otherwise undiagnosable: the loop
        sees a typed error but no one saw the screen. When ``diagnostics_dir`` is
        set we write a PNG (CDP Page.captureScreenshot) and the page HTML so the
        operator — or `orac browser doctor` — can see what the DOM actually looked
        like. Capture failure must never mask the original error, so everything
        here is swallowed and returns None.
        """
        if not self.diagnostics_dir:
            return None
        try:
            out_dir = Path(self.diagnostics_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base = out_dir / f"{stamp}-{label}"
            shot = page._session.call("Page.captureScreenshot", {"format": "png"})
            data = shot.get("data") if isinstance(shot, dict) else None
            if data:
                import base64 as _b64  # noqa: PLC0415
                base.with_suffix(".png").write_bytes(_b64.b64decode(data))
            html = page._session.evaluate("document.documentElement.outerHTML")
            base.with_suffix(".html").write_text(str(html or ""), encoding="utf-8")
            return str(base)
        except Exception:  # noqa: BLE001 — diagnostics are best-effort
            return None

    def _chat(self, page: Any, text: str) -> str:
        sel = _provider(self.provider)
        url = sel.url

        page.goto(url, wait_until="networkidle", timeout=30_000)

        # --- Phase 0: count existing response elements before submission ------
        response_sel = sel.response
        before_count = len(page.query_selector_all(response_sel))

        # --- Phase 1: type and submit -----------------------------------------
        try:
            inp = _wait_for_first(page, sel.input, timeout_ms=15_000)
        except (TimeoutError, Exception) as exc:  # noqa: BLE001
            # No chat input matched any candidate: the provider redirected us to a
            # login wall (or every input selector rotted). We are already navigated
            # there, so bring that tab to the front — the login is now 'popped' in
            # the ORAC browser window — capture a diagnostic, and raise an explicit,
            # actionable signal instead of an opaque timeout the loop would retry.
            try:
                page._session.call("Page.bringToFront")
            except Exception:  # noqa: BLE001 — surfacing login matters more than focus
                pass
            self._capture_diagnostics(page, f"{self.provider}-no-input")
            raise BrowserLoginRequired(self.provider, url) from exc
        inp.click()
        page.keyboard.press("Control+a")
        page.keyboard.type(text, delay=5)

        # Click the first send button that appears; fall back to Enter only when
        # none of the send candidates resolve (some composers submit on Enter).
        try:
            send_btn = _wait_for_first(page, sel.send, timeout_ms=5_000)
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
            self._capture_diagnostics(page, f"{self.provider}-no-response")
            raise TimeoutError(
                f"Browser foundation timed out after {self.timeout_seconds}s "
                f"waiting for {self.provider!r} to start responding."
            )

        # --- Phase 3: wait for the response to actually finish ----------------
        # Done only when NO independent signal still says "in progress": no
        # streaming candidate matches (if the provider has one), no stop-generating
        # button is present, AND the text has held still for `stabilise_seconds`.
        # Requiring all three is a conjunction for correctness — it stops a brief
        # mid-generation pause from being mistaken for completion (the failure mode
        # of the old text-stability-only path for Gemini), not a fallback that
        # masks a failure.
        last_text = ""
        last_change_at = time.monotonic()
        while time.monotonic() < deadline:
            if self.poll_interval > 0:
                time.sleep(self.poll_interval)
            streaming = _any_present(page, sel.streaming)
            generating = _any_present(page, sel.stop)
            els = page.query_selector_all(response_sel)
            current = els[-1].inner_text() if els else ""
            if current != last_text:
                last_change_at = time.monotonic()
                last_text = current
            text_stable = time.monotonic() - last_change_at >= self.stabilise_seconds
            if not streaming and not generating and text_stable:
                break
        else:
            self._capture_diagnostics(page, f"{self.provider}-incomplete")
            raise TimeoutError(
                f"Browser foundation timed out after {self.timeout_seconds}s "
                f"waiting for {self.provider!r} response to complete."
            )

        # --- Phase 4: extract and validate the response text ------------------
        els = page.query_selector_all(response_sel)
        if not els:
            raise RuntimeError(
                f"Browser foundation: response element disappeared after streaming "
                f"({self.provider!r}, selector={response_sel!r})."
            )
        last_el = els[-1]

        # Some providers (Gemini) nest the actual prose in an inner element.
        text = ""
        if sel.response_inner:
            inner = last_el.query_selector(sel.response_inner)
            if inner:
                text = inner.inner_text().strip()
        if not text:
            text = last_el.inner_text().strip()

        # A non-answer must never masquerade as the model's reply: an empty turn
        # means the provider produced nothing usable; a rate-cap banner means the
        # request was refused, not answered. Both surface typed (review-after).
        if not text:
            raise ProviderUnavailable(
                self.provider, "empty response after the turn completed"
            )
        _check_provider_error(self.provider, text)
        return text


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


def provider_login_ready(provider: str, cdp_url: str, *, settle: float = 3.0) -> bool:
    """True if *provider* has a logged-in session (its chat input is present).

    Read-only: navigates the ORAC browser to the provider and checks for the
    input box. A login wall has no input, so this returns False.
    """
    sel = _provider(provider)
    try:
        with _open_browser_page(cdp_url, timeout=30.0) as page:
            page.goto(sel.url)
            time.sleep(settle)
            return any(
                bool(page._session.evaluate(f"!!document.querySelector({json.dumps(c)})"))
                for c in sel.input
            )
    except (BrowserPrimitiveError, OSError, URLError):
        return False


def pop_provider_login(provider: str, cdp_url: str, *, settle: float = 3.0) -> bool:
    """Surface *provider*'s login when needed.

    Opens the provider in the ORAC browser. If already logged in, returns True
    and does nothing further. If not, brings that tab to the front so the login
    page is 'popped' in front of the operator, and returns False. Useful for an
    unattended start: sign in once, then the loop can use the provider.
    """
    if provider_login_ready(provider, cdp_url, settle=settle):
        return True
    url = _provider(provider).url
    try:
        with _open_browser_page(cdp_url, timeout=30.0) as page:
            page.goto(url)
            try:
                page._session.call("Page.bringToFront")
            except Exception:  # noqa: BLE001 — focus is best-effort
                pass
    except (BrowserPrimitiveError, OSError, URLError):
        pass
    return False


def _field_counts(page: Any, candidates: tuple[str, ...]) -> list[tuple[str, int]]:
    """For each candidate selector, how many elements it currently matches."""
    counts: list[tuple[str, int]] = []
    for candidate in candidates:
        n = page._session.evaluate(
            f"document.querySelectorAll({json.dumps(candidate)}).length"
        )
        counts.append((candidate, int(n or 0)))
    return counts


def browser_doctor(
    provider: str,
    cdp_url: str = "http://localhost:9222",
    *,
    probe: bool = True,
    settle: float = 3.0,
) -> dict[str, Any]:
    """Health-check one provider's selectors against the live DOM.

    Reports, per field, which candidate currently matches (so a human sees which
    selector rotted), whether the session is logged in, and — when logged in and
    ``probe`` is on — the result of a real one-word round trip through the brain.
    The probe is what actually exercises the response/streaming/stop selectors
    (idle, they legitimately match nothing); a green field report with a failing
    probe pinpoints completion-path rot. The doctor never raises: it returns the
    fault so an operator or a cron can read it.
    """
    sel = _provider(provider)
    report: dict[str, Any] = {
        "provider": provider, "url": sel.url, "fields": {}, "login_ready": None,
        "probe": None, "error": None,
    }
    try:
        with _open_browser_page(cdp_url, timeout=30.0) as page:
            page.goto(sel.url)
            time.sleep(settle)
            report["fields"] = {
                "input": _field_counts(page, sel.input),
                "send": _field_counts(page, sel.send),
                "response": _field_counts(page, (sel.response,)),
                "streaming": _field_counts(page, sel.streaming),
                "stop": _field_counts(page, sel.stop),
            }
            report["login_ready"] = any(n for _, n in report["fields"]["input"])
    except (BrowserPrimitiveError, OSError, URLError) as exc:
        report["error"] = f"cannot reach browser/CDP at {cdp_url}: {exc}"
        return report

    if probe and report["login_ready"]:
        brain = BrowserFoundationBrain(provider=provider, cdp_url=cdp_url, timeout_seconds=90)
        start = time.monotonic()
        try:
            reply = brain.think(
                "doctor", "doer", Task(title="browser doctor probe"),
                "Reply with exactly one word: ok",
            )
            report["probe"] = {
                "ok": bool(reply.strip()), "elapsed_s": round(time.monotonic() - start, 1),
                "reply": reply.strip()[:60],
            }
        except Exception as exc:  # noqa: BLE001 — the doctor reports faults, never raises
            report["probe"] = {
                "ok": False, "elapsed_s": round(time.monotonic() - start, 1),
                "error": f"{type(exc).__name__}: {exc}",
            }
    return report


def format_doctor_report(report: dict[str, Any]) -> str:
    """Render a browser_doctor result as an operator-facing text block."""
    lines = [f"browser doctor — {report['provider']} ({report['url']})"]
    if report.get("error"):
        lines.append(f"  ERROR: {report['error']}")
        return "\n".join(lines)
    login = report.get("login_ready")
    lines.append(f"  login: {'ready' if login else 'NOT logged in (sign in, then re-run)'}")
    for field_name, counts in report.get("fields", {}).items():
        matched = [f"{c} ({n})" for c, n in counts if n]
        if matched:
            lines.append(f"  {field_name:9}: {matched[0]}")
        elif field_name == "input" and not login:
            lines.append(f"  {field_name:9}: — (login wall)")
        else:
            # send/response/streaming/stop don't exist on an idle composer; the
            # probe is what proves them. Only a failed probe means "stale".
            lines.append(f"  {field_name:9}: none idle (exercised by probe)")
    probe = report.get("probe")
    if probe is None:
        lines.append("  probe: skipped (not logged in)")
    elif probe.get("ok"):
        lines.append(f"  probe: OK in {probe['elapsed_s']}s -> {probe.get('reply')!r}")
    else:
        lines.append(f"  probe: FAILED in {probe['elapsed_s']}s -> {probe.get('error')}")
    return "\n".join(lines)


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
