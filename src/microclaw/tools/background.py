"""Background task manager — run commands in threads, check status later."""

from __future__ import annotations

import subprocess
import threading
import uuid
from typing import Any


class BackgroundManager:
    """Run shell commands in background threads and track their status."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self._notification_queue: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def run(self, command: str, cwd: str | None = None) -> str:
        """Start a background thread, return task_id immediately."""
        task_id = str(uuid.uuid4())[:8]
        with self._lock:
            self.tasks[task_id] = {
                "status": "running",
                "result": None,
                "command": command,
            }
        thread = threading.Thread(
            target=self._execute,
            args=(task_id, command, cwd),
            daemon=True,
        )
        thread.start()
        return f"Background task {task_id} started: {command[:80]}"

    def _execute(self, task_id: str, command: str, cwd: str | None) -> None:
        """Thread target: run subprocess, capture output, push to queue."""
        try:
            r = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"

        with self._lock:
            self.tasks[task_id]["status"] = status
            self.tasks[task_id]["result"] = output or "(no output)"
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "(no output)")[:500],
            })

    def check(self, task_id: str | None = None) -> str:
        """Check status of one task or list all."""
        with self._lock:
            if task_id:
                t = self.tasks.get(task_id)
                if not t:
                    return f"Error: Unknown task {task_id}"
                return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"

            lines = []
            for tid, t in self.tasks.items():
                lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
            return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self) -> list[dict[str, Any]]:
        """Return and clear all pending completion notifications."""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs
