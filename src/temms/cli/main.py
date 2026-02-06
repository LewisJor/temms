"""
Main TEMMS CLI application using Typer.
"""

import typer
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.table import Table

from temms import __version__
from temms.cli import slot, condition

app = typer.Typer(
    name="temms",
    help="TEMMS - Tactical Edge Model Management System",
    add_completion=False,
)

console = Console()

# Add subcommands
app.add_typer(slot.app, name="slot", help="Manage model slots")
app.add_typer(condition.app, name="condition", help="Manage runtime conditions")


@app.command()
def init(
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
    data_dir: Path = typer.Option(
        Path("/var/lib/temms"),
        "--data-dir",
        "-d",
        help="Data directory path",
    ),
):
    """Initialize TEMMS configuration and directories."""
    from temms.core.config import Config

    console.print(f"[bold green]Initializing TEMMS...[/bold green]")

    # Create directories
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "models").mkdir(exist_ok=True)
    (data_dir / "cache").mkdir(exist_ok=True)
    (data_dir / "packages").mkdir(exist_ok=True)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    (config_path.parent / "policies").mkdir(exist_ok=True)
    (config_path.parent / "slots").mkdir(exist_ok=True)

    # Create config with actual data directory paths
    from temms.core.config import DatabaseConfig, StorageConfig, PolicyConfig
    config = Config(
        database=DatabaseConfig(path=data_dir / "temms.db"),
        storage=StorageConfig(
            model_dir=data_dir / "models",
            cache_dir=data_dir / "cache",
        ),
        policy=PolicyConfig(policy_dir=config_path.parent / "policies"),
    )
    config.save(config_path)

    console.print(f"✓ Created configuration: {config_path}")
    console.print(f"✓ Created data directory: {data_dir}")
    console.print(f"\n[bold]Next steps:[/bold]")
    console.print("  1. Import a package: temms import <package_dir>")
    console.print("  2. Configure slots: temms slot create <name>")
    console.print("  3. Load a policy: temms policy load <policy.yaml>")
    console.print("  4. Start daemon: temms daemon start")


