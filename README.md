# microclaw 🦀

A personal AI assistant CLI — a Python rewrite of [OpenClaw](https://github.com/ultraworkers/claw-code) — supporting **Anthropic**, **OpenAI**, **xAI**, **DashScope**, and **DeepSeek** providers.

## Quickstart

### Install from GitHub

```bash
pip install git+https://github.com/milo-ally/microclaw.git
```

### Install from source

```bash
# 1. Clone and enter the project
git clone https://github.com/milo-ally/microclaw.git
cd microclaw

# 2. Install
pip install -e .

# 3. Set your API credentials and run!
# ── Using DeepSeek (OpenAI-compatible) ──
export OPENAI_API_KEY=sk-your-key-here
microclaw --boot

# ── Using Anthropic (Claude) ──
export ANTHROPIC_API_KEY=sk-ant-your-key-here
microclaw --model claude-sonnet-4-20250514 --boot

# ── Using OpenAI (GPT) ──
export OPENAI_API_KEY=sk-your-key-here
microclaw --model gpt-4o --boot
```

> **First time?** Use `--boot` to start. Without it, microclaw resumes your latest session automatically.

### Windows

```powershell
# 1. Clone and enter the project
cd microclaw

# 2. Install
pip install -e .

# 3. Set your API credentials and run!
$env:OPENAI_API_KEY = "sk-your-key-here"
microclaw --boot
```

### One-shot prompts

```bash
# Ask a single question (non-interactive)
microclaw -p "explain this concept"

# JSON output for scripting
microclaw -p "list files" --output-format json

# With full tool access
microclaw --permission-mode danger-full-access -p "run the test suite"
```

## Features

- **Multi-provider**: Anthropic SDK + OpenAI SDK with automatic model routing
- **Credential-aware routing**: `deepseek-chat` auto-detects which provider to use based on your env vars
- **Bootstrap identity system**: Personality loaded from `~/.microclaw/` markdown files (IDENTITY.md, SOUL.md, USER.md, AGENTS.md, TOOLS.md)
- **Session persistence**: JSONL-based session save/resume — resumes latest session by default
- **Agent teams**: spawn persistent teammates with JSONL inbox communication, shutdown/plan-approval protocols
- **Task system**: persistent task tracking with status, priority, and dependencies
- **Skill system**: discover skills from `./skills` and `~/.microclaw/skills`, load on demand
- **Permission modes**: read-only, workspace-write (default), danger-full-access
- **Three-layer compaction**: micro-compact, auto-compact, manual compact for infinite sessions
- **Streaming** terminal output with markdown rendering

## Workspace & Bootstrap

MicroClaw stores its identity and configuration in `~/.microclaw/` (overridable via `--workspace` or `MICROCLAW_WORKSPACE`).

```
~/.microclaw/
├── IDENTITY.md       # Who am I? Name, nature, vibe, communication style
├── SOUL.md           # Persona, boundaries, tone
├── USER.md           # User profile + preferred address
├── AGENTS.md         # Operating instructions + memory system
├── TOOLS.md          # Available tools list + conventions
├── BOOTSTRAP.md      # First-run ritual (deleted after first use)
├── memory/
│   ├── MEMORY.md     # Long-term curated memory
│   └── logs/         # Daily notes YYYY-MM-DD.md
├── sessions/         # JSONL session persistence
├── skills/           # Global skill catalog
├── team/             # Agent team inbox/config
└── tasks/            # Task system storage
```

Run `microclaw setup` to initialize the workspace, or let it auto-init on first run.

## CLI Reference

| Flag | Description |
|---|---|
| `--boot`, `-b` | Start a fresh session (default: resume latest) |
| `--model`, `-m` | Model to use (default: deepseek-chat) |
| `--prompt`, `-p` | Run a single prompt and exit |
| `--workspace`, `-w` | Workspace directory (default: ~/.microclaw) |
| `--permission-mode` | Permission level: read-only, workspace-write, danger-full-access |
| `--resume`, `-r` | Resume a specific session by ID or path |
| `--no-stream` | Disable streaming output |
| `setup` | Initialize workspace at ~/.microclaw |
| `doctor` | Check configuration and diagnose issues |

## Built-in Tools

| Tool | Permission | Description |
|---|---|---|
| `bash` | danger-full-access | Execute a shell command |
| `read_file` | read-only | Read a text file |
| `write_file` | workspace-write | Write a file |
| `edit_file` | workspace-write | Replace text in a file |
| `web_fetch` | read-only | Fetch URL → readable text |
| `web_search` | read-only | Search the web |
| `compact` | read-only | Compress conversation history |
| `load_skill` | read-only | Load a skill by name |
| `task_create` | read-only | Create a task |
| `task_update` | read-only | Update task status/priority |
| `task_list` | read-only | List all tasks |
| `task_get` | read-only | Get task details |
| `background_run` | workspace-write | Run a background command |
| `check_background` | read-only | Check background status |
| `spawn_teammate` | workspace-write | Spawn a persistent agent |
| `list_teammates` | read-only | List all teammates |
| `send_message` | read-only | Send message to teammate |
| `read_inbox` | read-only | Read lead's inbox |
| `wait_teammate` | read-only | Wait for teammate reply |
| `broadcast` | read-only | Broadcast to all teammates |
| `shutdown_request` | read-only | Request teammate shutdown |
| `shutdown_status` | read-only | Check shutdown status |
| `plan_approval` | read-only | Approve/reject teammate plan |

## Slash Commands

| Command | Description |
|---|---|
| `/help` | Show available commands |
| `/status` | Show session status |
| `/model <name>` | Switch model |
| `/permissions <mode>` | Change permission mode |
| `/compact` | Compact conversation history |
| `/clear` | Clear session |
| `/cost` | Show token usage |
| `/tasks [cmd]` | Manage tasks (list, add, done, start, stop, update, get, delete, clear) |
| `/team [cmd]` | Manage agent team (spawn, shutdown, inbox, approve, reject, plans, cost) |
| `/skills [cmd]` | List or manage skills (use, drop, show, reload) |
| `/session` | List managed sessions |
| `/resume <ref>` | Resume a session |
| `/export [path]` | Export session to JSON |
| `/version` | Show version |
| `/exit`, `/quit` | Exit the REPL |

## Model Aliases

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

## Environment Variables

| Variable | Description |
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

## Architecture

```
src/microclaw/
├── __init__.py          # Version, DEFAULT_MODEL, CRAB_EMOJI
├── __main__.py          # python -m microclaw
├── bootstrap.py         # Workspace init, bootstrap markdown loading
├── cli.py               # CLI entry point, arg parsing, REPL
├── runtime.py           # ConversationRuntime — agentic loop
├── session.py           # Session persistence (JSONL)
├── compact.py           # Three-layer compaction
├── permissions.py       # Permission system
├── render.py            # Terminal rendering + streaming
├── skills.py            # Skill discovery and loading
├── providers/
│   ├── __init__.py      # Provider detection & routing
│   ├── anthropic.py     # Anthropic provider
│   ├── openai.py        # OpenAI/xAI/DashScope provider
│   └── types.py         # Shared message/event types
└── tools/
    ├── __init__.py      # Tool exports
    ├── registry.py      # Tool specs & registry
    ├── executor.py      # Tool dispatch & execution
    ├── bash.py          # Shell command execution
    ├── file.py          # File read/write/edit
    ├── web.py           # Web fetch & search
    ├── background.py    # Background command runner
    ├── tasks.py         # Persistent task system
    └── team.py          # Agent teams (MessageBus, TeamManager)
```
