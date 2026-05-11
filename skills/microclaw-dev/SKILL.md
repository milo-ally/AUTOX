---
name: microclaw-dev
description: Guide for developing and extending microclaw — a personal AI assistant CLI with interaction gateway (web, queue, WeChat). Covers architecture, channels layer, tools, providers, skills, and bootstrap system.
tags: [microclaw, development, architecture, channels, web, wechat, tools, providers, bootstrap]
---

# MicroClaw Development Skill

You are an expert in the microclaw codebase. This skill provides architectural knowledge and practical guidance for extending microclaw itself — helping the agent understand its own structure and capabilities.

## Project Structure

```
src/microclaw/
├── __init__.py           # Version (0.1.0), DEFAULT_MODEL, CRAB_EMOJI
├── __main__.py           # python -m microclaw entry
├── bootstrap.py          # Workspace init, bootstrap markdown file loading
├── cli.py                # CLI entry, arg parsing, REPL, slash commands
├── runtime.py            # ConversationRuntime — core agentic loop
├── session.py            # Session persistence (JSONL)
├── permissions.py        # PermissionMode enum, PermissionPolicy
├── compact.py            # Three-layer compaction (micro, auto, manual)
├── render.py             # Rich terminal output, streaming, markdown
├── skills.py             # SkillLoader — discover/load SKILL.md files
├── providers/
│   ├── __init__.py       # Provider detection/routing, MODEL_ALIASES
│   ├── types.py          # Shared types: Message, ContentBlock, Usage, etc.
│   ├── anthropic.py      # Anthropic provider (Anthropic SDK)
│   └── openai.py         # OpenAI-compatible provider (OpenAI SDK)
├── tools/
│   ├── __init__.py       # Tool exports
│   ├── registry.py       # ToolSpec, ToolRegistry, mvp_tool_specs()
│   ├── executor.py       # ToolExecutor — dispatch & error handling
│   ├── bash.py           # Shell command execution
│   ├── file.py           # read_file / write_file / edit_file
│   ├── web.py            # web_fetch / web_search
│   ├── background.py     # BackgroundManager
│   ├── tasks.py          # Persistent TaskSystem
│   └── team.py           # Agent teams: TeamManager, MessageBus, ProtocolTracker
└── channels/             # Interaction layer (gateway above the core)
    ├── __init__.py       # Package exports: Channel, InboundMessage, OutboundEvent, TurnSink
    ├── base.py           # Core abstractions: Channel, InboundMessage, OutboundEvent, TurnSink
    ├── engine.py         # ChannelEngine — headless runtime host for channels
    ├── serve.py          # `microclaw serve` entry point + argparse
    ├── sinks.py          # StreamingTurnSink, BufferedTurnSink, ThrottledBufferedSink
    ├── wechat_cli.py     # `microclaw wechat` CLI (login/list/logout)
    └── transports/
        ├── __init__.py   # Package docstring
        ├── queue.py      # QueueChannel — local JSONL inbox/outbox
        ├── web.py        # WebChannel — FastAPI + SSE browser UI
        └── wechat.py     # WechatChannel — iLink long-poll + QR login + account store
```

## Bootstrap System

MicroClaw loads its identity and persona from workspace markdown files, not hardcoded Python. The workspace defaults to `~/.microclaw/` (overridable via `--workspace` or `MICROCLAW_WORKSPACE` env var).

### Bootstrap Files (injection order)

| File | Label | Purpose |
|---|---|---|
| `IDENTITY.md` | Identity | Who am I? Name, nature, vibe, communication style, core principles |
| `SOUL.md` | Persona & Boundaries | Persona traits, behavioral boundaries |
| `USER.md` | User Profile | User preferences, preferred address |
| `AGENTS.md` | Operating Instructions | Session startup ritual, memory system, safety rules, group chat etiquette |
| `TOOLS.md` | Tool Notes | Available tools list, tool-specific conventions |
| `BOOTSTRAP.md` | First-Run Ritual | One-time birth certificate — deleted after first run |

### Key Functions (in `bootstrap.py`)

