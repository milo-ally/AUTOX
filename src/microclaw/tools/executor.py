"""Tool executor — dispatches tool calls to their implementations."""

from __future__ import annotations

import json
from typing import Any

from microclaw.skills import SkillLoader
from microclaw.tools.bash import run_bash
from microclaw.tools.file import run_read_file, run_write_file, run_edit_file
from microclaw.tools.background import BackgroundManager
from microclaw.tools.tasks import TaskSystem, TaskSystemError
from microclaw.tools.team import TeamManager
from microclaw.tools.web import run_web_fetch, run_web_search


class ToolError(Exception):
    """Error raised when a tool invocation fails."""

    pass


class CompactRequested(Exception):
    """Signal raised when the compact tool is called.

    The runtime intercepts this to perform session compaction.
    """

    def __init__(self, focus: str = ""):
        self.focus = focus


class ToolExecutor:
    """Dispatches tool calls to the appropriate handler."""

    def __init__(
        self,
        allowed_tools: set[str] | None = None,
        skill_loader: SkillLoader | None = None,
    ):
        self._allowed_tools = allowed_tools
        self._task_system = TaskSystem()
        self._bg_manager = BackgroundManager()
        self._team_manager: TeamManager | None = None
        self._skill_loader = skill_loader or SkillLoader()

    def execute(self, tool_name: str, input_data: Any) -> str:
        """Execute a tool by name with the given input.

        Args:
            tool_name: Name of the tool to execute.
            input_data: Tool input (parsed JSON dict or string).

        Returns:
            JSON string with the tool result.

        Raises:
            ToolError: If the tool is not allowed, not found, or execution fails.
        """
        if self._allowed_tools is not None and tool_name not in self._allowed_tools:
            raise ToolError(
                f"tool `{tool_name}` is not enabled by the current --allowedTools setting"
            )

        # Parse input if it's a string
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except json.JSONDecodeError as e:
                raise ToolError(f"invalid tool input JSON: {e}") from e

        try:
            result = self._dispatch(tool_name, input_data)
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2, ensure_ascii=False)
        except (ToolError, CompactRequested):
            raise
        except Exception as e:
            raise ToolError(str(e)) from e

    def _dispatch(self, tool_name: str, input_data: dict[str, Any]) -> Any:
        """Route to the correct tool handler."""
        if tool_name == "compact":
            raise CompactRequested(focus=input_data.get("focus", ""))

        dispatch_table = {
            "bash": run_bash,
            "read_file": run_read_file,
            "write_file": run_write_file,
            "edit_file": run_edit_file,
            "web_fetch": run_web_fetch,
            "web_search": run_web_search,
            "task_create": self._run_task_create,
            "task_update": self._run_task_update,
            "task_list": self._run_task_list,
            "task_get": self._run_task_get,
            "background_run": self._run_background_run,
            "check_background": self._run_check_background,
            "load_skill": self._run_load_skill,
            "spawn_teammate": self._run_spawn_teammate,
            "list_teammates": self._run_list_teammates,
            "send_message": self._run_send_message,
            "read_inbox": self._run_read_inbox,
            "wait_teammate": self._run_wait_teammate,
            "broadcast": self._run_broadcast,
            "shutdown_request": self._run_shutdown_request,
            "shutdown_status": self._run_shutdown_status,
            "plan_approval": self._run_plan_approval,
        }

        handler = dispatch_table.get(tool_name)
        if handler is None:
            raise ToolError(f"unknown tool: {tool_name}")

        return handler(input_data)

    def _run_task_create(self, input_data: dict[str, Any]) -> str:
        """Create a new task."""
        try:
            result = self._task_system.create_task(input_data)
        except TaskSystemError as e:
            raise ToolError(str(e)) from e
        return self._render_task_result("create", result)

    def _run_task_update(self, input_data: dict[str, Any]) -> str:
        """Update an existing task."""
        try:
            result = self._task_system.update_task(input_data)
        except TaskSystemError as e:
            raise ToolError(str(e)) from e
        return self._render_task_result("update", result)

    def _run_task_list(self, input_data: dict[str, Any]) -> str:
        """List all tasks."""
        try:
            result = self._task_system.list_tasks()
        except TaskSystemError as e:
            raise ToolError(str(e)) from e
        return self._render_task_result("list", result)

    def _run_task_get(self, input_data: dict[str, Any]) -> str:
        """Get a task by ID."""
        try:
            result = self._task_system.get_task(input_data["task_id"])
        except TaskSystemError as e:
            raise ToolError(str(e)) from e
        return self._render_task_result("get", result)

    @staticmethod
    def _render_task_result(action: str, result: dict[str, Any]) -> str:
        """Render a TaskSystem result dict into a concise, user-friendly string.

        The TaskSystem kernel returns structured dicts; this method formats
        them into readable text so the LLM doesn't dump raw JSON at the user.
        """
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}

        if action == "list":
            tasks = result.get("tasks", [])
            if not tasks:
                return "No tasks."
            lines = []
            for t in tasks:
                marker = markers.get(t["status"], "[?]")
                blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
                lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
            done = sum(1 for t in tasks if t["status"] == "completed")
            lines.append(f"\n({done}/{len(tasks)} completed)")
            return "\n".join(lines)

        if action == "get":
            task = result.get("task", {})
            marker = markers.get(task.get("status"), "[?]")
            lines = [f"{marker} #{task['id']}: {task['subject']}"]
            if task.get("description"):
                lines.append(f"  description: {task['description']}")
            if task.get("priority"):
                lines.append(f"  priority: {task['priority']}")
            if task.get("blockedBy"):
                lines.append(f"  blocked by: {task['blockedBy']}")
            if task.get("owner"):
                lines.append(f"  owner: {task['owner']}")
            return "\n".join(lines)

        if action == "create":
            task = result.get("task", {})
            marker = markers.get(task.get("status"), "[?]")
            return f"{marker} #{task['id']}: {task['subject']}"

        if action == "update":
            task = result.get("task", {})
            marker = markers.get(task.get("status"), "[?]")
            msg = f"{marker} #{task['id']}: {task['subject']}"
            unblocked = result.get("unblocked_task_ids", [])
            if unblocked:
                msg += f"\nUnblocked tasks: {unblocked}"
            return msg

        # Fallback: return the raw message field or JSON
        return result.get("message", json.dumps(result, ensure_ascii=False))

    def _run_background_run(self, input_data: dict[str, Any]) -> str:
        """Run a command in a background thread."""
        command = str(input_data.get("command", "")).strip()
        if not command:
            raise ToolError("missing required field 'command'")
        return self._bg_manager.run(command)

    def _run_check_background(self, input_data: dict[str, Any]) -> str:
        """Check background task status."""
        task_id = input_data.get("task_id")
        return self._bg_manager.check(task_id)

    def _run_load_skill(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Load a skill body by name."""
        name = str(input_data.get("name", "")).strip()
        if not name:
            raise ToolError("missing required field 'name'")
        return {
            "status": "ok",
            "name": name,
            "content": self._skill_loader.get_tool_content(name),
        }

    # -- Team tool handlers --

    def _ensure_team_manager(self) -> TeamManager:
        """Lazy-initialize the TeamManager (needs model from CLI)."""
        if self._team_manager is None:
            raise ToolError(
                "team features not available — model not set. "
                "Use /team spawn from the REPL instead."
            )
        return self._team_manager

    def set_team_manager(self, manager: TeamManager) -> None:
        """Set the TeamManager instance (called by CLI after initialization)."""
        self._team_manager = manager
    
    @property
    def team_manager(self) -> TeamManager | None:
        return self._team_manager

    def _run_spawn_teammate(self, input_data: dict[str, Any]) -> str:
        tm = self._ensure_team_manager()
        name = str(input_data.get("name", "")).strip()
        role = str(input_data.get("role", "")).strip()
        prompt = str(input_data.get("prompt", "")).strip()
        if not name or not role or not prompt:
            raise ToolError("spawn_teammate requires 'name', 'role', and 'prompt'")
        return tm.spawn(name, role, prompt)

    def _run_list_teammates(self, input_data: dict[str, Any]) -> str:
        tm = self._ensure_team_manager()
        return tm.list_all()

    def _run_send_message(self, input_data: dict[str, Any]) -> str:
        tm = self._ensure_team_manager()
        to = str(input_data.get("to", "")).strip()
        content = str(input_data.get("content", "")).strip()
        msg_type = str(input_data.get("msg_type", "message")).strip()
        if not to or not content:
            raise ToolError("send_message requires 'to' and 'content'")
        return tm.send_message(to, content, msg_type)

    def _run_read_inbox(self, input_data: dict[str, Any]) -> str:
        tm = self._ensure_team_manager()
        msgs = tm.read_lead_inbox()
        return json.dumps(msgs, indent=2, ensure_ascii=False)

    def _run_wait_teammate(self, input_data: dict[str, Any]) -> str:
        tm = self._ensure_team_manager()
        teammate = str(input_data.get("teammate", "")).strip()
        timeout_seconds = int(input_data.get("timeout_seconds", 10))
        if not teammate:
            raise ToolError("wait_teammate requires 'teammate'")
        try:
            msgs = tm.wait_for_reply(teammate, timeout_seconds=timeout_seconds)
        except ValueError as e:
            raise ToolError(str(e)) from e
        return json.dumps(msgs, indent=2, ensure_ascii=False)

    def _run_broadcast(self, input_data: dict[str, Any]) -> str:
        tm = self._ensure_team_manager()
        content = str(input_data.get("content", "")).strip()
        if not content:
            raise ToolError("broadcast requires 'content'")
        return tm.broadcast(content)

    def _run_shutdown_request(self, input_data: dict[str, Any]) -> str:
        tm = self._ensure_team_manager()
        teammate = str(input_data.get("teammate", "")).strip()
        if not teammate:
            raise ToolError("shutdown_request requires 'teammate'")
        return tm.request_shutdown(teammate)

    def _run_shutdown_status(self, input_data: dict[str, Any]) -> str:
        tm = self._ensure_team_manager()
        request_id = str(input_data.get("request_id", "")).strip()
        if not request_id:
            raise ToolError("shutdown_status requires 'request_id'")
        return tm.check_shutdown_status(request_id)

    def _run_plan_approval(self, input_data: dict[str, Any]) -> str:
        tm = self._ensure_team_manager()
        request_id = str(input_data.get("request_id", "")).strip()
        approve = bool(input_data.get("approve", False))
        feedback = str(input_data.get("feedback", "")).strip()
        if not request_id:
            raise ToolError("plan_approval requires 'request_id'")
        return tm.review_plan(request_id, approve, feedback)
