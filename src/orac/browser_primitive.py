from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class BrowserPrimitiveError(RuntimeError):
    """Raised when the local CDP browser primitive cannot complete an action."""


class _WebSocket:
    """Tiny blocking WebSocket client for Chrome DevTools Protocol JSON messages.

    This is intentionally narrow: client-to-browser JSON commands and browser-to-
    client JSON replies/events. It implements enough RFC 6455 framing to talk to
    Chrome's local DevTools endpoint, avoiding a Playwright/websocket dependency.
    """

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout
        self._socket: socket.socket | ssl.SSLSocket | None = None

    def __enter__(self) -> "_WebSocket":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise BrowserPrimitiveError(f"Unsupported DevTools websocket URL: {self.url!r}")
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        raw = socket.create_connection((host, port), timeout=self.timeout)
        raw.settimeout(self.timeout)
        sock: socket.socket | ssl.SSLSocket = raw
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            sock.close()
            raise BrowserPrimitiveError("Chrome DevTools websocket upgrade failed.")
        self._socket = sock

    def close(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_text(json.dumps(payload, separators=(",", ":")))

    def recv_json(self) -> dict[str, Any]:
        return json.loads(self._recv_text())

    def _send_text(self, text: str) -> None:
        if self._socket is None:
            raise BrowserPrimitiveError("WebSocket is not connected.")
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._socket.sendall(bytes(header) + mask + masked)

    def _recv_text(self) -> str:
        message = bytearray()
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x8:  # close
                raise BrowserPrimitiveError("Chrome DevTools websocket closed.")
            if opcode == 0x9:  # ping: respond pong
                self._send_control(0xA, payload)
                continue
            if opcode in {0x1, 0x0}:  # text or continuation
                message.extend(payload)
                if fin:
                    return message.decode("utf-8")
            elif opcode == 0xA:  # pong
                continue
            else:
                raise BrowserPrimitiveError(f"Unsupported websocket opcode {opcode}.")

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        if self._socket is None:
            raise BrowserPrimitiveError("WebSocket is not connected.")
        head = self._recv_exact(2)
        first, second = head[0], head[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        if self._socket is None:
            raise BrowserPrimitiveError("WebSocket is not connected.")
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self._socket.recv(length - len(chunks))
            if not chunk:
                raise BrowserPrimitiveError("Chrome DevTools websocket closed mid-frame.")
            chunks.extend(chunk)
        return bytes(chunks)

    def _send_control(self, opcode: int, payload: bytes) -> None:
        if self._socket is None:
            raise BrowserPrimitiveError("WebSocket is not connected.")
        if len(payload) > 125:
            payload = payload[:125]
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._socket.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)


class CDPSession:
    def __init__(self, websocket_url: str, timeout: float = 30.0) -> None:
        self.websocket_url = websocket_url
        self.timeout = timeout
        self._ws = _WebSocket(websocket_url, timeout=timeout)
        self._next_id = 1

    def __enter__(self) -> "CDPSession":
        self._ws.__enter__()
        self.call("Runtime.enable")
        self.call("Page.enable")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._ws.__exit__(*_exc)

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        msg_id = self._next_id
        self._next_id += 1
        self._ws.send_json({"id": msg_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = self._ws.recv_json()
            if message.get("id") != msg_id:
                continue  # DevTools event for another domain; ignore.
            if "error" in message:
                raise BrowserPrimitiveError(
                    f"CDP {method} failed: {message['error'].get('message', message['error'])}"
                )
            return dict(message.get("result") or {})
        raise BrowserPrimitiveError(f"CDP {method} timed out.")

    def evaluate(self, expression: str, *, timeout: float | None = None) -> Any:
        old_timeout = self.timeout
        if timeout is not None:
            self.timeout = timeout
        try:
            result = self.call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": True,
                },
            )
        finally:
            self.timeout = old_timeout
        remote = result.get("result", {})
        if "value" in remote:
            return remote["value"]
        if remote.get("type") == "undefined":
            return None
        return remote.get("description")


@dataclass(frozen=True)
class BrowserElement:
    page: "BrowserPage"
    selector: str
    index: int = 0
    child_selector: str | None = None

    def _js_get_element(self) -> str:
        child = json.dumps(self.child_selector) if self.child_selector else "null"
        return (
            f"const els = Array.from(document.querySelectorAll({json.dumps(self.selector)}));"
            f"const base = els[{self.index}];"
            f"const el = base && {child} ? base.querySelector({child}) : base;"
        )

    def click(self) -> None:
        self.page._session.evaluate(
            "(() => {"
            f"{self._js_get_element()}"
            "if (!el) throw new Error('selector not found');"
            "el.scrollIntoView({block:'center', inline:'center'});"
            "el.focus && el.focus(); el.click(); return true;"
            "})()"
        )
        self.page._active_selector = self.selector
        self.page._active_index = self.index

    def inner_text(self) -> str:
        value = self.page._session.evaluate(
            "(() => {"
            f"{self._js_get_element()}"
            "return el ? (el.innerText || el.textContent || '') : '';"
            "})()"
        )
        return str(value or "")

    def query_selector(self, selector: str) -> "BrowserElement | None":
        found = self.page._session.evaluate(
            "(() => {"
            f"{self._js_get_element()}"
            f"return !!(el && el.querySelector({json.dumps(selector)}));"
            "})()"
        )
        if not found:
            return None
        return BrowserElement(self.page, self.selector, self.index, selector)


class BrowserKeyboard:
    def __init__(self, page: "BrowserPage") -> None:
        self.page = page

    def press(self, key: str) -> None:
        if key.lower() in {"control+a", "meta+a"}:
            self.page._session.evaluate("document.execCommand && document.execCommand('selectAll')")
            return
        if key == "Enter":
            self.page._session.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
            self.page._session.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
            return
        self.page._session.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        self.page._session.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})

    def type(self, text: str, delay: int = 0) -> None:
        del delay  # Input.insertText inserts atomically; per-char delay is moot.
        # Rich-text editors keep an internal document model and IGNORE DOM
        # mutations they did not initiate: Claude and ChatGPT use ProseMirror,
        # Gemini uses Quill. Setting `el.textContent = text` is therefore silently
        # dropped — an EMPTY prompt is submitted with no error. CDP Input.insertText
        # emulates IME/paste insertion: a trusted native event the editor's
        # transaction pipeline picks up, the same path a real keystroke takes.
        # The element is already focused (BrowserElement.click) and a prior
        # select-all leaves the existing content selected, so insertText replaces
        # it rather than appending. No DOM-write fallback: a failure here is a real
        # CDP/editor fault worth surfacing, not masking with the broken textContent
        # path this method exists to replace.
        self.page._session.call("Input.insertText", {"text": text})


