"""The BACKLOG.md index checkboxes and task-detail Status lines must not drift.

The backlog records whether a task is done twice: once as a checkbox in the
index sections of BACKLOG.md, and once as the `- Status:` line of the task's
`###` detail entry. An implementation agent reads the index to decide what is
true, so a stale checkbox is a correctness defect in the dispatch contract, not
hygiene. QA-006 exists because `AGENT-001`, `OBS-001`, `PRIV-002`, and later
`RAG-010`/`RAG-011` were found checked `[x]` while their details still read
`Status: Todo`; this test is the enforcement that keeps the two representations
consistent. Tasks cancelled by folding into another entry's definition of done
are intentionally listed without a checkbox, so a detail status other than
`Done` needs no index entry at all.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKLOG = REPO_ROOT / "BACKLOG.md"

_TASK_ID = r"[A-Z]+-\d+"
_INDEX_LINE = re.compile(rf"^\s*-\s+\[([ xX])\]\s+`({_TASK_ID})`")
_DETAIL_HEADING = re.compile(rf"^### ({_TASK_ID})\b")
_STATUS_LINE = re.compile(r"^- Status: `([^`]+)`")


def _index_checkboxes(text: str) -> dict[str, bool]:
    """Map each task ID to its checkbox state, demanding one consistent entry."""
    checked: dict[str, bool] = {}
    for line in text.splitlines():
        match = _INDEX_LINE.match(line)
        if match is None:
            continue
        task_id, is_checked = match.group(2), match.group(1) in "xX"
        if task_id in checked and checked[task_id] != is_checked:
            raise ValueError(f"index lists {task_id} both checked and unchecked")
        checked[task_id] = is_checked
    return checked


def _detail_statuses(text: str) -> dict[str, str]:
    """Map each `### TASK-ID` heading to the status under it."""
    statuses: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = _DETAIL_HEADING.match(line)
        if heading is not None:
            current = heading.group(1)
            continue
        if current is None:
            continue
        status = _STATUS_LINE.match(line)
        if status is not None:
            statuses[current] = status.group(1)
            current = None
    return statuses


def test_index_checkboxes_agree_with_detail_status() -> None:
    text = BACKLOG.read_text(encoding="utf-8")
    checked = _index_checkboxes(text)
    statuses = _detail_statuses(text)

    failures: list[str] = []
    for task_id, is_checked in sorted(checked.items()):
        status = statuses.get(task_id)
        if status is None:
            failures.append(f"{task_id}: in the index but has no `### {task_id}` detail entry")
            continue
        if is_checked and status != "Done":
            failures.append(f"{task_id}: index `[x]` but detail Status is `{status}`")
        if not is_checked and status == "Done":
            failures.append(f"{task_id}: detail Status is `Done` but index checkbox is `[ ]`")
    for task_id, status in sorted(statuses.items()):
        if status == "Done" and not checked.get(task_id, False):
            failures.append(f"{task_id}: detail Status is `Done` but the index has no `[x]` entry")

    assert not failures, "\n".join(failures)
