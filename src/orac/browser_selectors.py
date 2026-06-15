"""Externalized provider chat-UI selectors for the browser foundation.

The selector strings live in ``browser_selectors.json`` beside this module so a
provider redesign is a data edit, not a code change. Each of ``input`` / ``send``
/ ``stop`` is a priority-ordered tuple of candidate selectors: the first that
matches the live DOM wins, and if none match the calling code fails loud. Prefer
stable anchors (ARIA role/label, ``data-testid``) over class fragments, which rot
fastest. ``orac browser doctor`` reports which field has gone stale.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_CONFIG_PATH = Path(__file__).with_name("browser_selectors.json")


@dataclass(frozen=True)
class ProviderSelectors:
    """One provider's resolved selector set. Lists are priority-ordered."""

    name: str
    url: str
    input: tuple[str, ...]
    send: tuple[str, ...]
    response: str
    response_inner: str | None
    streaming: tuple[str, ...]
    stop: tuple[str, ...]


@lru_cache(maxsize=4)
def load_provider_selectors(path: str | None = None) -> dict[str, ProviderSelectors]:
    """Load the provider selector config. Cached per path; keys starting with
    ``_`` (comments / metadata) are ignored. A malformed entry fails loud."""
    config_path = Path(path) if path else _CONFIG_PATH
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    providers: dict[str, ProviderSelectors] = {}
    for name, spec in raw.items():
        if name.startswith("_"):
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"browser selector entry {name!r} is not an object.")
        for field in ("url", "input", "send", "response"):
            if field not in spec:
                raise ValueError(f"browser selector {name!r} is missing {field!r}.")
        if not spec["input"] or not spec["send"]:
            raise ValueError(f"browser selector {name!r} needs ≥1 input and send candidate.")
        providers[name] = ProviderSelectors(
            name=name,
            url=str(spec["url"]),
            input=tuple(spec["input"]),
            send=tuple(spec["send"]),
            response=str(spec["response"]),
            response_inner=spec.get("response_inner"),
            streaming=tuple(spec.get("streaming", ())),
            stop=tuple(spec.get("stop", ())),
        )
    if not providers:
        raise ValueError("browser selector config has no providers.")
    return providers
