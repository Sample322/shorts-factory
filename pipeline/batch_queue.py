"""Очередь jobs — поставить N серий, обработать одну за другой.

State хранится в JSON. UI читает/пишет. Worker dequeue по одному.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .utils import get_logger

log = get_logger("batch_queue")

DEFAULT_QUEUE_PATH = Path("cache/jobs/queue.json")


def _load(path: Path = DEFAULT_QUEUE_PATH) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("jobs", []) if isinstance(data, dict) else []
    except Exception as e:
        log.warning(f"Queue load fail: {e}")
        return []


def _save(jobs: list[dict], path: Path = DEFAULT_QUEUE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def enqueue(
    sources: list[str],
    params: dict,
    queue_path: Path = DEFAULT_QUEUE_PATH,
) -> str:
    """Добавляет job в очередь. Возвращает queue_id."""
    jobs = _load(queue_path)
    job = {
        "id": uuid.uuid4().hex[:10],
        "sources": sources,
        "params": params,
        "status": "pending",
        "added_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "error": None,
    }
    jobs.append(job)
    _save(jobs, queue_path)
    return job["id"]


def list_pending(queue_path: Path = DEFAULT_QUEUE_PATH) -> list[dict]:
    return [j for j in _load(queue_path) if j["status"] == "pending"]


def list_all(queue_path: Path = DEFAULT_QUEUE_PATH) -> list[dict]:
    return _load(queue_path)


def pop_next(queue_path: Path = DEFAULT_QUEUE_PATH) -> dict | None:
    """Атомарно: первый pending → in_progress."""
    jobs = _load(queue_path)
    for j in jobs:
        if j["status"] == "pending":
            j["status"] = "in_progress"
            j["started_at"] = time.time()
            _save(jobs, queue_path)
            return j
    return None


def mark_done(
    job_id: str,
    error: str | None = None,
    queue_path: Path = DEFAULT_QUEUE_PATH,
) -> None:
    jobs = _load(queue_path)
    for j in jobs:
        if j["id"] == job_id:
            j["status"] = "error" if error else "done"
            j["finished_at"] = time.time()
            j["error"] = error
            break
    _save(jobs, queue_path)


def clear_finished(queue_path: Path = DEFAULT_QUEUE_PATH) -> int:
    jobs = _load(queue_path)
    kept = [j for j in jobs if j["status"] in ("pending", "in_progress")]
    removed = len(jobs) - len(kept)
    _save(kept, queue_path)
    return removed


def remove_job(
    job_id: str, queue_path: Path = DEFAULT_QUEUE_PATH,
) -> bool:
    jobs = _load(queue_path)
    new_jobs = [j for j in jobs if j["id"] != job_id]
    if len(new_jobs) == len(jobs):
        return False
    _save(new_jobs, queue_path)
    return True
