from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orac.models import Task

# The deterministic floor of the rugged decomposition pipeline.
#
# A model proposes a plan; this module turns that loose proposal into a set of
# executable *contracts* and checks the things ORAC can know WITHOUT model
# judgment — valid work kind, an available doer, a named verifier per slice, no
# two slices owning the same resource, no placeholder goals. The semantic
# question ("do these slices actually cover the intent?") stays with the model
# plan-review (plan_review.py); this floor is the cheap, fail-closed gate that
# runs first so a structurally broken plan never reaches the model at all.
#
# Design ref: docs/rugged-decomposition-pipeline.md §3 (slice contract), §4.3
# (plan-review gate), §8 steps 1–3.


_VAGUE_GOALS = {
    "finish the feature",
    "clean up everything",
    "make it production ready",
    "verify it works",
    "do it",
    "handle everything",
}


@dataclass(frozen=True)
class SliceContract:
    """One executable decomposition slice.

    The deterministic contract shape under the model-generated plan: one owner,
    bounded inputs/resources, at least one verifier, and the evidence that can be
    returned to the parent before integration. A slice that cannot name how it
    will be checked is not executable work (the §2 invariant).
    """

    sub_intent: str
    goal: str
    work_kind: str
    acceptance_criteria: tuple[str, ...] = ()
    inputs: dict[str, Any] | None = None
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    owned_paths_or_resources: tuple[str, ...] = ()
    verifier: tuple[str, ...] = ()
    risk_class: str | None = None
    budget: float | None = None
    expected_artifact: str | None = None
    return_evidence: tuple[str, ...] = ()
    integration_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "sub_intent": self.sub_intent,
            "goal": self.goal,
            "work_kind": self.work_kind,
            "acceptance_criteria": list(self.acceptance_criteria),
            "inputs": dict(self.inputs or {}),
            "allowed_tools": list(self.allowed_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "owned_paths_or_resources": list(self.owned_paths_or_resources),
            "verifier": list(self.verifier),
            "return_evidence": list(self.return_evidence),
        }
        if self.risk_class is not None:
            data["risk_class"] = self.risk_class
        if self.budget is not None:
            data["budget"] = self.budget
        if self.expected_artifact is not None:
            data["expected_artifact"] = self.expected_artifact
        if self.integration_note is not None:
            data["integration_note"] = self.integration_note
        return data

    @classmethod
    def from_mapping(
        cls,
        data: dict[str, Any],
        *,
        work_kind: str,
        default_verifiers: tuple[str, ...] = (),
    ) -> "SliceContract":
        """Build a contract from a model-proposed slice mapping.

        A slice that omitted its verifier inherits ``default_verifiers`` (the work
        kind's own checks), so "no slice without a verifier" can hold before the
        validator even runs — the model is not required to restate the kind's
        standard verification, only to override it when a slice needs something
        narrower.
        """
        verifier = _as_tuple(data.get("verifier", data.get("verifiers", default_verifiers)))
        if not verifier:
            verifier = default_verifiers
        return cls(
            sub_intent=str(data.get("sub_intent", "")).strip(),
            goal=str(data.get("goal", data.get("sub_intent", ""))).strip(),
            work_kind=str(data.get("work_kind", work_kind)).strip() or work_kind,
            acceptance_criteria=_as_tuple(data.get("acceptance_criteria", ())),
            inputs=dict(data.get("inputs", {}) or {}),
            allowed_tools=_as_tuple(data.get("allowed_tools", ())),
            forbidden_tools=_as_tuple(data.get("forbidden_tools", ())),
            owned_paths_or_resources=_as_tuple(data.get("owned_paths_or_resources", ())),
            verifier=verifier,
            risk_class=_optional_str(data.get("risk_class")),
            budget=_optional_float(data.get("budget")),
            expected_artifact=_optional_str(data.get("expected_artifact")),
            return_evidence=_as_tuple(data.get("return_evidence", ())),
            integration_note=_optional_str(data.get("integration_note")),
        )


@dataclass(frozen=True)
class DecompositionScore:
    """A plan's structural shape at a glance (the §10 metrics, per parent)."""

    slice_count: int
    coverage: float
    overlap_count: int
    unverified_count: int
    estimated_cost: float
    risk: str
    recommendation: str  # direct | decompose | reject
    reasons: tuple[str, ...] = ()


