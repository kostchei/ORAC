"""Validate the browser-foundation chain end to end against the logged-in
providers. Waits for Claude to be logged in (the operator does that in the open
ORAC Chrome window), then sends ONE trivial prompt through each provider via the
real BrowserFoundationBrain path the daemon uses, with a delay between each.

Throwaway operator script. Posts a tiny prompt to each real account.
"""
from __future__ import annotations

import json
import time

from orac.browser_brain import BrowserFoundationBrain
from orac.browser_primitive import open_cdp_page
from orac.models import Task, TaskStatus

CDP = "http://localhost:9222"
PROVIDERS = ["claude", "openai", "gemini"]
INPUT_SEL = {
    "claude": 'div.ProseMirror[role="textbox"]',
    "openai": "#prompt-textarea",
    "gemini": 'rich-textarea .ql-editor[contenteditable="true"]',
}
PROMPT = "Connectivity check. Reply with exactly the single word: OK (nothing else)."
DELAY_BETWEEN = 4.0  # seconds between provider calls


def logged_in(provider: str) -> bool:
    sel = INPUT_SEL[provider]
    try:
        with open_cdp_page(CDP) as page:
            page.goto(_url(provider))
            time.sleep(3.0)
            return bool(page._session.evaluate("!!document.querySelector(%s)" % json.dumps(sel)))
    except Exception as exc:
        print(f"  [{provider}] login probe error: {exc}")
        return False


def _url(provider: str) -> str:
    return {
        "claude": "https://claude.ai/new",
        "openai": "https://chatgpt.com",
        "gemini": "https://gemini.google.com/app",
    }[provider]


def wait_for_claude(max_seconds: int = 300) -> bool:
    print("Waiting for Claude login in the ORAC browser window...")
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if logged_in("claude"):
            print("  claude is logged in.")
            return True
        time.sleep(8.0)
    print("  timed out waiting for Claude login.")
    return False


def validate(provider: str) -> None:
    task = Task(title="connectivity check", status=TaskStatus.IN_PROGRESS)
    brain = BrowserFoundationBrain(provider=provider)
    t0 = time.monotonic()
    try:
        reply = brain.think("Validator", "connectivity check", task, PROMPT)
        dt = time.monotonic() - t0
        snippet = " ".join(reply.split())[:120]
        print(f"[{provider:7}] OK in {dt:4.0f}s -> {snippet!r}")
    except Exception as exc:
        dt = time.monotonic() - t0
        print(f"[{provider:7}] FAILED in {dt:4.0f}s -> {type(exc).__name__}: {exc}")


def main() -> None:
    wait_for_claude()
    print("\n--- validating each provider (real prompt, delay between) ---")
    for i, provider in enumerate(PROVIDERS):
        if not logged_in(provider):
            print(f"[{provider:7}] SKIPPED — not logged in.")
            continue
        validate(provider)
        if i < len(PROVIDERS) - 1:
            time.sleep(DELAY_BETWEEN)


if __name__ == "__main__":
    main()
