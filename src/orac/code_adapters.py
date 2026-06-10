from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from orac.adapters import Adapter
from orac.models import CapabilityRequest
from orac.tooling import ToolResult

# The Builder's real tools: read/search/write code, run tests, and checkpoint via
# git. Writes are confined to approved repo roots; nothing here may touch a path
# outside them. These are the code-creation capabilities that make ORAC able to
# help build the rest of itself (docs/roadmap.md Milestone A).

READ_TOOLS = frozenset({"repo.read_file", "repo.search", "git.status"})
WRITE_TOOLS = frozenset(
    {
        "repo.write_file",
        "repo.edit_file",
        "git.create_branch",
        "git.commit",
        "git.push",
        "git.revert",
        "git.stash",
        "git.stash_pop",
    }
)
TEST_TOOLS = frozenset({"repo.run_tests"})
CODE_TOOLS = READ_TOOLS | WRITE_TOOLS | TEST_TOOLS

SEARCH_RESULT_LIMIT = 200


@dataclass(frozen=True)
class CodeAdapterSet:
    """Code/git adapters bound to a fixed set of approved repo roots.

    Every path argument is resolved and checked to fall inside an approved root;
    anything outside raises (no fallback, no silent clamp).
    """

    approved_roots: tuple[Path, ...]

    def adapters(self) -> dict[str, Adapter]:
        return {
            "repo.read_file": self.read_file,
            "repo.search": self.search,
            "repo.write_file": self.write_file,
            "repo.edit_file": self.edit_file,
            "repo.run_tests": self.run_tests,
            "git.create_branch": self.create_branch,
            "git.commit": self.commit,
            "git.push": self.push,
            "git.revert": self.revert,
            "git.stash": self.stash,
            "git.stash_pop": self.stash_pop,
            "git.status": self.status,
        }

    # --- path guards ------------------------------------------------------

    def _resolve_in_root(self, raw: str) -> Path:
        candidate = Path(raw)
        if not candidate.is_absolute() and len(self.approved_roots) == 1:
            # The agent names paths relative to its repo, not the process cwd.
            # Resolve them against the approved root so a bare "mod.py" lands in
            # the repo, not wherever ORAC happens to be running from.
            candidate = self.approved_roots[0] / candidate
        path = candidate.resolve()
        for root in self.approved_roots:
            if path == root or root in path.parents:
                return path
        raise PermissionError(f"Path {path} is outside the approved repo roots.")

    def _root_for(self, raw_root: str | None) -> Path:
        if raw_root is None:
            if len(self.approved_roots) != 1:
                raise ValueError("Repo root is ambiguous; pass a 'root' argument.")
            return self.approved_roots[0]
        root = Path(raw_root).resolve()
        if root not in self.approved_roots:
            raise PermissionError(f"Root {root} is not an approved repo root.")
        return root

    # --- subprocess helpers ----------------------------------------------

    def _run(self, args: list[str], root: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=root, capture_output=True, text=True, timeout=timeout
        )

    def _git(self, root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = self._run(["git", *args], root)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    # --- read adapters ----------------------------------------------------

    def read_file(self, req: CapabilityRequest) -> ToolResult:
        path = self._resolve_in_root(req.args["path"])
        if not path.is_file():
            raise FileNotFoundError(f"repo.read_file: no file at {path}.")
        content = path.read_text(encoding="utf-8")
        return ToolResult(
            "repo.read_file",
            f"Read {len(content)} chars from {path}.",
            {"path": str(path), "content": content},
        )

    def search(self, req: CapabilityRequest) -> ToolResult:
        root = self._root_for(req.args.get("root"))
        query = req.args["query"]
        matches: list[dict[str, object]] = []
        for path in root.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # not a searchable text file
            for lineno, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    matches.append(
                        {"path": str(path), "line": lineno, "text": line.strip()}
                    )
                    if len(matches) >= SEARCH_RESULT_LIMIT:
                        break
            if len(matches) >= SEARCH_RESULT_LIMIT:
                break
        return ToolResult(
            "repo.search",
            f"Found {len(matches)} match(es) for {query!r}.",
            {"query": query, "matches": matches, "count": len(matches)},
        )

    def status(self, req: CapabilityRequest) -> ToolResult:
        root = self._root_for(req.args.get("root"))
        proc = self._git(root, "status", "--porcelain")
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        return ToolResult(
            "git.status",
            f"{len(lines)} change(s) in working tree.",
            {"root": str(root), "changes": lines},
        )

    # --- write adapters ---------------------------------------------------

    def write_file(self, req: CapabilityRequest) -> ToolResult:
        path = self._resolve_in_root(req.args["path"])
        content = req.args["content"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult(
            "repo.write_file",
            f"Wrote {len(content)} chars to {path}.",
            {"path": str(path), "bytes": len(content.encode("utf-8"))},
        )

    def edit_file(self, req: CapabilityRequest) -> ToolResult:
        """Replace one exact, unique occurrence of ``old`` with ``new`` in a file.

        The surgical alternative to a whole-file rewrite (the Karpathy guideline:
        prefer small, reviewable diffs over large opaque ones). Fail-closed, no
        fuzzy matching: ``old`` must appear exactly once, or the edit raises and
        nothing is written — an ambiguous or absent anchor is a fault to surface,
        not to guess at. ``old`` and ``new`` must differ.
        """
        path = self._resolve_in_root(req.args["path"])
        old = req.args["old"]
        new = req.args["new"]
        if old == new:
            raise ValueError("repo.edit_file: 'old' and 'new' are identical; no edit.")
        if not path.is_file():
            raise FileNotFoundError(f"repo.edit_file: no file at {path}.")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ValueError(
                f"repo.edit_file: 'old' text not found in {path}; nothing to edit."
            )
        if count > 1:
            raise ValueError(
                f"repo.edit_file: 'old' text occurs {count}x in {path}; it must be "
                "unique. Include more surrounding context to disambiguate."
            )
        path.write_text(text.replace(old, new), encoding="utf-8")
        return ToolResult(
            "repo.edit_file",
            f"Edited {path}: replaced {len(old)} chars with {len(new)}.",
            {"path": str(path), "old_len": len(old), "new_len": len(new)},
        )

    def create_branch(self, req: CapabilityRequest) -> ToolResult:
        root = self._root_for(req.args.get("root"))
        name = req.args["name"]
        self._git(root, "checkout", "-b", name)
        return ToolResult(
            "git.create_branch",
            f"Created and checked out branch {name!r}.",
            {"root": str(root), "branch": name},
        )

    def commit(self, req: CapabilityRequest) -> ToolResult:
        """Commit exactly the named paths — one logical change per commit.

        ``paths`` is mandatory: fine-grained rollback (revert one feature,
        keep the rest) only works if each commit contains a single change.
        A sweep-everything commit is not possible through this adapter.
        """
        root = self._root_for(req.args.get("root"))
        message = req.args["message"]
        raw_paths = req.args.get("paths")
        if not raw_paths:
            raise ValueError(
                "git.commit requires explicit 'paths' (one logical change per "
                "commit); sweeping the whole tree is not allowed."
            )
        paths = [str(self._resolve_in_root(p)) for p in raw_paths]
        self._git(root, "add", "--", *paths)
        self._git(
            root,
            "-c",
            "user.name=ORAC Builder",
            "-c",
            "user.email=builder@orac.local",
            "commit",
            "-m",
            message,
            "--",
            *paths,
        )
        sha = self._git(root, "rev-parse", "HEAD").stdout.strip()
        return ToolResult(
            "git.commit",
            f"Committed {sha[:8]} ({len(paths)} path(s)): {message}",
            {"root": str(root), "sha": sha, "message": message, "paths": paths},
        )

    def stash(self, req: CapabilityRequest) -> ToolResult:
        """Set aside uncommitted noise so a commit captures only its change."""
        root = self._root_for(req.args.get("root"))
        label = req.args.get("label", "orac-builder")
        proc = self._git(root, "stash", "push", "--include-untracked", "-m", label)
        stashed = "No local changes" not in proc.stdout
        return ToolResult(
            "git.stash",
            f"Stashed working tree as {label!r}." if stashed else "Nothing to stash.",
            {"root": str(root), "label": label, "stashed": stashed},
        )

    def stash_pop(self, req: CapabilityRequest) -> ToolResult:
        root = self._root_for(req.args.get("root"))
        self._git(root, "stash", "pop")
        return ToolResult(
            "git.stash_pop",
            "Restored stashed working tree.",
            {"root": str(root)},
        )

    def revert(self, req: CapabilityRequest) -> ToolResult:
        """Undo a committed change by creating an inverse commit.

        This is the rollback primitive behind review-after: every notified
        change can be reversed with `git.revert sha` without rewriting history,
        so a reviewed "not ok" has a one-step undo even after a push.
        """
        root = self._root_for(req.args.get("root"))
        sha = req.args["sha"]
        self._git(
            root,
            "-c",
            "user.name=ORAC Builder",
            "-c",
            "user.email=builder@orac.local",
            "revert",
            "--no-edit",
            sha,
        )
        new_sha = self._git(root, "rev-parse", "HEAD").stdout.strip()
        return ToolResult(
            "git.revert",
            f"Reverted {sha[:8]} with inverse commit {new_sha[:8]}.",
            {"root": str(root), "reverted": sha, "revert_commit": new_sha},
        )

    def push(self, req: CapabilityRequest) -> ToolResult:
        root = self._root_for(req.args.get("root"))
        remote = req.args.get("remote", "origin")
        push_args = ["push", remote]
        branch = req.args.get("branch")
        if branch:
            push_args.append(branch)
        # Record what is being published: the review queue needs the head sha so
        # a "not ok" verdict can be rolled back (`git.revert sha`) later.
        sha = self._git(root, "rev-parse", "HEAD").stdout.strip()
        head_branch = branch or self._git(
            root, "rev-parse", "--abbrev-ref", "HEAD"
        ).stdout.strip()
        proc = self._git(root, *push_args)
        return ToolResult(
            "git.push",
            f"Pushed {sha[:8]} ({head_branch}) to {remote}.",
            {
                "root": str(root),
                "remote": remote,
                "branch": head_branch,
                "sha": sha,
                "detail": proc.stderr.strip(),
            },
        )

    # --- test adapter -----------------------------------------------------

    def run_tests(self, req: CapabilityRequest) -> ToolResult:
        root = self._root_for(req.args.get("root"))
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        target = req.args.get("target")
        if target:
            cmd.append(str(self._resolve_in_root(target)))
        proc = self._run(cmd, root, timeout=600)
        passed = proc.returncode == 0
        summary = (proc.stdout + proc.stderr).strip()[-2000:]
        return ToolResult(
            "repo.run_tests",
            f"Tests {'passed' if passed else 'failed'} (rc={proc.returncode}).",
            {"passed": passed, "returncode": proc.returncode, "summary": summary},
        )


def code_adapters_for(roots: tuple[Path | str, ...]) -> dict[str, Adapter]:
    resolved = tuple(Path(r).resolve() for r in roots)
    return CodeAdapterSet(resolved).adapters()
