from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.decomposition import (
    SliceContract,
    normalize_decomposition,
    score_decomposition,
    validate_decomposition,
)
from orac.models import Board, Task, TaskStatus
from orac.work import run_orchestrated_goal

# The work kind these tests validate against: a real doer with two verifiers.
_KINDS = {"code", "comms"}
_VERIFIERS = ("run_tests", "verify_local_app")


def _parent() -> Task:
    return Task(title="parent", status=TaskStatus.IN_PROGRESS)


def _slice(sub: str, **over) -> dict:
    base = {"sub_intent": sub, "goal": f"do {sub}"}
    base.update(over)
    return base


def _validate(parent, slices, *, kind="code", doer=True, allowed=_VERIFIERS, max_slices=None):
    return validate_decomposition(
        parent,
        slices,
        work_kind=kind,
        known_work_kinds=_KINDS,
        allowed_verifiers=allowed,
        doer_available=doer,
        max_slices=max_slices,
    )


# --- the contract shape ------------------------------------------------------


def test_normalize_defaults_verifier_and_preserves_contract_fields() -> None:
    raw = [_slice("a", owned_paths_or_resources=["mod.py"], expected_artifact="mod.py")]
    out = normalize_decomposition(raw, work_kind="code", default_verifiers=_VERIFIERS)

    assert out[0]["verifier"] == list(_VERIFIERS)          # inherited from the kind
    assert out[0]["owned_paths_or_resources"] == ["mod.py"]  # not flattened away
    assert out[0]["expected_artifact"] == "mod.py"


def test_explicit_verifier_overrides_the_default() -> None:
    raw = [_slice("a", verifier=["run_tests"])]
    out = normalize_decomposition(raw, work_kind="code", default_verifiers=_VERIFIERS)
    assert out[0]["verifier"] == ["run_tests"]


def test_slice_contract_roundtrips_through_mapping() -> None:
    c = SliceContract.from_mapping(
        _slice("a", verifier=["run_tests"], return_evidence=["test output"]),
        work_kind="code",
    )
    d = c.to_dict()
    assert d["sub_intent"] == "a" and d["goal"] == "do a"
    assert d["verifier"] == ["run_tests"] and d["return_evidence"] == ["test output"]


# --- the deterministic floor -------------------------------------------------


def test_clean_plan_has_no_errors() -> None:
    slices = normalize_decomposition(
        [_slice("a", owned_paths_or_resources=["a.py"]),
         _slice("b", owned_paths_or_resources=["b.py"])],
        work_kind="code", default_verifiers=_VERIFIERS,
    )
    assert _validate(_parent(), slices) == []


def test_missing_verifier_is_rejected() -> None:
    # no explicit verifier AND no default to inherit -> the slice has none
    slices = normalize_decomposition([_slice("a")], work_kind="code", default_verifiers=())
    errors = _validate(_parent(), slices, allowed=())
    assert any("has no verifier" in e for e in errors)


def test_vague_goal_is_rejected() -> None:
    errors = _validate(_parent(), [_slice("a", goal="finish the feature", verifier=["run_tests"])])
    assert any("vague goal" in e for e in errors)


def test_overlapping_ownership_is_rejected() -> None:
    slices = [
        _slice("a", verifier=["run_tests"], owned_paths_or_resources=["mod.py"]),
        _slice("b", verifier=["run_tests"], owned_paths_or_resources=["mod.py"]),
    ]
    errors = _validate(_parent(), slices)
    assert any("overlaps ownership 'mod.py'" in e for e in errors)


def test_unknown_work_kind_is_rejected() -> None:
    errors = _validate(_parent(), [_slice("a", verifier=["run_tests"])], kind="quantum")
    assert any("unknown work kind 'quantum'" in e for e in errors)


def test_missing_doer_is_rejected() -> None:
    errors = _validate(_parent(), [_slice("a", verifier=["run_tests"])], doer=False)
    assert any("has no doer agent" in e for e in errors)


def test_unknown_verifier_name_is_rejected() -> None:
    errors = _validate(_parent(), [_slice("a", verifier=["lick_it"])])
    assert any("unknown verifier" in e for e in errors)


def test_closed_parent_cannot_be_decomposed() -> None:
    parent = Task(title="done", status=TaskStatus.DONE)
    errors = _validate(parent, [_slice("a", verifier=["run_tests"])])
    assert any("cannot decompose closed work" in e for e in errors)


def test_too_many_slices_is_rejected() -> None:
    slices = [_slice(f"s{i}", verifier=["run_tests"]) for i in range(3)]
    errors = _validate(_parent(), slices, max_slices=2)
    assert any("max is 2" in e for e in errors)


# --- scoring (telemetry / recommendation) ------------------------------------


def _score(parent, slices, *, doer=True):
    return score_decomposition(
        parent, slices, work_kind="code", known_work_kinds=_KINDS,
        allowed_verifiers=_VERIFIERS, doer_available=doer, resource_slice=0.25,
    )


def test_single_valid_slice_recommends_direct() -> None:
    s = normalize_decomposition([_slice("a")], work_kind="code", default_verifiers=_VERIFIERS)
    assert _score(_parent(), s).recommendation == "direct"


def test_multi_valid_slice_recommends_decompose() -> None:
    s = normalize_decomposition(
        [_slice("a", owned_paths_or_resources=["a.py"]),
         _slice("b", owned_paths_or_resources=["b.py"])],
        work_kind="code", default_verifiers=_VERIFIERS,
    )
    score = _score(_parent(), s)
    assert score.recommendation == "decompose" and score.estimated_cost == 0.5


def test_invalid_plan_recommends_reject() -> None:
    score = _score(_parent(), [_slice("a")], doer=False)
    assert score.recommendation == "reject" and score.coverage == 0.0


# --- integration: the floor short-circuits before the model plan-review ------


def _repo(tmp_path):
    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    store = BrokerStore(tmp_path).init()
    return ToolBroker.from_store(store, repo_root=tmp_path), store


@dataclass
class OneShotBrain:
    """Answers the propose call, then asserts if asked anything else."""

    script: list[str]
    prompts: list[str] = field(default_factory=list)

    def think_json(self, agent: str, role: str, task: Task, prompt: str, schema: dict) -> str:
        self.prompts.append(prompt)
        if not self.script:
            raise AssertionError(
                "model consulted again after propose — the deterministic floor "
                "should have short-circuited before plan review"
            )
        return self.script.pop(0)


def test_orchestrated_goal_blocks_invalid_plan_before_plan_review(tmp_path) -> None:
    broker, store = _repo(tmp_path)
    board = Board()
    parent = Task(title="two parts", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    # A plan whose two slices own the SAME path: a structural fault the floor
    # catches. The brain is scripted with ONLY the propose reply, so if plan
    # review (or any builder) were reached, OneShotBrain would raise.
    overlap_plan = json.dumps(
        {
            "slices": [
                {"sub_intent": "a", "goal": "add a", "owned_paths_or_resources": ["mod.py"]},
                {"sub_intent": "b", "goal": "add b", "owned_paths_or_resources": ["mod.py"]},
            ]
        }
    )

    children = run_orchestrated_goal(
        board, parent, "build two modules", "deliver both modules",
        "code", OneShotBrain([overlap_plan]), broker, {"repo_root": str(tmp_path)},
    )

    assert children == []
    assert parent.status is TaskStatus.BLOCKED
    assert store.subagent_roster_count() == 0  # nothing admitted
    assert any("failed structural validation" in e.message for e in parent.work_log)
    assert any("overlaps ownership" in e.message for e in parent.work_log)
