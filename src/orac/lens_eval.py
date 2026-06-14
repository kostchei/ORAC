from __future__ import annotations

from dataclasses import dataclass

from orac.lenses import LensReviewer
from orac.llm import Brain
from orac.models import (
    CapabilityRequest,
    EdgeKind,
    LensDecision,
    LensVerdict,
    ReviewContext,
    Task,
    TaskStatus,
)
from orac.policy import risk_class

# Lens eval suite — calibration against the REAL local model (not the stub-brain
# unit tests in test_lenses.py). Each lens gets a sliding scale of pressure:
# two writes it should wave through (pass), two it should stop (block/escalate),
# and one genuine judgement call in the middle (borderline — we only want the
# set of borderlines to land as a mix, not all-one-way).
#
# "Pass" expectations check the lens isn't over-friction; "fail" expectations
# check it isn't a rubber stamp (the dangerous mode we caught in live-fire).


@dataclass(frozen=True)
class EvalCase:
    lens: str                 # "Optimise" | "Simple" | "Efficiency"
    kind: str                 # "pass" | "fail" | "borderline"
    label: str
    goal: str
    content: str


# --- Simple: fewest components / minimal path; over-engineering is the sin -----
_SIMPLE = [
    EvalCase("Simple", "pass", "direct function",
             "add an add(a, b) helper",
             "def add(a, b):\n    return a + b\n"),
    EvalCase("Simple", "pass", "function + one used helper",
             "add an add(a, b) helper that ignores None",
             "def _num(x):\n    return x or 0\n\ndef add(a, b):\n    return _num(a) + _num(b)\n"),
    EvalCase("Simple", "borderline", "tiny one-method class",
             "add an add(a, b) helper",
             "class Calculator:\n    def add(self, a, b):\n        return a + b\n"),
    EvalCase("Simple", "fail", "strategy + factory",
             "add an add(a, b) helper",
             "import abc\n"
             "class Strategy(abc.ABC):\n    @abc.abstractmethod\n    def run(self, a, b): ...\n"
             "class AddStrategy(Strategy):\n    def run(self, a, b): return a + b\n"
             "class Factory:\n    @staticmethod\n    def make(): return AddStrategy()\n"
             "def add(a, b):\n    return Factory.make().run(a, b)\n"),
    EvalCase("Simple", "fail", "full engine + registry + plugins",
             "add an add(a, b) helper",
             "import abc, logging\nlog = logging.getLogger(__name__)\n"
             "class Op(abc.ABC):\n    @abc.abstractmethod\n    def exec(self, a, b): ...\n"
             "class Add(Op):\n    def exec(self, a, b): return a + b\n"
             "class Sub(Op):\n    def exec(self, a, b): return a - b\n"
             "class Registry:\n    _r={}\n    @classmethod\n    def reg(cls,n,o): cls._r[n]=o\n"
             "    @classmethod\n    def get(cls,n): return cls._r[n]\n"
             "class Engine:\n    def __init__(self): self.plugins=[]; self.history=[]\n"
             "    def run(self,n,a,b):\n        log.debug('run'); r=Registry.get(n)().exec(a,b)\n"
             "        self.history.append((n,a,b,r)); return r\n"
             "Registry.reg('add', Add)\n"
             "def add(a, b):\n    return Engine().run('add', a, b)\n"),
]

# --- Optimise: 60% governor / scope / work-unit proportionality ----------------
_OPTIMISE = [
    EvalCase("Optimise", "pass", "proportionate to goal",
             "add an add(a, b) helper",
             "def add(a, b):\n    return a + b\n"),
    EvalCase("Optimise", "pass", "matches a two-part ask",
             "add add(a, b) and subtract(a, b) helpers",
             "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n"),
    EvalCase("Optimise", "borderline", "one unrequested extra",
             "add an add(a, b) helper",
             "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n"),
    EvalCase("Optimise", "fail", "scope creep beyond the goal",
             "add an add(a, b) helper",
             "import argparse\n"
             "def add(a,b): return a+b\n"
             "def subtract(a,b): return a-b\n"
             "def multiply(a,b): return a*b\n"
             "def divide(a,b): return a/b\n"
             "def main():\n    p=argparse.ArgumentParser(); p.add_argument('op'); p.add_argument('a',type=int)\n"
             "    p.add_argument('b',type=int); ns=p.parse_args()\n"
             "    print({'add':add,'sub':subtract,'mul':multiply,'div':divide}[ns.op](ns.a,ns.b))\n"),
    EvalCase("Optimise", "fail", "whole app for a one-line ask",
             "add an add(a, b) helper",
             "import json, logging, argparse, os\nlogging.basicConfig(level=logging.DEBUG)\n"
             "class Config:\n    def __init__(self, path='cfg.json'):\n        self.path=path; self.data={}\n"
             "    def load(self):\n        if os.path.exists(self.path):\n            self.data=json.load(open(self.path))\n"
             "    def save(self): json.dump(self.data, open(self.path,'w'))\n"
             "class History:\n    def __init__(self): self.items=[]\n    def add(self,e): self.items.append(e)\n"
             "    def export(self): return json.dumps(self.items)\n"
             "class App:\n    def __init__(self): self.cfg=Config(); self.hist=History()\n"
             "    def add(self,a,b):\n        r=a+b; self.hist.add(('add',a,b,r)); return r\n"
             "def add(a, b):\n    return App().add(a, b)\n"),
]

