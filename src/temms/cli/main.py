"""
Main TEMMS CLI application using Typer.
"""

import importlib.util
import os
import platform
import shutil
import socket
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from temms import __version__
from temms.cli import condition, slot

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
    console.print(
        "  1. Import a signed package: temms import <package_dir> --signing-key-file <key>"
    )
    console.print("  2. Configure slots: temms slot create <name>")
    console.print("  3. Load a policy: temms policy load <policy.yaml>")
    console.print("  4. Start daemon: temms daemon start")


@app.command()
def import_package(
    package_path: Path = typer.Argument(..., help="Path to TEMMS package directory or archive"),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="Verify model hashes"),
    require_signature: bool = typer.Option(
        True,
        "--require-signature/--allow-unsigned-package",
        help="Require and verify signature.json before import",
    ),
    signing_key: Optional[str] = typer.Option(
        None,
        "--signing-key",
        help="Inline package signing key for signature verification",
    ),
    signing_key_file: Optional[Path] = typer.Option(
        None,
        "--signing-key-file",
        help="File containing package signing key",
    ),
    device_profile: Optional[str] = typer.Option(
        None,
        "--device-profile",
        help="Validate package compatibility for this device profile",
    ),
    strict_metadata: bool = typer.Option(
        True,
        "--strict-metadata/--allow-lab-metadata",
        help="Require production package metadata before import",
    ),
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
    from temms.core.signing import read_signing_key

    if not package_path.exists():
        console.print(f"[red]Error: Package not found: {package_path}[/red]")
        raise typer.Exit(1)

    config = Config.load(config_path)
    cache = ModelCache(config.database.path)
    storage = ModelStorage(config.storage.model_dir)
    importer = PackageImporter(
        config.storage.cache_dir,
        cache,
        storage,
        active_policy_dir=config.policy.policy_dir,
        require_signature=require_signature,
        signing_key=_package_signing_key(signing_key, signing_key_file, read_signing_key),
        device_profile=device_profile,
        strict_metadata=strict_metadata,
    )

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
            console.print(f"Active policy dir: {config.policy.policy_dir}")

    except Exception as e:
        console.print(f"[red]Error importing package: {e}[/red]")
        raise typer.Exit(1)


@app.command("import")
def import_alias(
    package_path: Path = typer.Argument(..., help="Path to TEMMS package directory or archive"),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="Verify model hashes"),
    require_signature: bool = typer.Option(
        True,
        "--require-signature/--allow-unsigned-package",
        help="Require and verify signature.json before import",
    ),
    signing_key: Optional[str] = typer.Option(None, "--signing-key"),
    signing_key_file: Optional[Path] = typer.Option(None, "--signing-key-file"),
    device_profile: Optional[str] = typer.Option(None, "--device-profile"),
    strict_metadata: bool = typer.Option(
        True,
        "--strict-metadata/--allow-lab-metadata",
        help="Require production package metadata before import",
    ),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Import a TEMMS package (alias for import-package)."""
    import_package(
        package_path,
        verify,
        require_signature,
        signing_key,
        signing_key_file,
        device_profile,
        strict_metadata,
        config_path,
    )


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
def evidence(
    slot: Optional[str] = typer.Option(
        None,
        "--slot",
        "-s",
        help="Only include decisions for one slot",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        "-n",
        help="Maximum number of recent decisions to include",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write evidence bundle JSON to this path",
    ),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Export local decision evidence for offline audit/reconstruction."""
    import json

    from temms.conditions.store import ConditionStore
    from temms.core.cache import ModelCache
    from temms.core.config import Config
    from temms.daemon.deployment_state import DeploymentStateStore
    from temms.daemon.pending_ops import PendingOperationsStore
    from temms.evidence import EvidenceBundleBuilder
    from temms.policy.engine import PolicyEngine
    from temms.slots.manager import SlotManager

    if not config_path.exists():
        console.print("[red]Error: TEMMS not initialized. Run 'temms init' first.[/red]")
        raise typer.Exit(1)

    config = Config.load(config_path)
    model_cache = ModelCache(config.database.path)
    slot_manager = SlotManager(config.database.path)
    condition_store = ConditionStore(config.database.path)
    policy_engine = PolicyEngine(condition_store)

    policy_dir = config.policy.policy_dir
    if policy_dir.exists():
        policy_files = sorted(
            list(policy_dir.glob("*.yaml")) + list(policy_dir.glob("*.yml"))
        )
        for policy_file in policy_files:
            try:
                policy_engine.load_policy_from_file(policy_file)
            except Exception as exc:
                console.print(f"[yellow]Skipping invalid policy {policy_file}: {exc}[/yellow]")

    data_dir = config.database.path.parent
    pending_path = data_dir / "pending_operations.json"
    deployment_path = data_dir / "deployment_state.json"

    pending_operations = []
    if pending_path.exists():
        pending_operations = PendingOperationsStore(pending_path).read_all()

    deployment_state = None
    if deployment_path.exists():
        deployment_state = DeploymentStateStore(deployment_path)._read()

    bundle = EvidenceBundleBuilder(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
    ).build(
        slot_name=slot,
        limit=limit,
        offline_mode=bool(
            deployment_state and deployment_state.get("state") == "OFFLINE"
        ),
        pending_operations=pending_operations,
        deployment_state=deployment_state,
    )

    rendered = json.dumps(bundle, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        console.print(f"[green]Evidence bundle exported:[/green] {output}")
        console.print(f"Decisions: {len(bundle['decisions'])}")
        console.print(f"SHA256: {bundle['integrity']['payload_sha256']}")
    else:
        console.print(rendered)


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
    host: Optional[str] = typer.Option(
        None,
        "--host",
        help="Inference server host (defaults to TEMMS_HOST or 0.0.0.0)",
    ),
    port: Optional[int] = typer.Option(
        None,
        "--port",
        "-p",
        help="Inference server port (defaults to TEMMS_PORT or 8080)",
    ),
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

        daemon_overrides: dict[str, Any] = {}
        if host is not None:
            daemon_overrides["inference_host"] = host
        if port is not None:
            daemon_overrides["inference_port"] = port

        # Load config if exists
        if config_path.exists():
            config = Config.load(config_path)
            daemon_config = DaemonConfig(
                db_path=config.database.path,
                model_dir=config.storage.model_dir,
                policy_dir=config.policy.policy_dir,
                **daemon_overrides,
            )
        else:
            daemon_config = DaemonConfig(**daemon_overrides)

        if foreground:
            console.print(f"[bold green]Starting TEMMS daemon in foreground...[/bold green]")
            console.print(f"  Host: {daemon_config.inference_host}")
            console.print(f"  Port: {daemon_config.inference_port}")
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

            # Copy to the active policy directory watched by the daemon.
            policies_dir = config.policy.policy_dir
            policies_dir.mkdir(parents=True, exist_ok=True)
            import shutil

            dest = policies_dir / policy_file.name
            shutil.copy(policy_file, dest)
            console.print(f"  Copied to: {dest}")

        except Exception as e:
            console.print(f"[red]Error loading policy: {e}[/red]")
            raise typer.Exit(1)

    elif action == "list":
        policies_dir = config.policy.policy_dir

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
                    console.print(
                        f"[green]Policies loaded in daemon: {data['policies_count']}[/green]"
                    )
                else:
                    console.print("[yellow]Could not get policy status from daemon[/yellow]")
        except Exception:
            console.print("[yellow]Daemon not running - showing installed policies[/yellow]")
            policy(action="list", policy_file=None, config_path=config_path)

    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        console.print("Valid actions: load, list, status")
        raise typer.Exit(1)


@app.command()
def doctor(
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
    port: int = typer.Option(8080, "--port", "-p", help="API port to check"),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable JSON diagnostics",
    ),
):
    """Run edge-agent diagnostics for this VM/device."""
    import json

    report = build_doctor_report(config_path=config_path, port=port)
    if json_output:
        typer.echo(json.dumps(report, indent=2, sort_keys=True))
        return

    console.print("[bold]TEMMS Doctor[/bold]\n")

    system_table = Table(title="System")
    system_table.add_column("Check", style="cyan")
    system_table.add_column("Value")
    system_table.add_row("OS", report["system"]["os"])
    system_table.add_row("Arch", report["system"]["arch"])
    system_table.add_row("Machine", report["system"]["machine"])
    system_table.add_row("Python", report["system"]["python"])
    if report["system"]["board_model"]:
        system_table.add_row("Board", report["system"]["board_model"])
    system_table.add_row("Device profile", report["system"]["device_profile"])
    system_table.add_row("TEMMS", report["temms_version"])
    console.print(system_table)

    profiles_table = Table(title="Known MVP Device Profiles")
    profiles_table.add_column("Profile", style="cyan")
    profiles_table.add_column("Description")
    for profile, metadata in report["known_device_profiles"].items():
        profiles_table.add_row(profile, metadata.get("description", ""))
    console.print(profiles_table)

    if not report["config"]["found"]:
        console.print(f"[yellow]Config not found:[/yellow] {config_path}")
    path_strategy = report.get("path_strategy", {})
    if path_strategy.get("non_root_fallback"):
        console.print(
            "[yellow]Path strategy:[/yellow] using non-root fallback "
            f"{path_strategy.get('data_dir')}"
        )
    else:
        console.print(f"Path strategy: {path_strategy.get('source', 'unknown')}")

    paths_table = Table(title="Paths")
    paths_table.add_column("Path", style="cyan")
    paths_table.add_column("Exists")
    paths_table.add_column("Writable")
    for path_info in report["paths"]:
        paths_table.add_row(
            path_info["path"],
            "yes" if path_info["exists"] else "no",
            "yes" if path_info["writable"] else "no",
        )
    console.print(paths_table)

    runtime_table = Table(title="Runtimes and Accelerators")
    runtime_table.add_column("Capability", style="cyan")
    runtime_table.add_column("Status")
    for name in ("onnxruntime", "tflite", "tflite_runtime", "tensorflow", "torch", "tensorrt"):
        runtime = report["runtimes"].get(name, {})
        status = "found" if runtime.get("available") else "missing"
        providers = runtime.get("providers") or []
        if providers:
            status += ": " + ",".join(providers)
        if runtime.get("module"):
            status += f" ({runtime['module']})"
        runtime_table.add_row(name, status)
    for name, accelerator in report["accelerators"].items():
        runtime_table.add_row(
            name,
            "found" if accelerator.get("available") else "missing",
        )
    console.print(runtime_table)

    security_table = Table(title="Security Readiness")
    security_table.add_column("Check", style="cyan")
    security_table.add_column("Status")
    security = report["security"]
    security_table.add_row(
        "Control API token",
        "configured" if security["api_token_configured"] else "not configured",
    )
    security_table.add_row(
        "Hub token",
        security["hub_token_source"],
    )
    security_table.add_row(
        "Rollout signature enforcement",
        "enabled" if security["rollout_require_signature"] else "disabled",
    )
    security_table.add_row(
        "Package signing key",
        security["signing_key_source"],
    )
    console.print(security_table)

    ports_table = Table(title="Ports")
    ports_table.add_column("Endpoint", style="cyan")
    ports_table.add_column("Host")
    ports_table.add_column("Port")
    ports_table.add_column("Status")
    for port_info in report["ports"]:
        ports_table.add_row(
            port_info["name"],
            port_info.get("configured_host") or port_info["host"],
            str(port_info["port"]),
            port_info["status"],
        )
    console.print(ports_table)

    if report["model_cache"] is not None:
        cache = report["model_cache"]
        health = cache["health"]
        console.print(
            f"Model cache: {cache['models']} models, {cache['packages']} packages, "
            f"{cache['total_size_bytes']} bytes on disk, health={health['status']}"
        )
        if health["issues"]:
            for issue in health["issues"]:
                console.print(
                    f"[red]Cache issue:[/red] {issue['model_id']} "
                    f"{issue['type']} ({issue['path']})"
                )


