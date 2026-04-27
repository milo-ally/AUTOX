"""Terminal rendering — markdown streaming and formatted output."""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

from rich import box
from rich.console import Console
from rich.console import Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


console = Console()
ACCENT = "yellow"
MUTED = "bright_black"
SUCCESS = "green"
TEXT_SOFT = "#9fb0c0"
CRAB_EMOJI = "🦀"


def _info_grid(rows: list[tuple[str, str]]) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=MUTED, justify="right", no_wrap=True)
    grid.add_column(style="bold white")
    for label, value in rows:
        grid.add_row(label, value)
    return grid


def _metric_panel(title: str, rows: list[tuple[str, str]], border_style: str) -> Panel:
    return Panel(
        _info_grid(rows),
        title=title,
        title_align="left",
        border_style=border_style,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _ascii_banner() -> Text:
    banner = Text(style="bold #ffd866")
    banner.append("███╗   ███╗██╗ ██████╗██████╗  ██████╗  ██████╗██╗      █████╗ ██╗    ██╗\n")
    banner.append("████╗ ████║██║██╔════╝██╔══██╗██╔═══██╗██╔════╝██║     ██╔══██╗██║    ██║\n")
    banner.append("██╔████╔██║██║██║     ██████╔╝██║   ██║██║     ██║     ███████║██║ █╗ ██║\n")
    banner.append("██║╚██╔╝██║██║██║     ██╔══██╗██║   ██║██║     ██║     ██╔══██║██║███╗██║\n")
    banner.append("██║ ╚═╝ ██║██║╚██████╗██║  ██║╚██████╔╝╚██████╗███████╗██║  ██║╚███╔███╔╝\n")
    banner.append("╚═╝     ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ ", style="bold #e6b800")
    banner.append("\n🦀 microclaw\n", style="bold #ff4422")
    return banner


def print_banner(model: str, permission_mode: str, session_id: str) -> None:
    """Print the startup banner."""
    title = _ascii_banner()

    intro = Table.grid(expand=True)
    intro.add_column(ratio=3)
    intro.add_column(ratio=2)

    left = Table.grid(padding=(0, 0))
    left.add_row(Text("🦀 Your own personal AI assistant. The crab way.", style=TEXT_SOFT))
    left.add_row(Text("Python rewrite of OpenClaw — multi-provider, multi-agent, skills, sessions.", style=MUTED))

    right = _info_grid([
        ("Model", model),
        ("Permissions", permission_mode),
        ("Session", session_id),
    ])
    intro.add_row(left, right)

    shortcuts = Text()
    shortcuts.append("/help", style=f"bold {ACCENT}")
    shortcuts.append(" commands", style="white")
    shortcuts.append("  •  ", style=MUTED)
    shortcuts.append("/status", style=f"bold {ACCENT}")
    shortcuts.append(" context", style="white")
    shortcuts.append("  •  ", style=MUTED)
    shortcuts.append("/resume latest", style=MUTED)

    body = Group(title, Text(""), intro, Text(""), shortcuts)
    panel = Panel(
        body,
        border_style=ACCENT,
        box=box.ROUNDED,
        padding=(1, 2),
        subtitle=f"[{MUTED}]🦀 v0.1.0 · openclaw python rewrite[/{MUTED}]",
        subtitle_align="right",
    )
    console.print(panel)

def print_help() -> None:
    """Print the REPL help as a tree structure."""
    from rich.tree import Tree
    from rich.text import Text

    tree = Tree("🦀 microclaw commands", guide_style=f"bold {ACCENT}")

    # -- General --
    gen = tree.add(Text("general", style=f"bold {ACCENT}"))
    gen.add("/help                Show this help")
    gen.add("/status              Show session status (model, message count, session ID)")
    gen.add("/model <name>        Switch LLM model  (e.g. /model deepseek-v4-flash)")
    gen.add("/permissions <mode>  Set: read-only | workspace-write | danger-full-access")
    gen.add("/version             Show microclaw version")
    gen.add("/exit  /quit         Exit the REPL")

    # -- Session --
    ses = tree.add(Text("session", style=f"bold {ACCENT}"))
    ses.add("/compact [focus]     Summarize conversation history to free context window")
    ses.add("/clear               Clear conversation history (tasks persist — use /tasks clear)")
    ses.add("/cost                Show cumulative token usage and estimated cost")
    ses.add("/history             Show last 20 prompts from this session")
    ses.add("/session             List managed sessions")
    ses.add("/resume <id|latest>  Resume a previous session")
    ses.add("/export [path]       Export session to JSON file")

    # -- Tasks --
    tsk = tree.add(Text("tasks", style=f"bold {ACCENT}"))
    tsk.add("/tasks               List all tasks with status summary")
    tsk.add("/tasks add <subj>    Create task  (opts: --status <s> --priority <p>)")
    tsk.add("/tasks done <id>     Mark task completed")
    tsk.add("/tasks start <id>   Mark task in_progress")
    tsk.add("/tasks stop <id>    Mark task pending")
    tsk.add("/tasks update <id>  Update task  (opts: --status, --priority)")
    tsk.add("/tasks get <id>     Show full task details")
    tsk.add("/tasks delete <id>  Delete a task")
    tsk.add("/tasks clear        Delete ALL tasks and reset ID counter to 1")

    # -- Skills --
    sk = tree.add(Text("skills", style=f"bold {ACCENT}"))
    sk.add("/skills               List available skills")
    sk.add("/skills use <name>    Activate a skill")
    sk.add("/skills drop <name>   Deactivate a skill")
    sk.add("/skills reload        Reload skill catalog from disk")

    # -- Team --
    tm = tree.add(Text("team", style=f"bold {ACCENT}"))
    tm.add("/team                 Show team roster and status")
    tm.add("/team spawn <n> <r>   Spawn a teammate (--prompt <task>)")
    tm.add("/team shutdown <name>  Request teammate graceful shutdown")
    tm.add("/team inbox           Check lead's inbox for messages")
    tm.add("/team approve <id>    Approve a pending plan request")
    tm.add("/team reject <id>     Reject a pending plan request")
    tm.add("/team plans           List pending plan requests")
    tm.add("/team cost            Show per-teammate token usage")

    console.print(tree)


def print_status(model: str, session_id: str, messages: int, usage: Any) -> None:
    """Print the current session status."""
    console.print(_metric_panel(
        "Session Status",
        [
            ("Model", model),
            ("Session", session_id),
            ("Messages", f"{messages:,}"),
            ("Input", f"{getattr(usage, 'input_tokens', 0):,} tokens"),
            ("Output", f"{getattr(usage, 'output_tokens', 0):,} tokens"),
        ],
        border_style="blue",
    ))


def print_cost(usage: Any, model: str) -> None:
    """Print token usage and estimated cost."""
    input_tokens = getattr(usage, "input_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", 0)
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0)

    console.print(_metric_panel(
        "Usage",
        [
            ("Model", model),
            ("Input", f"{input_tokens:,} tokens"),
            ("Output", f"{output_tokens:,} tokens"),
            ("Cache Read", f"{cache_read:,}"),
            ("Cache Create", f"{cache_creation:,}"),
        ],
        border_style=SUCCESS,
    ))