@app.command()
def import_package(
    package_path: Path = typer.Argument(..., help="Path to TEMMS package directory"),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="Verify model hashes"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Import a TEMMS package (models + policies)."""
    from temms.core.config import Config
    from temms.core.cache import ModelCache
    from temms.core.storage import ModelStorage
    from temms.core.package import PackageImporter

    if not package_path.exists():
        console.print(f"[red]Error: Package not found: {package_path}[/red]")
        raise typer.Exit(1)

    config = Config.load(config_path)
    cache = ModelCache(config.database.path)
    storage = ModelStorage(config.storage.model_dir)
    importer = PackageImporter(config.storage.cache_dir, cache, storage)

    console.print(f"[bold]Importing package:[/bold] {package_path}")

    try:
        with console.status("[bold green]Importing package..."):
            result = importer.import_package(package_path, verify=verify)

        console.print(f"[green]✓ Package imported successfully[/green]")
        console.print(f"\nPackage: {result.manifest.name} v{result.manifest.version}")
        console.print(f"Models imported: {len(result.models)}")
        for model in result.models:
            console.print(f"  - {model.name} v{model.version} ({model.format.value})")

        if result.policies:
            console.print(f"\nPolicies imported: {len(result.policies)}")
            for policy in result.policies:
                console.print(f"  - {policy.name}")

    except Exception as e:
        console.print(f"[red]Error importing package: {e}[/red]")
        raise typer.Exit(1)


@app.command("import")
def import_alias(
    package_path: Path = typer.Argument(..., help="Path to TEMMS package directory"),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="Verify model hashes"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Import a TEMMS package (alias for import-package)."""
    import_package(package_path, verify, config_path)


@app.command()
def status(
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Show TEMMS system status."""
    from temms.core.config import Config
    from temms.core.cache import ModelCache
    from temms.slots.manager import SlotManager

    if not config_path.exists():
        console.print("[red]Error: TEMMS not initialized. Run 'temms init' first.[/red]")
        raise typer.Exit(1)

    config = Config.load(config_path)
    cache = ModelCache(config.database.path)
    slot_manager = SlotManager(config.database.path)

    console.print("[bold]TEMMS System Status[/bold]\n")

    # Cached models
    models = cache.list_models()
    console.print(f"Cached models: {len(models)}")

    # Packages
    packages = cache.list_packages()
    console.print(f"Imported packages: {len(packages)}")

    # Slots
    slots = slot_manager.list_slots()
    console.print(f"\nSlots: {len(slots)}")
    for slot in slots:
        status_icon = "✓" if slot.state.value == "running" else "○"
        console.print(f"  {status_icon} {slot.name}: {slot.state.value}")


@app.command()
def version():
    """Show TEMMS version."""
    console.print(f"TEMMS v{__version__}")


@app.command()
def daemon(
    action: str = typer.Argument(..., help="Action: start, stop, status"),
    foreground: bool = typer.Option(
        False, "--foreground", "-f", help="Run in foreground (don't daemonize)"
    ),
    host: str = typer.Option("0.0.0.0", "--host", help="Inference server host"),
    port: int = typer.Option(8080, "--port", "-p", help="Inference server port"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Manage TEMMS daemon."""
    import asyncio
    import os
    import signal

    if action == "start":
        from temms.daemon.service import TEMMSDaemon, DaemonConfig
        from temms.core.config import Config

        # Load config if exists
        if config_path.exists():
            config = Config.load(config_path)
            daemon_config = DaemonConfig(
                inference_host=host,
                inference_port=port,
                db_path=config.database.path,
                model_dir=config.storage.model_dir,
                policy_dir=config_path.parent / "policies",
            )
        else:
            daemon_config = DaemonConfig(
                inference_host=host,
                inference_port=port,
            )

        if foreground:
            console.print(f"[bold green]Starting TEMMS daemon in foreground...[/bold green]")
            console.print(f"  Host: {host}")
            console.print(f"  Port: {port}")
            console.print(f"  Config: {config_path}")
            console.print("\nPress Ctrl+C to stop\n")

            try:
                daemon = TEMMSDaemon.from_config(daemon_config)
                asyncio.run(daemon.start())
            except KeyboardInterrupt:
                console.print("\n[yellow]Daemon stopped by user[/yellow]")
        else:
            # Fork to background
            console.print(f"[bold green]Starting TEMMS daemon...[/bold green]")
            pid_file = Path("/var/run/temms.pid")

            # Check if already running
            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                try:
                    os.kill(pid, 0)  # Check if process exists
                    console.print(f"[yellow]Daemon already running (PID: {pid})[/yellow]")
                    raise typer.Exit(1)
                except ProcessLookupError:
                    pid_file.unlink()  # Stale PID file

            # Double fork to daemonize
            try:
                pid = os.fork()
                if pid > 0:
                    console.print(f"[green]Daemon started (PID: {pid})[/green]")
                    raise typer.Exit(0)
            except OSError as e:
                console.print(f"[red]Fork failed: {e}[/red]")
                raise typer.Exit(1)

            # Child process
            os.setsid()

            # Second fork
            try:
                pid = os.fork()
                if pid > 0:
                    os._exit(0)
            except OSError as e:
                os._exit(1)

            # Write PID file
            try:
                pid_file.parent.mkdir(parents=True, exist_ok=True)
                pid_file.write_text(str(os.getpid()))
            except Exception:
                pass

            # Redirect stdout/stderr
            import sys
            sys.stdout = open("/var/log/temms.log", "a")
            sys.stderr = sys.stdout

            # Start daemon
            daemon = TEMMSDaemon.from_config(daemon_config)
            asyncio.run(daemon.start())

    elif action == "stop":
        pid_file = Path("/var/run/temms.pid")

        if not pid_file.exists():
            console.print("[yellow]Daemon not running[/yellow]")
            raise typer.Exit(0)

        pid = int(pid_file.read_text().strip())

        try:
            os.kill(pid, signal.SIGTERM)
            console.print(f"[green]Daemon stopped (PID: {pid})[/green]")
            pid_file.unlink()
        except ProcessLookupError:
            console.print("[yellow]Daemon not running (stale PID file removed)[/yellow]")
            pid_file.unlink()
        except PermissionError:
            console.print("[red]Permission denied. Try with sudo.[/red]")
            raise typer.Exit(1)

    elif action == "status":
        import httpx

        pid_file = Path("/var/run/temms.pid")

        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, 0)
                console.print(f"[green]Daemon running (PID: {pid})[/green]")
            except ProcessLookupError:
                console.print("[yellow]Daemon not running (stale PID file)[/yellow]")
                raise typer.Exit(1)
        else:
            console.print("[yellow]Daemon not running[/yellow]")
            raise typer.Exit(1)

        # Check API health
        try:
            with httpx.Client() as client:
                response = client.get(f"http://{host}:{port}/v1/health", timeout=2)
                if response.status_code == 200:
                    console.print(f"[green]API healthy[/green]")

                # Get system status
                status_response = client.get(f"http://{host}:{port}/v1/status", timeout=2)
                if status_response.status_code == 200:
                    data = status_response.json()
                    console.print(f"\nSystem status: {data['status']}")
                    console.print(f"Slots: {len(data['slots'])}")
                    console.print(f"Conditions: {data['conditions_count']}")
                    console.print(f"Policies: {data['policies_count']}")
                    console.print(f"Uptime: {data['uptime_seconds']:.1f}s")
        except Exception as e:
            console.print(f"[yellow]API not responding: {e}[/yellow]")

    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        console.print("Valid actions: start, stop, status")
        raise typer.Exit(1)


@app.command()
def policy(
    action: str = typer.Argument(..., help="Action: load, list, status"),
    policy_file: Optional[Path] = typer.Argument(None, help="Policy file path"),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Manage policies."""
    from temms.core.config import Config
    from temms.conditions.store import ConditionStore
    from temms.policy.engine import PolicyEngine

    if not config_path.exists():
        console.print("[red]Error: TEMMS not initialized. Run 'temms init' first.[/red]")
        raise typer.Exit(1)

    config = Config.load(config_path)
    condition_store = ConditionStore(config.database.path)
    policy_engine = PolicyEngine(condition_store)

    if action == "load":
        if policy_file is None:
            console.print("[red]Error: Policy file required[/red]")
            raise typer.Exit(1)

        if not policy_file.exists():
            console.print(f"[red]Error: Policy file not found: {policy_file}[/red]")
            raise typer.Exit(1)

        try:
            loaded_policy = policy_engine.load_policy_from_file(policy_file)
            console.print(f"[green]✓ Loaded policy: {loaded_policy.metadata.name}[/green]")
            console.print(f"  Slot: {loaded_policy.spec.slot}")
            console.print(f"  Rules: {len(loaded_policy.spec.rules)}")

            # Copy to policies directory
            policies_dir = config_path.parent / "policies"
            policies_dir.mkdir(exist_ok=True)
            import shutil
            dest = policies_dir / policy_file.name
            shutil.copy(policy_file, dest)
            console.print(f"  Copied to: {dest}")

        except Exception as e:
            console.print(f"[red]Error loading policy: {e}[/red]")
            raise typer.Exit(1)

    elif action == "list":
        policies_dir = config_path.parent / "policies"

        if not policies_dir.exists():
            console.print("[yellow]No policies directory found[/yellow]")
            raise typer.Exit(0)

        policy_files = list(policies_dir.glob("*.yaml")) + list(policies_dir.glob("*.yml"))

        if not policy_files:
            console.print("[yellow]No policies found[/yellow]")
            raise typer.Exit(0)

        table = Table(title="Installed Policies")
        table.add_column("Name", style="cyan")
        table.add_column("Slot", style="green")
        table.add_column("Rules", style="yellow")
        table.add_column("File", style="dim")

        for pf in policy_files:
            try:
                loaded = policy_engine.load_policy_from_file(pf)
                table.add_row(
                    loaded.metadata.name,
                    loaded.spec.slot,
                    str(len(loaded.spec.rules)),
                    pf.name,
                )
            except Exception as e:
                table.add_row(pf.stem, "[red]Error[/red]", "-", pf.name)

        console.print(table)

    elif action == "status":
        # Show loaded policies in current daemon (if running)
        import httpx

        try:
            with httpx.Client() as client:
                response = client.get("http://localhost:8080/v1/status", timeout=2)
                if response.status_code == 200:
                    data = response.json()
                    console.print(f"[green]Policies loaded in daemon: {data['policies_count']}[/green]")
                else:
                    console.print("[yellow]Could not get policy status from daemon[/yellow]")
        except Exception:
            console.print("[yellow]Daemon not running - showing installed policies[/yellow]")
            policy(action="list", policy_file=None, config_path=config_path)

    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        console.print("Valid actions: load, list, status")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