def build_doctor_report(config_path: Path, port: int = 8080) -> dict[str, Any]:
    """Build machine-readable edge-agent diagnostics."""
    from temms.core.cache import ModelCache
    from temms.core.cache_health import model_cache_health
    from temms.core.config import Config
    from temms.core.runtime_profiles import detect_runtime_capabilities, known_device_profiles
    from temms.core.storage import ModelStorage

    capabilities = detect_runtime_capabilities()
    config = Config.load(config_path) if config_path.exists() else None
    if config is not None:
        path_strategy = {
            "source": "config",
            "non_root_fallback": False,
            "data_dir": str(config.database.path.parent),
        }
        paths = [
            ("database_dir", config.database.path.parent),
            ("model_dir", config.storage.model_dir),
            ("cache_dir", config.storage.cache_dir),
            ("package_dir", config.storage.cache_dir.parent / "packages"),
            ("policy_dir", config.policy.policy_dir),
        ]
    else:
        path_strategy, paths = _doctor_missing_config_paths()

    path_reports = []
    for name, path in paths:
        exists = path.exists()
        writable_target = path if exists else _nearest_existing_parent(path)
        write_probe = _probe_path_writable(writable_target)
        path_reports.append(
            {
                "name": name,
                "path": str(path),
                "exists": exists,
                "writable_target": str(writable_target),
                "writable": write_probe["ok"],
                "write_probe": write_probe,
            }
        )

    ports = _doctor_ports(config, port)

    model_cache_report = None
    if config is not None:
        cache = ModelCache(config.database.path)
        storage = ModelStorage(config.storage.model_dir)
        stats = storage.get_storage_stats()
        models = cache.list_models()
        model_cache_report = {
            "database": str(config.database.path),
            "models": len(models),
            "packages": len(cache.list_packages()),
            "model_count_on_disk": stats["model_count"],
            "total_size_bytes": stats["total_size_bytes"],
            "storage_path": stats["storage_path"],
            "health": model_cache_health(models),
        }

    return {
        "schema_version": "temms-doctor/v1",
        "temms_version": __version__,
        "config": {
            "path": str(config_path),
            "found": config is not None,
        },
        "system": {
            "os": capabilities.os,
            "machine": capabilities.machine,
            "arch": capabilities.to_dict()["arch"],
            "python": capabilities.python,
            "board_model": capabilities.board_model,
            "device_profile": capabilities.device_profile,
        },
        "known_device_profiles": known_device_profiles(),
        "path_strategy": path_strategy,
        "paths": path_reports,
        "runtimes": capabilities.runtimes,
        "accelerators": capabilities.accelerators,
        "security": _doctor_security_report(),
        "port": ports[0],
        "ports": ports,
        "model_cache": model_cache_report,
    }


def _doctor_ports(config: Any | None, api_port: int) -> list[dict[str, Any]]:
    """Return local port checks for configured edge-agent endpoints."""
    ports: list[dict[str, Any]] = [
        _port_report(
            name="api",
            host="127.0.0.1",
            port=api_port,
            configured_host=(config.inference.host if config is not None else None),
        )
    ]
    if config is not None:
        configured_api = int(config.inference.http_port)
        if configured_api != api_port:
            ports.append(
                _port_report(
                    name="configured_api",
                    host="127.0.0.1",
                    port=configured_api,
                    configured_host=config.inference.host,
                )
            )
        ports.append(
            _port_report(
                name="grpc",
                host="127.0.0.1",
                port=int(config.inference.grpc_port),
                configured_host=config.inference.host,
            )
        )
    else:
        ports.append(_port_report(name="grpc", host="127.0.0.1", port=50051))
    return ports


