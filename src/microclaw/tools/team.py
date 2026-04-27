"""Agent teams — autonomous teammates with JSONL inbox communication.

Adapted from learn-claude-code s09–s11.

Key concepts:
  Teammate: persistent named agent running its own LLM loop in a thread.
  MessageBus: JSONL append-only inbox per teammate (send → append, read → advance cursor).
  Protocols: shutdown_request / plan_approval with request_id correlation FSM.
  Autonomy: WORK → IDLE → poll (inbox + task board) → resume WORK or timeout → SHUTDOWN.
  Identity re-injection: after context compression, re-inject identity block.

Lifecycle:
  spawn -> WORKING -> IDLE -> poll -> WORKING -> ... -> SHUTDOWN
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from microclaw.providers import create_provider
from microclaw.providers.types import ContentBlock, Message, ToolDefinition, Usage
from microclaw.tools.bash import run_bash
from microclaw.tools.file import run_read_file, run_write_file, run_edit_file


# -- Constants --

DEFAULT_TEAM_ROOT = os.environ.get(
    "MICROCLAW_TEAM_DIR",
    os.path.expanduser("~/.microclaw/team"),
)

WORKDIR = Path.cwd()


def _team_dir_for_workspace(workspace: Path | None = None) -> Path:
    """Return the team directory for a given workspace, scoped by SHA1 hash."""
    ws = (workspace or WORKDIR).resolve()
    workspace_key = hashlib.sha1(str(ws).encode("utf-8")).hexdigest()[:12]
    team_dir = Path(DEFAULT_TEAM_ROOT) / workspace_key
    team_dir.mkdir(parents=True, exist_ok=True)
    return team_dir

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# Idle polling defaults (s11 autonomy)
POLL_INTERVAL = int(os.environ.get("MICROCLAW_POLL_INTERVAL", "5"))  # seconds
# 0 means "stay online indefinitely" until new work arrives or shutdown is requested.
IDLE_TIMEOUT = int(os.environ.get("MICROCLAW_IDLE_TIMEOUT", "0"))  # seconds
WAIT_TIMEOUT_MAX = int(os.environ.get("MICROCLAW_WAIT_TIMEOUT_MAX", "30"))  # seconds


# -- MessageBus: JSONL inbox per teammate --


class MessageBus:
    """Append-only JSONL inbox per teammate.

    Send appends a line. Read returns only unread lines and advances a persistent cursor
    without deleting the underlying inbox file.
    """

    def __init__(self, inbox_dir: Path | None = None, wake_callback: Any = None):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cursor_dir = self.dir / ".cursor"
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._wake_callback = wake_callback

    def _inbox_path(self, name: str) -> Path:
        return self.dir / f"{name}.jsonl"

    def _cursor_path(self, name: str) -> Path:
        return self.cursor_dir / f"{name}.offset"

    def _read_cursor(self, name: str) -> int:
        cursor_path = self._cursor_path(name)
        if not cursor_path.exists():
            return 0
        try:
            return max(0, int(cursor_path.read_text(encoding="utf-8").strip() or "0"))
        except (OSError, ValueError):
            return 0

    def _write_cursor(self, name: str, offset: int) -> None:
        self._cursor_path(name).write_text(str(max(0, offset)), encoding="utf-8")

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: dict[str, Any] | None = None,
    ) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg: dict[str, Any] = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self._inbox_path(to)
        with self._lock:
            with open(inbox_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        if self._wake_callback:
            self._wake_callback(to)
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list[dict[str, Any]]:
        inbox_path = self._inbox_path(name)
        if not inbox_path.exists():
            return []

        messages: list[dict[str, Any]] = []

        with self._lock:
            cursor = self._read_cursor(name)
            try:
                file_size = inbox_path.stat().st_size
            except OSError:
                return []
            if cursor > file_size:
                cursor = 0

            with open(inbox_path, "r", encoding="utf-8") as f:
                f.seek(cursor)
                for line in f:
                    if line.strip():
                        try:
                            messages.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                self._write_cursor(name, f.tell())
        return messages

    def broadcast(self, sender: str, content: str, teammates: list[str]) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# -- ProtocolTracker: request_id correlation for shutdown and plan approval --


class ProtocolTracker:
    """Track pending shutdown and plan approval requests by request_id.

    FSM: pending -> approved | rejected
    """

    def __init__(self) -> None:
        self.shutdown_requests: dict[str, dict[str, Any]] = {}
        self.plan_requests: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_shutdown_request(self, target: str) -> str:
        req_id = str(uuid.uuid4())[:8]
        with self._lock:
            self.shutdown_requests[req_id] = {"target": target, "status": "pending"}
        return req_id

    def resolve_shutdown(self, req_id: str, approve: bool) -> dict[str, Any] | None:
        with self._lock:
            req = self.shutdown_requests.get(req_id)
            if req is None:
                return None
            req["status"] = "approved" if approve else "rejected"
        return req

    def get_shutdown_status(self, req_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.shutdown_requests.get(req_id)

    def create_plan_request(self, sender: str, plan: str) -> str:
        req_id = str(uuid.uuid4())[:8]
        with self._lock:
            self.plan_requests[req_id] = {
                "from": sender,
                "plan": plan,
                "status": "pending",
            }
        return req_id

    def resolve_plan(self, req_id: str, approve: bool) -> dict[str, Any] | None:
        with self._lock:
            req = self.plan_requests.get(req_id)
            if req is None:
                return None
            req["status"] = "approved" if approve else "rejected"
        return req

    def get_plan_status(self, req_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.plan_requests.get(req_id)

    def all_pending_plans(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"request_id": rid, **req}
                for rid, req in self.plan_requests.items()
                if req["status"] == "pending"
            ]


# -- Teammate tool definitions (subset for teammate agent loop) --


def _teammate_tool_definitions() -> list[ToolDefinition]:
    """Tool definitions available to teammate agent loops."""
    return [
        ToolDefinition(
            name="bash",
            description="Run a shell command.",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="read_file",
            description="Read file contents.",
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
        ),
        ToolDefinition(
            name="write_file",
            description="Write content to file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="edit_file",
            description="Replace exact text in file.",
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
        ),
        ToolDefinition(
            name="send_message",
            description="Send message to a teammate's inbox.",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "content": {"type": "string"},
                    "msg_type": {
                        "type": "string",
                        "enum": sorted(VALID_MSG_TYPES),
                    },
                },
                "required": ["to", "content"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="read_inbox",
            description="Read and drain your inbox.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="shutdown_response",
            description="Respond to a shutdown request. Approve to shut down, reject to keep working.",
            input_schema={
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                    "approve": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["request_id", "approve"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="plan_approval",
            description="Submit a plan for lead approval before major work.",
            input_schema={
                "type": "object",
                "properties": {"plan": {"type": "string"}},
                "required": ["plan"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="idle",
            description="Signal that you have no more work right now. Enters idle polling phase where you will check for new messages and tasks.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="claim_task",
            description="Claim a task from the task board by ID.",
            input_schema={
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        ),
    ]


# -- Teammate tool execution --


def _execute_teammate_tool(
    sender: str,
    tool_name: str,
    args: dict[str, Any],
    bus: MessageBus,
    tracker: ProtocolTracker,
) -> str:
    """Execute a tool call from a teammate's agent loop.
    
    Returns a string. Dict results are JSON-serialized so the provider's
    message converter gets a string for tool role content (OpenAI API
    requirement). This mirrors ToolExecutor.execute() behavior.
    """
    # Base tools (all return dict -> serialize to JSON string)
    if tool_name == "bash":
        return json.dumps(run_bash(args), indent=2, ensure_ascii=False)
    if tool_name == "read_file":
        return json.dumps(run_read_file(args), indent=2, ensure_ascii=False)
    if tool_name == "write_file":
        return json.dumps(run_write_file(args), indent=2, ensure_ascii=False)
    if tool_name == "edit_file":
        return json.dumps(run_edit_file(args), indent=2, ensure_ascii=False)
    # Communication tools (already return strings)
    if tool_name == "send_message":
        return bus.send(
            sender,
            args["to"],
            args["content"],
            args.get("msg_type", "message"),
        )
    if tool_name == "read_inbox":
        msgs = bus.read_inbox(sender)
        return json.dumps(msgs, indent=2, ensure_ascii=False)
    # Protocol tools
    if tool_name == "shutdown_response":
        req_id = args["request_id"]
        approve = args["approve"]
        tracker.resolve_shutdown(req_id, approve)
        bus.send(
            sender,
            "lead",
            args.get("reason", ""),
            "shutdown_response",
            {"request_id": req_id, "approve": approve},
        )
        return f"Shutdown {'approved' if approve else 'rejected'}"
    if tool_name == "plan_approval":
        plan_text = args.get("plan", "")
        req_id = tracker.create_plan_request(sender, plan_text)
        bus.send(
            sender,
            "lead",
            plan_text,
            "plan_approval_response",
            {"request_id": req_id, "plan": plan_text},
        )
        return f"Plan submitted (request_id={req_id}). Waiting for lead approval."
    # Autonomy tools (s11)
    if tool_name == "idle":
        return "Entering idle phase. Will poll for new tasks and messages."
    if tool_name == "claim_task":
        return _claim_task(args["task_id"], sender)
    return f"Unknown tool: {tool_name}"


# -- Teammate agent loop (runs in a thread) --


# -- Task board scanning + claiming (s11 autonomy) --


def _scan_unclaimed_tasks() -> list[dict[str, Any]]:
    """Scan the task board for unclaimed (pending, no owner, no blockers) tasks."""
    from microclaw.tools.tasks import TaskSystem
    ts = TaskSystem()
    try:
        result = ts.list_tasks()
    except Exception:
        return []
    tasks = result.get("tasks", [])
    return [
        t for t in tasks
        if t.get("status") == "pending"
        and not t.get("owner")
        and not t.get("blockedBy")
    ]


def _claim_task(task_id: int, owner: str) -> str:
    """Claim a task from the task board by ID."""
    from microclaw.tools.tasks import TaskSystem
    ts = TaskSystem()
    try:
        result = ts.update_task({"task_id": task_id, "status": "in_progress", "owner": owner})
        return f"Claimed task #{task_id}"
    except Exception as e:
        return f"Error claiming task #{task_id}: {e}"


def _make_identity_block(name: str, role: str, team_name: str) -> Message:
    """Create an identity re-injection block (s11)."""
    return Message.user_text(
        f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>"
    )


def _teammate_loop(
    name: str,
    role: str,
    prompt: str,
    model: str,
    bus: MessageBus,
    tracker: ProtocolTracker,
    usage_dict: dict[str, Usage],
    usage_lock: threading.Lock,
    wake_event: threading.Event | None = None,
    log_callback: Any = None,
    status_callback: Any = None,
    team_name: str = "default",
) -> None:
    """Run a teammate's autonomous agent loop in its own thread.

    s11 lifecycle: WORK → IDLE → poll (inbox + task board) → resume or timeout → SHUTDOWN

    WORK phase: standard agentic loop (LLM → tools → repeat until no tool_use).
    IDLE phase: poll inbox and task board every POLL_INTERVAL for up to IDLE_TIMEOUT.
    """
    provider = create_provider(model)
    tools = _teammate_tool_definitions()
    sys_prompt = (
        f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
        f"Use send_message to communicate with teammates. "
        f"When lead sends you a new message or task, promptly acknowledge receipt with a short "
        f"send_message to lead before or while you begin working. "
        f"When you finish a unit of work or have a substantive finding, you must send_message "
        f"to lead with a concise update before you stop or idle. "
        f"Do not assume tool logs are a final report to lead. "
        f"Submit plans via plan_approval before major work. "
        f"Respond to shutdown_request with shutdown_response. "
        f"Use idle tool when you have no more work. You will auto-claim new tasks."
    )
    messages: list[Message] = [Message.user_text(prompt)]

    while True:
        # -- WORK PHASE: standard agent loop --
        if status_callback:
            status_callback(name, "working")
        should_exit = _work_phase(
            name, role, team_name, model, provider, tools, sys_prompt,
            messages, bus, tracker, usage_dict, usage_lock, log_callback,
        )
        if should_exit:
            return

        # -- IDLE PHASE: poll for inbox messages and unclaimed tasks --
        if status_callback:
            status_callback(name, "idle")
        resume = _idle_poll(name, messages, bus, wake_event=wake_event)
        if not resume:
            # Timeout → shutdown
            return

        # Identity re-injection if context was compressed
        if len(messages) <= 3:
            messages.insert(0, _make_identity_block(name, role, team_name))
            messages.insert(1, Message.assistant_text(f"I am {name}. Continuing."))


def _work_phase(
    name: str,
    role: str,
    team_name: str,
    model: str,
    provider: Any,
    tools: list[ToolDefinition],
    sys_prompt: str,
    messages: list[Message],
    bus: MessageBus,
    tracker: ProtocolTracker,
    usage_dict: dict[str, Usage],
    usage_lock: threading.Lock,
    log_callback: Any = None,
) -> bool:
    """Run the WORK phase of a teammate's loop. Returns True if should exit."""
    should_exit = False
    sent_update_to_lead = False
    acknowledged_lead = False
    lead_update_reminder_sent = False
    lead_ack_reminder_sent = False
    requires_lead_ack = False
    lead_task_active = False

    for _ in range(50):
        # Check inbox before each iteration
        inbox = bus.read_inbox(name)
        for msg in inbox:
            if msg.get("type") == "shutdown_request":
                should_exit = True
                return should_exit
            if msg.get("from") == "lead":
                requires_lead_ack = True
                lead_task_active = True
                acknowledged_lead = False
                sent_update_to_lead = False
                lead_ack_reminder_sent = False
                lead_update_reminder_sent = False
            messages.append(
                Message.user_text(f"<inbox>{json.dumps(msg, ensure_ascii=False)}</inbox>")
            )

        if should_exit:
            break

        if requires_lead_ack and not acknowledged_lead and not lead_ack_reminder_sent:
            messages.append(
                Message.user_text(
                    "<reminder>Lead assigned or asked you something. "
                    "Promptly acknowledge receipt with a short send_message to lead, "
                    "then continue the work.</reminder>"
                )
            )
            lead_ack_reminder_sent = True

        # LLM call
        try:
            assistant_message, turn_usage = provider.send_turn(
                messages=messages,
                tools=tools,
                system=sys_prompt,
                max_tokens=8000,
            )
        except Exception:
            return False  # Error → go to idle

        # Track usage
        with usage_lock:
            if name not in usage_dict:
                usage_dict[name] = Usage()
            usage_dict[name].accumulate(turn_usage)

        messages.append(assistant_message)

        # Check for tool uses
        pending = assistant_message.tool_uses()
        if not pending:
            if lead_task_active and not sent_update_to_lead and not lead_update_reminder_sent:
                messages.append(
                    Message.user_text(
                        "<reminder>You have not reported back to lead yet. "
                        "Send a concise update to lead with send_message before finishing.</reminder>"
                    )
                )
                lead_update_reminder_sent = True
                continue
            break  # No more tool calls → work done, go to idle

        # Execute tools
        idle_requested = False
        results: list[ContentBlock] = []
        for block in pending:
            tool_name = block.name or ""
            tool_input = block.input or {}

            if tool_name == "idle":
                if sent_update_to_lead:
                    idle_requested = True
                    output = "Entering idle phase. Will poll for new tasks."
                else:
                    output = (
                        "Error: Before idling, send a concise update to lead with send_message."
                    )
            else:
                try:
                    output = _execute_teammate_tool(name, tool_name, tool_input, bus, tracker)
                except Exception as e:
                    output = f"Error: {e}"

            if tool_name == "send_message" and str(tool_input.get("to", "")).strip() == "lead":
                if requires_lead_ack and not acknowledged_lead:
                    acknowledged_lead = True
                    lead_ack_reminder_sent = False
                else:
                    sent_update_to_lead = True
                    if lead_task_active:
                        lead_update_reminder_sent = False

            if log_callback:
                log_callback(name, tool_name, output)

            results.append(
                ContentBlock(
                    type="tool_result",
                    tool_use_id=block.id,
                    name=tool_name,
                    content=output,
                )
            )
            # If teammate approved shutdown, mark for exit
            if tool_name == "shutdown_response" and tool_input.get("approve"):
                should_exit = True

        messages.append(Message(role="tool", content=results))
        if idle_requested:
            break  # Idle requested → go to idle phase

    return should_exit


