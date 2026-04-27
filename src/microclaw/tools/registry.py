"""Tool registry — definitions, specs, and lookup."""

from __future__ import annotations

from typing import Any

from microclaw.providers.types import ToolDefinition
from microclaw.permissions import PermissionMode


class ToolSpec:
    """Describes a tool's metadata and required permission level."""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        required_permission: PermissionMode = PermissionMode.READ_ONLY,
    ):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.required_permission = required_permission

    def to_tool_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )


def mvp_tool_specs() -> list[ToolSpec]:
    """Return the built-in tool specifications."""
    return [
        ToolSpec(
            name="bash",
            description="Execute a shell command in the current workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1},
                    "description": {"type": "string"},
                    "run_in_background": {"type": "boolean"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.DANGER_FULL_ACCESS,
        ),
        ToolSpec(
            name="read_file",
            description="Read a text file from the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="write_file",
            description="Write a text file in the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.WORKSPACE_WRITE,
        ),
        ToolSpec(
            name="edit_file",
            description="Replace text in a workspace file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.WORKSPACE_WRITE,
        ),
        ToolSpec(
            name="web_fetch",
            description="Fetch a URL and convert it into readable text.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "prompt": {"type": "string"},
                },
                "required": ["url", "prompt"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="web_search",
            description="Search the web for current information.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 2},
                    "allowed_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="load_skill",
            description="Load the full body of an available skill by name.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="compact",
            description="Trigger manual conversation compression. Use when the conversation is getting long or you want to focus on specific context.",
            input_schema={
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": "What to preserve in the summary (e.g. 'current task', 'file edits')",
                    },
                },
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="task_create",
            description="Create a new task with a subject and optional details.",
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "blockedBy": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "owner": {"type": "string"},
                },
                "required": ["subject"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="task_update",
            description="Update a task's status, dependencies, or other fields.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "minimum": 1},
                    "subject": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "addBlockedBy": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "removeBlockedBy": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "owner": {"type": "string"},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="task_list",
            description="List all tasks with status summary.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="task_get",
            description="Get full details of a task by ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "minimum": 1},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="background_run",
            description="Run command in background thread. Returns task_id immediately.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.WORKSPACE_WRITE,
        ),
        ToolSpec(
            name="check_background",
            description="Check background task status. Omit task_id to list all.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        # -- Team tools (s09: agent teams, s10: team protocols) --
        ToolSpec(
            name="spawn_teammate",
            description="Spawn a persistent teammate that runs its own LLM agent loop in a thread. Teammates communicate asynchronously via inboxes and should acknowledge and report back to lead.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique name for the teammate"},
                    "role": {"type": "string", "description": "Role description (e.g. coder, tester, reviewer)"},
                    "prompt": {"type": "string", "description": "Initial task/prompt for the teammate"},
                },
                "required": ["name", "role", "prompt"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.WORKSPACE_WRITE,
        ),
        ToolSpec(
            name="list_teammates",
            description="List all teammates with their name, role, and current status so you can monitor asynchronous progress.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="send_message",
            description="Send a message to a teammate's inbox. This is asynchronous: expect acknowledgment and follow-up later, not an immediate return value.",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Teammate name to send to"},
                    "content": {"type": "string", "description": "Message content"},
                    "msg_type": {
                        "type": "string",
                        "enum": sorted(["message", "broadcast", "shutdown_request", "shutdown_response", "plan_approval_response"]),
                        "description": "Message type (default: message)",
                    },
                },
                "required": ["to", "content"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="read_inbox",
            description="Read new messages for lead from the persistent inbox. Use this to collect teammate acknowledgments, reports, and protocol responses.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="wait_teammate",
            description="Wait briefly for a specific teammate reply when lead is blocked on a critical acknowledgment or report. Timeout is capped to avoid unbounded schedule slip.",
            input_schema={
                "type": "object",
                "properties": {
                    "teammate": {"type": "string", "description": "Teammate name to wait for"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "description": "Requested wait time in seconds; capped internally"},
                },
                "required": ["teammate"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="broadcast",
            description="Send a message to all teammates asynchronously.",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Message content to broadcast"},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="shutdown_request",
            description="Request a teammate to shut down gracefully. Returns a request_id for tracking.",
            input_schema={
                "type": "object",
                "properties": {
                    "teammate": {"type": "string", "description": "Name of the teammate to shut down"},
                },
                "required": ["teammate"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="shutdown_status",
            description="Check the status of a shutdown request by request_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "request_id": {"type": "string", "description": "The request_id returned by shutdown_request"},
                },
                "required": ["request_id"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
        ToolSpec(
            name="plan_approval",
            description="Approve or reject a teammate's plan. Provide request_id + approve + optional feedback.",
            input_schema={
                "type": "object",
                "properties": {
                    "request_id": {"type": "string", "description": "The plan request_id"},
                    "approve": {"type": "boolean", "description": "True to approve, False to reject"},
                    "feedback": {"type": "string", "description": "Optional feedback for the teammate"},
                },
                "required": ["request_id", "approve"],
                "additionalProperties": False,
            },
            required_permission=PermissionMode.READ_ONLY,
        ),
    ]


class ToolRegistry:
    """Manages available tools and their specs."""

    def __init__(self, allowed_tools: set[str] | None = None):
        self._specs: dict[str, ToolSpec] = {}
        self._allowed_tools = allowed_tools  # None means all allowed

        for spec in mvp_tool_specs():
            self._specs[spec.name] = spec

    def tool_definitions(self) -> list[ToolDefinition]:
        """Get tool definitions for API requests, filtered by allowed_tools."""
        result = []
        for name, spec in self._specs.items():
            if self._allowed_tools is not None and name not in self._allowed_tools:
                continue
            result.append(spec.to_tool_definition())
        return result

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def is_allowed(self, name: str) -> bool:
        if self._allowed_tools is None:
            return True
        return name in self._allowed_tools

    def allowed_tool_names(self) -> list[str]:
        names = []
        for name in self._specs:
            if self.is_allowed(name):
                names.append(name)
        return names

    def required_permission(self, name: str) -> PermissionMode:
        spec = self._specs.get(name)
        if spec:
            return spec.required_permission
        return PermissionMode.DANGER_FULL_ACCESS