def _doctor_missing_config_paths() -> tuple[dict[str, Any], list[tuple[str, Path]]]:
    """Return daemon-aligned path defaults when no config file exists yet."""
    from temms.daemon.service import (
        SYSTEM_DATA_DIR,
        SYSTEM_POLICY_DIR,
        _default_data_dir,
        _path_can_be_created,
        _user_state_dir,
    )

    data_dir = _default_data_dir()
    policy_dir = SYSTEM_POLICY_DIR
    fallback_required = (
        not _path_can_be_created(data_dir)
        or not _path_can_be_created(policy_dir)
    )
    if fallback_required:
        data_dir = _user_state_dir()
        policy_dir = data_dir / "policies"

    return (
        {
            "source": "user_state_fallback" if fallback_required else "daemon_defaults",
            "non_root_fallback": fallback_required,
            "data_dir": str(data_dir),
            "policy_dir": str(policy_dir),
            "system_data_dir": str(SYSTEM_DATA_DIR),
            "system_policy_dir": str(SYSTEM_POLICY_DIR),
        },
        [
            ("data_dir", data_dir),
            ("package_dir", data_dir / "packages"),
            ("policy_dir", policy_dir),
        ],
    )


def _port_report(
    name: str,
    host: str,
    port: int,
    *,
    configured_host: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "host": host,
        "configured_host": configured_host,
        "port": port,
        "status": _port_status(host, port),
    }


def _port_status(host: str, port: int) -> str:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex((host, port)) == 0:
            return "in use"
    return "free"


def _doctor_security_report() -> dict[str, Any]:
    """Return non-secret control-plane and package-signing readiness."""
    api_token = os.environ.get("TEMMS_API_TOKEN")
    hub_token = os.environ.get("TEMMS_HUB_TOKEN")
    signing_key = os.environ.get("TEMMS_PACKAGE_SIGNING_KEY")
    signing_key_file = os.environ.get("TEMMS_PACKAGE_SIGNING_KEY_FILE")
    signing_key_path = Path(signing_key_file) if signing_key_file else None

    if hub_token:
        hub_token_source = "TEMMS_HUB_TOKEN"
    elif api_token:
        hub_token_source = "TEMMS_API_TOKEN fallback"
    else:
        hub_token_source = "not configured"

    if signing_key_file:
        signing_key_source = "TEMMS_PACKAGE_SIGNING_KEY_FILE"
    elif signing_key:
        signing_key_source = "TEMMS_PACKAGE_SIGNING_KEY"
    else:
        signing_key_source = "not configured"

    return {
        "api_token_configured": bool(api_token),
        "control_auth_enabled": bool(api_token),
        "hub_token_configured": bool(hub_token),
        "hub_token_source": hub_token_source,
        "rollout_require_signature": _env_bool_default_true(
            os.environ.get("TEMMS_ROLLOUT_REQUIRE_SIGNATURE")
        ),
        "signing_key_configured": bool(signing_key or signing_key_file),
        "signing_key_source": signing_key_source,
        "signing_key_file": str(signing_key_path) if signing_key_path else None,
        "signing_key_file_exists": (
            signing_key_path.exists() if signing_key_path is not None else None
        ),
    }


def _env_bool_default_true(value: Optional[str]) -> bool:
    """Parse an environment boolean whose unset default is true."""
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _nearest_existing_parent(path: Path) -> Path:
    """Return the closest existing parent for a path that may not exist yet."""
    current = path.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _probe_path_writable(path: Path) -> dict[str, Any]:
    """Create and delete a tiny probe file to prove directory writability."""
    if not path.exists():
        return {
            "ok": False,
            "path": str(path),
            "attempted": False,
            "error": "target does not exist",
        }
    if not path.is_dir():
        return {
            "ok": False,
            "path": str(path),
            "attempted": False,
            "error": "target is not a directory",
        }

    try:
        with tempfile.NamedTemporaryFile(
            prefix=".temms-doctor-",
            dir=path,
            delete=True,
        ) as probe:
            probe.write(b"ok")
            probe.flush()
        return {"ok": True, "path": str(path), "attempted": True, "error": None}
    except Exception as e:
        return {
            "ok": False,
            "path": str(path),
            "attempted": True,
            "error": str(e),
        }


def module_status(module_name: str) -> str:
    """Return a compact availability status for an optional runtime."""
    if importlib.util.find_spec(module_name) is None:
        return "missing"
    if module_name == "onnxruntime":
        try:
            import onnxruntime as ort

            return "found: " + ",".join(ort.get_available_providers())
        except Exception:
            return "found"
    return "found"