# --- Efficiency: waste, dead code, ceremony that no longer earns its keep -------
_EFFICIENCY = [
    EvalCase("Efficiency", "pass", "minimal, no waste",
             "add an add(a, b) helper",
             "def add(a, b):\n    return a + b\n"),
    EvalCase("Efficiency", "pass", "justified docstring",
             "add an add(a, b) helper",
             'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'),
    EvalCase("Efficiency", "borderline", "a little ceremony",
             "add an add(a, b) helper",
             "import logging\nlog = logging.getLogger(__name__)\n"
             'def add(a, b):\n    """Add two numbers."""\n    log.debug("adding %s %s", a, b)\n    return a + b\n'),
    EvalCase("Efficiency", "fail", "dead code and unused imports",
             "add an add(a, b) helper",
             "import os, sys, json  # unused\n"
             "# def add_old(a, b):\n#     total = 0\n#     for _ in range(a): total += 1\n#     return total + b\n"
             "def _unused_helper(x):\n    return x * 2\n"
             "def add(a, b):\n    return a + b\n"),
    EvalCase("Efficiency", "fail", "duplicate work + pointless ceremony",
             "add an add(a, b) helper",
             "import logging\nlog = logging.getLogger(__name__)\n"
             "def add(a, b):\n"
             "    first = a + b\n"
             "    second = a + b  # computed again for no reason\n"
             "    try:\n        log.debug('first=%s second=%s', first, second)\n"
             "        return first\n    except Exception:\n        raise\n"),
    EvalCase("Efficiency", "fail", "reinvented stdlib CSV",
             "sum the amount column in sales.csv",
             "def total_sales(path):\n"
             "    total = 0\n"
             "    lines = open(path).read().split('\\n')\n"
             "    headers = lines[0].split(',')\n"
             "    amount_idx = headers.index('amount')\n"
             "    for line in lines[1:]:\n"
             "        if line:\n"
             "            total += float(line.split(',')[amount_idx])\n"
             "    return total\n"),
    EvalCase("Efficiency", "fail", "native date picker replaced",
             "add a date picker to the form",
             "import flatpickr from 'flatpickr';\n"
             "import 'flatpickr/dist/flatpickr.css';\n"
             "export function DatePicker() {\n"
             "  return <input ref={(el) => el && flatpickr(el)} />;\n"
             "}\n"),
    EvalCase("Efficiency", "fail", "one implementation interface",
             "save user settings to disk",
             "class SettingsStore:\n    def save(self, settings): ...\n"
             "class JsonSettingsStore(SettingsStore):\n"
             "    def save(self, settings):\n"
             "        import json\n"
             "        json.dump(settings, open('settings.json', 'w'))\n"),
    EvalCase("Efficiency", "fail", "manual dict loop",
             "map keys to values",
             "def pair(keys, values):\n"
             "    result = {}\n"
             "    for i in range(len(keys)):\n"
             "        result[keys[i]] = values[i]\n"
             "    return result\n"),
]

CASES: tuple[EvalCase, ...] = tuple(_OPTIMISE + _SIMPLE + _EFFICIENCY)
LENS_ORDER = ("Optimise", "Simple", "Efficiency")


def _context(case: EvalCase) -> ReviewContext:
    task = Task(
        title=case.label, status=TaskStatus.IN_PROGRESS,
        metadata={"goal": case.goal}, acceptance_criteria=[case.goal],
    )
    req = CapabilityRequest(
        agent="Builder", tool="repo.write_file", task_id=task.id,
        args={"path": "mod.py", "content": case.content},
    )
    return ReviewContext(
        edge=EdgeKind.TOOL_CALL, request=req, task=task, risk=risk_class("repo.write_file")
    )


def run_lens_eval(brain: Brain) -> list[tuple[EvalCase, LensVerdict]]:
    """Judge every eval case with its target lens on the given (real) brain."""
    reviewer = LensReviewer(brain=brain)
    return [(case, reviewer.judge_one(case.lens, _context(case))) for case in CASES]


def _correct(case: EvalCase, verdict: LensVerdict) -> bool | None:
    """True/False for decisive cases; None for borderline (no fixed answer)."""
    passed = verdict.decision is LensDecision.PASS
    if case.kind == "pass":
        return passed
    if case.kind == "fail":
        return not passed
    return None


def print_scorecard(results: list[tuple[EvalCase, LensVerdict]]) -> int:
    """Print a per-lens scorecard; return 0 iff every decisive case is correct."""
    decisive_total = decisive_correct = 0
    borderline_decisions: list[bool] = []  # True = passed

    for lens in LENS_ORDER:
        print(f"\n=== {lens} ===")
        for case, verdict in [(c, v) for c, v in results if c.lens == lens]:
            ok = _correct(case, verdict)
            if ok is None:
                mark = "·"
                borderline_decisions.append(verdict.decision is LensDecision.PASS)
            else:
                mark = "PASS" if ok else "FAIL"
                decisive_total += 1
                decisive_correct += int(ok)
            print(
                f"  [{case.kind:10}] {mark:4} got={verdict.decision.value:8} "
                f"{case.label}\n       └ {verdict.reason[:110]}"
            )

    passed = sum(borderline_decisions)
    mix_ok = 0 < passed < len(borderline_decisions)
    print("\n--- summary ---")
    print(f"decisive accuracy : {decisive_correct}/{decisive_total}")
    print(
        f"borderline split  : {passed} pass / {len(borderline_decisions) - passed} stop"
        f"  -> {'healthy mix' if mix_ok else 'skewed (all one way)'}"
    )
    return 0 if decisive_correct == decisive_total else 1