def print_team_cost(
    teammate_usage: dict[str, Any],
    lead_usage: Any,
    model: str,
) -> None:
    """Print per-teammate token usage breakdown."""
    from rich.table import Table

    table = Table(title="Token Usage by Agent", box=box.ROUNDED, padding=(0, 2))
    table.add_column("Agent", style="bold white")
    table.add_column("Input", justify="right", style="yellow")
    table.add_column("Output", justify="right", style="yellow")
    table.add_column("Total", justify="right", style="bold green")

    # Lead row
    lead_in = getattr(lead_usage, "input_tokens", 0)
    lead_out = getattr(lead_usage, "output_tokens", 0)
    table.add_row("lead", f"{lead_in:,}", f"{lead_out:,}", f"{lead_in + lead_out:,}")

    # Teammate rows
    total_in = lead_in
    total_out = lead_out
    for name, usage in teammate_usage.items():
        u_in = getattr(usage, "input_tokens", 0)
        u_out = getattr(usage, "output_tokens", 0)
        table.add_row(name, f"{u_in:,}", f"{u_out:,}", f"{u_in + u_out:,}")
        total_in += u_in
        total_out += u_out

    # Total row
    table.add_row("─" * 6, "─" * 8, "─" * 8, "─" * 8, style="dim")
    table.add_row(
        "[bold]Total[/bold]",
        f"{total_in:,}",
        f"{total_out:,}",
        f"[bold]{total_in + total_out:,}[/bold]",
    )

    console.print(table)


