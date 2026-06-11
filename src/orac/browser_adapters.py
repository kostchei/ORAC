from __future__ import annotations

from orac.adapters import Adapter
from orac.browser_brain import cdp_reachable
from orac.browser_primitive import BrowserPrimitiveError, open_cdp_page
from orac.models import CapabilityRequest
from orac.tooling import ToolResult

# The browser verification adapter: the frontend twin of repo.run_tests. A code
# goal that changes the UI cannot be trusted on the doer's self-reported "done"
# any more than a backend one — generation is cheap, verification is scarce. This
# drives the running local app through the dependency-free CDP primitive
# (orac.browser_primitive) and confirms it actually loaded, so a blank or
# unreachable app blocks the task instead of reaching DONE on the model's word.
#
# Read-only: it navigates and inspects, it never writes. Classified
# reversible-local in policy.py; not in LLM_REVIEWED_TOOLS (no state change).

DEFAULT_CDP_URL = "http://localhost:9222"


def verify_local_app(req: CapabilityRequest) -> ToolResult:
    """Navigate to a running local app and confirm it rendered.

    Requires ``app_url``; ``cdp_url`` defaults to the documented local Chrome
    DevTools endpoint. Success means the page reached a ready state with
    non-empty body content. A missing url, an unreachable browser, or an empty
    page all return ``verified=False`` with detail — the verifier turns that into
    a blocked task. Nothing here fails open.
    """
    app_url = req.args.get("app_url")
    if not app_url:
        raise ValueError("browser.verify_local_app requires an 'app_url' argument.")
    cdp_url = str(req.args.get("cdp_url") or DEFAULT_CDP_URL)

    if not cdp_reachable(cdp_url):
        return ToolResult(
            "browser.verify_local_app",
            f"Chrome CDP not reachable at {cdp_url}; cannot verify {app_url}.",
            {"verified": False, "app_url": app_url, "summary": "CDP unreachable"},
        )

    try:
        with open_cdp_page(cdp_url) as page:
            page.goto(str(app_url))
            body_len = page._session.evaluate(
                "(() => (document.body && (document.body.innerText || '').trim().length) || 0)()"
            )
    except (BrowserPrimitiveError, TimeoutError, OSError) as exc:
        return ToolResult(
            "browser.verify_local_app",
            f"Could not load {app_url}: {exc}",
            {"verified": False, "app_url": app_url, "summary": str(exc)},
        )

    length = int(body_len or 0)
    verified = length > 0
    summary = (
        f"loaded with {length} chars of body text"
        if verified
        else "page loaded but body was empty"
    )
    return ToolResult(
        "browser.verify_local_app",
        f"{app_url}: {summary}.",
        {"verified": verified, "app_url": app_url, "body_chars": length, "summary": summary},
    )


def browser_adapters() -> dict[str, Adapter]:
    """Always-available, read-only browser adapters (no repo root needed)."""
    return {"browser.verify_local_app": verify_local_app}