- `resolve_workspace()` — resolve workspace path from CLI arg / env var / default
- `init_workspace()` — create workspace + default markdown files (never overwrites)
- `load_bootstrap_files()` — read all bootstrap .md files from workspace
- `build_bootstrap_prompt_blocks()` — format files into system prompt sections
- `consume_bootstrap()` — delete BOOTSTRAP.md after first-run ritual

### Workspace Layout

```
~/.microclaw/
├── IDENTITY.md
├── SOUL.md
├── USER.md
├── AGENTS.md
├── TOOLS.md
├── BOOTSTRAP.md          # (deleted after first run)
├── memory/
│   ├── MEMORY.md         # Long-term curated memory
│   └── logs/             # Daily notes YYYY-MM-DD.md
├── sessions/             # JSONL session persistence
├── skills/               # Global skill catalog
├── team/                 # Agent team inbox/config
└── tasks/                # Task system storage
```

## Complete Tool Inventory

All built-in tools are defined as `ToolSpec` objects in `tools/registry.py:mvp_tool_specs()` and dispatched in `tools/executor.py:_dispatch()`.

| Tool | Permission | Description |
|---|---|---|
| `bash` | DANGER_FULL_ACCESS | Execute a shell command |
| `read_file` | READ_ONLY | Read a text file with optional offset/limit |
| `write_file` | WORKSPACE_WRITE | Write a file |
| `edit_file` | WORKSPACE_WRITE | Replace text in a file |
| `web_fetch` | READ_ONLY | Fetch URL → readable text |
| `web_search` | READ_ONLY | Search the web |
| `load_skill` | READ_ONLY | Load skill body by name |
| `compact` | READ_ONLY | Trigger conversation compression |
| `task_create` | READ_ONLY | Create a task |
| `task_update` | READ_ONLY | Update task status/priority/dependencies |
| `task_list` | READ_ONLY | List all tasks |
| `task_get` | READ_ONLY | Get task details by ID |
| `background_run` | WORKSPACE_WRITE | Run shell command in background |
| `check_background` | READ_ONLY | Check background task status |
| `spawn_teammate` | WORKSPACE_WRITE | Spawn a persistent agent teammate |
| `list_teammates` | READ_ONLY | List all teammates |
| `send_message` | READ_ONLY | Send async message to a teammate |
| `read_inbox` | READ_ONLY | Read lead's inbox |
| `wait_teammate` | READ_ONLY | Wait for teammate reply |
| `broadcast` | READ_ONLY | Broadcast to all teammates |
| `shutdown_request` | READ_ONLY | Request graceful teammate shutdown |
| `shutdown_status` | READ_ONLY | Check shutdown request status |
| `plan_approval` | READ_ONLY | Approve/reject teammate plan |

## How to Add a New Tool

### 1. Create the implementation file

Create `src/microclaw/tools/my_tool.py`:

```python
"""My new tool — does something useful."""

from __future__ import annotations


def my_tool(path: str, option: bool = False) -> str:
    """Execute the tool logic. Return a string result."""
    result = f"processed {path}"
    return result
```

### 2. Register the spec in `tools/registry.py`

Add a `ToolSpec` to the `mvp_tool_specs()` function:

```python
ToolSpec(
    name="my_tool",
    description="Describe what the tool does.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "option": {"type": "boolean"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    required_permission=PermissionMode.READ_ONLY,
),
```

### 3. Wire up dispatch in `tools/executor.py`

Add an entry to the `dispatch_table` dict in `_dispatch()`:

```python
"my_tool": self._run_my_tool,
```

And add the handler method:

```python
def _run_my_tool(self, input_data: dict[str, Any]) -> str:
    from microclaw.tools.my_tool import my_tool
    return my_tool(**input_data)
```

### Permission Levels

- **READ_ONLY** (0): read_file, web_fetch, web_search, list_teammates, etc.
- **WORKSPACE_WRITE** (1): write_file, edit_file, spawn_teammate, background_run, etc.
- **DANGER_FULL_ACCESS** (2): bash

A PermissionMode allows any equal or lower level (2 ≥ 1 ≥ 0).

## Provider Routing & Model Aliases

