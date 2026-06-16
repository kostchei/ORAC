from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orac.adapters import Adapter
from orac.models import CapabilityRequest
from orac.rollback_contract import fs_write_contract
from orac.tooling import ToolResult

# The first non-git mutating tool, and the reference producer of a RollbackContract.
#
# It writes files UNDER .orac/outputs/ — outside version control, so git.revert
# cannot undo them. That is exactly the case the rollback-contract framework exists
# for: the write records how to undo itself (restore prior content, or delete if it
# was new), the action lands in the review-after queue (classified `notify`), and
# `orac rollback` applies the contract's inverse. Confined to .orac/outputs/ so it
# is bounded and safe — it cannot touch the repo or arbitrary disk.

FS_TOOLS = frozenset({"fs.write_external_file"})


@dataclass(frozen=True)
class ExternalFsAdapterSet:
    outputs_dir: Path

    def adapters(self) -> dict[str, Adapter]:
        return {"fs.write_external_file": self.write_external_file}

    def _resolve(self, raw: str) -> Path:
        out = self.outputs_dir.resolve()
        candidate = (out / raw).resolve()
        if candidate == out or out in candidate.parents:
            return candidate
        raise PermissionError(
            f"fs.write_external_file: {candidate} is outside the outputs dir {out}."
        )

    def write_external_file(self, req: CapabilityRequest) -> ToolResult:
        path = self._resolve(req.args["path"])
        content = req.args["content"]
        existed = path.is_file()
        before = path.read_text(encoding="utf-8") if existed else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        contract = fs_write_contract(str(path), existed, before)
        return ToolResult(
            "fs.write_external_file",
            f"Wrote {len(content)} chars to external file {path}.",
            {"path": str(path), "rollback_contract": contract},
        )


def fs_adapters_for(repo_root: Path | str) -> dict[str, Adapter]:
    outputs = Path(repo_root).resolve() / ".orac" / "outputs"
    return ExternalFsAdapterSet(outputs).adapters()