@app.command()
def benchmark(
    model: str = typer.Argument(..., help="Cached model ID or model name"),
    slot_name: str = typer.Option("benchmark", "--slot", help="Temporary benchmark slot"),
    samples: int = typer.Option(5, "--samples", "-n", min=1, help="Measured inference runs"),
    warmup: int = typer.Option(1, "--warmup", min=0, help="Warmup inference runs"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write JSON result"),
    hub_url: Optional[str] = typer.Option(
        None,
        "--hub-url",
        help="Publish benchmark evidence to this Hub Lite API base URL",
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help="Hub API token; defaults to TEMMS_HUB_TOKEN or TEMMS_API_TOKEN",
    ),
    device_id: Optional[str] = typer.Option(
        None,
        "--device-id",
        help="Device ID to attach when publishing benchmark evidence",
    ),
    package_id: Optional[str] = typer.Option(
        None,
        "--package-id",
        help="Package ID to attach when publishing benchmark evidence",
    ),
    runtime_target_id: Optional[str] = typer.Option(
        None,
        "--runtime-target-id",
        help="Runtime target ID to attach when publishing benchmark evidence",
    ),
    actor: str = typer.Option(
        "operator:cli",
        "--actor",
        help="Actor label recorded when publishing benchmark evidence",
    ),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Benchmark a cached model on this edge device."""
    import json

    from temms.benchmark import run_benchmark_sync, write_benchmark_result
    from temms.core.cache import ModelCache
    from temms.core.config import Config
    from temms.core.storage import ModelStorage

    if not config_path.exists():
        console.print("[red]Error: TEMMS not initialized. Run 'temms init' first.[/red]")
        raise typer.Exit(1)

    config = Config.load(config_path)
    cache = ModelCache(config.database.path)
    storage = ModelStorage(config.storage.model_dir)

    try:
        result = run_benchmark_sync(
            cache,
            storage,
            model,
            slot_name=slot_name,
            samples=samples,
            warmup=warmup,
        )
    except Exception as e:
        console.print(f"[red]Benchmark failed: {e}[/red]")
        raise typer.Exit(1)

    if output is not None:
        write_benchmark_result(result, output)
        console.print(f"[green]Benchmark written:[/green] {output}")
    else:
        console.print(json.dumps(result, indent=2, sort_keys=True))

    if hub_url is not None:
        import httpx

        with httpx.Client(
            base_url=_hub_api_url(hub_url),
            headers=_hub_auth_headers(token),
            timeout=30.0,
        ) as client:
            record = _checked_json(
                client.post(
                    "/benchmarks",
                    json={
                        "device_id": device_id,
                        "package_id": package_id,
                        "runtime_target_id": runtime_target_id,
                        "result": result,
                        "actor": actor,
                    },
                )
            )
        console.print(f"[green]Benchmark published:[/green] {record['benchmark_id']}")


@app.command()
def package(
    action: str = typer.Argument(
        ...,
        help="Action: from-mlflow, validate, sign, archive, inspect",
    ),
    source: Optional[str] = typer.Argument(None, help="Model URI or package path"),
    slot_name: Optional[str] = typer.Option(None, "--slot", help="Target TEMMS slot"),
    policy_path: Optional[Path] = typer.Option(None, "--policy", help="Policy YAML to include"),
    output_dir: Path = typer.Option(Path("."), "--output", "-o", help="Output directory"),
    tracking_uri: Optional[str] = typer.Option(None, "--tracking-uri", help="MLflow tracking URI"),
    model_format: Optional[str] = typer.Option(None, "--format", help="Model format override"),
    require_schema: bool = typer.Option(
        True,
        "--require-schema/--allow-missing-schema",
        help="Require input/output schema metadata when building MLflow packages",
    ),
    require_runtime_constraints: bool = typer.Option(
        True,
        "--require-runtime-constraints/--allow-missing-runtime-constraints",
        help="Require runtime constraints when building MLflow packages",
    ),
    device_profile: Optional[str] = typer.Option(
        None,
        "--device-profile",
        help="Target/check device profile such as x86_64-cpu or orin-tensorrt",
    ),
    runtime_constraints: Optional[list[str]] = typer.Option(
        None,
        "--runtime-constraint",
        help="Runtime constraint override as key=JSON; repeatable for from-mlflow",
    ),
    runtime_options: Optional[list[str]] = typer.Option(
        None,
        "--runtime-option",
        help="Runtime loader option override as key=JSON; repeatable for from-mlflow",
    ),
    model_artifact: Optional[str] = typer.Option(
        None,
        "--model-artifact",
        help="Relative MLflow artifact path to package when a run contains multiple model files",
    ),
    signing_key: Optional[str] = typer.Option(None, "--signing-key", help="Inline signing key"),
    signing_key_file: Optional[Path] = typer.Option(
        None,
        "--signing-key-file",
        help="File containing signing key",
    ),
    require_signature: bool = typer.Option(
        True,
        "--require-signature/--allow-unsigned-package",
        help="Require signature during validation",
    ),
    check_runtime: bool = typer.Option(
        False,
        "--check-runtime",
        help="Validate runtime constraints against this VM or --device-profile",
    ),
    strict_metadata: bool = typer.Option(
        False,
        "--strict-metadata",
        help="Require production metadata: schemas, provenance, runtime constraints, benchmarks",
    ),
    archive: bool = typer.Option(
        False,
        "--archive",
        help="Create a .temms.tar.zst archive for from-mlflow output",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace an existing from-mlflow package output",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable JSON for validate or inspect",
    ),
):
    """Build, validate, and sign TEMMS edge packages."""
    from temms.core.package_archive import create_package_archive, sign_package_artifact
    from temms.core.package_builder import build_package_from_mlflow
    from temms.core.package_catalog import catalog_entry_from_package
    from temms.core.signing import read_signing_key, validate_package

    key = _package_signing_key(signing_key, signing_key_file, read_signing_key)

    if action == "from-mlflow":
        if source is None:
            console.print("[red]Model URI required, e.g. models:/name/version[/red]")
            raise typer.Exit(1)
        if slot_name is None:
            console.print("[red]--slot is required for MLflow packages[/red]")
            raise typer.Exit(1)
        if policy_path is not None and not policy_path.exists():
            console.print(f"[red]Policy not found: {policy_path}[/red]")
            raise typer.Exit(1)

        try:
            if json_output:
                package_dir = build_package_from_mlflow(
                    model_uri=source,
                    slot=slot_name,
                    policy_path=policy_path,
                    output_dir=output_dir,
                    tracking_uri=tracking_uri,
                    model_format=model_format,
                    device_profile=device_profile,
                    runtime_constraints_override=_parse_json_key_value_options(runtime_constraints),
                    runtime_options_override=_parse_json_key_value_options(runtime_options),
                    model_artifact_path=model_artifact,
                    require_schema=require_schema,
                    require_runtime_constraints=require_runtime_constraints,
                    signing_key=key,
                    strict_metadata=strict_metadata,
                    archive=archive,
                    overwrite=overwrite,
                )
            else:
                with console.status("[bold green]Building TEMMS package from MLflow..."):
                    package_dir = build_package_from_mlflow(
                        model_uri=source,
                        slot=slot_name,
                        policy_path=policy_path,
                        output_dir=output_dir,
                        tracking_uri=tracking_uri,
                        model_format=model_format,
                        device_profile=device_profile,
                        runtime_constraints_override=_parse_json_key_value_options(
                            runtime_constraints
                        ),
                        runtime_options_override=_parse_json_key_value_options(runtime_options),
                        model_artifact_path=model_artifact,
                        require_schema=require_schema,
                        require_runtime_constraints=require_runtime_constraints,
                        signing_key=key,
                        strict_metadata=strict_metadata,
                        archive=archive,
                        overwrite=overwrite,
                    )
            entry = catalog_entry_from_package(
                package_dir,
                require_signature=bool(key),
                signing_key=key,
                device_profiles=[device_profile] if device_profile else None,
                strict_metadata=strict_metadata,
            )
            if json_output:
                import json

                typer.echo(
                    json.dumps(
                        {
                            "schema_version": "temms-package-build/v1",
                            "action": "from-mlflow",
                            "package": entry,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return
            console.print(f"[green]Package created:[/green] {package_dir}")
            console.print(f"SHA256: {entry['sha256']}")
            if key:
                console.print("Signature: signature.json")
        except Exception as e:
            console.print(f"[red]Package build failed: {e}[/red]")
            raise typer.Exit(1)

    elif action == "validate":
        if source is None:
            console.print("[red]Package path required[/red]")
            raise typer.Exit(1)
        result = validate_package(
            Path(source),
            require_signature=require_signature,
            signing_key=key,
            device_profile=device_profile,
            check_runtime_constraints=check_runtime,
            strict_metadata=strict_metadata,
        )
        if json_output:
            import json

            payload = {
                "schema_version": "temms-package-validation/v1",
                "valid": result.valid,
                "errors": result.errors,
                "warnings": result.warnings,
                "signature_verified": result.signature_verified,
                "signature": result.signature_metadata,
                "device_profile": device_profile,
                "runtime_checked": check_runtime,
                "strict_metadata": strict_metadata,
                "package": _package_validation_summary(result.manifest),
            }
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
            if not result.valid:
                raise typer.Exit(1)
            return
        for warning in result.warnings:
            console.print(f"[yellow]Warning:[/yellow] {warning}")
        if not result.valid:
            for error in result.errors:
                console.print(f"[red]Error:[/red] {error}")
            raise typer.Exit(1)
        console.print("[green]Package valid[/green]")
        if result.signature_verified:
            console.print("Signature verified")
            if result.signature_metadata:
                console.print(f"Signer: {result.signature_metadata.get('signer', 'unknown')}")
                key_fingerprint = result.signature_metadata.get("key_fingerprint")
                if key_fingerprint:
                    console.print(f"Key fingerprint: {key_fingerprint}")

    elif action == "sign":
        if source is None:
            console.print("[red]Package path required[/red]")
            raise typer.Exit(1)
        if key is None:
            console.print("[red]Signing key required[/red]")
            raise typer.Exit(1)
        signature_path = sign_package_artifact(Path(source), key)
        console.print(f"[green]Signed package:[/green] {signature_path}")

    elif action == "archive":
        if source is None:
            console.print("[red]Package directory required[/red]")
            raise typer.Exit(1)
        try:
            archive_path = create_package_archive(Path(source))
        except Exception as e:
            console.print(f"[red]Archive failed: {e}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Archived package:[/green] {archive_path}")

    elif action == "inspect":
        if source is None:
            console.print("[red]Package path required[/red]")
            raise typer.Exit(1)
        try:
            entry = catalog_entry_from_package(
                Path(source),
                require_signature=require_signature,
                signing_key=key,
                device_profiles=[device_profile] if device_profile else None,
                strict_metadata=strict_metadata,
            )
        except Exception as e:
            console.print(f"[red]Package inspect failed: {e}[/red]")
            raise typer.Exit(1)

        if json_output:
            import json

            typer.echo(json.dumps(entry, indent=2, sort_keys=True))
            return

        metadata = entry["metadata"]
        validation = metadata.get("validation", {})
        table = Table(title="TEMMS Package")
        table.add_column("Field", style="bold")
        table.add_column("Value")
        table.add_row("Package ID", entry["package_id"])
        table.add_row("Name", entry["name"])
        table.add_row("Version", entry["version"])
        table.add_row("SHA256", entry["sha256"])
        table.add_row("Path", entry["path"])
        table.add_row(
            "Device Profiles",
            ", ".join(entry.get("device_profiles") or []) or "any",
        )
        table.add_row(
            "Signature",
            "verified" if validation.get("signature_verified") else "not verified",
        )
        table.add_row("Models", str(len(metadata.get("models", []))))
        table.add_row("Policies", str(len(metadata.get("policies", []))))
        console.print(table)
        if validation.get("warnings"):
            for warning in validation["warnings"]:
                console.print(f"[yellow]Warning:[/yellow] {warning}")

    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        console.print("Valid actions: from-mlflow, validate, sign, archive, inspect")
        raise typer.Exit(1)


def _package_validation_summary(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return compact package metadata for machine-readable validation output."""
    if manifest is None:
        return None
    models = manifest.get("models") if isinstance(manifest.get("models"), list) else []
    policies = manifest.get("policies") if isinstance(manifest.get("policies"), list) else []
    return {
        "schema_version": manifest.get("schema_version"),
        "package_id": manifest.get("package_id"),
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "models": len(models),
        "policies": len(policies),
        "compatibility": manifest.get("compatibility") or {},
        "source_registry": manifest.get("source_registry"),
        "mlflow_run_id": manifest.get("mlflow_run_id"),
    }


@app.command()
def hub(
    action: str = typer.Argument(
        ...,
        help=(
            "Action: enroll, devices, packages, runtime-targets, rollouts, status, "
            "package-from-mlflow, register-package, register-runtime, validate-runtime, "
            "runtime-validations, benchmarks, preview-compatibility, assign, apply, rollback, "
            "export, import, replay-telemetry, telemetry"
        ),
    ),
    source: Optional[str] = typer.Argument(
        None,
        help=(
            "MLflow model URI, package path, rollout ID, air-gap bundle path, "
            "or telemetry bundle path"
        ),
    ),
    hub_url: str = typer.Option(
        "http://127.0.0.1:8080",
        "--hub-url",
        help="TEMMS Hub Lite API base URL",
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help="Hub API token; defaults to TEMMS_HUB_TOKEN or TEMMS_API_TOKEN",
    ),
    device_id: Optional[str] = typer.Option(None, "--device-id", help="Target device ID"),
    package_id: Optional[str] = typer.Option(None, "--package-id", help="Package ID"),
    slot_name: Optional[str] = typer.Option(None, "--slot", help="Target rollout slot"),
    rollout_id: Optional[str] = typer.Option(None, "--rollout-id", help="Rollout ID"),
    runtime_target_id: Optional[str] = typer.Option(
        None,
        "--runtime-target-id",
        help="Container runtime target for rollout assignment or runtime registration",
    ),
    require_runtime_validation: bool = typer.Option(
        False,
        "--require-runtime-validation",
        help="Require a passing runtime-target validation before rollout assignment",
    ),
    image: Optional[str] = typer.Option(
        None,
        "--image",
        help="Container image for register-runtime",
    ),
    os_name: str = typer.Option(
        "linux",
        "--os",
        help="Runtime target OS for register-runtime",
    ),
    arch: Optional[str] = typer.Option(
        None,
        "--arch",
        help="Runtime target architecture such as amd64 or arm64",
    ),
    runtimes: Optional[list[str]] = typer.Option(
        None,
        "--runtime",
        help="Runtime available in the target image; repeatable",
    ),
    providers: Optional[list[str]] = typer.Option(
        None,
        "--provider",
        help="ONNX provider available in the target image; repeatable",
    ),
    accelerators: Optional[list[str]] = typer.Option(
        None,
        "--accelerator",
        help="Accelerator available to the target image; repeatable",
    ),
    tracking_uri: Optional[str] = typer.Option(
        None,
        "--tracking-uri",
        help="MLflow tracking URI for package-from-mlflow",
    ),
    model_artifact: Optional[str] = typer.Option(
        None,
        "--model-artifact",
        help="Relative MLflow artifact path for package-from-mlflow",
    ),
    require_schema: bool = typer.Option(
        True,
        "--require-schema/--allow-missing-schema",
        help="Require input/output schema metadata for package-from-mlflow",
    ),
    archive: bool = typer.Option(
        True,
        "--archive/--directory-package",
        help="Build an archive or directory package for package-from-mlflow",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace an existing Hub package-from-mlflow output",
    ),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file"),
    include_packages: bool = typer.Option(
        False,
        "--include-packages",
        help="Embed package artifacts when exporting an air-gap bundle",
    ),
    pull_image: bool = typer.Option(
        False,
        "--pull-image",
        help="Pull the runtime target image before container validation",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the runtime validation Docker command without running it",
    ),
    timeout_s: int = typer.Option(
        300,
        "--timeout-s",
        help="Runtime target validation timeout in seconds",
    ),
    strict_metadata: bool = typer.Option(
        True,
        "--strict-metadata/--no-strict-metadata",
        help="Require production package metadata during package registration and validation",
    ),
    require_signature: bool = typer.Option(
        True,
        "--require-signature/--allow-unsigned-package",
        help="Require signature for package registration or rollout apply",
    ),
    signing_key: Optional[str] = typer.Option(None, "--signing-key", help="Inline signing key"),
    signing_key_file: Optional[Path] = typer.Option(
        None,
        "--signing-key-file",
        help="File containing signing key",
    ),
    device_profile: Optional[str] = typer.Option(
        None,
        "--device-profile",
        help="Device profile for enrollment or package registration",
    ),
    labels: Optional[list[str]] = typer.Option(
        None,
        "--label",
        help="Device enrollment label in key=value form; repeatable",
    ),
    inventory: Optional[list[str]] = typer.Option(
        None,
        "--inventory",
        help="Device enrollment inventory in key=value form; repeatable",
    ),
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help="Operator or automation actor recorded in rollout audit history",
    ),
    reason: str = typer.Option(
        "cli rollback",
        "--reason",
        help="Reason recorded for Hub Lite rollback actions",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
):
    """Operate Hub Lite from the CLI."""
    import json

    import httpx

    from temms.core.runtime_target_runner import validate_runtime_target_package
    from temms.core.signing import read_signing_key

    base_url = _hub_api_url(hub_url)
    headers = _hub_auth_headers(token)
    key = _hub_package_signing_key(signing_key, signing_key_file, read_signing_key)

    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=30.0) as client:
            if action == "enroll":
                if device_id is None:
                    console.print("[red]--device-id is required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        "/devices/enroll",
                        json={
                            "device_id": device_id,
                            "profile": device_profile,
                            "labels": _parse_key_value_options(labels),
                            "inventory": _parse_key_value_options(inventory),
                        },
                    )
                )
            elif action == "devices":
                payload = _checked_json(client.get("/devices"))
            elif action == "packages":
                payload = _checked_json(client.get("/packages"))
            elif action == "runtime-targets":
                payload = _checked_json(client.get("/runtime-targets"))
            elif action == "runtime-validations":
                params = {}
                if package_id:
                    params["package_id"] = package_id
                if runtime_target_id:
                    params["runtime_target_id"] = runtime_target_id
                payload = _checked_json(
                    client.get("/runtime-targets/validations", params=params or None)
                )
            elif action == "benchmarks":
                params = {}
                if device_id:
                    params["device_id"] = device_id
                if package_id:
                    params["package_id"] = package_id
                if runtime_target_id:
                    params["runtime_target_id"] = runtime_target_id
                payload = _checked_json(client.get("/benchmarks", params=params or None))
            elif action == "rollouts":
                payload = _checked_json(client.get("/rollouts"))
            elif action == "status":
                payload = _checked_json(client.get("/deployment-status"))
            elif action == "telemetry":
                payload = _checked_json(client.get("/telemetry"))
            elif action == "package-from-mlflow":
                if source is None:
                    console.print("[red]MLflow model URI required, e.g. models:/name/version[/red]")
                    raise typer.Exit(1)
                if slot_name is None:
                    console.print("[red]--slot is required[/red]")
                    raise typer.Exit(1)
                runtime_constraints: dict[str, Any] = {}
                if device_profile:
                    runtime_constraints["device_profiles"] = [device_profile]
                if runtimes:
                    runtime_constraints["runtimes"] = runtimes
                if providers:
                    runtime_constraints["preferred_providers"] = providers
                if accelerators:
                    runtime_constraints["accelerators"] = accelerators
                runtime_options: dict[str, Any] = {}
                if providers:
                    runtime_options["providers"] = providers
                payload = _checked_json(
                    client.post(
                        "/packages/from-mlflow",
                        json={
                            "model_uri": source,
                            "slot": slot_name,
                            "tracking_uri": tracking_uri,
                            "device_profile": device_profile,
                            "runtime_constraints": runtime_constraints,
                            "runtime_options": runtime_options,
                            "model_artifact_path": model_artifact,
                            "require_schema": require_schema,
                            "require_signature": require_signature,
                            "signing_key": key,
                            "archive": archive,
                            "overwrite": overwrite,
                            "strict_metadata": strict_metadata,
                            "actor": actor,
                        },
                    )
                )
            elif action == "register-package":
                if source is None:
                    console.print("[red]Package path required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        "/packages/register",
                        json={
                            "package_path": str(Path(source).expanduser()),
                            "require_signature": require_signature,
                            "signing_key": key,
                            "device_profiles": [device_profile] if device_profile else None,
                            "strict_metadata": strict_metadata,
                            "actor": actor,
                        },
                    )
                )
            elif action == "register-runtime":
                if runtime_target_id is None or image is None:
                    console.print("[red]--runtime-target-id and --image are required[/red]")
                    raise typer.Exit(1)
                runtime_inventory = {runtime: {"available": True} for runtime in (runtimes or [])}
                if providers:
                    runtime_inventory.setdefault("onnxruntime", {"available": True})[
                        "providers"
                    ] = providers
                accelerator_inventory = {
                    accelerator: {"available": True} for accelerator in (accelerators or [])
                }
                constraints: dict[str, Any] = {}
                if device_profile:
                    constraints["device_profiles"] = [device_profile]
                if runtimes:
                    constraints["runtimes"] = runtimes
                if providers:
                    constraints["preferred_providers"] = providers
                if accelerators:
                    constraints["accelerators"] = accelerators
                payload = _checked_json(
                    client.post(
                        "/runtime-targets",
                        json={
                            "runtime_target_id": runtime_target_id,
                            "name": runtime_target_id,
                            "image": image,
                            "os": os_name,
                            "arch": arch,
                            "device_profiles": [device_profile] if device_profile else [],
                            "runtimes": runtime_inventory,
                            "accelerators": accelerator_inventory,
                            "runtime_constraints": constraints,
                            "labels": _parse_key_value_options(labels),
                            "actor": actor,
                        },
                    )
                )
            elif action == "validate-runtime":
                if source is None or runtime_target_id is None:
                    console.print("[red]Package path and --runtime-target-id are required[/red]")
                    raise typer.Exit(1)
                targets_payload = _checked_json(client.get("/runtime-targets"))
                runtime_target = _find_runtime_target(
                    targets_payload.get("runtime_targets", []),
                    runtime_target_id,
                )
                result = validate_runtime_target_package(
                    runtime_target,
                    Path(source),
                    require_signature=require_signature,
                    strict_metadata=strict_metadata,
                    signing_key=key,
                    signing_key_file=signing_key_file,
                    pull_image=pull_image,
                    dry_run=dry_run,
                    timeout_s=timeout_s,
                )
                payload = {
                    "schema_version": "temms-runtime-target-validation/v1",
                    **result.to_dict(),
                }
                result_payload = dict(payload)
                validation_record = _checked_json(
                    client.post(
                        "/runtime-targets/validations",
                        json={
                            "runtime_target_id": runtime_target_id,
                            "package_id": package_id,
                            "package_path": str(Path(source).expanduser()),
                            "result": result_payload,
                            "actor": actor,
                        },
                    )
                )
                payload["validation_record"] = validation_record
            elif action == "preview-compatibility":
                if device_id is None or package_id is None:
                    console.print("[red]--device-id and --package-id are required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        "/compatibility/preview",
                        json={
                            "device_id": device_id,
                            "package_id": package_id,
                            "runtime_target_id": runtime_target_id,
                        },
                    )
                )
            elif action == "assign":
                if device_id is None or package_id is None:
                    console.print("[red]--device-id and --package-id are required[/red]")
                    raise typer.Exit(1)
                request = {
                    "device_id": device_id,
                    "package_id": package_id,
                    "slot": slot_name,
                    "rollout_id": rollout_id,
                    "runtime_target_id": runtime_target_id,
                    "require_runtime_validation": require_runtime_validation,
                    "actor": actor,
                }
                payload = _checked_json(client.post("/rollouts", json=request))
            elif action == "apply":
                target_rollout = source or rollout_id
                if target_rollout is None:
                    console.print("[red]Rollout ID required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        f"/rollouts/{target_rollout}/apply",
                        json={
                            "require_signature": require_signature,
                            "signing_key": key,
                            "actor": actor,
                        },
                    )
                )
            elif action == "rollback":
                target_rollout = source or rollout_id
                if target_rollout is None:
                    console.print("[red]Rollout ID required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        f"/rollouts/{target_rollout}/rollback",
                        json={"reason": reason, "actor": actor},
                    )
                )
            elif action == "export":
                payload = _checked_json(
                    client.post(
                        "/airgap/export",
                        json={"include_packages": include_packages},
                    )
                )
                if output is not None:
                    output.write_text(
                        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
                    )
                    console.print(f"[green]Hub bundle written:[/green] {output}")
                    return
            elif action == "import":
                if source is None:
                    console.print("[red]Bundle path required[/red]")
                    raise typer.Exit(1)
                bundle = json.loads(Path(source).read_text(encoding="utf-8"))
                payload = _checked_json(client.post("/airgap/import", json=bundle))
            elif action == "replay-telemetry":
                if source is None:
                    console.print("[red]Telemetry bundle path required[/red]")
                    raise typer.Exit(1)
                bundle = json.loads(Path(source).read_text(encoding="utf-8"))
                payload = _checked_json(
                    client.post(
                        "/telemetry/replay",
                        json={"bundle": bundle, "device_id": device_id, "actor": actor},
                    )
                )
            else:
                console.print(f"[red]Unknown action: {action}[/red]")
                console.print(
                    "Valid actions: enroll, devices, packages, runtime-targets, rollouts, "
                    "status, package-from-mlflow, register-package, register-runtime, "
                    "validate-runtime, runtime-validations, benchmarks, preview-compatibility, "
                    "assign, apply, rollback, export, import, replay-telemetry, telemetry"
                )
                raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Hub command failed: {e}[/red]")
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        if action == "validate-runtime" and not payload.get("ok", True):
            raise typer.Exit(1)
        if action == "preview-compatibility" and not payload.get("compatible", True):
            raise typer.Exit(1)
        return
    _print_hub_payload(action, payload)
    if action == "validate-runtime" and not payload.get("ok", True):
        raise typer.Exit(1)
    if action == "preview-compatibility" and not payload.get("compatible", True):
        raise typer.Exit(1)


