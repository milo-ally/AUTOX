"""Persistent task system backing the task_create, task_update, task_list, and task_get tools."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_TASKS_ROOT = os.environ.get(
    "MICROCLAW_TASK_DIR",
    os.path.expanduser("~/.microclaw/tasks"),
)
VALID_STATUSES = {"pending", "in_progress", "completed"}
VALID_PRIORITIES = {"high", "medium", "low"}


class TaskSystemError(Exception):
    """Raised when a task-system operation is invalid."""

    pass


class TaskSystem:
    """Manage persistent tasks stored as JSON files under the microclaw home."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        workspace_key = hashlib.sha1(str(self.workspace).encode("utf-8")).hexdigest()[:12]
        root = Path(DEFAULT_TASKS_ROOT)
        self.dir = root / workspace_key
        self.dir.mkdir(parents=True, exist_ok=True)

    def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Execute a task action (internal dispatch).

        The primary tools (task_create, task_update, task_list, task_get)
        call their dedicated methods directly.  This method remains for
        internal use by delete / clear / replace actions.

        Supported actions:
        - delete
        - clear
        - replace
        """
        action = input_data.get("action")
        if not action:
            action = "replace" if "todos" in input_data else "list"

        action = str(action)

        if action == "list":
            return self.list_tasks()
        if action == "get":
            return self.get_task(self._require_task_id(input_data))
        if action == "create":
            return self.create_task(input_data)
        if action == "update":
            return self.update_task(input_data)
        if action == "delete":
            return self.delete_task(self._require_task_id(input_data))
        if action == "clear":
            return self.clear_tasks()
        if action == "replace":
            return self.replace_tasks(input_data.get("todos", []))

        raise TaskSystemError(f"unknown task action: {action}")

    def list_tasks(self) -> dict[str, Any]:
        tasks = self._load_all()
        counts = {
            "pending": sum(1 for task in tasks if task["status"] == "pending"),
            "in_progress": sum(1 for task in tasks if task["status"] == "in_progress"),
            "completed": sum(1 for task in tasks if task["status"] == "completed"),
        }
        return {
            "status": "ok",
            "action": "list",
            "tasks": tasks,
            "counts": counts,
            "summary": self._summary_lines(tasks),
        }

    def get_task(self, task_id: int) -> dict[str, Any]:
        task = self._load(task_id)
        return {
            "status": "ok",
            "action": "get",
            "task": task,
        }

    def create_task(self, input_data: dict[str, Any]) -> dict[str, Any]:
        subject = str(
            input_data.get("subject")
            or input_data.get("content")
            or ""
        ).strip()
        if not subject:
            raise TaskSystemError("create requires `subject` or `content`")

        task = self._build_task(
            task_id=self._next_id(),
            subject=subject,
            description=str(input_data.get("description", "")).strip(),
            status=str(input_data.get("status", "pending")),
            priority=str(input_data.get("priority", "medium")),
            blocked_by=input_data.get("blockedBy", []),
            owner=str(input_data.get("owner", "")).strip(),
        )
        self._save(task)
        return {
            "status": "ok",
            "action": "create",
            "task": task,
            "message": f"Created task #{task['id']}.",
        }

    def update_task(self, input_data: dict[str, Any]) -> dict[str, Any]:
        task_id = self._require_task_id(input_data)
        task = self._load(task_id)

        if "subject" in input_data:
            subject = str(input_data["subject"]).strip()
            if not subject:
                raise TaskSystemError("subject cannot be empty")
            task["subject"] = subject

        if "content" in input_data:
            content = str(input_data["content"]).strip()
            if not content:
                raise TaskSystemError("content cannot be empty")
            task["subject"] = content

        if "description" in input_data:
            task["description"] = str(input_data.get("description", "")).strip()

        if "priority" in input_data:
            task["priority"] = self._validate_priority(str(input_data["priority"]))

        if "owner" in input_data:
            task["owner"] = str(input_data.get("owner", "")).strip()

        if "status" in input_data:
            task["status"] = self._validate_status(str(input_data["status"]))

        if "blockedBy" in input_data:
            task["blockedBy"] = self._normalize_blocked_by(input_data.get("blockedBy", []), task_id)

        if "addBlockedBy" in input_data:
            additions = self._normalize_blocked_by(input_data.get("addBlockedBy", []), task_id)
            task["blockedBy"] = sorted(set(task["blockedBy"]) | set(additions))

        if "removeBlockedBy" in input_data:
            removals = {int(value) for value in input_data.get("removeBlockedBy", [])}
            task["blockedBy"] = [value for value in task["blockedBy"] if value not in removals]

        self._save(task)
        if task["status"] == "completed":
            unblocked = self._clear_dependency(task_id)
        else:
            unblocked = []

        return {
            "status": "ok",
            "action": "update",
            "task": task,
            "unblocked_task_ids": unblocked,
            "message": f"Updated task #{task_id}.",
        }

    def delete_task(self, task_id: int) -> dict[str, Any]:
        task = self._load(task_id)
        self._path(task_id).unlink()

        updated = []
        for other in self._load_all():
            if task_id in other.get("blockedBy", []):
                other["blockedBy"] = [value for value in other["blockedBy"] if value != task_id]
                self._save(other)
                updated.append(other["id"])

        return {
            "status": "ok",
            "action": "delete",
            "deleted_task": task,
            "updated_task_ids": updated,
            "message": f"Deleted task #{task_id}.",
        }

    def clear_tasks(self) -> dict[str, Any]:
        count = 0
        for path in self.dir.glob("task_*.json"):
            path.unlink()
            count += 1
        return {
            "status": "ok",
            "action": "clear",
            "tasks_cleared": count,
            "message": f"Cleared {count} tasks.",
        }

    def replace_tasks(self, todos: list[dict[str, Any]]) -> dict[str, Any]:
        self.clear_tasks()
        created = []
        next_id = 1
        id_map: dict[str, int] = {}

        for raw in todos:
            legacy_id = str(raw.get("id", next_id))
            task = self._build_task(
                task_id=next_id,
                subject=str(raw.get("content") or raw.get("subject") or "").strip(),
                description=str(raw.get("description", "")).strip(),
                status=str(raw.get("status", "pending")),
                priority=str(raw.get("priority", "medium")),
                blocked_by=[],
                owner=str(raw.get("owner", "")).strip(),
            )
            self._save(task)
            created.append(task)
            id_map[legacy_id] = next_id
            next_id += 1

        for index, raw in enumerate(todos, start=1):
            blocked_values = raw.get("blockedBy", [])
            if not blocked_values:
                continue
            task = self._load(index)
            remapped: list[int] = []
            for value in blocked_values:
                key = str(value)
                if key in id_map:
                    remapped.append(id_map[key])
                elif isinstance(value, int):
                    remapped.append(value)
            task["blockedBy"] = sorted(set(self._normalize_blocked_by(remapped, task["id"])))
            self._save(task)

        return {
            "status": "ok",
            "action": "replace",
            "tasks_written": len(created),
            "tasks": self._load_all(),
            "message": f"Replaced task list with {len(created)} tasks.",
        }

    def _path(self, task_id: int) -> Path:
        return self.dir / f"task_{task_id}.json"

    def _next_id(self) -> int:
        ids = [task["id"] for task in self._load_all()]
        return (max(ids) + 1) if ids else 1

    def _load(self, task_id: int) -> dict[str, Any]:
        path = self._path(task_id)
        if not path.exists():
            raise TaskSystemError(f"task {task_id} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_all(self) -> list[dict[str, Any]]:
        tasks = []
        for path in sorted(self.dir.glob("task_*.json"), key=self._task_sort_key):
            tasks.append(json.loads(path.read_text(encoding="utf-8")))
        return tasks

    def _save(self, task: dict[str, Any]) -> None:
        self._path(int(task["id"])).write_text(
            json.dumps(task, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _task_sort_key(self, path: Path) -> int:
        try:
            return int(path.stem.split("_")[1])
        except (IndexError, ValueError):
            return 0

    def _build_task(
        self,
        task_id: int,
        subject: str,
        description: str,
        status: str,
        priority: str,
        blocked_by: list[Any],
        owner: str,
    ) -> dict[str, Any]:
        subject = subject.strip()
        if not subject:
            raise TaskSystemError("task subject cannot be empty")

        normalized = {
            "id": int(task_id),
            "subject": subject,
            "description": description,
            "status": self._validate_status(status),
            "priority": self._validate_priority(priority),
            "blockedBy": self._normalize_blocked_by(blocked_by, task_id),
            "owner": owner,
        }
        return normalized

    def _validate_status(self, status: str) -> str:
        if status not in VALID_STATUSES:
            raise TaskSystemError(f"invalid status: {status}")
        return status

    def _validate_priority(self, priority: str) -> str:
        if priority not in VALID_PRIORITIES:
            raise TaskSystemError(f"invalid priority: {priority}")
        return priority

    def _normalize_blocked_by(self, blocked_by: list[Any], task_id: int) -> list[int]:
        result: list[int] = []
        for value in blocked_by:
            blocked_id = int(value)
            if blocked_id == int(task_id):
                raise TaskSystemError("task cannot block itself")
            if not self._path(blocked_id).exists():
                raise TaskSystemError(f"blocked task {blocked_id} not found")
            result.append(blocked_id)
        return sorted(set(result))

    def _clear_dependency(self, completed_id: int) -> list[int]:
        updated: list[int] = []
        for task in self._load_all():
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"] = [value for value in task["blockedBy"] if value != completed_id]
                self._save(task)
                updated.append(task["id"])
        return updated

    def _summary_lines(self, tasks: list[dict[str, Any]]) -> list[str]:
        markers = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }
        lines = []
        for task in tasks:
            blocked = f" (blocked by: {task['blockedBy']})" if task.get("blockedBy") else ""
            priority = task.get("priority", "medium")
            lines.append(
                f"{markers.get(task['status'], '[?]')} #{task['id']} [{priority}] {task['subject']}{blocked}"
            )
        return lines

    def _require_task_id(self, input_data: dict[str, Any]) -> int:
        if "task_id" not in input_data:
            raise TaskSystemError("operation requires `task_id`")
        return int(input_data["task_id"])
