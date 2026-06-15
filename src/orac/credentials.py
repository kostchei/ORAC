"""Mini credential store — the first, small cut of the Group 2 credential vault.

Secrets (Slack bot/app tokens, the WhatsApp bridge session) are encrypted at rest
with Windows DPAPI (`CryptProtectData`, scoped to the current user — the OS holds
the key, so there is no key for ORAC to manage or leak) and stored as opaque blobs
in ``.orac/credentials.json`` keyed by a ``credential_ref``. Config and logs carry
only the ref, never the secret; ``redact`` scrubs any stored secret that reaches a
log line.

Windows-only by design (ORAC is Windows-primary). On other platforms it raises
rather than silently falling back to plaintext — an insecure fallback is worse
than a loud error.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any


class CredentialError(RuntimeError):
    """Raised when a secret cannot be sealed or opened."""


# --- Windows DPAPI via ctypes (no third-party dependency) --------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _to_blob(data: bytes) -> tuple["_DATA_BLOB", Any]:
        # Return the blob AND the backing buffer so the caller keeps it alive for
        # the duration of the API call (the blob only borrows the pointer).
        buffer = ctypes.create_string_buffer(bytes(data), len(data))
        blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        return blob, buffer

    def _from_blob(blob: "_DATA_BLOB") -> bytes:
        size = int(blob.cbData)
        out = ctypes.create_string_buffer(size)
        ctypes.memmove(out, blob.pbData, size)
        return out.raw

    def _dpapi_seal(plaintext: bytes) -> bytes:
        in_blob, _buf = _to_blob(plaintext)
        out_blob = _DATA_BLOB()
        if not _crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
        ):
            raise CredentialError("CryptProtectData failed")
        try:
            return _from_blob(out_blob)
        finally:
            _kernel32.LocalFree(out_blob.pbData)

    def _dpapi_open(blob_bytes: bytes) -> bytes:
        in_blob, _buf = _to_blob(blob_bytes)
        out_blob = _DATA_BLOB()
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
        ):
            raise CredentialError("CryptUnprotectData failed (wrong user, or tampered blob)")
        try:
            return _from_blob(out_blob)
        finally:
            _kernel32.LocalFree(out_blob.pbData)

else:  # pragma: no cover - exercised only off Windows
    def _dpapi_seal(plaintext: bytes) -> bytes:
        raise CredentialError(
            "The ORAC credential store requires Windows DPAPI; no secure backend on "
            f"{sys.platform!r}. Refusing to store secrets in plaintext."
        )

    def _dpapi_open(blob_bytes: bytes) -> bytes:
        raise CredentialError("The ORAC credential store requires Windows DPAPI.")


class CredentialStore:
    """Per-user encrypted key/value store for ORAC secrets, keyed by opaque refs."""

    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root)
        self.path = self.root / ".orac" / "credentials.json"

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CredentialError(f"Corrupt credential store at {self.path}") from exc
        return {str(k): str(v) for k, v in data.items()}

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def set(self, ref: str, secret: str) -> None:
        """Seal ``secret`` under ``ref`` (DPAPI-encrypted, base64-stored)."""
        if not ref:
            raise CredentialError("A credential ref must be non-empty.")
        sealed = base64.b64encode(_dpapi_seal(secret.encode("utf-8"))).decode("ascii")
        data = self._load()
        data[ref] = sealed
        self._write(data)

    def get(self, ref: str) -> str | None:
        """Open the secret under ``ref``, or None if there is no such ref."""
        sealed = self._load().get(ref)
        if sealed is None:
            return None
        return _dpapi_open(base64.b64decode(sealed)).decode("utf-8")

    def has(self, ref: str) -> bool:
        return ref in self._load()

    def delete(self, ref: str) -> bool:
        data = self._load()
        if ref not in data:
            return False
        del data[ref]
        self._write(data)
        return True

    def refs(self) -> list[str]:
        """The stored refs only — never the secrets (safe to print/log)."""
        return sorted(self._load())

    def redact(self, text: str) -> str:
        """Replace any stored secret value appearing in ``text`` with ``***``.

        A backstop for the logging layer: a token that slips into a log line is
        scrubbed before it lands. Refs are safe; only secret VALUES are scrubbed.
        """
        scrubbed = text
        for ref in self.refs():
            secret = self.get(ref)
            if secret:
                scrubbed = scrubbed.replace(secret, "***")
        return scrubbed