def _hub_api_url(url: str) -> str:
    """Normalize Hub Lite URL to the /v1/hub API prefix."""
    base = url.rstrip("/")
    if base.endswith("/v1/hub"):
        return base
    return f"{base}/v1/hub"


def _hub_auth_headers(token: Optional[str]) -> dict[str, str]:
    """Return Hub Lite auth headers for CLI calls."""
    resolved = token or os.environ.get("TEMMS_HUB_TOKEN") or os.environ.get("TEMMS_API_TOKEN")
    if not resolved:
        return {}
    return {"X-TEMMS-Token": resolved}


def _package_signing_key(signing_key, signing_key_file, read_key) -> Optional[str]:
    """Resolve package verification key from CLI args or TEMMS package env vars."""
    resolved = read_key(signing_key, signing_key_file)
    if resolved:
        return resolved
    env_key = os.environ.get("TEMMS_PACKAGE_SIGNING_KEY")
    if env_key:
        return env_key
    env_key_file = os.environ.get("TEMMS_PACKAGE_SIGNING_KEY_FILE")
    if env_key_file:
        return Path(env_key_file).read_text(encoding="utf-8").strip()
    return None


def _hub_package_signing_key(signing_key, signing_key_file, read_key) -> Optional[str]:
    """Resolve package verification key for Hub CLI calls."""
    return _package_signing_key(signing_key, signing_key_file, read_key)