Model routing in `providers/__init__.py` supports four provider kinds:

| ProviderKind | Model Prefixes | API Key Env Var | Default Base URL |
|---|---|---|---|
| ANTHROPIC | claude-, (deepseek* via ANTHROPIC_BASE_URL) | `ANTHROPIC_API_KEY` | Anthropic default |
| OPENAI | gpt-, gpt4, o1-, o3, o4, chatgpt, deepseek* | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| XAI | grok- | `XAI_API_KEY` | `https://api.x.ai/v1` |
| DASHSCOPE | qwen-, qwq- | `DASHSCOPE_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |

Model aliases (defined in `MODEL_ALIASES`):
- `sonnet` → `claude-sonnet-4-20250514`
- `opus` → `claude-opus-4-20250515`
- `haiku` → `claude-haiku-4-20250514`
- `4o` → `gpt-4o`, `4o-mini` → `gpt-4o-mini`
- `o3` → `o3`, `o4-mini` → `o4-mini`
- `grok` → `grok-3`, `grok-mini` → `grok-3-mini`
- `qwq` → `qwen-qwq-32b`
- `deepseek` → `deepseek-chat`

## Agent Teams

Teammates are persistent named agents running their own LLM loop in a daemon thread, communicating via JSONL inbox files under `~/.microclaw/team/<workspace-sha1>/`.

### Core Classes (in `tools/team.py`)

- **MessageBus** — JSONL append-only per-agent inbox
- **ProtocolTracker** — `request_id`-correlated FSM for shutdown/plan-approval
- **TeamManager** — Orchestrates spawn, shutdown, message routing, plan approval, token tracking
- **TeammateThread** — Wraps an LLM provider + tools loop in a daemon thread

## Task System

Tasks are stored as JSON files under `~/.microclaw/tasks/<workspace-id>/task_<id>.json`. The TaskSystem class in `tools/tasks.py` manages CRUD operations.

Task fields: `id`, `subject`, `description`, `status` (pending/in_progress/completed), `priority` (high/medium/low), `blockedBy`, `owner`.

## Agentic Loop (runtime.py)

`ConversationRuntime` drives the core loop:

1. Push user input as a message
2. Micro-compact old tool results (silently trim)
3. Auto-compact if estimated tokens > `THRESHOLD`
4. Send messages + tools to provider
5. Collect response (text + tool_use blocks)
6. If tool_use exists → execute each tool → add tool_result messages → loop
7. If no tool_use → return `TurnSummary`

Three-layer compaction (in `compact.py`):
- **Layer 1**: `micro_compact` — trim old tool results after each turn
- **Layer 2**: `auto_compact` — trigger when token estimate > THRESHOLD
- **Layer 3**: `manual_compact` — triggered by `compact` tool or `/compact` command

## Channels / Interaction Layer

The `channels/` package adds a thin gateway above the core runtime so non-TTY surfaces can drive `ConversationRuntime` without modifying the core.

### Architecture

```
Channel / Transport       (channels package)
    |
    v
ChannelEngine             (channels/engine.py — bridges runtime <-> TurnSink)
    |
    v
ConversationRuntime       (core — unchanged)
    |
    v
