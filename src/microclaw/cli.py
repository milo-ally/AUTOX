"""CLI entry point — argument parsing, REPL, and command dispatch."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from microclaw import __version__, DEFAULT_MODEL
from microclaw.permissions import PermissionMode
from microclaw.providers import create_provider, resolve_model_alias
from microclaw.providers.types import Usage
from microclaw.bootstrap import build_bootstrap_prompt_blocks, init_workspace, resolve_workspace
from microclaw.skills import SkillLoader
from microclaw.render import (
    Markdown,
    StreamingPrinter,
    console,
    print_banner,
    print_cost,
    print_help,
    print_status,
)
from microclaw.compact import compact_session, estimate_tokens
from microclaw.runtime import ConversationRuntime, RuntimeConfig
from microclaw.session import Session, new_session, load_session_by_reference, list_sessions
from microclaw.tools.executor import ToolExecutor
from microclaw.tools.registry import ToolRegistry
from microclaw.tools.tasks import TaskSystem
from microclaw.tools.team import TeamManager

try:
    from prompt_toolkit.completion import Completer, Completion
except ImportError:
    Completer = object  # type: ignore[assignment, misc]
    Completion = None  # type: ignore[assignment]


# -- Tab-completion for slash commands --
_SLASH_COMMANDS: dict[str, list[tuple[str, str]]] = {
    "/": [
        ("/help", "Show this help"),
        ("/status", "Show session status"),
        ("/model", "Switch LLM model"),
        ("/permissions", "Set permission mode"),
        ("/compact", "Compact conversation history"),
        ("/clear", "Clear session"),
        ("/cost", "Show token usage and cost"),
        ("/history", "Show prompt history"),
        ("/tasks", "Manage tasks"),
        ("/team", "Manage agent team"),
        ("/skills", "Manage skills"),
        ("/session", "List managed sessions"),
        ("/resume", "Resume a session"),
        ("/export", "Export session"),
        ("/version", "Show version"),
        ("/exit", "Exit the REPL"),
        ("/quit", "Exit the REPL"),
    ],
    "/tasks": [
        ("list", "List all tasks"),
        ("add", "Create a task"),
        ("done", "Mark task completed"),
        ("start", "Mark task in_progress"),
        ("stop", "Mark task pending"),
        ("update", "Update a task"),
        ("get", "Show task details"),
        ("delete", "Delete a task"),
        ("clear", "Delete ALL tasks"),
    ],
    "/team": [
        ("spawn", "Spawn a teammate"),
        ("shutdown", "Request teammate shutdown"),
        ("inbox", "Check lead inbox"),
        ("approve", "Approve a plan request"),
        ("reject", "Reject a plan request"),
        ("plans", "List pending plans"),
        ("cost", "Show per-teammate token usage"),
    ],
    "/skills": [
        ("list", "List available skills"),
        ("use", "Activate a skill"),
        ("drop", "Deactivate a skill"),
        ("reload", "Reload skill catalog"),
    ],
}

_SLASH_HELP: dict[str, str] = {
    "/help": "Show all available commands.",
    "/status": "Show current session status: model, session ID, message count.",
    "/model": (
        "/model              Show current model\n"
        "/model <name>       Switch to a different model\n"
        "  Examples: /model deepseek-v4-flash\n"
        "            /model gpt-4o"
    ),
    "/permissions": (
        "/permissions                    Show current permission mode\n"
        "/permissions <mode>            Set permission mode\n"
        "  Modes:  read-only  (default, safe)\n"
        "          workspace-write  (allow file writes)\n"
        "          danger-full-access  (allow everything)"
    ),
    "/compact": (
        "/compact            Compact conversation history\n"
        "/compact <focus>    Compact with a focus topic to preserve"
    ),
    "/clear": "Clear conversation history. Tasks are NOT cleared (use /tasks clear).",
    "/cost": "Show cumulative token usage and estimated cost for this session.",
    "/history": "Show the last 20 prompts from this session.",
    "/tasks": (
        "/tasks                      List all tasks with status summary\n"
        "/tasks add <subject>        Create a new task\n"
        "  Options: --status <pending|in_progress|completed>\n"
        "           --priority <high|medium|low>\n"
        "  Example: /tasks add Check disk usage --priority high\n"
        "/tasks done <id>            Mark task completed\n"
        "/tasks start <id>           Mark task in_progress\n"
        "/tasks stop <id>            Mark task pending\n"
        "/tasks update <id>          Update task fields\n"
        "  Options: --status, --priority\n"
        "  Example: /tasks update 3 --status in_progress --priority high\n"
        "/tasks get <id>             Show full task details\n"
        "/tasks delete <id>          Delete a task\n"
        "/tasks clear                Delete ALL tasks and reset ID counter to 1"
    ),
    "/skills": (
        "/skills                     List available skills\n"
        "/skills use <name>          Activate a skill\n"
        "/skills drop <name>         Deactivate a skill\n"
        "/skills reload              Reload skill catalog from disk"
    ),
    "/team": (
        "/team                       Show team roster and status\n"
        "/team spawn <name> <role>   Spawn a teammate (prompt via --prompt)\n"
        "  Example: /team spawn alice coder --prompt \"Fix the login bug\"\n"
        "/team shutdown <name>       Request a teammate to shut down gracefully\n"
        "/team inbox                 Check lead's inbox for messages\n"
        "/team approve <req_id>      Approve a pending plan request\n"
        "/team reject <req_id>       Reject a pending plan request\n"
        "/team plans                 List all pending plan requests\n"
        "/team cost                  Show per-teammate token usage breakdown"
    ),
    "/session": "List all managed sessions with ID, message count, and model.",
    "/resume": (
        "/resume <id>                Resume session by ID\n"
        "/resume latest              Resume the most recent session\n"
        "/resume <path>              Resume from a JSON file path"
    ),
    "/export": (
        "/export                     Export session to auto-named JSON file\n"
        "/export <path>              Export session to a specific file path"
    ),
    "/version": "Show microclaw version.",
    "/exit": "Exit the REPL.",
    "/quit": "Exit the REPL.",
}


class SlashCompleter(Completer):
    """Tab-completer for slash commands and their subcommands."""

    def get_completions(self, document: Any, complete_event: Any):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return

        parts = text.split()

        # First-level: completing the slash command itself
        if len(parts) <= 1:
            for cmd, desc in _SLASH_COMMANDS.get("/", []):
                if cmd.startswith(text):
                    yield Completion(
                        cmd,
                        start_position=-len(text),
                        display=cmd,
                        display_meta=desc,
                    )
            return

        # Second-level: completing subcommands for /tasks, /skills, etc.
        root = parts[0]
        if root in _SLASH_COMMANDS and len(parts) <= 2:
            subcmds = _SLASH_COMMANDS[root]
            partial = parts[1] if len(parts) == 2 else ""
            for sub, desc in subcmds:
                if sub.startswith(partial):
                    yield Completion(
                        sub,
                        start_position=-len(partial) if partial else 0,
                        display=f"{root} {sub}",
                        display_meta=desc,
                    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="microclaw",
        description="🦀 microclaw — personal AI assistant CLI (Python rewrite of OpenClaw)",
    )
    parser.add_argument("--version", action="version", version=f"microclaw {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    # setup subcommand
    setup_parser = subparsers.add_parser("setup", help="Initialize workspace at ~/.microclaw")
    setup_parser.add_argument("--workspace", default=None, help="Workspace directory (default: ~/.microclaw)")

    # doctor subcommand
    subparsers.add_parser("doctor", help="Check configuration and diagnose issues")

    # Default (REPL) arguments
    parser.add_argument(
        "--workspace", "-w",
        default=None,
        help="Workspace directory (default: ~/.microclaw)",
    )
    parser.add_argument(
        "--model", "-m",
        default=os.environ.get("MICROCLAW_MODEL", DEFAULT_MODEL),
        help="Model to use (default: %(default)s)",
    )
    parser.add_argument(
        "--prompt", "-p",
        default=None,
        help="Run a single prompt and exit (non-interactive mode)",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format for prompt mode (default: text)",
    )
    parser.add_argument(
        "--permission-mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="workspace-write",
        help="Permission mode for tool execution (default: workspace-write)",
    )
    parser.add_argument(
        "--allowed-tools",
        default=None,
        help="Comma-separated list of allowed tools (default: all)",
    )
    parser.add_argument(
        "--resume", "-r",
        default=None,
        help="Resume a session (session ID, 'latest', or path). Default: start fresh.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming output",
    )
    parser.add_argument(
        "--session-dir",
        default=None,
        help="Directory for session storage (default: ~/.microclaw/sessions)",
    )
    return parser.parse_args(argv)


class CliPermissionPrompter:
    """Interactive permission prompter for the CLI."""

    def __init__(self, mode: PermissionMode, pause_cb=None, resume_cb=None):
        self.mode = mode
        self.escalated = False  # Set to True when user picks "session"
        self._pause_cb = pause_cb
        self._resume_cb = resume_cb

    def ask(
        self,
        tool_name: str,
        tool_input: str,
        required: PermissionMode,
        current: PermissionMode,
    ) -> Any:
        """Ask the user for permission to execute a tool."""
        from microclaw.permissions import PermissionOutcome

        # Pause any Live display so input() works properly
        if self._pause_cb:
            self._pause_cb()

        console.print()
        console.print(
            f"  [yellow]⚠[/yellow] Tool [bold]{tool_name}[/bold] requires "
            f"[bold]{required.value}[/bold] permissions "
            f"(current: [dim]{current.value}[/dim])"
        )
        console.print(f"  [dim]Input:[/dim] {tool_input[:500]}{'...' if len(tool_input) > 500 else ''}")
        console.print()
        console.print("  [bold]1)[/bold] Allow once")
        console.print(f"  [bold]2)[/bold] Allow for this session (escalate to [bold]{required.value}[/bold])")
        console.print("  [bold]3)[/bold] Deny")

        try:
            response = input("  Choice [1/2/3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            if self._resume_cb:
                self._resume_cb()
            console.print("  [dim]Denied[/dim]")
            return PermissionOutcome.deny("user denied")

        # Resume Live display after input
        if self._resume_cb:
            self._resume_cb()

        if response in ("1", "y", "yes"):
            console.print("  [green]✓ Allowed once[/green]")
            return PermissionOutcome.allow()
        elif response in ("2", "a", "always", "session"):
            self.mode = required
            self.escalated = True
            console.print(f"  [green]✓ Permissions escalated to {required.value} for this session[/green]")
            return PermissionOutcome.allow()
        else:
            console.print("  [dim]Denied[/dim]")
            return PermissionOutcome.deny("user denied")


class MicroclawCli:
    """Main CLI controller — manages session, runtime, and REPL."""

    def __init__(
        self,
        model: str,
        permission_mode: PermissionMode,
        allowed_tools: set[str] | None = None,
        session: Session | None = None,
        no_stream: bool = False,
        workspace: str | None = None,
    ):
        self.model = resolve_model_alias(model)
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools
        self.no_stream = no_stream
        self.workspace = resolve_workspace(workspace)
        self._cumulative_usage = Usage()

        # Create or resume session
        if session:
            self.session = session
        else:
            self.session = new_session()
        self.session.model = self.model

        # Create provider
        self.provider = create_provider(self.model)

        # Load skills before creating prompt and executor
        self.skill_loader = SkillLoader()
        self.active_skills: list[str] = []

        # Create tool registry and executor
        self.tool_registry = ToolRegistry(allowed_tools=allowed_tools)
        self.tool_executor = ToolExecutor(
            allowed_tools=allowed_tools,
            skill_loader=self.skill_loader,
        )

        # Create team manager and wire it into the executor
        self.team_manager = TeamManager(model=self.model)
        self.team_manager.set_log_callback(self._teammate_log)
        self.tool_executor.set_team_manager(self.team_manager)

        # System prompt
        self.system_prompt = self._build_system_prompt()

    @staticmethod
    def _teammate_log(name: str, tool_name: str, output: str) -> None:
        """Log teammate tool execution to the console."""
        preview = str(output)[:300]
        console.print(f"  [dim][{name}] {tool_name}: {preview}{'...' if len(str(output)) > 300 else ''}[/dim]")

    def _build_system_prompt(self) -> list[str]:
        """Build the system prompt for the model.

        Identity, persona, and operating instructions are loaded from
        workspace bootstrap files (IDENTITY.md, SOUL.md, AGENTS.md, etc.)
        rather than hardcoded.  Only tool list and cwd are injected
        programmatically.
        """
        cwd = os.getcwd()
        tool_names = ", ".join(self.tool_registry.allowed_tool_names())
        prompt: list[str] = []

        # --- Bootstrap files (IDENTITY.md, SOUL.md, USER.md, AGENTS.md, TOOLS.md, BOOTSTRAP.md) ---
        bootstrap_blocks = build_bootstrap_prompt_blocks(self.workspace)
        if bootstrap_blocks:
            prompt.extend(bootstrap_blocks)
        else:
            # Fallback when no bootstrap files exist yet
            prompt.append("🦀 You are microclaw, a personal AI assistant (Python rewrite of OpenClaw).")

        # --- Programmatic context (always injected) ---
        prompt.append(f"Current working directory: {cwd}")
        prompt.append(f"You can use the following tools: {tool_names}.")
        prompt.append(
            "The compact tool compresses conversation history when it gets too long. "
            "Use it proactively when you notice the context is getting large."
        )
        prompt.append(
            "Skills are available for specialized workflows. Read the skill catalog below, "
            "and use the load_skill tool before tackling unfamiliar topics that match one of these skills."
        )
        prompt.append(f"Skills available:\n{self.skill_loader.get_descriptions_text(self.active_skills)}")

        for block in self.skill_loader.get_active_skill_prompt_blocks(self.active_skills):
            prompt.append(block)

        return prompt

    def _refresh_system_prompt(self) -> None:
        self.system_prompt = self._build_system_prompt()

    def _build_runtime(self) -> ConversationRuntime:
        """Build a fresh ConversationRuntime from current state."""
        config = RuntimeConfig(
            system_prompt=self.system_prompt,
            permission_mode=self.permission_mode,
            allowed_tools=self.allowed_tools,
            inbox_reader=self.team_manager.read_lead_inbox,
        )
        return ConversationRuntime(
            session=self.session,
            provider=self.provider,
            tool_executor=self.tool_executor,
            tool_registry=self.tool_registry,
            config=config,
        )

    def run_turn(self, user_input: str) -> None:
        """Run a single conversation turn with streaming output."""
        runtime = self._build_runtime()

        if self.no_stream:
            # Non-streaming mode
            prompter = CliPermissionPrompter(self.permission_mode)
            console.print("[dim]Thinking...[/dim]")
            try:
                summary = runtime.run_turn(user_input, prompter=prompter)
            except Exception as e:
                console.print(f"[bold red]✗ Error:[/bold red] {e}")
                return

            # Print the final assistant text
            for msg in summary.assistant_messages:
                text = msg.text_content()
                if text:
                    console.print(Markdown(text))
        else:
            # Streaming mode
            printer = StreamingPrinter()
            printer.start()
            prompter = CliPermissionPrompter(
                self.permission_mode,
                pause_cb=printer.pause,
                resume_cb=printer.resume,
            )

            def on_event(event_type: str, data: Any) -> None:
                if event_type == "text":
                    printer.append(data)
                elif event_type == "tool_execution_start":
                    printer.flush()
                    console.print(
                        f"\n[yellow]●[/yellow] [bold white]{data.get('name', 'tool')}[/bold white] "
                        f"[bright_black]running[/bright_black]"
                    )
                elif event_type == "tool_execution_end":
                    name = data.get("name", "tool")
                    result = data.get("result", "")
                    # Show a concise preview for human-readable results;
                    # skip JSON/structured output that starts with { or [
                    preview = ""
                    if result:
                        first_line = result.split("\n")[0].strip()[:300]
                        if first_line and not first_line.startswith(("{", "[")):
                            preview = f" [bright_black]{first_line}[/bright_black]"
                    if data.get("is_error"):
                        console.print(
                            f"[bold red]✗[/bold red] [bold white]{name}[/bold white] "
                            f"[red]failed[/red]{preview}"
                        )
                    else:
                        console.print(
                            f"[bold green]✓[/bold green] [bold white]{name}[/bold white] "
                            f"[green]done[/green]{preview}"
                        )

            try:
                summary = runtime.run_turn_streaming(
                    user_input,
                    on_event=on_event,
                    prompter=prompter,
                )
            except Exception as e:
                printer.finish()
                console.print(f"\n[bold red]✗ Error:[/bold red] {e}")
                return

            printer.finish()

        # Update session and usage from runtime
        self.session = runtime.session
        self._cumulative_usage.accumulate(runtime._cumulative_usage)
        # Propagate permission escalation
        if prompter.escalated:
            self.permission_mode = prompter.mode
        self.session.save()

    def run_prompt_mode(self, user_input: str, output_format: str = "text") -> None:
        """Run a single prompt and exit."""
        runtime = self._build_runtime()
        prompter = CliPermissionPrompter(self.permission_mode)

        try:
            summary = runtime.run_turn(user_input, prompter=prompter)
        except Exception as e:
            if output_format == "json":
                print(json.dumps({"error": str(e)}))
            else:
                console.print(f"[bold red]✗ Error:[/bold red] {e}")
            return

        self.session = runtime.session
        self._cumulative_usage.accumulate(runtime._cumulative_usage)
        self.session.save()

        if output_format == "json":
            final_text = ""
            for msg in summary.assistant_messages:
                text = msg.text_content()
                if text:
                    final_text += text
            print(json.dumps({
                "message": final_text,
                "model": self.model,
                "iterations": summary.iterations,
                "usage": {
                    "input_tokens": summary.usage.input_tokens,
                    "output_tokens": summary.usage.output_tokens,
                },
            }, ensure_ascii=False))
        else:
            for msg in summary.assistant_messages:
                text = msg.text_content()
                if text:
                    console.print(Markdown(text))

    def run_repl(self) -> None:
        """Run the interactive REPL."""
        print_banner(self.model, str(self.permission_mode), self.session.session_id)

        # Show session context info
        if self.session.messages:
            console.print(f"[dim]Resumed session {self.session.session_id[:12]} ({len(self.session.messages)} messages)[/dim]")
        else:
            console.print(f"[dim]Fresh session. Use --resume latest to continue a previous session.[/dim]")

        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.formatted_text import HTML
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.styles import Style

            history_file = os.path.expanduser("~/.microclaw/repl_history")
            os.makedirs(os.path.dirname(history_file), exist_ok=True)
            prompt_session = PromptSession(
                history=FileHistory(history_file),
                completer=SlashCompleter(),
                complete_while_typing=True,
            )
            prompt_style = Style.from_dict({
                "": "bg:#20262d #eef3f8",
                "bottom-toolbar": "bg:#13181d #6e8397",
                "prompt": "bg:#20262d #eef3f8",
                "promptmarker": "bg:#20262d #ffd866 bold",
                "promptplaceholder": "bg:#20262d #6f8598 italic",
                "prompttoolbar": "bg:#13181d #6e8397",
            })
            prompt_message = HTML('<prompt><promptmarker>› </promptmarker></prompt>')
            prompt_placeholder = HTML(
                '<promptplaceholder>🦀 Ask microclaw anything…</promptplaceholder>'
            )
            prompt_toolbar = HTML(
                '<prompttoolbar>  🦀 microclaw  •  Enter send  •  /help  •  /new  •  /status  </prompttoolbar>'
            )
        except ImportError:
            prompt_session = None
            prompt_style = None
            prompt_message = None
            prompt_placeholder = None
            prompt_toolbar = None

        while True:
            try:
                if prompt_session:
                    user_input = prompt_session.prompt(
                        prompt_message,
                        placeholder=prompt_placeholder,
                        bottom_toolbar=prompt_toolbar,
                        style=prompt_style,
                        wrap_lines=False,
                    )
                else:
                    user_input = input("> ")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Bye![/dim]")
                break

            trimmed = user_input.strip()
            if not trimmed:
                continue

            # Slash commands
            if trimmed.startswith("/"):
                should_exit = self._handle_slash_command(trimmed)
                if should_exit:
                    break
                continue

            # Regular input
            self.session.push_prompt_entry(trimmed)
            self.run_turn(trimmed)

    def _handle_slash_command(self, input_str: str) -> bool:
        """Handle a slash command. Returns True if the REPL should exit."""
        parts = input_str.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Handle --help for any slash command
        if args.strip() == "--help":
            help_text = _SLASH_HELP.get(command)
            if help_text:
                console.print(f"[bold]{command}[/bold]\n{help_text}")
            else:
                console.print(f"[dim]No detailed help for {command}.[/dim]")
            return False

        if command in ("/exit", "/quit"):
            self.session.save()
            console.print("[dim]Bye![/dim]")
            return True
        elif command == "/help":
            print_help()
        elif command == "/status":
            print_status(
                self.model,
                self.session.session_id,
                len(self.session.messages),
                self._cumulative_usage,
            )
        elif command == "/model":
            if not args:
                console.print(f"Current model: {self.model}")
            else:
                new_model = resolve_model_alias(args.strip())
                self.model = new_model
                self.provider = create_provider(new_model)
                self.session.model = new_model
                self.team_manager.model = new_model
                console.print(f"[green]Model set to {new_model}[/green]")
        elif command == "/permissions":
            if not args:
                console.print(f"Current permissions: {self.permission_mode}")
            else:
                try:
                    self.permission_mode = PermissionMode.from_str(args.strip())
                    console.print(f"[green]Permissions set to {self.permission_mode}[/green]")
                except ValueError as e:
                    console.print(f"[red]{e}[/red]")
        elif command == "/compact":
            self._do_compact(args.strip())
        elif command == "/clear":
            self.session = new_session()
            self.session.model = self.model
            console.print("[green]Session cleared.[/green]")
        elif command == "/cost":
            team_usage = self.team_manager.aggregate_usage()
            combined = Usage()
            combined.accumulate(self._cumulative_usage)
            combined.accumulate(team_usage)
            print_cost(combined, self.model)
            if team_usage.total_tokens() > 0:
                console.print(f"  [dim](includes {team_usage.total_tokens():,} teammate tokens)[/dim]")
        elif command == "/history":
            for entry in self.session.prompt_history[-20:]:
                console.print(f"  [dim]{entry.text}[/dim]")
        elif command == "/skills":
            self._handle_skill_command(args.strip())
        elif command == "/session":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No managed sessions.[/dim]")
            else:
                for s in sessions:
                    console.print(
                        f"  [dim]{s['id']}[/dim]  "
                        f"msgs={s['message_count']}  "
                        f"model={s.get('model', '?')}"
                    )
        elif command == "/resume":
            if not args:
                console.print("[dim]Usage: /resume <session-id|latest|path>[/dim]")
            else:
                try:
                    session = load_session_by_reference(args.strip())
                    self.session = session
                    self.model = session.model or self.model
                    self.provider = create_provider(self.model)
                    console.print(f"[green]Resumed session {session.session_id}[/green]")
                except FileNotFoundError as e:
                    console.print(f"[red]{e}[/red]")
        elif command == "/export":
            path = args.strip() or f"microclaw-export-{self.session.session_id}.json"
            self._export_session(path)
        elif command == "/tasks":
            self._handle_tasks_command(args.strip())
        elif command == "/team":
            self._handle_team_command(args.strip())
        elif command == "/version":
            console.print(f"microclaw {__version__}")
        else:
            console.print(f"[red]Unknown command: {command}[/red]")

        return False

    def _handle_skill_command(self, args: str) -> None:
        """Handle skill management commands."""
        parts = args.split()
        subcommand = parts[0].lower() if parts else "list"
        rest = parts[1:]

        if subcommand == "--help":
            console.print(f"[bold]/skills[/bold]\n{_SLASH_HELP['/skills']}")
            return

        if subcommand in ("list", "ls"):
            self._print_skills()
            return

        if subcommand in ("show", "active"):
            if not self.active_skills:
                console.print("[dim]No active skills.[/dim]")
            else:
                console.print(f"[green]Active skills:[/green] {', '.join(self.active_skills)}")
            return

        if subcommand == "reload":
            self.skill_loader.reload()
            self.active_skills = [name for name in self.active_skills if self.skill_loader.get(name)]
            self._refresh_system_prompt()
            console.print(f"[green]Reloaded {len(self.skill_loader.available_names())} skills.[/green]")
            return

        if subcommand in ("use", "enable", "add"):
            if not rest:
                console.print("[dim]Usage: /skills use <name>[/dim]")
                return
            name = rest[0]
            if not self.skill_loader.get(name):
                console.print(f"[red]Unknown skill: {name}[/red]")
                return
            if name not in self.active_skills:
                self.active_skills.append(name)
                self._refresh_system_prompt()
            console.print(f"[green]Skill activated:[/green] {name}")
            return

        if subcommand in ("drop", "disable", "remove", "rm"):
            if not rest:
                console.print("[dim]Usage: /skills drop <name>[/dim]")
                return
            name = rest[0]
            if name in self.active_skills:
                self.active_skills.remove(name)
                self._refresh_system_prompt()
                console.print(f"[green]Skill deactivated:[/green] {name}")
            else:
                console.print(f"[dim]Skill not active:[/dim] {name}")
            return

        console.print("[dim]Usage: /skills [list|show|reload|use <name>|drop <name>][/dim]")

    def _handle_team_command(self, args: str) -> None:
        """Handle /team slash command for agent team management."""
        parts = args.split()
        subcommand = parts[0].lower() if parts else "list"
        tm = self.team_manager

        if subcommand == "--help":
            console.print(f"[bold]/team[/bold]\n{_SLASH_HELP['/team']}")
            return

        if subcommand in ("list", "ls", ""):
            roster = tm.list_all()
            if roster == "No teammates.":
                console.print("[dim]No teammates. Use /team spawn or the spawn_teammate tool.[/dim]")
            else:
                console.print(roster)
            return

        if subcommand == "spawn":
            if len(parts) < 3:
                console.print("[dim]Usage: /team spawn <name> <role> [--prompt <prompt>][/dim]")
                return
            name = parts[1]
            role = parts[2]
            # Parse --prompt flag
            prompt = ""
            skip_next = False
            for i, p in enumerate(parts[3:], start=3):
                if skip_next:
                    skip_next = False
                    continue
                if p == "--prompt" and i + 1 < len(parts):
                    prompt = parts[i + 1]
                    skip_next = True
                else:
                    if prompt:
                        prompt += " " + p
                    else:
                        prompt = p
            if not prompt:
                console.print("[dim]Usage: /team spawn <name> <role> --prompt <prompt>[/dim]")
                return
            result = tm.spawn(name, role, prompt)
            console.print(f"[green]{result}[/green]")
            return

        if subcommand == "shutdown":
            if len(parts) < 2:
                console.print("[dim]Usage: /team shutdown <teammate>[/dim]")
                return
            teammate = parts[1]
            result = tm.request_shutdown(teammate)
            console.print(result)
            return

        if subcommand == "inbox":
            msgs = tm.read_lead_inbox()
            if not msgs:
                console.print("[dim]Inbox is empty.[/dim]")
            else:
                for msg in msgs:
                    sender = msg.get("from", "?")
                    content = msg.get("content", "")
                    msg_type = msg.get("type", "message")
                    console.print(f"  [bold][{msg_type}][/bold] from [yellow]{sender}[/yellow]: {content[:500]}{'...' if len(content) > 500 else ''}")
            return

        if subcommand == "approve":
            if len(parts) < 2:
                console.print("[dim]Usage: /team approve <request_id>[/dim]")
                return
            req_id = parts[1]
            feedback = " ".join(parts[2:]) if len(parts) > 2 else ""
            result = tm.review_plan(req_id, True, feedback)
            console.print(f"[green]{result}[/green]")
            return

        if subcommand == "reject":
            if len(parts) < 2:
                console.print("[dim]Usage: /team reject <request_id>[/dim]")
                return
            req_id = parts[1]
            feedback = " ".join(parts[2:]) if len(parts) > 2 else ""
            result = tm.review_plan(req_id, False, feedback)
            console.print(f"[yellow]{result}[/yellow]")
            return

        if subcommand == "plans":
            pending = tm.get_pending_plans()
            if not pending:
                console.print("[dim]No pending plan requests.[/dim]")
            else:
                for p in pending:
                    req_id = p.get("request_id", "?")
                    sender = p.get("from", "?")
                    plan = p.get("plan", "")[:300]
                    console.print(f"  [bold]{req_id}[/bold] from [yellow]{sender}[/yellow]: {plan}{'...' if len(p.get('plan', '')) > 300 else ''}")
            return

        if subcommand == "cost":
            from microclaw.render import print_team_cost
            print_team_cost(tm.get_teammate_usage(), self._cumulative_usage, self.model)
            return

        console.print("[dim]Usage: /team [list|spawn|shutdown|inbox|approve|reject|plans|cost][/dim]")

    def _handle_tasks_command(self, args: str) -> None:
        """Handle /tasks slash command for task CRUD."""
        parts = args.split()
        subcommand = parts[0].lower() if parts else "list"
        ts = TaskSystem()

        if subcommand == "--help":
            console.print(f"[bold]/tasks[/bold]\n{_SLASH_HELP['/tasks']}")
            return

        if subcommand in ("list", "ls", ""):
            result = ts.list_tasks()
            summary = result.get("summary", [])
            if not summary:
                console.print("[dim]No tasks.[/dim]")
            else:
                counts = result.get("counts", {})
                header = f"Tasks: {counts.get('pending', 0)} pending, {counts.get('in_progress', 0)} in_progress, {counts.get('completed', 0)} completed"
                console.print(f"[bold]{header}[/bold]")
                for line in summary:
                    if line.startswith("[x]"):
                        console.print(f"  [green]{line}[/green]")
                    elif line.startswith("[>]"):
                        console.print(f"  [yellow]{line}[/yellow]")
                    else:
                        console.print(f"  {line}")
            return

        if subcommand in ("add", "create", "new"):
            if len(parts) < 2:
                console.print("[dim]Usage: /tasks add <subject> [--status pending|in_progress|completed] [--priority high|medium|low][/dim]")
                return
            subject = []
            status = "pending"
            priority = "medium"
            skip_next = False
            for i, p in enumerate(parts[1:], start=1):
                if skip_next:
                    skip_next = False
                    continue
                if p == "--status" and i + 1 < len(parts):
                    status = parts[i + 1]
                    skip_next = True
                elif p == "--priority" and i + 1 < len(parts):
                    priority = parts[i + 1]
                    skip_next = True
                else:
                    subject.append(p)
            if not subject:
                console.print("[red]Subject is required.[/red]")
                return
            result = ts.create_task({
                "subject": " ".join(subject),
                "status": status,
                "priority": priority,
            })
            task = result.get("task", {})
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(task.get("status"), "[?]")
            console.print(f"[green]Created:[/green] {marker} #{task['id']}: {task['subject']}")
            return

        if subcommand in ("update", "done", "start", "stop"):
            if len(parts) < 2:
                console.print("[dim]Usage: /tasks update <id> [--status pending|in_progress|completed] [--priority high|medium|low][/dim]")
                return
            try:
                task_id = int(parts[1])
            except ValueError:
                console.print("[red]Task ID must be an integer.[/red]")
                return
            update_data = {"task_id": task_id}
            if subcommand == "done":
                update_data["status"] = "completed"
            elif subcommand == "start":
                update_data["status"] = "in_progress"
            elif subcommand == "stop":
                update_data["status"] = "pending"
            else:
                skip_next = False
                for i, p in enumerate(parts[2:], start=2):
                    if skip_next:
                        skip_next = False
                        continue
                    if p == "--status" and i + 1 < len(parts):
                        update_data["status"] = parts[i + 1]
                        skip_next = True
                    elif p == "--priority" and i + 1 < len(parts):
                        update_data["priority"] = parts[i + 1]
                        skip_next = True
            result = ts.update_task(update_data)
            task = result.get("task", {})
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(task.get("status"), "[?]")
            console.print(f"[green]Updated:[/green] {marker} #{task['id']}: {task['subject']}")
            unblocked = result.get("unblocked_task_ids", [])
            if unblocked:
                console.print(f"  [yellow]Unblocked tasks: {unblocked}[/yellow]")
            return

        if subcommand in ("get", "show", "info"):
            if len(parts) < 2:
                console.print("[dim]Usage: /tasks get <id>[/dim]")
                return
            try:
                task_id = int(parts[1])
            except ValueError:
                console.print("[red]Task ID must be an integer.[/red]")
                return
            result = ts.get_task(task_id)
            task = result.get("task", {})
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(task.get("status"), "[?]")
            console.print(f"[bold]{marker} #{task['id']}: {task['subject']}[/bold]")
            if task.get("description"):
                console.print(f"  description: {task['description']}")
            console.print(f"  priority: {task.get('priority', 'medium')}")
            if task.get("blockedBy"):
                console.print(f"  blocked by: {task['blockedBy']}")
            if task.get("owner"):
                console.print(f"  owner: {task['owner']}")
            return

        if subcommand in ("delete", "rm", "del", "remove"):
            if len(parts) < 2:
                console.print("[dim]Usage: /tasks delete <id>[/dim]")
                return
            try:
                task_id = int(parts[1])
            except ValueError:
                console.print("[red]Task ID must be an integer.[/red]")
                return
            result = ts.delete_task(task_id)
            console.print(f"[green]Deleted task #{task_id}[/green]")
            return

        if subcommand == "clear":
            result = ts.clear_tasks()
            console.print(f"[green]Cleared {result.get('tasks_cleared', 0)} tasks.[/green]")
            return

        console.print("[dim]Usage: /tasks [list|add <subject>|update <id>|get <id>|delete <id>|done <id>|start <id>|clear][/dim]")

    def _print_skills(self) -> None:
        """Render the available skill catalog."""
        from rich.table import Table

        names = self.skill_loader.available_names()
        if not names:
            console.print("[dim]No skills found. Add SKILL.md files under ./skills or ~/.microclaw/skills.[/dim]")
            return

        table = Table(title="Skills", box=None, padding=(0, 2))
        table.add_column("Name", style="bold yellow")
        table.add_column("Source", style="white")
        table.add_column("Active", style="white")
        table.add_column("Description", style="white")

        for name in names:
            skill = self.skill_loader.get(name)
            if skill is None:
                continue
            table.add_row(
                skill.name,
                skill.source,
                "yes" if name in self.active_skills else "",
                skill.description or "[dim]No description[/dim]",
            )

        console.print(table)

    def _do_compact(self, focus: str = "") -> None:
        """Compact the conversation history."""
        msg_count = len(self.session.messages)
        token_est = estimate_tokens(self.session.messages)
        console.print(
            f"  [dim]Before:[/dim] {msg_count} messages, ~{token_est:,} tokens"
        )

        compaction = compact_session(
            self.session,
            provider=self.provider,
            focus=focus,
        )

        console.print(
            f"  [green]After:[/green] {len(self.session.messages)} messages, "
            f"~{estimate_tokens(self.session.messages):,} tokens "
            f"(removed {compaction.removed_message_count} messages, "
            f"compaction #{compaction.count})"
        )

    def _export_session(self, path: str) -> None:
        """Export the session to a JSON file."""
        messages = []
        for msg in self.session.messages:
            msg_data = {
                "role": msg.role,
                "content": [],
            }
            for block in msg.content:
                block_data = {"type": block.type}
                if block.text:
                    block_data["text"] = block.text
                if block.name:
                    block_data["name"] = block.name
                if block.input:
                    block_data["input"] = block.input
                if block.content:
                    block_data["content"] = block.content
                msg_data["content"].append(block_data)
            messages.append(msg_data)

        export_data = {
            "session_id": self.session.session_id,
            "model": self.model,
            "messages": messages,
            "exported_at": __import__("time").time(),
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        console.print(f"[green]Session exported to {path}[/green]")


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    args = parse_args(argv)

    # Handle subcommands
    if args.command == "setup":
        ws = init_workspace(getattr(args, "workspace", None))
        console.print(f"[green]🦀 Workspace initialized at {ws}[/green]")
        return

    if args.command == "doctor":
        _run_doctor(getattr(args, "workspace", None))
        return

    # Auto-init workspace if it doesn't exist
    ws_path = resolve_workspace(getattr(args, "workspace", None))
    if not ws_path.exists():
        init_workspace(getattr(args, "workspace", None))

    # Resolve permission mode
    try:
        permission_mode = PermissionMode.from_str(args.permission_mode)
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    # Parse allowed tools
    allowed_tools: set[str] | None = None
    if args.allowed_tools:
        allowed_tools = {t.strip() for t in args.allowed_tools.split(",") if t.strip()}

    # Override session directory if specified
    if args.session_dir:
        os.environ["MICROCLAW_SESSION_DIR"] = os.path.expanduser(args.session_dir)
        import microclaw.session as _session_mod
        _session_mod.DEFAULT_SESSION_DIR = os.environ["MICROCLAW_SESSION_DIR"]

    # Start fresh by default; only resume when --resume is specified
    session = None
    if args.resume:
        try:
            session = load_session_by_reference(args.resume)
        except FileNotFoundError as e:
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    # Create the CLI
    cli = MicroclawCli(
        model=args.model,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        session=session,
        no_stream=args.no_stream,
        workspace=getattr(args, "workspace", None),
    )

    # Run in the appropriate mode
    if args.prompt:
        cli.run_prompt_mode(args.prompt, output_format=args.output_format)
    else:
        cli.run_repl()


def _run_doctor(workspace: str | None = None) -> None:
    """Check configuration and diagnose issues."""
    ws = resolve_workspace(workspace)

    console.print("[bold]🦀 microclaw doctor[/bold]\n")

    # Check workspace
    if ws.is_dir():
        console.print(f"[green]✓[/green] Workspace: {ws}")
        for fname in ["IDENTITY.md", "SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md"]:
            fpath = ws / fname
            if fpath.exists():
                console.print(f"  [green]✓[/green] {fname}")
            else:
                console.print(f"  [yellow]✗[/yellow] {fname} (missing)")
    else:
        console.print(f"[red]✗[/red] Workspace not found: {ws}")
        console.print("  Run [bold]microclaw setup[/bold] to create it")

    # Check API keys
    for key_name in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY", "XAI_API_KEY"]:
        val = os.environ.get(key_name)
        if val:
            console.print(f"  [green]✓[/green] {key_name} = {'*' * 8}{val[-4:]}")
        else:
            console.print(f"  [dim]○ {key_name} not set[/dim]")

    console.print()


if __name__ == "__main__":
    main()