def _checked_json(response) -> dict:
    """Return JSON or raise with a useful HTTP error."""
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
    return response.json()


def _find_runtime_target(
    runtime_targets: list[dict[str, Any]], runtime_target_id: str
) -> dict[str, Any]:
    """Find a runtime target by ID from a Hub Lite list response."""
    for runtime_target in runtime_targets:
        if runtime_target.get("runtime_target_id") == runtime_target_id:
            return runtime_target
    available = ", ".join(
        sorted(str(target.get("runtime_target_id")) for target in runtime_targets if target)
    )
    suffix = f" Available targets: {available}" if available else ""
    raise RuntimeError(f"Runtime target not found: {runtime_target_id}.{suffix}")


def _parse_key_value_options(values: Optional[list[str]]) -> dict[str, str]:
    """Parse repeated key=value CLI options."""
    parsed: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"Expected key=value, got: {value}")
        key, item_value = value.split("=", 1)
        if not key:
            raise ValueError(f"Expected non-empty key in: {value}")
        parsed[key] = item_value
    return parsed


def _parse_json_key_value_options(values: Optional[list[str]]) -> dict[str, Any]:
    """Parse repeated key=JSON CLI options."""
    import json

    parsed: dict[str, Any] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"Expected key=JSON, got: {value}")
        key, item_value = value.split("=", 1)
        if not key:
            raise ValueError(f"Expected non-empty key in: {value}")
        try:
            parsed[key] = json.loads(item_value)
        except json.JSONDecodeError:
            parsed[key] = item_value
    return parsed