def normalize_decomposition(
    slices: list[dict[str, Any]],
    *,
    work_kind: str,
    default_verifiers: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Return canonical slice dictionaries with verifier/default fields filled.

    Preserves the model's richer contract fields (owned paths, expected artifact,
    return evidence) instead of flattening them to a bare ``{sub_intent, goal}``
    pair, and defaults each slice's verifier from the work kind.
    """
    return [
        SliceContract.from_mapping(
            entry, work_kind=work_kind, default_verifiers=default_verifiers
        ).to_dict()
        for entry in slices
    ]


def validate_decomposition(
    parent: Task,
    slices: list[dict[str, Any]],
    *,
    work_kind: str,
    known_work_kinds: set[str],
    allowed_verifiers: tuple[str, ...],
    doer_available: bool,
    max_slices: int | None = None,
) -> list[str]:
    """The deterministic floor: structural problems a model need not be asked about.

    Returns a list of human-readable errors (empty == structurally sound). Checks
    the things ORAC can know without judgment: a valid kind with an available
    doer, non-empty executable slices, a verifier per slice drawn from the kind's
    allowed set, no two slices owning the same resource, no placeholder goals, and
    that the parent is still open. Runs *before* the model plan-review so a broken
    plan is rejected cheaply and fail-closed.
    """
    errors: list[str] = []
    if work_kind not in known_work_kinds:
        errors.append(f"unknown work kind {work_kind!r}")
    if not doer_available:
        errors.append(f"work kind {work_kind!r} has no doer agent")
    if not slices:
        errors.append("decomposition has no slices")
    if max_slices is not None and len(slices) > max_slices:
        errors.append(f"decomposition has {len(slices)} slices, max is {max_slices}")

    owned_seen: dict[str, int] = {}
    for index, raw in enumerate(slices):
        label = f"slice {index + 1}"
        contract = SliceContract.from_mapping(
            raw, work_kind=work_kind, default_verifiers=allowed_verifiers
        )
        if not contract.sub_intent:
            errors.append(f"{label} missing sub_intent")
        if not contract.goal:
            errors.append(f"{label} missing goal")
        if contract.goal.strip().lower() in _VAGUE_GOALS:
            errors.append(f"{label} has vague goal {contract.goal!r}")
        if not contract.verifier:
            errors.append(f"{label} has no verifier")
        unknown_verifiers = sorted(set(contract.verifier) - set(allowed_verifiers))
        if unknown_verifiers:
            errors.append(
                f"{label} names unknown verifier(s): {', '.join(unknown_verifiers)}"
            )
        for owned in contract.owned_paths_or_resources:
            owner = owned_seen.get(owned)
            if owner is not None:
                errors.append(
                    f"{label} overlaps ownership {owned!r} with slice {owner + 1}"
                )
            else:
                owned_seen[owned] = index

    if parent.status.value in {"done", "blocked"}:
        errors.append(f"parent task is {parent.status.value}; cannot decompose closed work")
    return errors


def score_decomposition(
    parent: Task,
    slices: list[dict[str, Any]],
    *,
    work_kind: str,
    known_work_kinds: set[str],
    allowed_verifiers: tuple[str, ...],
    doer_available: bool,
    resource_slice: float,
    max_slices: int | None = None,
) -> DecompositionScore:
    """Score a plan for the plan-review gate and the operator telemetry (§10).

    ``recommendation`` is the headline: ``reject`` if the deterministic floor
    found problems, ``direct`` if the plan is a single slice (decomposition is a
    cost — trivial work should stay single-doer, the §2 small-goal bypass), else
    ``decompose``.
    """
    errors = validate_decomposition(
        parent,
        slices,
        work_kind=work_kind,
        known_work_kinds=known_work_kinds,
        allowed_verifiers=allowed_verifiers,
        doer_available=doer_available,
        max_slices=max_slices,
    )
    overlap_count = sum("overlaps ownership" in error for error in errors)
    unverified_count = sum("has no verifier" in error for error in errors)
    coverage = 0.0 if errors else 1.0
    estimated_cost = len(slices) * resource_slice
    if errors:
        recommendation = "reject"
        risk = "invalid"
    elif len(slices) <= 1:
        recommendation = "direct"
        risk = "low"
    else:
        recommendation = "decompose"
        risk = "normal"
    return DecompositionScore(
        slice_count=len(slices),
        coverage=coverage,
        overlap_count=overlap_count,
        unverified_count=unverified_count,
        estimated_cost=estimated_cost,
        risk=risk,
        recommendation=recommendation,
        reasons=tuple(errors),
    )


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    return tuple(str(item) for item in value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
