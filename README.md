# microcode 🐍

An agentic coding assistant CLI — a Python rewrite of [claw-code](https://github.com/ultraworkers/claw-code) — supporting both **Anthropic** and **OpenAI** (including xAI, DashScope, DeepSeek) providers.

## Quickstart

### Install from GitHub

```bash
pip install git+https://github.com/milo-ally/microcode.git
```

### Install from source

```bash
# 1. Clone and enter the project
cd microcode

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
source .venv/bin/activate

# 4. Install microcode
pip install -e .

# 5. Set your API credentials and run!

# ── Using Anthropic-compatible endpoint (e.g. DeepSeek) ──
export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_API_KEY=sk-your-key-here
microcode --model deepseek-chat

# ── Using OpenAI-compatible endpoint (e.g. DeepSeek) ──
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_API_KEY=sk-your-key-here
microcode --model deepseek-chat

# ── Using native Anthropic (Claude) ──
export ANTHROPIC_API_KEY=sk-ant-your-key-here
microcode --model claude-sonnet-4-20250514

# ── Using native OpenAI (GPT) ──
export OPENAI_API_KEY=sk-your-key-here
microcode --model gpt-4o
```

### Windows

```powershell
# 1. Clone and enter the project
cd python-version

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
.venv\Scripts\Activate

# 4. Install microcode
pip install -e .

# 5. Set your API credentials and run!

# ── Using DeepSeek ──
$env:OPENAI_API_KEY = "sk-your-key-here"
$env:OPENAI_BASE_URL = "https://api.deepseek.com/v1"
microcode

# ── Using Anthropic (Claude) ──
$env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"
microcode --model claude-sonnet-4-20250514

# ── Using OpenAI (GPT) ──
$env:OPENAI_API_KEY = "sk-your-key-here"
microcode --model gpt-4o
```

### One-shot prompts

```bash
# Ask a single question (non-interactive)
microcode -p "explain this codebase"

# JSON output for scripting
microcode -p "list files" --output-format json

# With workspace file edits enabled
microcode --permission-mode workspace-write -p "track this task in TodoWrite and update README.md"

# With full tool access
microcode --permission-mode danger-full-access -p "fix the bug in main.py and run verification commands"
```

### Resume a session

```bash
microcode --resume latest
microcode --resume abc123def456
```

## Features

- **Multi-provider**: Anthropic SDK + OpenAI SDK with automatic model routing
- **Credential-aware routing**: `deepseek-chat` auto-detects which provider to use based on your env vars
- **Agentic loop**: Automatic tool-call → execute → respond cycle
- **Built-in tools**: bash, read_file, write_file, edit_file, glob_search, grep_search, WebFetch, WebSearch, load_skill, TodoWrite, compact, background_run
- **Agent teams**: spawn persistent teammates with JSONL inbox communication, shutdown/plan-approval protocols, per-agent token tracking
- **Persistent task tracking**: `TodoWrite` stores tasks under `~/.microcode/tasks/`, scoped by workspace
- **Skill system**: discover skills from `./skills` and `~/.microcode/skills`, then load them on demand with `load_skill`
- **Session persistence**: JSONL-based session save/resume
- **Permission modes**: read-only, workspace-write, danger-full-access
- **Interactive REPL** with slash commands (`/help`, `/status`, `/skills`, `/team`, `/compact`, `/model`, etc.)
- **Streaming** terminal output with markdown rendering

## TodoWrite Task System

`TodoWrite` is a persistent task manager, not just an in-memory todo acknowledgement.

- Tasks are stored under `~/.microcode/tasks/<workspace-id>/task_<id>.json`
- Task fields include `id`, `subject`, `description`, `status`, `priority`, `blockedBy`, and `owner`
- Supported statuses: `pending`, `in_progress`, `completed`
- Supported priorities: `high`, `medium`, `low`
- When a task is marked `completed`, its ID is automatically removed from other tasks' `blockedBy` lists
- `TodoWrite` requires `workspace-write` permissions because it writes task files
- Task storage can be overridden with `MICROCODE_TASK_DIR`
- The legacy `{"todos": [...]}` payload still works and is treated as a full task-list replacement

Supported `TodoWrite` actions:

| Action | Purpose |
|---|---|
| `list` | List all tasks with counts and summary lines |
| `get` | Fetch one task by `task_id` |
| `create` | Create a new task |
| `update` | Update task status, text, priority, owner, or dependencies |
| `delete` | Delete a task and remove it from dependents |
| `clear` | Remove all tasks for the current workspace from the task store |
| `replace` | Replace the full task list from a `todos` array |

Example payloads:

```json
{"action":"create","subject":"Refactor TodoWrite","description":"Upgrade it to a persistent task system","priority":"high"}
```

```json
{"action":"update","task_id":2,"status":"in_progress","addBlockedBy":[1]}
```

```json
{"action":"list"}
```

```json
{"todos":[{"id":"a","content":"Legacy task","status":"pending","priority":"low"}]}
```

## Agent Teams

microcode supports persistent named agents (teammates) that run their own LLM loop in a background thread, communicating via JSONL inboxes. Adapted from the `s09_agent_teams` and `s10_team_protocols` patterns.

### Core concepts

| Concept | Description |
|---|---|
| **Teammate** | Persistent named agent with its own LLM loop, running in a daemon thread |
| **MessageBus** | JSONL append-only inbox per agent — `send()` appends, `read_inbox()` drains |
| **Shutdown protocol** | Lead sends `shutdown_request` → teammate responds `shutdown_response` (approve/reject) |
| **Plan approval** | Teammate submits plan → lead approves/rejects via `plan_approval` |
| **ProtocolTracker** | `request_id`-correlated FSM: `pending → approved | rejected` |

### Teammate lifecycle

```
spawn → WORKING → IDLE → WORKING → ... → SHUTDOWN
```

### Communication flow

```
~/.microcode/team/<workspace-sha1>/
  config.json           ← team roster + statuses
  inbox/
    alice.jsonl         ← append-only, drain-on-read
    bob.jsonl
    lead.jsonl

  Lead ──send──→ alice.jsonl ──read──→ Alice's agent loop
  Alice ──send──→ lead.jsonl  ──read──→ Lead's next LLM turn
```

### Team tools (available to the lead agent)

| Tool | Permission | Description |
|---|---|---|
| `spawn_teammate` | workspace-write | Spawn a teammate with name, role, and prompt |
| `list_teammates` | read-only | List all teammates with status |
| `send_message` | read-only | Send a message to a teammate's inbox |
| `read_inbox` | read-only | Read and drain the lead's inbox |
| `broadcast` | read-only | Send a message to all teammates |
| `shutdown_request` | read-only | Request graceful shutdown of a teammate |
| `shutdown_status` | read-only | Check status of a shutdown request |
| `plan_approval` | read-only | Approve or reject a teammate's plan |

### `/team` slash commands

| Command | Description |
|---|---|
| `/team` | Show team roster and status |
| `/team spawn <name> <role> --prompt <task>` | Spawn a teammate |
| `/team shutdown <name>` | Request teammate graceful shutdown |
| `/team inbox` | Check lead's inbox for messages |
| `/team approve <req_id>` | Approve a pending plan request |
| `/team reject <req_id>` | Reject a pending plan request |
| `/team plans` | List all pending plan requests |
| `/team cost` | Show per-teammate token usage breakdown |

### Token tracking

Each teammate's LLM usage is tracked independently. Use `/team cost` to see the breakdown:

```
               Token Usage by Agent                
╭──────────┬────────────┬────────────┬────────────╮
│  Agent   │     Input  │    Output  │     Total  │
├──────────┼────────────┼────────────┼────────────┤
│  lead    │    12,340  │     2,100  │    14,440  │
│  alice   │     8,500  │     1,800  │    10,300  │
│  bob     │     6,200  │       900  │     7,100  │
│  ──────  │  ────────  │  ────────  │  ────────  │
│  Total   │    27,040  │     4,800  │    31,840  │
╰──────────┴────────────┴────────────┴────────────╯
```

`/cost` also includes teammate tokens in the total.

## Skill System

microcode supports the two-layer skill pattern from `s05_skill_loading.py`:

- Layer 1: compact skill metadata is injected into the system prompt
- Layer 2: full skill content is loaded on demand through the `load_skill` tool
- Workspace skills live under `./skills/**/skills.md`
- Global skills live under `~/.microcode/skills/**/skills.md`
- If a workspace skill and a global skill share the same name, the workspace skill wins

`SKILL.md` files may include YAML frontmatter:

```md
---
name: review
description: Review code changes carefully
tags:
  - code
  - review
---
Look for regressions first, then test gaps, then maintainability issues.
```

Available skill controls:

- `load_skill` tool: load a skill body by name during a model turn
- `/skills`: list discovered skills
- `/skills use <name>`: mark a skill active and inject its full body into the system prompt
- `/skills drop <name>`: remove an active skill from the system prompt
- `/skills show`: show active skills
- `/skills reload`: rescan local and global skill directories

Example layout:

```text
skills/
  review/
    SKILL.md
```

## Environment Variables

| Variable | Provider | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic | API key for Claude models |
| `ANTHROPIC_BASE_URL` | Anthropic | Override base URL (e.g. for DeepSeek) |
| `OPENAI_API_KEY` | OpenAI | API key for GPT / DeepSeek models |
| `OPENAI_BASE_URL` | OpenAI | Override base URL (e.g. for DeepSeek) |
| `XAI_API_KEY` | xAI | API key for Grok models |
| `XAI_BASE_URL` | xAI | Override base URL |
| `DASHSCOPE_API_KEY` | DashScope | API key for Qwen models |
| `DASHSCOPE_BASE_URL` | DashScope | Override base URL |
| `MICROCODE_MODEL` | All | Default model override |
| `MICROCODE_SKILLS_DIR` | All | Override global skill directory root (default: `~/.microcode/skills`) |
| `MICROCODE_TASK_DIR` | All | Override task storage root (default: `~/.microcode/tasks`) |
| `MICROCODE_TEAM_DIR` | All | Override team storage root (default: `~/.microcode/team`) |
| `MICROCODE_SESSION_DIR` | All | Override session storage directory (default: `~/.microcode/sessions`) |

## Model Aliases

Short names expand to full model IDs:

| Alias | Full Model |
|---|---|
| `sonnet` | `claude-sonnet-4-20250514` |
| `opus` | `claude-opus-4-20250515` |
| `haiku` | `claude-haiku-4-20250514` |
| `4o` | `gpt-4o` |
| `4o-mini` | `gpt-4o-mini` |
| `o3` | `o3` |
| `o4-mini` | `o4-mini` |
| `grok` | `grok-3` |
| `grok-mini` | `grok-3-mini` |
| `qwq` | `qwen-qwq-32b` |
| `deepseek` | `deepseek-chat` |

## Slash Commands

| Command | Description |
|---|---|
| `/help` | Show available commands |
| `/status` | Show session status |
| `/model <name>` | Switch model |
| `/permissions <mode>` | Change permission mode |
| `/compact` | Compact conversation history |
| `/clear` | Clear session |
| `/cost` | Show token usage and cost |
| `/history` | Show prompt history |
| `/skills [cmd]` | List or manage skills |
| `/team [cmd]` | Manage agent team (spawn, shutdown, inbox, approve, reject, plans, cost) |
| `/session` | List managed sessions |
| `/resume <ref>` | Resume a session |
| `/diff` | Show uncommitted changes |
| `/export [path]` | Export session to JSON |
| `/version` | Show version |
| `/exit`, `/quit` | Exit the REPL |

## Architecture

```
src/microcode/
├── __init__.py          # Package root
├── __main__.py          # python -m microcode
├── cli.py               # CLI entry point, arg parsing, REPL
├── skills.py            # Skill discovery, metadata, and full-body loading
├── providers/
│   ├── __init__.py      # Provider detection & routing
│   ├── anthropic.py     # Anthropic provider (anthropic SDK)
│   ├── openai.py        # OpenAI/xAI/DashScope (openai SDK)
│   └── types.py         # Shared message/event types
├── tools/
│   ├── __init__.py      # Tool exports
│   ├── registry.py      # Tool specs & registry
│   ├── executor.py      # Tool dispatch & execution
│   ├── bash.py          # Shell command execution
│   ├── file.py          # File read/write/edit
│   ├── search.py        # Glob & grep search
│   ├── tasks.py         # Persistent task system used by TodoWrite
│   ├── background.py    # Background command runner
│   ├── team.py          # Agent teams (MessageBus, TeamManager, ProtocolTracker)
│   └── web.py           # Web fetch & search
├── session.py           # Session persistence (JSONL)
├── runtime.py           # Conversation runtime (agentic loop)
├── permissions.py       # Permission system
└── render.py            # Terminal rendering
```
