"""
Condition management CLI commands.
"""

import typer
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.table import Table
import json

from temms.core.config import Config
from temms.conditions.store import ConditionStore

app = typer.Typer()
console = Console()


@app.command()
def set(
    path: str = typer.Argument(..., help="Condition path (e.g., weather.visibility_m)"),
    value: str = typer.Argument(..., help="Condition value (JSON)"),
    source: str = typer.Option("operator", "--source", "-s", help="Source identifier"),
    priority: int = typer.Option(1000, "--priority", "-p", help="Priority (1000 for operator override)"),
    confidence: float = typer.Option(1.0, "--confidence", "-c", help="Confidence (0.0-1.0)"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        help="Configuration file path",
    ),
):
    """Set a condition value (operator injection)."""
    config = Config.load(config_path)
    store = ConditionStore(config.database.path)

    # Parse value as JSON
    try:
        parsed_value = json.loads(value)
    except json.JSONDecodeError:
        # If not JSON, use as string
        parsed_value = value

    cond = store.set(
        path=path,
        value=parsed_value,
        source=source,
        priority=priority,
        confidence=confidence,
    )

    console.print(f"[green]✓ Condition set:[/green] {path} = {parsed_value}")
    console.print(f"  Source: {source} (priority: {priority})")


@app.command()
def get(
    path: str = typer.Argument(..., help="Condition path"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        help="Configuration file path",
    ),
):
    """Get a condition value."""
    config = Config.load(config_path)
    store = ConditionStore(config.database.path)

    cond = store.get(path)

    if cond is None:
        console.print(f"[yellow]Condition not found: {path}[/yellow]")
        return

    console.print(f"[bold]{path}[/bold]")
    console.print(f"  Value: {cond.value}")
    console.print(f"  Source: {cond.source} (priority: {cond.priority})")
    console.print(f"  Confidence: {cond.confidence}")
    console.print(f"  Updated: {cond.updated_at}")


@app.command("list")
def list_conditions(
    prefix: Optional[str] = typer.Option(None, "--prefix", "-p", help="Filter by prefix"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        help="Configuration file path",
    ),
):
    """List all conditions."""
    config = Config.load(config_path)
    store = ConditionStore(config.database.path)

    conditions = store.get_all(prefix=prefix)

    if not conditions:
        console.print("[yellow]No conditions found[/yellow]")
        return

    table = Table(title="Runtime Conditions")
    table.add_column("Path", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_column("Source", style="green")
    table.add_column("Priority", justify="right")
    table.add_column("Confidence", justify="right")

    for path, cond in sorted(conditions.items()):
        table.add_row(
            path,
            str(cond.value),
            cond.source,
            str(cond.priority),
            f"{cond.confidence:.2f}",
        )

    console.print(table)


@app.command()
def snapshot(
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        help="Configuration file path",
    ),
):
    """Show current condition snapshot (nested structure)."""
    config = Config.load(config_path)
    store = ConditionStore(config.database.path)

    snapshot = store.get_snapshot()

    console.print("[bold]Current Condition Snapshot[/bold]\n")
    console.print(json.dumps(snapshot, indent=2))


@app.command()
def clear_overrides(
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        help="Configuration file path",
    ),
):
    """Clear all operator overrides."""
    config = Config.load(config_path)
    store = ConditionStore(config.database.path)

    count = store.clear_operator_overrides()

    console.print(f"[green]✓ Cleared {count} operator override(s)[/green]")