def _print_hub_payload(action: str, payload: dict) -> None:
    """Print common Hub Lite payloads in compact tables."""
    if action == "enroll":
        console.print("[green]Hub device enrolled[/green]")
        console.print(f"Device: {payload.get('device_id', '')}")
        console.print(f"Profile: {payload.get('profile', '')}")
        return

    if action == "devices":
        table = Table(title="Hub Devices")
        table.add_column("Device")
        table.add_column("Profile")
        table.add_column("Status")
        table.add_column("Last Seen")
        for device in payload.get("devices", []):
            table.add_row(
                device.get("device_id", ""),
                device.get("profile", ""),
                device.get("status", ""),
                device.get("last_seen_at", ""),
            )
        console.print(table)
        return

    if action == "packages":
        table = Table(title="Hub Packages")
        table.add_column("Package")
        table.add_column("Name")
        table.add_column("Version")
        table.add_column("Profiles")
        for package in payload.get("packages", []):
            table.add_row(
                package.get("package_id", ""),
                package.get("name", ""),
                package.get("version", ""),
                ", ".join(package.get("device_profiles", []) or []),
            )
        console.print(table)
        return

    if action == "runtime-targets":
        table = Table(title="Hub Runtime Targets")
        table.add_column("Target")
        table.add_column("Image")
        table.add_column("OS/Arch")
        table.add_column("Profiles")
        table.add_column("Source")
        for target in payload.get("runtime_targets", []):
            table.add_row(
                target.get("runtime_target_id", ""),
                target.get("image", ""),
                f"{target.get('os', 'linux')}/{target.get('arch') or ''}",
                ", ".join(target.get("device_profiles", []) or []),
                target.get("source", ""),
            )
        console.print(table)
        return

    if action == "register-runtime":
        console.print("[green]Runtime target registered[/green]")
        console.print(f"Target: {payload.get('runtime_target_id', '')}")
        console.print(f"Image: {payload.get('image', '')}")
        return

    if action == "package-from-mlflow":
        package = payload.get("package", {})
        console.print("[green]Hub package built from MLflow[/green]")
        console.print(f"Package: {package.get('package_id', '')}")
        console.print(f"Path: {payload.get('package_path', '')}")
        console.print(f"Signed: {payload.get('signed', False)}")
        return

    if action == "validate-runtime":
        status = "ready" if payload.get("dry_run") else "passed" if payload.get("ok") else "failed"
        color = "green" if payload.get("ok") else "red"
        console.print(f"[{color}]Runtime target validation {status}[/{color}]")
        console.print(f"Target: {payload.get('runtime_target_id', '')}")
        console.print(f"Image: {payload.get('image', '')}")
        validation_record = payload.get("validation_record") or {}
        if validation_record.get("validation_id"):
            console.print(f"Evidence: {validation_record.get('validation_id')}")
        console.print(f"Command: {payload.get('command_text', '')}")
        if payload.get("exit_code") is not None:
            console.print(f"Exit code: {payload.get('exit_code')}")
        stdout = (payload.get("stdout") or "").strip()
        stderr = (payload.get("stderr") or "").strip()
        if stdout:
            console.print(f"stdout:\n{stdout}")
        if stderr:
            console.print(f"stderr:\n{stderr}")
        return

    if action == "runtime-validations":
        table = Table(title="Hub Runtime Validations")
        table.add_column("Validation")
        table.add_column("Package")
        table.add_column("Runtime")
        table.add_column("Result")
        table.add_column("Actor")
        table.add_column("Created")
        for validation in payload.get("runtime_validations", []):
            result = validation.get("result") or {}
            status = "preview" if result.get("dry_run") else "pass" if result.get("ok") else "fail"
            table.add_row(
                validation.get("validation_id", ""),
                validation.get("package_id") or validation.get("package_path") or "",
                validation.get("runtime_target_id", ""),
                status,
                validation.get("actor") or "",
                validation.get("created_at") or "",
            )
        console.print(table)
        return

    if action == "benchmarks":
        table = Table(title="Hub Benchmarks")
        table.add_column("Benchmark")
        table.add_column("Device")
        table.add_column("Package")
        table.add_column("Runtime")
        table.add_column("Model")
        table.add_column("p95 ms")
        table.add_column("Created")
        for benchmark in payload.get("benchmarks", []):
            result = benchmark.get("result") or {}
            latency = result.get("latency_ms") if isinstance(result.get("latency_ms"), dict) else {}
            p95 = latency.get("p95")
            table.add_row(
                benchmark.get("benchmark_id", ""),
                benchmark.get("device_id") or "",
                benchmark.get("package_id") or "",
                benchmark.get("runtime_target_id") or "",
                benchmark.get("model_id") or result.get("model_id") or "",
                "" if p95 is None else str(p95),
                benchmark.get("created_at") or "",
            )
        console.print(table)
        return

    if action == "preview-compatibility":
        color = "green" if payload.get("compatible") else "red"
        status = "compatible" if payload.get("compatible") else "blocked"
        device = payload.get("device") or {}
        package = payload.get("package") or {}
        runtime_target = payload.get("runtime_target") or {}
        console.print(f"[{color}]Rollout compatibility {status}[/{color}]")
        console.print(f"Device: {device.get('device_id', '')} ({device.get('profile', 'unknown')})")
        console.print(f"Package: {package.get('package_id', '')} v{package.get('version', '')}")
        console.print(
            "Runtime: "
            + (
                f"{runtime_target.get('runtime_target_id')} ({runtime_target.get('image')})"
                if runtime_target
                else "auto / device inventory"
            )
        )
        for failure in payload.get("failures", []):
            console.print(f"[red]Failure:[/red] {failure}")
        return

    if action == "rollouts":
        table = Table(title="Hub Rollouts")
        table.add_column("Rollout")
        table.add_column("Device")
        table.add_column("Package")
        table.add_column("Slot")
        table.add_column("Runtime")
        table.add_column("State")
        for rollout in payload.get("rollouts", []):
            table.add_row(
                rollout.get("rollout_id", ""),
                rollout.get("device_id", ""),
                rollout.get("package_id", ""),
                rollout.get("slot", "") or "",
                rollout.get("runtime_target_id", "") or "auto",
                rollout.get("state", ""),
            )
        console.print(table)
        return

    if action == "status":
        devices = payload.get("devices", {})
        deployments = payload.get("deployment_status", {})
        rollouts = payload.get("rollouts", {})
        telemetry = payload.get("telemetry_events", {})
        console.print("[bold]Hub Deployment Status[/bold]")
        console.print(f"Devices: {len(devices)}")
        console.print(f"Deployment snapshots: {len(deployments)}")
        console.print(f"Rollouts: {len(rollouts)}")
        console.print(f"Replayed telemetry events: {len(telemetry)}")
        return

    if action == "telemetry":
        table = Table(title="Hub Replayed Telemetry")
        table.add_column("Event")
        table.add_column("Type")
        table.add_column("Device")
        table.add_column("Timestamp")
        for event in payload.get("events", []):
            table.add_row(
                event.get("event_id", ""),
                event.get("event_type", ""),
                event.get("device_id", "") or "",
                event.get("timestamp", ""),
            )
        console.print(table)
        return

    console.print("[green]Hub command succeeded[/green]")
    if "rollout_id" in payload:
        console.print(f"Rollout: {payload['rollout_id']} ({payload.get('state', 'unknown')})")
    elif "package_id" in payload:
        console.print(f"Package: {payload['package_id']} v{payload.get('version', '')}")
    elif "status" in payload:
        console.print(f"Status: {payload['status']}")


