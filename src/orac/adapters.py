from __future__ import annotations

from pathlib import Path
from typing import Callable

from orac.models import CapabilityRequest
from orac.tooling import ToolResult

# A real tool: a callable that takes a CapabilityRequest and returns a ToolResult.
Adapter = Callable[[CapabilityRequest], ToolResult]

# Adapters are the first tools that touch a real external system (here, the
# local filesystem) rather than journaling an in-memory Task. They share the
# ToolResult shape with the journaling executor so the broker can wrap either
# into a CapabilityResult.

FS_READ_MAX_BYTES = 1_000_000


def fs_read(req: CapabilityRequest) -> ToolResult:
    """Read a single UTF-8 text file from disk.

    Read-only and side-effect free. Missing paths, directories, and oversized
    files raise rather than returning a partial or empty result.
    """
    raw_path = req.args.get("path")
    if not raw_path:
        raise ValueError("fs_read requires a 'path' argument.")
    path = Path(raw_path)
    if not path.is_file():
        raise FileNotFoundError(f"fs_read: no file at {path}.")
    size = path.stat().st_size
    if size > FS_READ_MAX_BYTES:
        raise ValueError(
            f"fs_read: {path} is {size} bytes, over the {FS_READ_MAX_BYTES} limit."
        )
    content = path.read_text(encoding="utf-8")
    return ToolResult(
        name="fs_read",
        message=f"Read {len(content)} chars from {path}.",
        data={"path": str(path), "content": content, "bytes": size},
    )


def default_adapters() -> dict[str, Adapter]:
    return {"fs_read": fs_read}