class StreamingPrinter:
    """Streaming output: raw text typewriter, then rendered markdown.

    Phase 1 (streaming): Live shows the raw markdown source text as
    it arrives — simple Text, not rendered Markdown.  This is stable
    and scroll-safe because Live only manages plain text.

    Phase 2 (flush/finish): Live stops (transient, raw text vanishes),
    then console.print(Markdown(...)) outputs the clean rendered version.
    No duplicate, no scroll corruption.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._live: Live | None = None
        self._started_at: float | None = None
        self._ticker_stop = threading.Event()
        self._ticker_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the live display."""
        self._started_at = time.monotonic()
        self._ticker_stop.clear()
        self._live = Live(
            Text("⠋ Thinking...", style="bold yellow"),
            console=console,
            refresh_per_second=12,
            transient=True,
        )
        self._live.start()
        self._ticker_thread = threading.Thread(target=self._tick, daemon=True)
        self._ticker_thread.start()

    def append(self, text: str) -> None:
        """Append text and update the live display with raw text."""
        self._buffer += text
        self._render()

    def start_reasoning(self) -> None:
        """Keep compatibility with the existing event hook."""
        pass

    def stop_reasoning(self) -> None:
        """Keep compatibility with the existing event hook."""
        pass

    def pause(self) -> None:
        """Temporarily pause the live display (e.g. for user input)."""
        if self._live:
            self._live.stop()

    def resume(self) -> None:
        """Resume the live display after a pause."""
        if self._live:
            self._live.start()

    def finish(self) -> None:
        """Stop everything, print rendered markdown, show done."""
        self._stop_all()
        self._print_buffer()
        if self._started_at is not None:
            elapsed = int(time.monotonic() - self._started_at)
            console.print(f"[green]✓[/green] Done [dim]({elapsed}s)[/dim]")

    def flush(self) -> None:
        """Stop Live (raw text vanishes), print rendered markdown, restart Live.

        The ticker thread keeps running — spinner animates continuously
        even during tool execution.
        """
        if self._live:
            self._live.stop()
            self._live = None
        self._print_buffer()
        # Restart Live for next segment (ticker still running)
        self._live = Live(
            Text("⠋ Running...", style="bold yellow"),
            console=console,
            refresh_per_second=12,
            transient=True,
        )
        self._live.start()

    def _print_buffer(self) -> None:
        """Print the buffer as rendered markdown and clear it."""
        if self._buffer.strip():
            try:
                console.print(Markdown(self._buffer))
            except Exception:
                console.print(self._buffer)
        self._buffer = ""

    def _render(self) -> None:
        """Render spinner + tail preview in the Live display."""
        if not self._live:
            return
        elapsed = int(time.monotonic() - self._started_at) if self._started_at else 0
        spinner_frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 8) % 10]
        status = Text(f"{spinner_frame} Running {elapsed}s", style="bold yellow")
        if self._buffer:
            lines = self._buffer.rstrip().split("\n")
            preview_lines = lines[-6:]
            preview = "\n".join(preview_lines)
            if len(preview) > 500:
                preview = preview[-500:]
            body = Text(preview, style="dim")
            self._live.update(Group(status, body))
        else:
            self._live.update(status)

    def _stop_all(self) -> None:
        """Stop the Live display and ticker thread."""
        self._ticker_stop.set()
        if self._ticker_thread and self._ticker_thread.is_alive():
            self._ticker_thread.join(timeout=0.2)
        if self._live:
            self._live.stop()
        self._live = None

    def _tick(self) -> None:
        """Refresh the spinner animation and timer (runs entire turn)."""
        while not self._ticker_stop.wait(0.15):
            self._render()