@app.command()
def mlflow(
    action: str = typer.Argument(..., help="Action: list, register, pull"),
    model_name: Optional[str] = typer.Argument(None, help="Model name (for pull)"),
    model_version: Optional[str] = typer.Option(None, "--version", "-v", help="Model version"),
    tracking_uri: Optional[str] = typer.Option(None, "--tracking-uri", help="MLflow tracking URI"),
    allow_dev_pull: bool = typer.Option(
        False,
        "--allow-dev-pull",
        help="Explicitly allow local-development direct MLflow pull",
    ),
    config_path: Path = typer.Option(
        Path("/etc/temms/temms.yaml"),
        "--config",
        "-c",
        help="Configuration file path",
    ),
):
    """Local-development MLflow bridge commands."""
    if action == "pull":
        if model_name is None:
            console.print("[red]Model name required for pull[/red]")
            raise typer.Exit(1)

        if model_version:
            model_uri = f"models:/{model_name}/{model_version}"
        else:
            model_uri = f"models:/{model_name}/<version>"

        console.print("[yellow]Direct MLflow pulls are for local development only.[/yellow]")
        console.print("Production edge deployments should build signed packages from the registry:")
        console.print(
            f"  temms package from-mlflow {model_uri} --slot <slot> "
            "--tracking-uri <uri> --signing-key-file <key> --archive"
        )
        if not allow_dev_pull:
            console.print("\n[red]Refusing direct MLflow pull without --allow-dev-pull.[/red]")
            raise typer.Exit(1)

    try:
        from temms.mlflow_bridge import MLflowBridge
    except ImportError:
        console.print("[red]MLflow not installed. Install with: pip install mlflow[/red]")
        raise typer.Exit(1)

    bridge = MLflowBridge(tracking_uri=tracking_uri)

    if not bridge.available:
        console.print("[red]MLflow package not available. Install with: pip install mlflow[/red]")
        raise typer.Exit(1)

    if action == "list":
        console.print("[bold]MLflow Registered Models[/bold]\n")
        models = bridge.list_models()

        if not models:
            console.print("[yellow]No models found in MLflow[/yellow]")
            raise typer.Exit(0)

        table = Table(title="MLflow Models")
        table.add_column("Name", style="cyan")
        table.add_column("Latest Version", style="green")
        table.add_column("Status", style="yellow")

        for model in models:
            versions = model.get("latest_versions", [])
            if versions:
                for v in versions:
                    table.add_row(
                        model["name"],
                        v.get("version", "-"),
                        v.get("status", "-"),
                    )
            else:
                table.add_row(model["name"], "-", "-")

        console.print(table)

    elif action == "register":
        from temms.core.config import Config
        from temms.core.cache import ModelCache

        if not config_path.exists():
            console.print("[red]TEMMS not initialized. Run 'temms init' first.[/red]")
            raise typer.Exit(1)

        config = Config.load(config_path)
        cache = ModelCache(config.database.path)
        models = cache.list_models()

        if not models:
            console.print("[yellow]No models in cache to register[/yellow]")
            raise typer.Exit(0)

        console.print(f"[bold]Registering {len(models)} models in MLflow...[/bold]")

        # Create a mock import result for registration
        from temms.core.package import ImportedPackageResult

        result = ImportedPackageResult(
            package=None,
            models=models,
            policies=[],
            manifest=None,
        )

        count = bridge.register_imported_models(result)
        console.print(f"[green]Registered {count} models in MLflow[/green]")

    elif action == "pull":
        console.print(f"[bold]Pulling {model_name} from MLflow...[/bold]")
        package_dir = bridge.pull_model(model_name, version=model_version)

        if package_dir:
            console.print(f"[green]Model pulled to: {package_dir}[/green]")
            console.print(
                "\nImport for local dev with: "
                f"temms import {package_dir} --allow-unsigned-package --allow-lab-metadata"
            )
        else:
            console.print("[red]Failed to pull model[/red]")
            raise typer.Exit(1)

    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        console.print("Valid actions: list, register, pull")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