Tools / Skills            (core — unchanged)
```

### Key Abstractions (in `channels/base.py`)

- **InboundMessage** — dataclass with `text`, `user_id`, `account_id`, `session_key`, `reply_to`, `meta`
- **OutboundEvent** — normalized event (`OutboundKind` enum): `TURN_START`, `TEXT_DELTA`, `THINKING_DELTA`, `TOOL_START`, `TOOL_END`, `FINAL`, `ERROR`, `TURN_END`
- **TurnSink** (ABC) — receives streaming events for one agent turn; two flavours:
  - `StreamingTurnSink` — emits each delta as it arrives (web SSE, terminal)
  - `BufferedTurnSink` — accumulates and delivers once at `on_final` (WeChat, SMS)
  - `ThrottledBufferedSink` — buffered but emits partial flushes on a time budget
- **Channel** (ABC) — `serve()` yields `InboundMessage`s; `open_sink()` returns a `TurnSink`

### ChannelEngine (`channels/engine.py`)

Headless runtime host. Builds the same stack as `MicroclawCli` (session, provider, skills, tools, executor) but without terminal UI. Forwards runtime streaming events to the `TurnSink`.

- `ChannelDenyPrompter` — default permission prompter for channels: denies tool escalation (channels run unattended)
- One engine instance = one logical conversation (Session), reused across inbound messages

### Transports

| Transport | File | Streaming | Description |
|---|---|---|---|
| **web** | `transports/web.py` | Yes | FastAPI + SSE browser UI on configurable host:port (default `127.0.0.1:8787`). Endpoints: `GET /` (UI), `GET /api/health`, `POST /api/chat`, `GET /api/events` (SSE) |
| **queue** | `transports/queue.py` | Yes | Local JSONL `inbox.jsonl`/`outbox.jsonl` under `~/.microclaw/channels/queue/<instance>/`. Cursor-based polling. Good for tests and integrations |
| **wechat** | `transports/wechat.py` | No | WeChat iLink bot: long-poll `getupdates`, persist `get_updates_buf`, send replies via `sendmessage`. QR login flow, account store, throttled buffered replies |

### WeChat Account Store

Accounts saved under `~/.microclaw/channels/wechat/accounts/<id>.json` with an index at `accounts.json`. Functions: `save_wechat_account()`, `load_wechat_account()`, `list_wechat_accounts()`, `delete_wechat_account()`, `login_wechat_with_qr()`.

### How to Add a New Channel

1. Create `channels/transports/my_channel.py`
2. Subclass `Channel`: implement `serve()` (yield `InboundMessage`s) and `open_sink()` (return a `TurnSink`)
3. Set `name` and `supports_streaming` class attributes
4. Register in `channels/serve.py:_build_channel()` and add argparse flags
5. For non-streaming channels, return a `BufferedTurnSink` or `ThrottledBufferedSink` from `open_sink()`

## CLI Subcommands

| Command | Description |
|---|---|
| `microclaw` | Start interactive REPL |
| `microclaw setup` | Initialize workspace at ~/.microclaw |
| `microclaw doctor` | Check configuration and diagnose issues |
| `microclaw -p "prompt"` | Non-interactive single prompt mode |
| `microclaw serve` | Start the interaction gateway (default: web on 127.0.0.1:8787) |
| `microclaw serve --channel web` | FastAPI + SSE browser UI |
| `microclaw serve --channel queue` | Local JSONL inbox/outbox |
| `microclaw serve --channel wechat` | WeChat iLink gateway |
| `microclaw wechat login` | Scan QR to login WeChat |
| `microclaw wechat list` | List saved WeChat accounts |
| `microclaw wechat logout <id>` | Delete a saved WeChat account |

`microclaw serve` and `microclaw wechat` are dispatched in `cli.py:main()` before argparse — the interaction layer is intentionally isolated from the core REPL.

## Environment Variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `ANTHROPIC_BASE_URL` | Override Anthropic base URL |
| `OPENAI_API_KEY` | OpenAI / DeepSeek API key |
| `OPENAI_BASE_URL` | Override OpenAI base URL |
| `XAI_API_KEY` | xAI (Grok) API key |
| `XAI_BASE_URL` | Override xAI base URL |
| `DASHSCOPE_API_KEY` | DashScope (Qwen) API key |
| `DASHSCOPE_BASE_URL` | Override DashScope base URL |
| `MICROCLAW_MODEL` | Default model override |
| `MICROCLAW_WORKSPACE` | Override workspace directory (default: ~/.microclaw) |
| `MICROCLAW_SKILLS_DIR` | Override global skill directory |
| `MICROCLAW_TASK_DIR` | Override task storage directory |
| `MICROCLAW_TEAM_DIR` | Override team storage directory |
| `MICROCLAW_SESSION_DIR` | Override session storage directory |
| `MICROCLAW_WECHAT_BASE_URL` | Override WeChat iLink runtime base URL |
| `MICROCLAW_WECHAT_TOKEN` | Override WeChat bot token (normally created by `microclaw wechat login`) |
