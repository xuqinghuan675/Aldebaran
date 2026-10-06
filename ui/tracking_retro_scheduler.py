"""Small scheduling helpers for tracking-task AI retrospectives."""
from __future__ import annotations

from datetime import datetime, timedelta


def should_auto_start_retrospective(
    task: dict,
    *,
    now: datetime | None = None,
    window: timedelta = timedelta(minutes=30),
) -> bool:
    if task.get('status') != 'closed':
        return False
    if task.get('grade') is None:
        return False
    if task.get('retrospective'):
        return False
    closed_at = str(task.get('closed_at') or '').strip()
    if not closed_at:
        return False
    try:
        closed_dt = datetime.fromisoformat(closed_at[:19])
    except ValueError:
        return False
    now = now or datetime.now()
    return timedelta(0) <= (now - closed_dt) <= window


def take_auto_retro_batch(
    tasks: list[dict],
    *,
    started_ids: set[str],
    running_count: int,
    max_concurrent: int,
    now: datetime | None = None,
    window: timedelta = timedelta(minutes=30),
) -> list[dict]:
    slots = max(0, int(max_concurrent) - int(running_count))
    if slots <= 0:
        return []

    batch: list[dict] = []
    for task in tasks:
        task_id = str(task.get('id') or '')
        if not task_id or task_id in started_ids:
            continue
        if not should_auto_start_retrospective(task, now=now, window=window):
            continue
        batch.append(task)
        if len(batch) >= slots:
            break
    return batch


def pending_retro_tasks(tasks: list[dict]) -> list[dict]:
    return [
        t for t in tasks
        if t.get('status') == 'closed'
        and t.get('grade') is not None
        and not t.get('retrospective')
    ]
