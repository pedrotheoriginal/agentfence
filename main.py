"""AgentFence CLI - Let AI act. Keep control."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agentfence_core import __version__
from agentfence_core.database import AuditDatabase, DatabaseError, Status
from agentfence_core.defaults import DEFAULT_CONFIG_YAML
from agentfence_core.engine import (
    CONFIG_FILENAME,
    EXIT_CONFIG_ERROR,
    EXIT_CONFIRM,
    EXIT_BLOCKED,
    EXIT_OK,
    EXIT_USAGE,
    Action,
    AgentFence,
    Config,
    ConfigError,
    ConfigNotFoundError,
    find_config,
    load_config,
)

app = typer.Typer(
    name="agentfence",
    help="🛡️  [bold]AgentFence[/bold] - the terminal firewall for AI agents. Let AI act. Keep control.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

console = Console()
err_console = Console(stderr=True)

STATUS_STYLES = {
    Status.ALLOWED: "bold green",
    Status.CONFIRMED: "bold cyan",
    Status.DENIED: "bold yellow",
    Status.BLOCKED: "bold red",
}
ACTION_STYLES = {Action.ALLOW: "green", Action.CONFIRM: "yellow", Action.BLOCK: "bold red"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load(config_path: Optional[Path]) -> Tuple[Config, Path]:
    """Locate and load the config or exit with a friendly error."""
    try:
        path = find_config(config_path)
        return load_config(path), path
    except ConfigNotFoundError as exc:
        err_console.print(f"[bold red]✖ {exc}[/bold red]")
        raise typer.Exit(EXIT_CONFIG_ERROR)
    except ConfigError as exc:
        err_console.print(Panel(Text(str(exc)), title="[bold red]Configuration error[/bold red]", border_style="red"))
        raise typer.Exit(EXIT_CONFIG_ERROR)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"AgentFence [bold]{__version__}[/bold]")
        raise typer.Exit()


ConfigOption = typer.Option(
    None,
    "--config",
    "-c",
    help="Path to agentfence.yaml (default: $AGENTFENCE_CONFIG, ./agentfence.yaml, ~/.agentfence/agentfence.yaml).",
)


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-v", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """AgentFence CLI."""


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

@app.command()
def init(
    path: Path = typer.Option(Path(CONFIG_FILENAME), "--path", "-p", help="Where to write the config file."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing file."),
) -> None:
    """Create a default [bold]agentfence.yaml[/bold] in the current directory."""
    target = path.expanduser()
    if target.exists() and not force:
        err_console.print(f"[bold red]✖ {target} already exists.[/bold red] Use --force to overwrite.")
        raise typer.Exit(EXIT_USAGE)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    except OSError as exc:
        err_console.print(f"[bold red]✖ Could not write {target}:[/bold red] {exc}")
        raise typer.Exit(EXIT_USAGE)

    config = load_config(target)
    counts = {action: sum(1 for r in config.rules if r.action is action) for action in Action}
    console.print(
        Panel(
            Text.from_markup(
                f"[bold green]✔ Created[/bold green] {target}\n\n"
                f"[red]{counts[Action.BLOCK]} block[/red] · "
                f"[yellow]{counts[Action.CONFIRM]} confirm[/yellow] · "
                f"[green]{counts[Action.ALLOW]} allow[/green] rules\n\n"
                "Next steps:\n"
                '  [bold]agentfence check "rm -rf /"[/bold]   dry-run a command\n'
                '  [bold]agentfence run "ls -la"[/bold]       run through the firewall\n'
                "  [bold]agentfence history[/bold]            review the audit log"
            ),
            title="[bold]🛡️  AgentFence ready[/bold]",
            border_style="green",
            expand=False,
        )
    )


@app.command()
def run(
    command: str = typer.Argument(..., help="Command line to evaluate and run (wrap it in quotes)."),
    config: Optional[Path] = ConfigOption,
) -> None:
    """Evaluate a command, enforce the policy and execute it only if permitted."""
    cfg, _ = _load(config)
    fence = AgentFence(cfg)
    raise typer.Exit(fence.run(command))


@app.command()
def check(
    command: str = typer.Argument(..., help="Command line to evaluate (it is NOT executed)."),
    config: Optional[Path] = ConfigOption,
) -> None:
    """Dry-run: show what AgentFence would do without executing anything.

    Exit codes: 0 allow, 10 confirm, 126 block.
    """
    if not command.strip():
        err_console.print("[bold red]✖ Empty command.[/bold red]")
        raise typer.Exit(EXIT_USAGE)

    cfg, _ = _load(config)
    decision = AgentFence(cfg, console=err_console).evaluate(command)
    style = ACTION_STYLES[decision.action]

    console.print(
        Text.assemble(
            (f"{decision.action.value.upper()} ", f"bold {style}"),
            (f"[{decision.rule_name or 'default'}] ", style),
            decision.reason,
        )
    )
    raise typer.Exit({Action.ALLOW: EXIT_OK, Action.CONFIRM: EXIT_CONFIRM, Action.BLOCK: EXIT_BLOCKED}[decision.action])


@app.command()
def rules(config: Optional[Path] = ConfigOption) -> None:
    """List all active rules."""
    cfg, path = _load(config)

    table = Table(title=f"AgentFence rules ({path})", header_style="bold", show_lines=False)
    table.add_column("Action")
    table.add_column("Name")
    table.add_column("Description", overflow="fold")
    order = {Action.BLOCK: 0, Action.CONFIRM: 1, Action.ALLOW: 2}
    for rule in sorted(cfg.rules, key=lambda r: order[r.action]):
        table.add_row(
            Text(rule.action.value.upper(), style=ACTION_STYLES[rule.action]),
            rule.name,
            rule.description,
        )
    console.print(table)
    console.print(f"Default action for unmatched commands: [bold]{cfg.default_action.value}[/bold]")


def _open_db(config_path: Optional[Path]) -> AuditDatabase:
    """Open the audit DB configured in the policy (or the default location)."""
    try:
        cfg = load_config(find_config(config_path))
        db_path = cfg.audit.db_path
    except ConfigNotFoundError:
        db_path = "~/.agentfence/audit.db"
    except ConfigError as exc:
        err_console.print(f"[bold red]✖ {exc}[/bold red]")
        raise typer.Exit(EXIT_CONFIG_ERROR)
    try:
        return AuditDatabase(db_path)
    except DatabaseError as exc:
        err_console.print(f"[bold red]✖ {exc}[/bold red]")
        raise typer.Exit(EXIT_USAGE)


@app.command()
def history(
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="Number of entries to show."),
    status: Optional[Status] = typer.Option(None, "--status", "-s", case_sensitive=False, help="Filter by status."),
    config: Optional[Path] = ConfigOption,
) -> None:
    """Show the audit log (newest first)."""
    db = _open_db(config)
    try:
        entries = db.history(limit=limit, status=status)
        counts = db.stats()
    except DatabaseError as exc:
        err_console.print(f"[bold red]✖ {exc}[/bold red]")
        raise typer.Exit(EXIT_USAGE)

    if not entries:
        console.print("[dim]No audit entries yet.[/dim]")
        return

    table = Table(title="AgentFence audit log", header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("Time")
    table.add_column("Status")
    table.add_column("Rule")
    table.add_column("Exit", justify="right")
    table.add_column("Command", overflow="fold")
    for entry in entries:
        table.add_row(
            str(entry.id),
            entry.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            Text(entry.status.value, style=STATUS_STYLES[entry.status]),
            entry.rule_name or "default",
            "-" if entry.exit_code is None else str(entry.exit_code),
            Text(entry.command),
        )
    console.print(table)
    console.print(
        Text.assemble(
            ("Totals: ", "bold"),
            (f"{counts['ALLOWED']} allowed  ", "green"),
            (f"{counts['CONFIRMED']} confirmed  ", "cyan"),
            (f"{counts['DENIED']} denied  ", "yellow"),
            (f"{counts['BLOCKED']} blocked", "red"),
        )
    )


if __name__ == "__main__":
    app()