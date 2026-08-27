from __future__ import annotations

from orac.chat_config import load_chat_config, save_chat_config
from orac.chat_gateway import ChatGateway
from orac.models import Board, Task, TaskStatus
from orac.promoter import list_promotions, promote_goal
from orac.storage import BoardStore


def test_promoter_writes_digest_and_exact_opt_in_checkbox(tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    roadmap = tmp_path / "docs" / "roadmap.md"
    roadmap.write_text("# Roadmap\n\n- [ ] Ship promoter stage\n", encoding="utf-8")
    task = Task(
        title="Ship promoter stage",
        status=TaskStatus.DONE,
        acceptance_criteria=["digest exists"],
        metadata={
            "promoter_checkboxes": [
                {"path": "docs/roadmap.md", "text": "Ship promoter stage"}
            ]
        },
    )
    task.add_log("Orchestrator", "Goal done — verification passed.")
    child = Task(title="slice", parent_id=task.id, status=TaskStatus.DONE)
    board = Board(tasks=[task, child])

    first = promote_goal(tmp_path, board, task)
    second = promote_goal(tmp_path, board, task)

    assert first == second
    assert first.completed_children == 1
    assert roadmap.read_text(encoding="utf-8").endswith("- [x] Ship promoter stage\n")
    assert len(list_promotions(tmp_path)) == 1
    assert task.work_log[-1].agent == "Promoter"


def test_chat_outbound_includes_new_completion_digest(tmp_path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    cfg = load_chat_config(store)
    cfg["enabled"] = True
    cfg["channels"]["slack"]["enabled"] = True
    cfg["channels"]["slack"]["authorized_senders"] = ["U42"]
    save_chat_config(store, cfg)
    gateway = ChatGateway(tmp_path)
    assert gateway.poll_outbound() == []

    task = Task(title="Visible work", status=TaskStatus.DONE)
    promote_goal(tmp_path, Board(tasks=[task]), task)
    outbound = gateway.poll_outbound()

    assert len(outbound) == 1
    assert outbound[0].target == "U42"
    assert "Completion digest" in outbound[0].text
    assert "Completed: Visible work" in outbound[0].text
    assert gateway.poll_outbound() == []
