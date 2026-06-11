"""Exercise the existing browser-foundation bootstrap, then chain it into the
new browser.verify_local_app tool against a real running app.

Throwaway operator script (not a test fixture). It proves the path the soak
depends on: when CDP is not already up, ensure_browser_foundation_ready finds
Chrome, launches it on the CDP port with a persistent profile, and waits for the
endpoint — after which the Builder's verify tool can confirm a live app rendered.

Usage: python scripts/browser_foundation_check.py [app_url]
"""
from __future__ import annotations

import sys

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.browser_brain import cdp_reachable, ensure_browser_foundation_ready
from orac.models import CapabilityRequest, Task, TaskStatus
from orac.storage import BoardStore
from orac.model_policy import ModelPolicyStore

APP_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
ROOT = "."


def main() -> None:
    policy = ModelPolicyStore(BoardStore(ROOT)).load_policy()
    cdp_url = str(policy.get("browser_cdp_url", "http://localhost:9222"))
    print(f"[before] cdp_reachable({cdp_url}) = {cdp_reachable(cdp_url)}")

    status = ensure_browser_foundation_ready(policy, orac_root=ROOT)
    print(f"[bootstrap] ok={status.get('ok')} action={status.get('action')}")
    print(f"[bootstrap] {status.get('message', '')}")
    print(f"[after] cdp_reachable({cdp_url}) = {cdp_reachable(cdp_url)}")

    if not status.get("ok"):
        raise SystemExit("browser foundation did not come up; cannot verify the app.")

    store = BrokerStore(ROOT).init()
    broker = ToolBroker.from_store(store, repo_root=ROOT)
    task = Task(title="[code] verify the running UI", status=TaskStatus.IN_PROGRESS)
    result = broker.request(
        CapabilityRequest(
            agent="Builder",
            tool="browser.verify_local_app",
            task_id=task.id,
            args={"app_url": APP_URL, "cdp_url": cdp_url},
        ),
        task,
    )
    print(f"\n[verify] status={result.status.value} tool={result.tool}")
    print(f"[verify] message={result.message}")
    print(f"[verify] data={result.data}")


if __name__ == "__main__":
    main()