def _idle_poll(
    name: str,
    messages: list[Message],
    bus: MessageBus,
    wake_event: threading.Event | None = None,
) -> bool:
    """Poll for inbox messages and unclaimed tasks during idle phase.

    Returns True if work was found (should resume WORK phase),
    False if timeout (should SHUTDOWN).
    """
    poll_interval = max(POLL_INTERVAL, 1)
    polls = None if IDLE_TIMEOUT <= 0 else max(1, IDLE_TIMEOUT // poll_interval)
    count = 0
    while polls is None or count < polls:
        count += 1
        if wake_event:
            wake_event.wait(timeout=poll_interval)
            wake_event.clear()
        else:
            time.sleep(poll_interval)

        # Check inbox
        inbox = bus.read_inbox(name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    return False  # Shutdown → don't resume
                messages.append(
                    Message.user_text(f"<inbox>{json.dumps(msg, ensure_ascii=False)}</inbox>")
                )
            return True  # Got messages → resume work

        # Check task board
        unclaimed = _scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]
            result = _claim_task(task["id"], name)
            if result.startswith("Error"):
                continue
            task_prompt = (
                f"<auto-claimed>Task #{task['id']}: {task.get('subject', '')}\n"
                f"{task.get('description', '')}</auto-claimed>"
            )
            messages.append(Message.user_text(task_prompt))
            messages.append(Message.assistant_text(f"Claimed task #{task['id']}. Working on it."))
            return True  # Found work → resume

    return False  # Finite timeout elapsed → shutdown


# -- TeamManager: orchestrates teammates --


class TeamManager:
    """Manages persistent named teammates with lifecycle, communication, and protocols."""

    def __init__(self, model: str, workspace: Path | None = None, team_dir: Path | None = None) -> None:
        self.model = model
        self.team_dir = team_dir or _team_dir_for_workspace(workspace)
        self.team_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.team_dir / "config.json"
        self._config_lock = threading.Lock()
        self.config = self._load_config()
        self.wake_events: dict[str, threading.Event] = {}
        self.bus = MessageBus(self.team_dir / "inbox", wake_callback=self._wake_recipient)
        self.tracker = ProtocolTracker()
        self.threads: dict[str, threading.Thread] = {}
        self._teammate_usage: dict[str, Usage] = {}
        self._usage_lock = threading.Lock()
        self._followup_lock = threading.Lock()
        self._pending_followups: dict[str, dict[str, Any]] = {}
        self._lead_inbox_lock = threading.Lock()
        self._lead_inbox_buffer: list[dict[str, Any]] = []
        self._log_callback: Any = None
        self._reconcile_member_statuses()

    def set_log_callback(self, callback: Any) -> None:
        """Set a callback for teammate log messages: callback(name, tool_name, output)."""
        self._log_callback = callback

    def _load_config(self) -> dict[str, Any]:
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"team_name": "default", "members": []}

    def _save_config(self) -> None:
        self.config_path.write_text(
            json.dumps(self.config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _find_member(self, name: str) -> dict[str, Any] | None:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _reconcile_member_statuses(self) -> None:
        """Mark persisted teammates without a live thread as offline."""
        changed = False
        with self._config_lock:
            for member in self.config.get("members", []):
                if member.get("status") in ("working", "idle"):
                    member["status"] = "offline"
                    changed = True
            if changed:
                self._save_config()

    def _set_member_status(self, name: str, status: str) -> None:
        """Update one teammate status in the persisted roster."""
        with self._config_lock:
            member = self._find_member(name)
            if not member:
                return
            if member.get("status") == status:
                return
            member["status"] = status
            self._save_config()

    def _is_member_active(self, name: str) -> bool:
        thread = self.threads.get(name)
        return thread is not None and thread.is_alive()

    def _wake_recipient(self, recipient: str) -> None:
        event = self.wake_events.get(recipient)
        if event:
            event.set()

    def _sync_runtime_statuses(self) -> None:
        """Downgrade non-running teammates from working/idle to offline."""
        changed = False
        with self._config_lock:
            for member in self.config.get("members", []):
                if member.get("status") in ("working", "idle") and not self._is_member_active(member["name"]):
                    member["status"] = "offline"
                    changed = True
            if changed:
                self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """Spawn a persistent teammate that runs its own agent loop in a thread."""
        member = self._find_member(name)
        if member:
            if self._is_member_active(name):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        wake_event = threading.Event()
        self.wake_events[name] = wake_event

        thread = threading.Thread(
            target=self._run_teammate,
            args=(name, role, prompt, wake_event),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _run_teammate(self, name: str, role: str, prompt: str, wake_event: threading.Event) -> None:
        """Thread entry point: run the teammate loop and update status on exit."""
        _teammate_loop(
            name=name,
            role=role,
            prompt=prompt,
            model=self.model,
            bus=self.bus,
            tracker=self.tracker,
            usage_dict=self._teammate_usage,
            usage_lock=self._usage_lock,
            wake_event=wake_event,
            log_callback=self._log_callback,
            status_callback=self._set_member_status,
            team_name=self.config.get("team_name", "default"),
        )
        self.wake_events.pop(name, None)
        self._set_member_status(name, "shutdown")

    def list_all(self) -> str:
        """List all teammates with name, role, and status."""
        self._sync_runtime_statuses()
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list[str]:
        return [m["name"] for m in self.config["members"]]

    def _record_outbound_assignment(self, teammate: str, content: str) -> None:
        with self._followup_lock:
            self._pending_followups[teammate] = {
                "status": "awaiting_ack",
                "content": content[:240],
                "updated_at": time.time(),
            }

    def _update_followups_from_inbox(self, msgs: list[dict[str, Any]]) -> None:
        with self._followup_lock:
            for msg in msgs:
                sender = str(msg.get("from", "")).strip()
                if not sender or sender not in self._pending_followups:
                    continue

                entry = self._pending_followups[sender]
                content = str(msg.get("content", ""))
                entry["updated_at"] = time.time()
                entry["last_reply"] = content[:240]

                if entry.get("status") == "awaiting_ack":
                    if len(content.strip()) >= 160 or "\n" in content:
                        self._pending_followups.pop(sender, None)
                    else:
                        entry["status"] = "awaiting_report"
                else:
                    self._pending_followups.pop(sender, None)

    def coordination_prompt_block(self) -> str | None:
        """Return a compact prompt block describing outstanding teammate follow-ups."""
        with self._followup_lock:
            if not self._pending_followups:
                return None
            lines = [
                "Active teammate follow-ups:",
                "Treat these as asynchronous delegations. Do not assume failure just because one inbox read is empty.",
            ]
            for name, entry in self._pending_followups.items():
                status = entry.get("status", "unknown")
                content = entry.get("content", "")
                if status == "awaiting_ack":
                    lines.append(f"- {name}: awaiting acknowledgment for: {content}")
                else:
                    lines.append(f"- {name}: acknowledged; still awaiting substantive report for: {content}")
            lines.append(f"If you are truly blocked on one of these follow-ups, you may use wait_teammate with a bounded timeout up to {WAIT_TIMEOUT_MAX}s.")
            lines.append("Otherwise, prefer monitoring inbox/team status and doing non-overlapping work instead of immediately redoing delegated work yourself.")
            return "\n".join(lines)

    # -- Lead-side operations (called by the lead's tools) --

    def send_message(self, to: str, content: str, msg_type: str = "message") -> str:
        member = self._find_member(to)
        if not member:
            return f"Error: Unknown teammate '{to}'"
        if not self._is_member_active(to):
            self._set_member_status(to, "offline")
            return f"Error: Teammate '{to}' is offline. Respawn it with /team spawn {to} <role> --prompt <task>"
        if msg_type == "message":
            self._record_outbound_assignment(to, content)
        return self.bus.send("lead", to, content, msg_type)

    def read_lead_inbox(self) -> list[dict[str, Any]]:
        with self._lead_inbox_lock:
            msgs = list(self._lead_inbox_buffer)
            self._lead_inbox_buffer.clear()
            msgs.extend(self.bus.read_inbox("lead"))
        if msgs:
            self._update_followups_from_inbox(msgs)
        return msgs

    def wait_for_reply(self, teammate: str, timeout_seconds: int = 10) -> list[dict[str, Any]]:
        """Wait briefly for a specific teammate reply while preserving unrelated inbox messages."""
        member = self._find_member(teammate)
        if not member:
            raise ValueError(f"Unknown teammate '{teammate}'")

        timeout_seconds = max(1, min(int(timeout_seconds), max(1, WAIT_TIMEOUT_MAX)))
        deadline = time.time() + timeout_seconds
        matched: list[dict[str, Any]] = []

        while time.time() < deadline:
            with self._lead_inbox_lock:
                inbox = list(self._lead_inbox_buffer)
                self._lead_inbox_buffer.clear()
                inbox.extend(self.bus.read_inbox("lead"))

                for msg in inbox:
                    if str(msg.get("from", "")).strip() == teammate:
                        matched.append(msg)
                    else:
                        self._lead_inbox_buffer.append(msg)

            if matched:
                self._update_followups_from_inbox(matched)
                return matched

            if not self._is_member_active(teammate):
                self._set_member_status(teammate, "offline")
                break

            time.sleep(0.5)

        return matched

    def broadcast(self, content: str) -> str:
        return self.bus.broadcast("lead", content, self.member_names())

    def request_shutdown(self, teammate: str) -> str:
        member = self._find_member(teammate)
        if not member:
            return f"Error: Unknown teammate '{teammate}'"
        if not self._is_member_active(teammate):
            self._set_member_status(teammate, "offline")
            return f"Error: Teammate '{teammate}' is offline."
        req_id = self.tracker.create_shutdown_request(teammate)
        self.bus.send(
            "lead",
            teammate,
            "Please shut down gracefully.",
            "shutdown_request",
            {"request_id": req_id},
        )
        return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"

    def check_shutdown_status(self, request_id: str) -> str:
        req = self.tracker.get_shutdown_status(request_id)
        if not req:
            return f"Error: Unknown request_id '{request_id}'"
        return json.dumps(req, ensure_ascii=False)

    def review_plan(self, request_id: str, approve: bool, feedback: str = "") -> str:
        req = self.tracker.resolve_plan(request_id, approve)
        if not req:
            return f"Error: Unknown plan request_id '{request_id}'"
        self.bus.send(
            "lead",
            req["from"],
            feedback,
            "plan_approval_response",
            {"request_id": request_id, "approve": approve, "feedback": feedback},
        )
        return f"Plan {'approved' if approve else 'rejected'} for '{req['from']}'"

    def get_pending_plans(self) -> list[dict[str, Any]]:
        return self.tracker.all_pending_plans()

    # -- Token usage --

    def get_teammate_usage(self) -> dict[str, Usage]:
        """Return a copy of per-teammate usage stats."""
        with self._usage_lock:
            return dict(self._teammate_usage)

    def aggregate_usage(self) -> Usage:
        """Aggregate all teammate usage into a single Usage."""
        total = Usage()
        with self._usage_lock:
            for usage in self._teammate_usage.values():
                total.accumulate(usage)
        return total
