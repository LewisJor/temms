"""
Slot management CLI commands for autonomous systems.
"""

import typer
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.table import Table

from temms.core.config import Config
from temms.slots.manager import SlotManager

app = typer.Typer()
console = Console()


@app.command()
def create(
    name: str = typer.Argument(..., help="Slot name"),
    description: str = typer.Option(..., "--description", "-d", help="Slot description"),
    required: bool = typer.Option(False, "--required", "-r", help="Required for operation"),
    default_model: Optional[str] = typer.Option(None, "--default", help="Default model name"),
    candidates: Optional[str] = typer.Option(None, "--candidates", help="Candidate models (comma-separated)"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Create a new model slot."""
    config = Config.load(config_path)
    manager = SlotManager(config.database.path)

    candidate_list = candidates.split(",") if candidates else []

    slot = manager.create_slot(
        name=name,
        description=description,
        required=required,
        default_model=default_model,
        candidates=candidate_list,
    )

    console.print(f"[green]✓ Created slot: {name}[/green]")
    console.print(f"  Description: {description}")
    console.print(f"  Required: {required}")
    if default_model:
        console.print(f"  Default model: {default_model}")


@app.command("list")
def list_slots(
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """List all model slots."""
    config = Config.load(config_path)
    manager = SlotManager(config.database.path)

    slots = manager.list_slots()

    if not slots:
        console.print("[yellow]No slots configured. Create one with 'temms slot create'[/yellow]")
        return

    table = Table(title="TEMMS Slots")
    table.add_column("Name", style="cyan")
    table.add_column("State", style="magenta")
    table.add_column("Active Model", style="green")
    table.add_column("Required", justify="center")
    table.add_column("Description")

    for slot in slots:
        table.add_row(
            slot.name,
            slot.state.value,
            slot.active_model_id[:12] if slot.active_model_id else "-",
            "✓" if slot.required else "",
            slot.description,
        )

    console.print(table)


@app.command()
def status(
    slot_name: str = typer.Argument(..., help="Slot name"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Show detailed status of a specific slot."""
    config = Config.load(config_path)
    manager = SlotManager(config.database.path)

    slot = manager.get_slot(slot_name)

    if not slot:
        console.print(f"[red]Error: Slot not found: {slot_name}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Slot: {slot.name}[/bold]\n")
    console.print(f"Description: {slot.description}")
    console.print(f"State: {slot.state.value}")
    console.print(f"Required: {slot.required}")
    console.print(f"Default model: {slot.default_model or 'none'}")
    console.print(f"Active model: {slot.active_model_id or 'none'}")
    console.print(f"Updated: {slot.updated_at}")

    if slot.candidates:
        console.print(f"\nCandidate models:")
        for candidate in slot.candidates:
            console.print(f"  - {candidate}")

    # Show recent decisions
    decisions = manager.get_decision_log(slot_name=slot_name, limit=5)
    if decisions:
        console.print(f"\n[bold]Recent decisions:[/bold]")
        for decision in decisions:
            console.print(
                f"  {decision['created_at']}: {decision['from_model'] or 'none'} → "
                f"{decision['to_model']} ({decision['trigger_type']})"
            )


@app.command()
def set(
    slot_name: str = typer.Argument(..., help="Slot name"),
    model_name: str = typer.Argument(..., help="Model name to activate"),
    reason: str = typer.Option("manual", "--reason", "-r", help="Reason for activation"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Set active model for a slot (operator override)."""
    from temms.core.cache import ModelCache

    config = Config.load(config_path)
    manager = SlotManager(config.database.path)
    cache = ModelCache(config.database.path)

    # Find model
    model = cache.find_model(model_name)
    if not model:
        console.print(f"[red]Error: Model not found: {model_name}[/red]")
        raise typer.Exit(1)

    # Activate in slot
    manager.activate_model(
        slot_name=slot_name,
        model_id=model.id,
        trigger_type="operator",
        trigger_detail=reason,
        audit_metadata={
            "model_id": model.id,
            "model_name": model.name,
            "model_version": model.version,
            "model_format": model.format.value,
            "model_sha256": model.sha256,
            "package_id": model.package_id,
            "provenance": model.metadata.get("provenance", {}),
        },
    )

    console.print(f"[green]✓ Activated {model_name} in slot '{slot_name}'[/green]")
    console.print(f"  Reason: {reason}")


@app.command()
def decisions(
    slot_name: Optional[str] = typer.Option(None, "--slot", "-s", help="Filter by slot"),
    limit: int = typer.Option(20, "--limit", "-l", help="Number of decisions to show"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Show decision audit log."""
    config = Config.load(config_path)
    manager = SlotManager(config.database.path)

    decisions = manager.get_decision_log(slot_name=slot_name, limit=limit)

    if not decisions:
        console.print("[yellow]No decisions logged[/yellow]")
        return

    table = Table(title=f"Decision Log{f' - {slot_name}' if slot_name else ''}")
    table.add_column("Timestamp", style="dim")
    table.add_column("Slot", style="cyan")
    table.add_column("From", style="red")
    table.add_column("To", style="green")
    table.add_column("Trigger", style="magenta")

    for decision in decisions:
        table.add_row(
            decision["created_at"][:19],  # Trim timestamp
            decision["slot"],
            decision["from_model"][:12] if decision["from_model"] else "-",
            decision["to_model"][:12] if decision["to_model"] else "-",
            f"{decision['trigger_type']}: {decision['trigger_detail']}",
        )

    console.print(table)