class BrowserPage:
    def __init__(self, session: CDPSession) -> None:
        self._session = session
        self.keyboard = BrowserKeyboard(self)
        self._active_selector: str | None = None
        self._active_index = 0

    def goto(self, url: str, wait_until: str = "load", timeout: int = 30_000) -> None:
        del wait_until
        self._session.call("Page.navigate", {"url": url})
        deadline = time.monotonic() + (timeout / 1000)
        while time.monotonic() < deadline:
            state = self._session.evaluate("document.readyState")
            if state in {"interactive", "complete"}:
                return
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for page navigation to {url!r}.")

    def query_selector_all(self, selector: str) -> list[BrowserElement]:
        count = self._session.evaluate(
            f"Array.from(document.querySelectorAll({json.dumps(selector)})).length"
        )
        return [BrowserElement(self, selector, i) for i in range(int(count or 0))]

    def wait_for_selector(self, selector: str, timeout: int = 30_000) -> BrowserElement:
        deadline = time.monotonic() + (timeout / 1000)
        while time.monotonic() < deadline:
            elements = self.query_selector_all(selector)
            if elements:
                return elements[0]
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for selector {selector!r}.")

    def close(self) -> None:
        # Target closure is optional for ORAC's long-lived browser profile. Leaving
        # it open is safer than closing a tab Chrome has already detached.
        return


class BrowserConnection:
    def __init__(self, session: CDPSession) -> None:
        self._session = session

    def __enter__(self) -> BrowserPage:
        self._session.__enter__()
        return BrowserPage(self._session)

    def __exit__(self, *exc: object) -> None:
        self._session.__exit__(*exc)


def open_cdp_page(cdp_url: str, *, timeout: float = 30.0) -> BrowserConnection:
    """Open a new Chrome tab through raw CDP and return a minimal page.

    The returned object is a context manager yielding a minimal page primitive with
    the small surface ORAC needs: goto, selector lookup, click, text insertion, and
    innerText extraction.
    """
    endpoint = f"{cdp_url.rstrip('/')}/json/new?about:blank"
    req = Request(endpoint, method="PUT")
    with urlopen(req, timeout=timeout) as resp:
        target = json.loads(resp.read().decode("utf-8"))
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise BrowserPrimitiveError("Chrome did not return a page websocket URL.")
    return BrowserConnection(CDPSession(str(ws_url), timeout=timeout))
