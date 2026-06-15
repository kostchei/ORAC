from __future__ import annotations

from orac.browser_primitive import BrowserConnection, BrowserPage


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_exc: object) -> None:
        self.exited = True

    def call(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params or {}))
        return {}


def test_browser_page_close_closes_its_chrome_target() -> None:
    session = FakeSession()
    page = BrowserPage(session, target_id="target-1")

    page.close()

    assert session.calls == [("Target.closeTarget", {"targetId": "target-1"})]


def test_browser_connection_passes_target_id_to_page() -> None:
    session = FakeSession()
    conn = BrowserConnection(session, target_id="target-2")

    page = conn.__enter__()
    try:
        page.close()
    finally:
        conn.__exit__(None, None, None)

    assert session.entered is True
    assert session.exited is True
    assert session.calls == [("Target.closeTarget", {"targetId": "target-2"})]
