"""
Main TEMMS CLI application using Typer.
"""

import importlib.util
import os
import shlex
import socket
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

keys_app = typer.Typer(help="Manage Ed25519 package signing keys")
app.add_typer(keys_app, name="keys")


@keys_app.command("generate")
def keys_generate(
    out_dir: Path = typer.Option(Path("."), "--out-dir", help="Where to write key files"),
    name: str = typer.Option("temms-signing", "--name", help="Base name for the key files"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing key files"),
) -> None:
    """Generate an Ed25519 signing keypair.

    The Hub signs packages with the private key; edge daemons are provisioned
    with the public key only and verify offline. Keep the private key secret.
    """
    from temms.core.signing import generate_ed25519_keypair

    out_dir.mkdir(parents=True, exist_ok=True)
    private_path = out_dir / f"{name}.private.pem"
    public_path = out_dir / f"{name}.public.pem"
    if not force and (private_path.exists() or public_path.exists()):
        console.print(f"[red]Key files already exist under {out_dir} (use --force)[/red]")
        raise typer.Exit(1)

    private_pem, public_pem, fingerprint = generate_ed25519_keypair()
    private_path.write_text(private_pem, encoding="utf-8")
    private_path.chmod(0o600)
    public_path.write_text(public_pem, encoding="utf-8")

    console.print(f"[green]Private key:[/green] {private_path} (keep secret; chmod 600)")
    console.print(f"[green]Public key: [/green] {public_path} (provision to edge daemons)")
    console.print(f"[green]Fingerprint:[/green] {fingerprint}")


@keys_app.command("fingerprint")
def keys_fingerprint(
    key_file: Path = typer.Argument(..., help="Path to a private or public key file"),
) -> None:
    """Print the fingerprint of a signing key (private or public)."""
    from temms.core.signing import signing_key_fingerprint

    console.print(signing_key_fingerprint(key_file.read_text(encoding="utf-8")))


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
    input_bundle: Optional[Path] = typer.Option(
        None,
        "--input",
        help="Read an existing evidence bundle JSON file",
    ),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Export an operator-readable mission replay summary",
    ),
    replay: bool = typer.Option(
        False,
        "--replay",
        help="Export a chronological mission replay artifact",
    ),
    summary_limit: int = typer.Option(
        20,
        "--summary-limit",
        help="Maximum number of recent timeline entries in a summary or replay",
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
    from temms.evidence import (
        EvidenceBundleBuilder,
        build_mission_replay,
        summarize_evidence_bundle,
    )
    from temms.policy.engine import PolicyEngine
    from temms.slots.manager import SlotManager

    if input_bundle is not None:
        if not input_bundle.exists():
            console.print(f"[red]Error: Evidence bundle not found: {input_bundle}[/red]")
            raise typer.Exit(1)
        try:
            bundle = json.loads(input_bundle.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            console.print(f"[red]Error: Invalid evidence JSON: {exc}[/red]")
            raise typer.Exit(1)
    else:
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
            policy_files = sorted(list(policy_dir.glob("*.yaml")) + list(policy_dir.glob("*.yml")))
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
            offline_mode=bool(deployment_state and deployment_state.get("state") == "OFFLINE"),
            pending_operations=pending_operations,
            deployment_state=deployment_state,
        )

    if summary and replay:
        console.print("[red]Error: Use either --summary or --replay, not both.[/red]")
        raise typer.Exit(1)

    if replay:
        payload = build_mission_replay(bundle, limit=summary_limit)
    elif summary:
        payload = summarize_evidence_bundle(bundle, limit=summary_limit)
    else:
        payload = bundle
    rendered = json.dumps(payload, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        label = "Mission replay" if replay else "Evidence summary" if summary else "Evidence bundle"
        console.print(f"[green]{label} exported:[/green] {output}")
        if replay:
            console.print(f"Replay events: {len(payload['events'])}")
        elif summary:
            console.print(f"Timeline entries: {len(payload['timeline'])}")
        else:
            console.print(f"Decisions: {len(bundle['decisions'])}")
            console.print(f"SHA256: {bundle['integrity']['payload_sha256']}")
    else:
        console.print(rendered, markup=False, highlight=False, soft_wrap=True)


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
        "RBAC roles",
        ", ".join(security["rbac_roles"]) if security["rbac_roles"] else "not configured",
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
    fallback_required = not _path_can_be_created(data_dir) or not _path_can_be_created(policy_dir)
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
    rbac_tokens = os.environ.get("TEMMS_RBAC_TOKENS")
    signing_key = os.environ.get("TEMMS_PACKAGE_SIGNING_KEY")
    signing_key_file = os.environ.get("TEMMS_PACKAGE_SIGNING_KEY_FILE")
    signing_key_path = Path(signing_key_file) if signing_key_file else None
    from temms.inference.server import parse_rbac_token_roles

    rbac_token_roles = parse_rbac_token_roles(rbac_tokens)
    rbac_roles = sorted({role for roles in rbac_token_roles.values() for role in roles})

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
        "control_auth_enabled": bool(api_token or rbac_token_roles),
        "hub_token_configured": bool(hub_token),
        "hub_token_source": hub_token_source,
        "rbac_tokens_configured": bool(rbac_token_roles),
        "rbac_roles": rbac_roles,
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
def control(
    action: str = typer.Argument(
        ...,
        help=(
            "Action: offline, online, deploy, sync-preview, sync, retarget-runtime, "
            "quarantine-blocked, requeue-dead-letters, acknowledge-dead-letters"
        ),
    ),
    control_url: str = typer.Option(
        "http://127.0.0.1:8080",
        "--control-url",
        help="TEMMS edge control API base URL",
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help="Control API token; defaults to TEMMS_HUB_TOKEN or TEMMS_API_TOKEN",
    ),
    payload_sha256: Optional[str] = typer.Option(
        None,
        "--payload-sha256",
        help="Pending DDIL intent payload SHA256 for retarget-runtime",
    ),
    package_id: Optional[str] = typer.Option(None, "--package-id", help="Package ID"),
    model_id: Optional[str] = typer.Option(None, "--model-id", help="Model ID"),
    device_id: Optional[str] = typer.Option(None, "--device-id", help="Target edge device ID"),
    runtime_target_id: Optional[str] = typer.Option(
        None,
        "--runtime-target-id",
        help=(
            "Runtime target for deploy or retarget-runtime; retarget-runtime can "
            "auto-select the measured candidate when omitted"
        ),
    ),
    slot_name: Optional[str] = typer.Option(None, "--slot", help="Target slot"),
    actor: str = typer.Option(
        "operator:temms-cli",
        "--actor",
        help="Operator or automation actor recorded in edge audit history",
    ),
    source: str = typer.Option(
        "temms-control-cli",
        "--source",
        help="Source label recorded on deploy intents",
    ),
    reason: Optional[str] = typer.Option(
        None,
        "--reason",
        help="Reason recorded for retarget/quarantine/acknowledgement actions",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="For requeue-dead-letters, bypass the safe ready-preflight gate",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
):
    """Operate the local edge control plane from the CLI."""
    import json

    import httpx

    base_url = _control_api_url(control_url)
    headers = _hub_auth_headers(token)

    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=30.0) as client:
            if action == "offline":
                payload = _checked_json(client.post("/offline"))
            elif action == "online":
                payload = _checked_json(client.post("/online"))
            elif action == "sync-preview":
                payload = _checked_json(client.get("/sync/preview"))
            elif action == "sync":
                payload = _checked_json(client.post("/sync"))
            elif action == "deploy":
                if model_id is None or slot_name is None:
                    console.print("[red]--model-id and --slot are required for deploy[/red]")
                    raise typer.Exit(1)
                request = _control_deploy_body(
                    actor=actor,
                    source=source,
                    package_id=package_id,
                    model_id=model_id,
                    device_id=device_id,
                    runtime_target_id=runtime_target_id,
                    slot=slot_name,
                    reason=reason,
                )
                payload = _checked_json(client.post("/deploy", json=request))
            elif action == "retarget-runtime":
                if payload_sha256 is None:
                    console.print("[red]--payload-sha256 is required for retarget-runtime[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        "/sync/retarget-runtime",
                        json=_control_mutation_body(
                            actor=actor,
                            reason=reason or "operator selected measured compatible runtime",
                            payload_sha256=payload_sha256,
                            runtime_target_id=runtime_target_id,
                        ),
                    )
                )
            elif action == "quarantine-blocked":
                payload = _checked_json(
                    client.post(
                        "/sync/quarantine-blocked",
                        json=_control_mutation_body(
                            actor=actor,
                            reason=reason or "operator quarantined blocked DDIL intent",
                        ),
                    )
                )
            elif action == "requeue-dead-letters":
                payload = _checked_json(
                    client.post(
                        "/sync/requeue-dead-letters",
                        json=_control_mutation_body(
                            actor=actor,
                            reason=reason or "operator requeued remediated DDIL intent",
                            payload_sha256s=[payload_sha256] if payload_sha256 else None,
                            require_ready=not force,
                            force=force,
                        ),
                    )
                )
            elif action == "acknowledge-dead-letters":
                payload = _checked_json(
                    client.post(
                        "/sync/acknowledge-dead-letters",
                        json=_control_mutation_body(
                            actor=actor,
                            reason=reason or "operator reviewed DDIL dead letter",
                        ),
                    )
                )
            else:
                console.print(f"[red]Unknown action: {action}[/red]")
                console.print(
                    "Valid actions: offline, online, deploy, sync-preview, sync, "
                    "retarget-runtime, quarantine-blocked, requeue-dead-letters, "
                    "acknowledge-dead-letters"
                )
                raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Control command failed: {e}[/red]")
        raise typer.Exit(1)

    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    _print_control_payload(action, payload)


def _control_deploy_body(
    *,
    actor: str,
    source: str,
    package_id: Optional[str],
    model_id: str,
    device_id: Optional[str],
    runtime_target_id: Optional[str],
    slot: str,
    reason: Optional[str],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "actor": actor,
            "source": source,
            "package_id": package_id,
            "model_id": model_id,
            "device_id": device_id,
            "runtime_target_id": runtime_target_id,
            "slot": slot,
            "reason": reason,
        }.items()
        if value
    }


def _control_mutation_body(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value}


@app.command()
def hub(
    action: str = typer.Argument(
        ...,
        help=(
            "Action: enroll, devices, packages, runtime-targets, rollouts, status, "
            "readiness, edge-runtime-mission, verify-edge-proof, package-from-mlflow, "
            "mission-package-plan, mission-package-download, mission-package-stage, "
            "register-package, register-runtime, validate-runtime, runtime-validations, "
            "benchmarks, preview-compatibility, compatibility-matrix, promote-package, "
            "rollout-plans, create-rollout-plan, advance-rollout-plan, pause-rollout-plan, "
            "resume-rollout-plan, assign, approve, apply, rollback, export, import, "
            "ingest-evidence, evidence, replay-telemetry, telemetry"
        ),
    ),
    source: Optional[str] = typer.Argument(
        None,
        help=(
            "MLflow model URI, package path, rollout ID, air-gap bundle path, "
            "telemetry bundle path, evidence bundle path, mission YAML path, "
            "mission package artifact path, or edge-runtime proof path"
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
    model_id: Optional[str] = typer.Option(
        None,
        "--model-id",
        help="Model ID inside a multi-model package",
    ),
    slot_name: Optional[str] = typer.Option(None, "--slot", help="Target rollout slot"),
    rollout_id: Optional[str] = typer.Option(None, "--rollout-id", help="Rollout ID"),
    rollout_plan_id: Optional[str] = typer.Option(
        None,
        "--plan-id",
        help="Rollout plan ID for coordinated rollout actions",
    ),
    target_device_ids: Optional[list[str]] = typer.Option(
        None,
        "--target-device-id",
        help="Device ID included in a rollout plan; repeatable",
    ),
    batch_size: int = typer.Option(
        1,
        "--batch-size",
        min=1,
        help="Number of devices assigned per rollout-plan batch",
    ),
    runtime_target_id: Optional[str] = typer.Option(
        None,
        "--runtime-target-id",
        help="Container runtime target for rollout assignment or runtime registration",
    ),
    mission_yaml: Optional[str] = typer.Option(
        None,
        "--mission-yaml",
        help="Inline mission YAML used for mission package planning",
    ),
    mission_yaml_file: Optional[Path] = typer.Option(
        None,
        "--mission-yaml-file",
        help="Mission YAML file used for mission package planning",
    ),
    mission_goal: Optional[str] = typer.Option(
        None,
        "--goal",
        help="Mission goal for mission package planning",
    ),
    sensor: Optional[str] = typer.Option(
        None,
        "--sensor",
        help="Sensor input bound into the mission package",
    ),
    latency_budget_ms: Optional[float] = typer.Option(
        None,
        "--latency-budget-ms",
        help="p95 latency budget in milliseconds for the mission package SLO",
    ),
    min_throughput_ips: Optional[float] = typer.Option(
        None,
        "--min-throughput-ips",
        help="Minimum inference throughput for the mission package SLO",
    ),
    switch_policy: Optional[str] = typer.Option(
        None,
        "--switch-policy",
        help="Model switching policy bound into the mission package",
    ),
    confidence_threshold: Optional[float] = typer.Option(
        None,
        "--confidence-threshold",
        min=0.0,
        max=1.0,
        help="Confidence threshold for model switching",
    ),
    fallback_model_id: Optional[str] = typer.Option(
        None,
        "--fallback-model-id",
        help="Fallback model ID for mission package handling policy",
    ),
    ddil_mode: Optional[str] = typer.Option(
        None,
        "--ddil-mode",
        help="DDIL behavior mode bound into the mission package",
    ),
    require_runtime_validation: bool = typer.Option(
        False,
        "--require-runtime-validation",
        help="Require a passing runtime-target validation before rollout assignment",
    ),
    require_approval: bool = typer.Option(
        False,
        "--require-approval",
        help="Require rollout approval before edge apply",
    ),
    promotion_state: Optional[str] = typer.Option(
        None,
        "--promotion-state",
        help="Package promotion target: validated, approved, released, or retired",
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
    include_device_inventory: bool = typer.Option(
        False,
        "--include-device-inventory",
        help="Include device heartbeat inventory rows in compatibility matrices",
    ),
    pull_image: bool = typer.Option(
        False,
        "--pull-image",
        help="Pull the runtime target image before container validation",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the runtime validation command without running it",
    ),
    local_runtime: bool = typer.Option(
        False,
        "--local-runtime",
        help="Validate in-process against the runtime target inventory instead of using Docker",
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
        help="Reason recorded for Hub Lite approval or rollback actions",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
    require_go: bool = typer.Option(
        False,
        "--require-go",
        help="Exit non-zero unless readiness or edge-runtime-mission status is go",
    ),
    min_runtime_fit: Optional[float] = typer.Option(
        None,
        "--min-runtime-fit",
        min=0.0,
        max=100.0,
        help="Exit non-zero unless the selected runtime fit score meets this threshold",
    ),
    require_best_runtime: bool = typer.Option(
        False,
        "--require-best-runtime",
        help="Exit non-zero unless the selected runtime target is the best measured eligible target",
    ),
    require_capability_lock: bool = typer.Option(
        False,
        "--require-capability-lock",
        help="Exit non-zero unless the proof carries a locked runtime capability basis",
    ),
    require_proof_signature: bool = typer.Option(
        False,
        "--require-proof-signature",
        help="Require and verify an edge-runtime proof attestation with --signing-key",
    ),
    max_proof_age_seconds: Optional[float] = typer.Option(
        None,
        "--max-proof-age-seconds",
        min=0.0,
        help="Exit non-zero unless the proof export timestamp is no older than this many seconds",
    ),
):
    """Operate Hub Lite from the CLI."""
    import json

    from temms.core.signing import read_signing_key

    key = _hub_package_signing_key(signing_key, signing_key_file, read_signing_key)

    if action == "verify-edge-proof":
        if source is None:
            console.print("[red]Edge-runtime proof path required[/red]")
            raise typer.Exit(1)
        payload = _verify_edge_runtime_proof(
            Path(source),
            require_go=require_go,
            min_runtime_fit=min_runtime_fit,
            require_best_runtime=require_best_runtime,
            require_capability_lock=require_capability_lock,
            max_proof_age_seconds=max_proof_age_seconds,
            expected_path={
                "package_id": package_id,
                "model_id": model_id,
                "device_id": device_id,
                "runtime_target_id": runtime_target_id,
                "slot": slot_name,
            },
            signing_key=key,
            require_attestation=require_proof_signature,
        )
        if json_output:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_edge_runtime_proof_verification(payload)
        if not payload.get("valid") or payload.get("requested_gate_failures"):
            raise typer.Exit(1)
        return

    import httpx

    from temms.core.runtime_target_runner import validate_runtime_target_package

    base_url = _hub_api_url(hub_url)
    headers = _hub_auth_headers(token)
    readiness_proof_payload: dict[str, Any] | None = None

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
            elif action == "rollout-plans":
                payload = _checked_json(client.get("/rollout-plans"))
            elif action == "status":
                payload = _checked_json(client.get("/deployment-status"))
            elif action in {"readiness", "edge-runtime-mission"}:
                params = _hub_readiness_query_params(
                    package_id=package_id,
                    model_id=model_id,
                    device_id=device_id,
                    runtime_target_id=runtime_target_id,
                    slot=slot_name,
                )
                readiness_payload = _checked_json(
                    client.get("/readiness", params=params or None)
                )
                readiness_proof_payload = readiness_payload
                if action == "edge-runtime-mission":
                    mission = readiness_payload.get("edge_runtime_mission")
                    payload = mission if isinstance(mission, dict) else {}
                else:
                    payload = readiness_payload
            elif action in {"mission-package-plan", "mission-package-download"}:
                request = _hub_mission_package_request_body(
                    source=source,
                    package_id=package_id,
                    model_id=model_id,
                    device_id=device_id,
                    runtime_target_id=runtime_target_id,
                    slot=slot_name,
                    goal=mission_goal,
                    mission_yaml=mission_yaml,
                    mission_yaml_file=mission_yaml_file,
                    sensor=sensor,
                    latency_budget_ms=latency_budget_ms,
                    min_throughput_ips=min_throughput_ips,
                    switch_policy=switch_policy,
                    confidence_threshold=confidence_threshold,
                    fallback_model_id=fallback_model_id,
                    ddil_mode=ddil_mode,
                    require_go=require_go,
                    min_runtime_fit=min_runtime_fit,
                    require_best_runtime=require_best_runtime,
                    require_capability_lock=require_capability_lock,
                    require_proof_signature=require_proof_signature,
                )
                endpoint = (
                    "/mission-package/download"
                    if action == "mission-package-download"
                    else "/mission-package/plan"
                )
                payload = _checked_json(client.post(endpoint, json=request))
            elif action == "mission-package-stage":
                request = _hub_mission_package_stage_request(
                    source=source,
                    rollout_id=rollout_id,
                    actor=actor,
                    reason=reason,
                )
                payload = _checked_json(client.post("/mission-package/stage", json=request))
            elif action == "telemetry":
                payload = _checked_json(client.get("/telemetry"))
            elif action == "evidence":
                payload = _checked_json(client.get("/evidence"))
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
                    local=local_runtime,
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
                request = {
                    "device_id": device_id,
                    "package_id": package_id,
                    "runtime_target_id": runtime_target_id,
                }
                if model_id:
                    request["model_id"] = model_id
                payload = _checked_json(
                    client.post(
                        "/compatibility/preview",
                        json=request,
                    )
                )
            elif action == "compatibility-matrix":
                request = {
                    "package_ids": [package_id] if package_id else None,
                    "device_ids": [device_id] if device_id else None,
                    "runtime_target_ids": [runtime_target_id] if runtime_target_id else None,
                    "include_device_inventory": include_device_inventory,
                }
                if model_id:
                    request["model_ids"] = [model_id]
                payload = _checked_json(client.post("/compatibility/matrix", json=request))
            elif action == "promote-package":
                target_package = source or package_id
                if target_package is None or promotion_state is None:
                    console.print("[red]Package ID and --promotion-state are required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        f"/packages/{target_package}/promote",
                        json={
                            "state": promotion_state,
                            "reason": reason,
                            "actor": actor,
                        },
                    )
                )
            elif action == "create-rollout-plan":
                devices = list(target_device_ids or [])
                if device_id:
                    devices.append(device_id)
                if package_id is None or not devices:
                    console.print(
                        "[red]--package-id and at least one target device are required[/red]"
                    )
                    raise typer.Exit(1)
                request = {
                    "plan_id": rollout_plan_id,
                    "package_id": package_id,
                    "device_ids": devices,
                    "slot": slot_name,
                    "runtime_target_id": runtime_target_id,
                    "batch_size": batch_size,
                    "require_runtime_validation": require_runtime_validation,
                    "require_approval": require_approval,
                    "actor": actor,
                }
                if model_id:
                    request["model_id"] = model_id
                payload = _checked_json(client.post("/rollout-plans", json=request))
            elif action == "advance-rollout-plan":
                target_plan = source or rollout_plan_id
                if target_plan is None:
                    console.print("[red]Rollout plan ID required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        f"/rollout-plans/{target_plan}/advance",
                        json={"limit": batch_size, "actor": actor},
                    )
                )
            elif action == "pause-rollout-plan":
                target_plan = source or rollout_plan_id
                if target_plan is None:
                    console.print("[red]Rollout plan ID required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        f"/rollout-plans/{target_plan}/pause",
                        json={"reason": reason, "actor": actor},
                    )
                )
            elif action == "resume-rollout-plan":
                target_plan = source or rollout_plan_id
                if target_plan is None:
                    console.print("[red]Rollout plan ID required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        f"/rollout-plans/{target_plan}/resume",
                        json={"reason": reason, "actor": actor},
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
                    "require_approval": require_approval,
                    "actor": actor,
                }
                if model_id:
                    request["model_id"] = model_id
                payload = _checked_json(client.post("/rollouts", json=request))
            elif action == "approve":
                target_rollout = source or rollout_id
                if target_rollout is None:
                    console.print("[red]Rollout ID required[/red]")
                    raise typer.Exit(1)
                payload = _checked_json(
                    client.post(
                        f"/rollouts/{target_rollout}/approve",
                        json={"reason": reason, "actor": actor},
                    )
                )
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
            elif action == "ingest-evidence":
                if source is None:
                    console.print("[red]Evidence bundle path required[/red]")
                    raise typer.Exit(1)
                bundle = json.loads(Path(source).read_text(encoding="utf-8"))
                payload = _checked_json(
                    client.post(
                        "/evidence/ingest",
                        json={"bundle": bundle, "device_id": device_id, "actor": actor},
                    )
                )
            else:
                console.print(f"[red]Unknown action: {action}[/red]")
                console.print(
                    "Valid actions: enroll, devices, packages, runtime-targets, rollouts, "
                    "status, readiness, edge-runtime-mission, verify-edge-proof, "
                    "package-from-mlflow, mission-package-plan, mission-package-download, "
                    "mission-package-stage, "
                    "register-package, register-runtime, validate-runtime, "
                    "runtime-validations, benchmarks, preview-compatibility, "
                    "compatibility-matrix, promote-package, rollout-plans, "
                    "create-rollout-plan, advance-rollout-plan, pause-rollout-plan, "
                    "resume-rollout-plan, assign, approve, apply, rollback, export, import, "
                    "ingest-evidence, evidence, replay-telemetry, telemetry"
                )
                raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Hub command failed: {e}[/red]")
        raise typer.Exit(1)

    gate_failures = _hub_gate_failures(
        action,
        payload,
        require_go=require_go,
        min_runtime_fit=min_runtime_fit,
        require_best_runtime=require_best_runtime,
        require_capability_lock=require_capability_lock,
        runtime_context=readiness_proof_payload,
    )
    if output is not None and action in {"readiness", "edge-runtime-mission"}:
        proof_payload = _hub_edge_runtime_proof_payload(
            action=action,
            payload=payload,
            readiness=readiness_proof_payload or payload,
            require_go=require_go,
            min_runtime_fit=min_runtime_fit,
            require_best_runtime=require_best_runtime,
            require_capability_lock=require_capability_lock,
            gate_failures=gate_failures,
            signing_key=key,
        )
        _write_json_file(output, proof_payload)
        if not json_output:
            console.print(f"[green]Edge mission proof written:[/green] {output}")
    if output is not None and action in {"mission-package-plan", "mission-package-download"}:
        _write_json_file(output, payload)
        if not json_output:
            console.print(f"[green]Mission package written:[/green] {output}")
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        if action == "validate-runtime" and not payload.get("ok", True):
            raise typer.Exit(1)
        if action == "preview-compatibility" and not payload.get("compatible", True):
            raise typer.Exit(1)
        if gate_failures:
            raise typer.Exit(1)
        return
    _print_hub_payload(action, payload)
    if action == "validate-runtime" and not payload.get("ok", True):
        raise typer.Exit(1)
    if action == "preview-compatibility" and not payload.get("compatible", True):
        raise typer.Exit(1)
    if gate_failures:
        for failure in gate_failures:
            console.print(f"[red]Gate failed:[/red] {failure}")
        raise typer.Exit(1)


def _hub_api_url(url: str) -> str:
    """Normalize Hub Lite URL to the /v1/hub API prefix."""
    base = url.rstrip("/")
    if base.endswith("/v1/hub"):
        return base
    return f"{base}/v1/hub"


def _control_api_url(url: str) -> str:
    """Normalize daemon URL to the /v1/control API prefix."""
    base = url.rstrip("/")
    if base.endswith("/v1/control"):
        return base
    return f"{base}/v1/control"


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


def _hub_readiness_query_params(
    *,
    package_id: Optional[str],
    model_id: Optional[str],
    device_id: Optional[str],
    runtime_target_id: Optional[str],
    slot: Optional[str],
) -> dict[str, str]:
    """Return non-empty query params for Hub readiness selection."""
    return {
        key: value
        for key, value in {
            "package_id": package_id,
            "model_id": model_id,
            "device_id": device_id,
            "runtime_target_id": runtime_target_id,
            "slot": slot,
        }.items()
        if value
    }


def _hub_mission_package_request_body(
    *,
    source: Optional[str],
    package_id: Optional[str],
    model_id: Optional[str],
    device_id: Optional[str],
    runtime_target_id: Optional[str],
    slot: Optional[str],
    goal: Optional[str],
    mission_yaml: Optional[str],
    mission_yaml_file: Optional[Path],
    sensor: Optional[str],
    latency_budget_ms: Optional[float],
    min_throughput_ips: Optional[float],
    switch_policy: Optional[str],
    confidence_threshold: Optional[float],
    fallback_model_id: Optional[str],
    ddil_mode: Optional[str],
    require_go: bool,
    min_runtime_fit: Optional[float],
    require_best_runtime: bool,
    require_capability_lock: bool,
    require_proof_signature: bool,
) -> dict[str, Any]:
    """Build the mission package request body for Hub plan/download calls."""
    source_path = Path(source).expanduser() if source else None
    yaml_file = mission_yaml_file or (
        source_path if source_path is not None and source_path.exists() else None
    )
    if mission_yaml and yaml_file is not None:
        raise ValueError("Use either --mission-yaml or --mission-yaml-file/source path, not both")
    if source and source_path is not None and yaml_file is None:
        raise ValueError(f"Mission YAML path not found: {source}")

    yaml_text = mission_yaml
    if yaml_file is not None:
        if not yaml_file.exists():
            raise ValueError(f"Mission YAML path not found: {yaml_file}")
        yaml_text = yaml_file.read_text(encoding="utf-8")

    body: dict[str, Any] = {
        "package_id": package_id,
        "model_id": model_id,
        "device_id": device_id,
        "runtime_target_id": runtime_target_id,
        "slot": slot,
        "goal": goal,
        "mission_yaml": yaml_text,
        "sensor": sensor,
        "latency_budget_ms": latency_budget_ms,
        "min_throughput_ips": min_throughput_ips,
        "switch_policy": switch_policy,
        "confidence_threshold": confidence_threshold,
        "fallback_model_id": fallback_model_id,
        "ddil_mode": ddil_mode,
        "min_runtime_fit": min_runtime_fit,
    }
    if require_go:
        body["require_go"] = True
    if require_best_runtime:
        body["require_best_runtime"] = True
    if require_capability_lock:
        body["require_capability_lock"] = True
    if require_proof_signature:
        body["require_proof_signature"] = True
    return {key: value for key, value in body.items() if value not in (None, "")}


def _hub_mission_package_stage_request(
    *,
    source: Optional[str],
    rollout_id: Optional[str],
    actor: Optional[str],
    reason: str,
) -> dict[str, Any]:
    """Return the Hub path/body for staging a mission package deployment intent."""
    if source is None:
        raise ValueError("Mission package JSON path required")
    path = Path(source).expanduser()
    if not path.exists():
        raise ValueError(f"Mission package JSON path not found: {path}")
    try:
        import json

        package = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Mission package JSON could not be read: {exc}") from exc
    if not isinstance(package, dict):
        raise ValueError("Mission package JSON must be an object")
    if package.get("schema_version") != "temms-edge-mission-package/v1":
        raise ValueError("Mission package JSON must use schema temms-edge-mission-package/v1")

    request: dict[str, Any] = {"mission_package": package}
    if rollout_id:
        request["rollout_id"] = rollout_id
    if actor:
        request["actor"] = actor
    if reason and reason != "cli rollback":
        request["reason"] = reason
    return request


def _normalize_hub_mission_package_command_path(path: str) -> str:
    normalized = path.strip()
    if normalized.startswith("/v1/hub/"):
        normalized = normalized.removeprefix("/v1/hub")
    if normalized == "v1/hub/rollouts":
        normalized = "/rollouts"
    if normalized == "rollouts":
        normalized = "/rollouts"
    return normalized


def _hub_gate_failures(
    action: str,
    payload: dict[str, Any],
    *,
    require_go: bool,
    min_runtime_fit: Optional[float],
    require_best_runtime: bool,
    require_capability_lock: bool,
    runtime_context: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Return operator-facing gate failures for readiness/mission checks."""
    if action not in {"readiness", "edge-runtime-mission"}:
        return []

    failures: list[str] = []
    status = str(payload.get("status") or "unknown")
    if require_go and status != "go":
        failures.append(f"{action} status is {status}, expected go")

    if min_runtime_fit is not None:
        score = _hub_runtime_fit_score(action, payload)
        if score is None:
            failures.append("runtime fit score is missing")
        elif score < min_runtime_fit:
            failures.append(
                f"runtime fit score {score:g}/100 is below required {min_runtime_fit:g}/100"
            )
    if require_best_runtime:
        failures.extend(
            _runtime_target_best_gate_failures(runtime_context or payload or {})
        )
    if require_capability_lock:
        failures.extend(
            _runtime_capability_lock_gate_failures(runtime_context or payload or {})
        )
    return failures


def _runtime_target_best_gate_failures(runtime_context: dict[str, Any]) -> list[str]:
    target_selection = _runtime_target_selection_for_gate(runtime_context)
    if not target_selection:
        return ["runtime target selection proof is missing"]

    status = str(target_selection.get("status") or "").lower()
    selected = str(target_selection.get("selected_runtime_target_id") or "")
    best = str(target_selection.get("best_runtime_target_id") or "")
    score_delta = _optional_float(target_selection.get("score_delta"))
    selected_is_best = bool(selected and best and selected == best)
    if (
        status == "best"
        and (not selected or not best or selected_is_best)
        and (score_delta is None or score_delta <= 0)
    ):
        return []
    if selected_is_best and (score_delta is None or score_delta <= 0):
        return []
    if selected and best and selected != best:
        return [
            f"selected runtime target {selected} is not best measured target {best}"
        ]
    if score_delta is not None and score_delta > 0:
        return [
            f"selected runtime target trails best measured target by {score_delta:g} points"
        ]
    if status:
        return [f"runtime target selection status is {status}, expected best"]
    return ["runtime target selection proof is missing best-runtime status"]


def _runtime_target_selection_for_gate(runtime_context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(runtime_context, dict):
        return {}
    readiness = (
        runtime_context.get("readiness")
        if isinstance(runtime_context.get("readiness"), dict)
        else {}
    )
    for source in (
        runtime_context,
        runtime_context.get("edge_execution_contract"),
        runtime_context.get("runtime_decision"),
        runtime_context.get("runtime_fit"),
        readiness.get("edge_execution_contract"),
        readiness.get("runtime_decision"),
        readiness.get("runtime_fit"),
    ):
        if not isinstance(source, dict):
            continue
        target_selection = source.get("target_selection")
        if isinstance(target_selection, dict) and target_selection:
            return target_selection
    return {}


def _runtime_capability_lock_gate_failures(runtime_context: dict[str, Any]) -> list[str]:
    lock = _runtime_capability_lock_for_gate(runtime_context)
    if not lock:
        return ["runtime capability lock proof is missing"]

    status = str(lock.get("status") or "").lower()
    digest = str(lock.get("capability_sha256") or "")
    raw_failures = lock.get("failures") if isinstance(lock.get("failures"), list) else []
    lock_failures = [str(failure) for failure in raw_failures if failure]
    failures: list[str] = []
    if status != "locked":
        failures.append(f"runtime capability lock status is {status or 'missing'}, expected locked")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        failures.append("runtime capability lock capability_sha256 is missing or invalid")
    if lock_failures:
        failures.append(
            "runtime capability lock has failures: " + "; ".join(lock_failures[:3])
        )
    return failures


def _runtime_capability_lock_for_gate(runtime_context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(runtime_context, dict):
        return {}
    readiness = (
        runtime_context.get("readiness")
        if isinstance(runtime_context.get("readiness"), dict)
        else {}
    )
    for source in (
        runtime_context,
        runtime_context.get("edge_execution_contract"),
        runtime_context.get("runtime_decision"),
        runtime_context.get("runtime_fit"),
        readiness.get("edge_execution_contract"),
        readiness.get("runtime_decision"),
        readiness.get("runtime_fit"),
    ):
        if not isinstance(source, dict):
            continue
        lock = source.get("runtime_capability_lock")
        if isinstance(lock, dict) and lock:
            return lock
    return {}


def _optional_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hub_runtime_fit_score(action: str, payload: dict[str, Any]) -> Optional[float]:
    if action == "readiness":
        runtime_fit = payload.get("runtime_fit")
    else:
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        runtime_fit = metrics.get("runtime_fit")
    if not isinstance(runtime_fit, dict):
        return None
    score = runtime_fit.get("score")
    try:
        return float(score)
    except (TypeError, ValueError):
        return None


def _parse_proof_timestamp(value: Any) -> Any:
    """Parse an ISO8601 proof timestamp into an aware UTC datetime."""
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _edge_runtime_proof_freshness(
    proof: dict[str, Any],
    *,
    max_age_seconds: Optional[float],
) -> dict[str, Any]:
    """Return freshness status for an edge-runtime proof export timestamp."""
    from datetime import datetime, timezone

    exported_at = proof.get("exported_at")
    exported = _parse_proof_timestamp(exported_at)
    result: dict[str, Any] = {
        "schema_version": "temms-edge-runtime-proof-freshness/v1",
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "exported_at": exported_at,
        "max_age_seconds": max_age_seconds,
        "age_seconds": None,
        "status": "not_requested" if max_age_seconds is None else "unknown",
        "errors": [],
    }
    if max_age_seconds is None:
        return result
    if exported is None:
        result["status"] = "failed"
        result["errors"] = ["proof exported_at timestamp is missing or invalid"]
        return result

    age_seconds = max(0.0, (datetime.now(timezone.utc) - exported).total_seconds())
    result["age_seconds"] = age_seconds
    if age_seconds > max_age_seconds:
        result["status"] = "stale"
        result["errors"] = [
            f"proof age {age_seconds:g}s exceeds max {max_age_seconds:g}s"
        ]
    else:
        result["status"] = "fresh"
    return result


def _edge_runtime_proof_path_expectations(
    actual_path: dict[str, Any],
    *,
    expected_path: dict[str, Any],
) -> dict[str, Any]:
    """Return expected-vs-actual path binding status for a proof."""
    expected = {
        key: str(value)
        for key, value in expected_path.items()
        if value not in (None, "")
    }
    mismatches: list[dict[str, str]] = []
    for key, expected_value in expected.items():
        actual_value = str(actual_path.get(key) or "")
        if actual_value != expected_value:
            mismatches.append(
                {
                    "field": key,
                    "expected": expected_value,
                    "actual": actual_value or "missing",
                }
            )
    if not expected:
        status = "not_requested"
    elif mismatches:
        status = "mismatch"
    else:
        status = "matched"
    return {
        "schema_version": "temms-edge-runtime-proof-path-expectations/v1",
        "status": status,
        "expected": expected,
        "actual": _readable_proof_path(actual_path),
        "mismatches": mismatches,
        "errors": [
            f"expected {item['field']} {item['expected']}, proof has {item['actual']}"
            for item in mismatches
        ],
    }


def _readable_proof_path(path: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(path.get(key) or "")
        for key in ("package_id", "model_id", "device_id", "runtime_target_id", "slot")
        if path.get(key) not in (None, "")
    }


def _hub_edge_runtime_proof_payload(
    *,
    action: str,
    payload: dict[str, Any],
    readiness: dict[str, Any],
    require_go: bool,
    min_runtime_fit: Optional[float],
    require_best_runtime: bool,
    require_capability_lock: bool,
    gate_failures: list[str],
    signing_key: Optional[str] = None,
) -> dict[str, Any]:
    """Build a portable proof envelope for selected model/runtime/edge checks."""
    from temms.hub_lite import build_edge_runtime_proof

    return build_edge_runtime_proof(
        readiness,
        source_action=action,
        require_go=require_go,
        min_runtime_fit=min_runtime_fit,
        require_best_runtime=require_best_runtime,
        require_capability_lock=require_capability_lock,
        signing_key=signing_key,
        signer="temms-cli",
    )


def _verify_edge_runtime_proof(
    path: Path,
    *,
    require_go: bool,
    min_runtime_fit: Optional[float],
    require_best_runtime: bool,
    require_capability_lock: bool,
    max_proof_age_seconds: Optional[float],
    expected_path: dict[str, Any],
    signing_key: Optional[str] = None,
    require_attestation: bool = False,
) -> dict[str, Any]:
    """Verify a portable edge-runtime proof without contacting Hub Lite."""
    import json

    from temms.hub_lite import verify_edge_runtime_proof_attestation

    proof_path = path.expanduser()
    try:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return _invalid_edge_runtime_proof_result(
            proof_path,
            [f"proof file is not readable: {exc}"],
        )
    except json.JSONDecodeError as exc:
        return _invalid_edge_runtime_proof_result(
            proof_path,
            [f"invalid JSON proof: {exc}"],
        )
    if not isinstance(proof, dict):
        return _invalid_edge_runtime_proof_result(
            proof_path,
            ["proof root must be a JSON object"],
        )

    errors: list[str] = []
    if proof.get("schema_version") != "temms-edge-runtime-proof/v1":
        errors.append(
            "schema_version is "
            f"{proof.get('schema_version') or 'missing'}, expected temms-edge-runtime-proof/v1"
        )

    integrity = proof.get("integrity") if isinstance(proof.get("integrity"), dict) else {}
    recorded_hash = str(integrity.get("payload_sha256") or "")
    unsigned_proof = dict(proof)
    unsigned_proof.pop("integrity", None)
    computed_hash = _canonical_json_hash(unsigned_proof)
    if not recorded_hash:
        errors.append("integrity.payload_sha256 is missing")
    elif recorded_hash != computed_hash:
        errors.append("integrity mismatch: payload_sha256 does not match proof content")

    attestation = (
        integrity.get("attestation")
        if isinstance(integrity.get("attestation"), dict)
        else {}
    )
    attestation_result: dict[str, Any] = {
        "schema_version": "temms-edge-runtime-proof-attestation-verification/v1",
        "required": require_attestation,
        "present": bool(attestation),
        "verified": False,
        "status": "unsigned",
        "errors": [],
        "algorithm": attestation.get("algorithm"),
        "signer": attestation.get("signer"),
        "signed_at": attestation.get("signed_at"),
        "key_fingerprint": attestation.get("key_fingerprint"),
    }
    if signing_key and (attestation or require_attestation):
        attestation_result.update(
            verify_edge_runtime_proof_attestation(proof, signing_key)
        )
        attestation_result["required"] = require_attestation
        attestation_result["present"] = bool(attestation)
        attestation_result["status"] = (
            "verified" if attestation_result.get("verified") else "failed"
        )
        should_fail_for_attestation = require_attestation or bool(attestation)
        if should_fail_for_attestation and attestation_result.get("errors"):
            errors.extend(
                f"attestation: {error}" for error in attestation_result["errors"]
            )
    elif require_attestation:
        attestation_result["status"] = "missing_key"
        attestation_result["errors"] = [
            "signing key is required to verify proof attestation"
        ]
        errors.extend(f"attestation: {error}" for error in attestation_result["errors"])
    elif attestation:
        attestation_result["status"] = "present_unverified"

    gate_action, gate_payload = _edge_runtime_proof_payload_for_gate(proof)
    selection = proof.get("selection") if isinstance(proof.get("selection"), dict) else {}
    mission = (
        proof.get("edge_runtime_mission")
        if isinstance(proof.get("edge_runtime_mission"), dict)
        else {}
    )
    mission_path = mission.get("path") if isinstance(mission.get("path"), dict) else {}
    proof_path_selection = mission_path or selection
    runtime_decision = (
        proof.get("runtime_decision")
        if isinstance(proof.get("runtime_decision"), dict)
        else {}
    )
    edge_execution_contract = (
        proof.get("edge_execution_contract")
        if isinstance(proof.get("edge_execution_contract"), dict)
        else {}
    )
    requested_gate_failures = _hub_gate_failures(
        gate_action,
        gate_payload,
        require_go=require_go,
        min_runtime_fit=min_runtime_fit,
        require_best_runtime=require_best_runtime,
        require_capability_lock=require_capability_lock,
        runtime_context={
            "edge_execution_contract": edge_execution_contract,
            "runtime_decision": runtime_decision,
            "readiness": proof.get("readiness")
            if isinstance(proof.get("readiness"), dict)
            else {},
        },
    )
    proof_freshness = _edge_runtime_proof_freshness(
        proof,
        max_age_seconds=max_proof_age_seconds,
    )
    requested_gate_failures.extend(
        f"proof freshness: {error}" for error in proof_freshness.get("errors", [])
    )
    path_expectations = _edge_runtime_proof_path_expectations(
        proof_path_selection,
        expected_path=expected_path,
    )
    requested_gate_failures.extend(
        f"proof path: {error}" for error in path_expectations.get("errors", [])
    )
    requested_gate_supplied = (
        require_go
        or min_runtime_fit is not None
        or require_best_runtime
        or require_capability_lock
        or max_proof_age_seconds is not None
        or bool(path_expectations.get("expected"))
    )
    if not requested_gate_supplied:
        requested_gate_status = "not_requested"
    elif requested_gate_failures:
        requested_gate_status = "failed"
    else:
        requested_gate_status = "passed"
    requested_gate_policy: dict[str, Any] = {"require_go": require_go}
    if min_runtime_fit is not None:
        requested_gate_policy["min_runtime_fit"] = min_runtime_fit
    if require_best_runtime:
        requested_gate_policy["require_best_runtime"] = True
    if require_capability_lock:
        requested_gate_policy["require_capability_lock"] = True
    if max_proof_age_seconds is not None:
        requested_gate_policy["max_proof_age_seconds"] = max_proof_age_seconds
    if path_expectations.get("expected"):
        requested_gate_policy["expected_path"] = path_expectations["expected"]

    runtime_fit_score = proof.get("runtime_fit_score")
    if runtime_fit_score is None:
        runtime_fit_score = _hub_runtime_fit_score(gate_action, gate_payload)
    target_runtime_coverage = _edge_target_runtime_coverage(
        edge_execution_contract or runtime_decision
    )
    runtime_decision_trace = _edge_runtime_decision_trace(proof)
    runtime_decision_trace_consistency = _edge_runtime_decision_trace_consistency(
        proof,
        runtime_decision_trace,
    )
    errors.extend(
        f"runtime decision trace: {error}"
        for error in runtime_decision_trace_consistency.get("errors", [])
    )
    edge_execution_manifest = (
        proof.get("edge_execution_manifest")
        if isinstance(proof.get("edge_execution_manifest"), dict)
        else {}
    )
    edge_execution_manifest_consistency = _edge_execution_manifest_consistency(
        proof,
    )
    errors.extend(
        f"edge execution manifest: {error}"
        for error in edge_execution_manifest_consistency.get("errors", [])
    )
    component_digest_consistency = _edge_runtime_proof_component_digest_consistency(
        proof,
    )
    errors.extend(
        f"component digests: {error}"
        for error in component_digest_consistency.get("errors", [])
    )

    return {
        "schema_version": "temms-edge-runtime-proof-verification/v1",
        "proof_path": str(proof_path),
        "valid": not errors,
        "errors": errors,
        "source_action": proof.get("source_action"),
        "gate_status": proof.get("gate_status"),
        "gate_policy": proof.get("gate_policy") if isinstance(proof.get("gate_policy"), dict) else {},
        "gate_failures": proof.get("gate_failures")
        if isinstance(proof.get("gate_failures"), list)
        else [],
        "requested_gate_status": requested_gate_status,
        "requested_gate_policy": requested_gate_policy,
        "requested_gate_failures": requested_gate_failures,
        "status": proof.get("status") or gate_payload.get("status") or "unknown",
        "runtime_fit_score": runtime_fit_score,
        "selection": selection,
        "path": mission_path or selection,
        "runtime_decision": runtime_decision,
        "edge_execution_contract": edge_execution_contract,
        "edge_execution_manifest": edge_execution_manifest,
        "edge_execution_manifest_consistency": edge_execution_manifest_consistency,
        "component_digests": proof.get("component_digests")
        if isinstance(proof.get("component_digests"), dict)
        else {},
        "component_digest_consistency": component_digest_consistency,
        "runtime_decision_trace": runtime_decision_trace,
        "runtime_decision_trace_consistency": runtime_decision_trace_consistency,
        "target_runtime_coverage": target_runtime_coverage,
        "proof_freshness": proof_freshness,
        "path_expectations": path_expectations,
        "integrity": {
            "recorded_payload_sha256": recorded_hash,
            "computed_payload_sha256": computed_hash,
        },
        "attestation": attestation_result,
    }


def _invalid_edge_runtime_proof_result(path: Path, errors: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "temms-edge-runtime-proof-verification/v1",
        "proof_path": str(path),
        "valid": False,
        "errors": errors,
        "source_action": None,
        "gate_status": "unknown",
        "gate_policy": {},
        "gate_failures": [],
        "requested_gate_status": "failed",
        "requested_gate_policy": {},
        "requested_gate_failures": [],
        "status": "unknown",
        "runtime_fit_score": None,
        "selection": {},
        "path": {},
        "runtime_decision": {},
        "edge_execution_contract": {},
        "edge_execution_manifest": {},
        "edge_execution_manifest_consistency": {
            "schema_version": "temms-edge-execution-manifest-consistency/v1",
            "status": "not_checked",
            "checked_fields": 0,
            "errors": [],
        },
        "component_digests": {},
        "component_digest_consistency": {
            "schema_version": "temms-edge-runtime-proof-component-digest-consistency/v1",
            "status": "not_checked",
            "checked_components": 0,
            "recorded": {},
            "computed": {},
            "errors": [],
        },
        "runtime_decision_trace": {
            "schema_version": "temms-runtime-decision-trace/v1",
            "source": "missing",
            "rows": [],
            "commands": [],
        },
        "runtime_decision_trace_consistency": {
            "schema_version": "temms-runtime-decision-trace-consistency/v1",
            "status": "unknown",
            "errors": errors,
        },
        "target_runtime_coverage": {
            "schema_version": "temms-target-runtime-coverage/v1",
            "assessed": 0,
            "eligible": 0,
            "blocked": 0,
            "requires_edge_execution": [],
            "commands": [],
        },
        "proof_freshness": {
            "schema_version": "temms-edge-runtime-proof-freshness/v1",
            "checked_at": "",
            "exported_at": None,
            "max_age_seconds": None,
            "age_seconds": None,
            "status": "unknown",
            "errors": errors,
        },
        "path_expectations": {
            "schema_version": "temms-edge-runtime-proof-path-expectations/v1",
            "status": "unknown",
            "expected": {},
            "actual": {},
            "mismatches": [],
            "errors": errors,
        },
        "integrity": {
            "recorded_payload_sha256": "",
            "computed_payload_sha256": "",
        },
        "attestation": {
            "schema_version": "temms-edge-runtime-proof-attestation-verification/v1",
            "required": False,
            "present": False,
            "verified": False,
            "status": "unknown",
            "errors": [],
            "algorithm": None,
            "signer": None,
            "signed_at": None,
            "key_fingerprint": None,
        },
    }


def _edge_runtime_proof_payload_for_gate(
    proof: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    source_action = str(proof.get("source_action") or "")
    if source_action == "readiness":
        readiness = proof.get("readiness") if isinstance(proof.get("readiness"), dict) else {}
        return "readiness", readiness
    mission = (
        proof.get("edge_runtime_mission")
        if isinstance(proof.get("edge_runtime_mission"), dict)
        else {}
    )
    return "edge-runtime-mission", mission


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json_dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _canonical_json_hash(payload: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json_dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _edge_runtime_proof_component_digests(proof: dict[str, Any]) -> dict[str, Any]:
    digests: dict[str, Any] = {
        "schema_version": "temms-edge-runtime-proof-component-digests/v1",
    }
    for component_name in (
        "runtime_workbench",
        "runtime_decision_trace",
        "edge_execution_manifest",
    ):
        component = proof.get(component_name)
        if isinstance(component, dict) and component:
            digests[f"{component_name}_sha256"] = _canonical_json_hash(component)
    return digests


def _edge_runtime_proof_component_digest_consistency(
    proof: dict[str, Any],
) -> dict[str, Any]:
    recorded = (
        proof.get("component_digests")
        if isinstance(proof.get("component_digests"), dict)
        else {}
    )
    computed = _edge_runtime_proof_component_digests(proof)
    errors: list[str] = []
    computed_hashes = {
        key: value for key, value in computed.items() if key.endswith("_sha256")
    }
    if not recorded:
        if computed_hashes:
            errors.append("component_digests is missing")
        return {
            "schema_version": "temms-edge-runtime-proof-component-digest-consistency/v1",
            "status": "missing" if errors else "not_present",
            "checked_components": len(computed_hashes),
            "recorded": {},
            "computed": computed,
            "errors": errors,
        }

    expected_schema = "temms-edge-runtime-proof-component-digests/v1"
    if recorded.get("schema_version") != expected_schema:
        errors.append(
            "schema_version is "
            f"{recorded.get('schema_version') or 'missing'}, expected {expected_schema}"
        )

    recorded_hashes = {
        key: value for key, value in recorded.items() if key.endswith("_sha256")
    }
    for key, expected in sorted(computed_hashes.items()):
        actual = recorded_hashes.get(key)
        if not actual:
            errors.append(f"{key} is missing")
        elif actual != expected:
            errors.append(f"{key} does not match proof component")
    for key in sorted(set(recorded_hashes) - set(computed_hashes)):
        errors.append(f"{key} is recorded but proof component is missing")

    return {
        "schema_version": "temms-edge-runtime-proof-component-digest-consistency/v1",
        "status": "mismatch" if errors else "consistent",
        "checked_components": len(computed_hashes),
        "recorded": recorded,
        "computed": computed,
        "errors": errors,
    }


def json_dumps(payload: Any, **kwargs: Any) -> str:
    import json

    return json.dumps(payload, **kwargs)


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


def _print_control_payload(action: str, payload: dict) -> None:
    """Print local edge control responses in operator-readable form."""
    if action in {"offline", "online"}:
        mode = "offline" if payload.get("offline_mode") else "online"
        color = "yellow" if mode == "offline" else "green"
        console.print(f"[bold {color}]Edge control: {mode}[/bold {color}]")
        console.print(f"Status: {payload.get('status', '')}")
        return

    if action == "deploy":
        status = str(payload.get("status") or "")
        color = "yellow" if status == "buffered" else "green"
        console.print(f"[bold {color}]Deploy intent {status or 'submitted'}[/bold {color}]")
        console.print(f"Offline: {payload.get('offline', False)}")
        return

    if action == "sync-preview":
        _print_control_sync_preview(payload)
        return

    if action == "sync":
        console.print(f"[bold green]DDIL sync {payload.get('status', 'complete')}[/bold green]")
        console.print(f"Replayed: {payload.get('replayed', 0)}")
        console.print(f"Skipped: {payload.get('skipped', 0)}")
        console.print(f"Cleared: {payload.get('pending_cleared', 0)}")
        preflight = payload.get("preflight")
        if isinstance(preflight, dict):
            console.print(
                "Replay plan before sync: "
                f"{preflight.get('status', 'unknown')} "
                f"({preflight.get('ready', 0)} ready / "
                f"{preflight.get('blocked', 0)} blocked / "
                f"{preflight.get('total', 0)} total)"
            )
        return

    if action == "retarget-runtime":
        previous = payload.get("previous_runtime_target_id") or "previous"
        target = payload.get("runtime_target_id") or "target"
        console.print("[bold green]DDIL runtime retargeted[/bold green]")
        console.print(f"Runtime: {previous} -> {target}")
        if payload.get("payload_sha256"):
            console.print(f"Previous payload: {payload['payload_sha256']}")
        if payload.get("updated_payload_sha256"):
            console.print(f"Updated payload: {payload['updated_payload_sha256']}")
        if payload.get("actor"):
            console.print(f"Actor: {payload['actor']}")
        preflight = payload.get("preflight_after")
        if isinstance(preflight, dict):
            _print_control_sync_preview(preflight)
        return

    if action == "quarantine-blocked":
        console.print("[bold yellow]DDIL blocked intents quarantined[/bold yellow]")
        console.print(f"Quarantined: {payload.get('quarantined', 0)}")
        console.print(f"Remaining: {payload.get('remaining', 0)}")
        return

    if action == "requeue-dead-letters":
        console.print("[bold green]DDIL dead letters requeued[/bold green]")
        console.print(f"Requeued: {payload.get('requeued', 0)}")
        console.print(f"Pending: {payload.get('pending', 0)}")
        if payload.get("require_ready") is True:
            console.print("Ready preflight required: yes")
        if payload.get("blocked"):
            console.print(f"Blocked candidates: {payload.get('blocked', 0)}")
        preflight = payload.get("preflight")
        if isinstance(preflight, dict):
            _print_control_sync_preview(preflight)
        return

    if action == "acknowledge-dead-letters":
        console.print("[bold green]DDIL dead letters acknowledged[/bold green]")
        console.print(f"Acknowledged: {payload.get('acknowledged', 0)}")
        console.print(f"Remaining: {payload.get('remaining', 0)}")
        return

    console.print("[green]Control command succeeded[/green]")
    if "status" in payload:
        console.print(f"Status: {payload['status']}")


def _print_control_sync_preview(payload: dict[str, Any]) -> None:
    """Print a non-mutating DDIL replayability plan."""
    status = str(payload.get("status") or "unknown")
    color = "red" if status == "blocked" else "green" if status == "ready" else "yellow"
    console.print(f"[bold {color}]DDIL sync preview: {status}[/bold {color}]")
    console.print(
        "Queue: "
        f"{payload.get('ready', 0)} ready / "
        f"{payload.get('blocked', 0)} blocked / "
        f"{payload.get('total', 0)} total"
    )
    advisories = payload.get("optimization_advisories", 0)
    if advisories:
        console.print(f"Runtime optimization advisories: {advisories}")

    entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
    if not entries:
        return

    repairs = [
        (entry.get("index"), _control_runtime_repair(entry))
        for entry in entries
        if _control_runtime_repair(entry)
    ]
    if repairs:
        console.print("Runtime repair candidates:")
        for index, repair in repairs:
            console.print(f"  intent {index}: {repair}")

    table = Table(title="Pending DDIL Intents")
    table.add_column("Index")
    table.add_column("Operation")
    table.add_column("Replay")
    table.add_column("Runtime")
    table.add_column("Repair")
    table.add_column("Digest")
    table.add_column("Detail")
    for entry in entries:
        selection = (
            entry.get("hub_readiness_selection")
            if isinstance(entry.get("hub_readiness_selection"), dict)
            else {}
        )
        runtime = entry.get("runtime_target_id") or selection.get("runtime_target_id")
        table.add_row(
            str(entry.get("index", "")),
            str(entry.get("operation") or ""),
            _control_replay_status(entry),
            str(runtime or ""),
            _control_runtime_repair(entry),
            _short_digest(entry.get("payload_sha256")),
            str(entry.get("reason") or ""),
        )
    console.print(table)


def _control_replay_status(entry: dict[str, Any]) -> str:
    replay_status = str(entry.get("replay_status") or "")
    if replay_status:
        return replay_status
    return "ready" if entry.get("ready") else "blocked"


def _control_runtime_repair(entry: dict[str, Any]) -> str:
    current = str(entry.get("runtime_target_id") or "")
    target = str(entry.get("hub_best_runtime_target_id") or "")
    if not target:
        target = _control_runtime_repair_action_target(entry)
    if not target:
        return ""
    previous = current or str(entry.get("runtime_remediation_previous_runtime_target_id") or "")
    delta = entry.get("hub_runtime_score_delta")
    if previous == target and _numeric_zero_or_none(delta):
        return ""
    detail = f"{previous} -> {target}" if previous else target
    if delta is not None:
        detail += f" (+{delta} fit)"
    return detail


def _numeric_zero_or_none(value: Any) -> bool:
    if value is None:
        return True
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _control_runtime_repair_action_target(entry: dict[str, Any]) -> str:
    gate_sets = [entry.get("hub_optimization_gates"), entry.get("hub_blocking_gates")]
    for gates in gate_sets:
        if not isinstance(gates, list):
            continue
        for gate in gates:
            if not isinstance(gate, dict):
                continue
            for action in gate.get("actions", []):
                if not isinstance(action, dict):
                    continue
                refs = action.get("refs") if isinstance(action.get("refs"), dict) else {}
                target = str(refs.get("runtime_target_id") or "").strip()
                if target:
                    return target
    return ""


def _short_digest(value: Any) -> str:
    text = str(value or "")
    return text[:12] if text else ""


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
        table.add_column("Promotion")
        table.add_column("Profiles")
        for package in payload.get("packages", []):
            promotion = (
                package.get("promotion") if isinstance(package.get("promotion"), dict) else {}
            )
            table.add_row(
                package.get("package_id", ""),
                package.get("name", ""),
                package.get("version", ""),
                promotion.get("state", "candidate"),
                ", ".join(package.get("device_profiles", []) or []),
            )
        console.print(table)
        return

    if action == "promote-package":
        promotion = payload.get("promotion") if isinstance(payload.get("promotion"), dict) else {}
        console.print("[green]Hub package promoted[/green]")
        console.print(f"Package: {payload.get('package_id', '')}")
        console.print(f"State: {promotion.get('state', '')}")
        console.print(f"Actor: {promotion.get('actor') or ''}")
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

    if action == "readiness":
        _print_hub_readiness(payload)
        return

    if action == "edge-runtime-mission":
        _print_edge_runtime_mission(payload)
        return

    if action in {"mission-package-plan", "mission-package-download"}:
        _print_mission_package_plan(payload, downloaded=action == "mission-package-download")
        return

    if action == "mission-package-stage":
        rollout = payload.get("rollout") if isinstance(payload.get("rollout"), dict) else payload
        console.print("[green]Mission package deployment intent staged[/green]")
        console.print(
            f"Rollout: {payload.get('rollout_id') or rollout.get('rollout_id', '')} "
            f"({payload.get('rollout_state') or rollout.get('state', 'unknown')})"
        )
        if payload.get("package_identity_sha256"):
            console.print(
                f"Package identity: {payload.get('package_identity_sha256')}"
            )
        if rollout.get("device_id"):
            console.print(f"Device: {rollout.get('device_id')}")
        if rollout.get("package_id"):
            console.print(f"Package: {rollout.get('package_id')}")
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

    if action == "compatibility-matrix":
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        table = Table(title="Hub Compatibility Matrix")
        table.add_column("Package")
        table.add_column("Model")
        table.add_column("Device")
        table.add_column("Runtime")
        table.add_column("Compatible")
        table.add_column("Ready")
        table.add_column("Validation")
        table.add_column("Blockers")
        for cell in payload.get("cells", []):
            runtime = cell.get("runtime_target_id") or "device inventory"
            validation = (
                "pass"
                if cell.get("runtime_validation_ready")
                else "missing" if cell.get("runtime_target_id") else "inventory"
            )
            blockers = list(cell.get("assignment_blockers") or cell.get("failures") or [])
            blockers_text = "; ".join(str(blocker) for blocker in blockers[:2])
            if len(blockers) > 2:
                blockers_text += f"; +{len(blockers) - 2} more"
            table.add_row(
                cell.get("package_id", ""),
                cell.get("model_id") or "package",
                cell.get("device_id", ""),
                runtime,
                "yes" if cell.get("compatible") else "no",
                "yes" if cell.get("assignment_ready") else "no",
                validation,
                blockers_text or "ready",
            )
        console.print(table)
        console.print(
            "Ready: " f"{counts.get('assignment_ready', 0)}/{counts.get('cells', 0)} " "cells"
        )
        return

    if action == "rollout-plans":
        table = Table(title="Hub Rollout Plans")
        table.add_column("Plan")
        table.add_column("Package")
        table.add_column("Slot")
        table.add_column("Runtime")
        table.add_column("State")
        table.add_column("Batch")
        table.add_column("Targets")
        table.add_column("Updated")
        for plan in payload.get("rollout_plans", []):
            counts = plan.get("counts") if isinstance(plan.get("counts"), dict) else {}
            target_summary = (
                f"{counts.get('assigned', 0)} assigned / "
                f"{counts.get('pending', 0)} pending / "
                f"{counts.get('blocked', 0)} blocked"
            )
            table.add_row(
                plan.get("plan_id", ""),
                plan.get("package_id", ""),
                plan.get("slot", "") or "",
                plan.get("runtime_target_id", "") or "auto",
                plan.get("state", ""),
                str(plan.get("current_batch", 0)),
                target_summary,
                plan.get("updated_at", "") or "",
            )
        console.print(table)
        return

    if action in {
        "create-rollout-plan",
        "advance-rollout-plan",
        "pause-rollout-plan",
        "resume-rollout-plan",
    }:
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        console.print(f"[green]Rollout plan {payload.get('state', 'updated')}[/green]")
        console.print(f"Plan: {payload.get('plan_id', '')}")
        console.print(f"Package: {payload.get('package_id', '')}")
        console.print(
            "Targets: "
            f"{counts.get('assigned', 0)} assigned / "
            f"{counts.get('pending', 0)} pending / "
            f"{counts.get('blocked', 0)} blocked"
        )
        rollout_ids = [
            target.get("rollout_id")
            for target in payload.get("targets", [])
            if target.get("rollout_id")
        ]
        if rollout_ids:
            console.print("Rollouts: " + ", ".join(str(rollout_id) for rollout_id in rollout_ids))
        return

    if action == "rollouts":
        table = Table(title="Hub Rollouts")
        table.add_column("Rollout")
        table.add_column("Device")
        table.add_column("Package")
        table.add_column("Slot")
        table.add_column("Runtime")
        table.add_column("State")
        table.add_column("Approval")
        for rollout in payload.get("rollouts", []):
            approval = rollout.get("approval") if isinstance(rollout.get("approval"), dict) else {}
            table.add_row(
                rollout.get("rollout_id", ""),
                rollout.get("device_id", ""),
                rollout.get("package_id", ""),
                rollout.get("slot", "") or "",
                rollout.get("runtime_target_id", "") or "auto",
                rollout.get("state", ""),
                approval.get("state", "not_required"),
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

    if action == "evidence":
        table = Table(title="Hub Evidence Bundles")
        table.add_column("Evidence")
        table.add_column("Device")
        table.add_column("Exported")
        table.add_column("Ingested")
        table.add_column("Headline")
        for record in payload.get("evidence_bundles", []):
            table.add_row(
                record.get("evidence_id", ""),
                record.get("device_id", "") or "",
                record.get("exported_at", "") or "",
                record.get("ingested_at", "") or "",
                record.get("headline", "") or "",
            )
        console.print(table)
        return

    if action == "ingest-evidence":
        record = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        duplicate = " duplicate" if record.get("duplicate") else ""
        console.print(f"[green]Evidence ingested{duplicate}[/green]")
        console.print(f"Evidence: {record.get('evidence_id', '')}")
        console.print(f"Device: {record.get('device_id', '') or 'unknown'}")
        if record.get("headline"):
            console.print(f"Headline: {record.get('headline')}")
        return

    console.print("[green]Hub command succeeded[/green]")
    if "rollout_id" in payload:
        console.print(f"Rollout: {payload['rollout_id']} ({payload.get('state', 'unknown')})")
    elif "package_id" in payload:
        console.print(f"Package: {payload['package_id']} v{payload.get('version', '')}")
    elif "status" in payload:
        console.print(f"Status: {payload['status']}")


def _print_hub_readiness(payload: dict[str, Any]) -> None:
    """Print the full deployment readiness verdict in an operator-readable form."""
    status = str(payload.get("status") or "unknown")
    color = _hub_status_color(status)
    console.print(f"[bold {color}]Hub readiness: {status}[/bold {color}]")
    if payload.get("headline"):
        console.print(str(payload["headline"]))
    if payload.get("next_action"):
        console.print(f"Next action: {payload['next_action']}")

    selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
    if selection:
        console.print(
            "Path: "
            f"{selection.get('model_id') or 'package'} -> "
            f"{selection.get('runtime_target_id') or 'auto'} -> "
            f"{selection.get('device_id') or 'edge'}"
        )

    table = Table(title="Readiness Gates")
    table.add_column("Gate")
    table.add_column("Status")
    table.add_column("State")
    table.add_column("Detail")
    for gate in payload.get("gates", []):
        if not isinstance(gate, dict):
            continue
        table.add_row(
            str(gate.get("label") or gate.get("gate_id") or ""),
            str(gate.get("status") or ""),
            str(gate.get("state") or ""),
            str(gate.get("detail") or ""),
        )
    console.print(table)

    actions = [action for action in payload.get("actions", []) if isinstance(action, dict)]
    if actions:
        action_table = Table(title="Readiness Actions")
        action_table.add_column("Action")
        action_table.add_column("Kind")
        action_table.add_column("Gate")
        action_table.add_column("Command")
        for action in actions:
            command = action.get("command") if isinstance(action.get("command"), dict) else {}
            command_text = ""
            if command:
                command_text = f"{command.get('method', '')} {command.get('path', '')}".strip()
            action_table.add_row(
                str(action.get("label") or action.get("action_id") or ""),
                str(action.get("kind") or ""),
                str(action.get("gate_id") or ""),
                command_text,
            )
        console.print(action_table)


def _print_mission_package_plan(payload: dict[str, Any], *, downloaded: bool) -> None:
    """Print a compact mission package handoff summary."""
    selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
    mission = payload.get("mission") if isinstance(payload.get("mission"), dict) else {}
    proof_gate = payload.get("proof_gate") if isinstance(payload.get("proof_gate"), dict) else {}
    integrity = payload.get("integrity") if isinstance(payload.get("integrity"), dict) else {}
    deployment_intent = (
        payload.get("deployment_intent")
        if isinstance(payload.get("deployment_intent"), dict)
        else {}
    )
    edge_handoff = (
        payload.get("edge_handoff")
        if isinstance(payload.get("edge_handoff"), dict)
        else {}
    )
    command = (
        deployment_intent.get("command")
        if isinstance(deployment_intent.get("command"), dict)
        else {}
    )
    handoff_commands = (
        edge_handoff.get("commands")
        if isinstance(edge_handoff.get("commands"), dict)
        else {}
    )
    status = str(proof_gate.get("status") or "planned")
    color = _hub_status_color(status)
    label = "downloaded" if downloaded else "planned"
    console.print(f"[bold {color}]Mission package {label}: {status}[/bold {color}]")
    if mission.get("goal"):
        console.print(f"Goal: {mission.get('goal')}")
    console.print(
        "Path: "
        f"{selection.get('model_id') or 'model'} -> "
        f"{selection.get('runtime_target_id') or 'runtime'} -> "
        f"{selection.get('device_id') or 'edge'}"
    )
    if selection.get("package_id"):
        console.print(f"Package: {selection.get('package_id')}")
    sensor_value = mission.get("sensor")
    slot_value = mission.get("slot") or selection.get("slot")
    if sensor_value and slot_value:
        console.print(f"Sensor: {sensor_value} / {slot_value}")
    elif sensor_value:
        console.print(f"Sensor: {sensor_value}")
    elif slot_value:
        console.print(f"Slot: {slot_value}")
    if integrity.get("package_identity_sha256"):
        console.print(f"Package identity: {integrity.get('package_identity_sha256')}")
    if deployment_intent.get("rollout_id"):
        console.print(f"Deploy intent: {deployment_intent.get('rollout_id')}")
    if command.get("path"):
        console.print(f"Command: {command.get('method', 'POST')} {command.get('path')}")
    stage_command = (
        handoff_commands.get("stage_package")
        if isinstance(handoff_commands.get("stage_package"), dict)
        else {}
    )
    apply_command = (
        handoff_commands.get("apply_rollout")
        if isinstance(handoff_commands.get("apply_rollout"), dict)
        else {}
    )
    if stage_command.get("path"):
        console.print(
            f"Stage package: {stage_command.get('method', 'POST')} {stage_command.get('path')}"
        )
    if apply_command.get("path"):
        console.print(
            f"Apply rollout: {apply_command.get('method', 'POST')} {apply_command.get('path')}"
        )


def _print_edge_runtime_mission(payload: dict[str, Any]) -> None:
    """Print the compact selected model/runtime/edge proof."""
    if not payload:
        console.print("[red]Edge runtime mission is not available[/red]")
        return

    status = str(payload.get("status") or "unknown")
    color = _hub_status_color(status)
    console.print(f"[bold {color}]Edge Runtime Mission: {status}[/bold {color}]")
    if payload.get("headline"):
        console.print(str(payload["headline"]))
    if payload.get("detail"):
        console.print(str(payload["detail"]))

    path = payload.get("path") if isinstance(payload.get("path"), dict) else {}
    if path:
        console.print(
            "Path: "
            f"{path.get('model_id') or 'model'} -> "
            f"{path.get('runtime_target_id') or 'runtime'} -> "
            f"{path.get('device_id') or 'edge'}"
        )
    if payload.get("next_action"):
        console.print(f"Next action: {payload['next_action']}")

    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    table = Table(title="On-Device Proof")
    table.add_column("Metric")
    table.add_column("Status")
    table.add_column("Detail")
    for key in (
        "runtime_fit",
        "target_selection",
        "runtime_lane",
        "artifact_fit",
        "live_inventory",
        "performance",
        "resources",
        "runtime_validation",
        "production_admission",
        "ddil_repair",
    ):
        metric = metrics.get(key)
        if not isinstance(metric, dict):
            continue
        table.add_row(
            _mission_metric_label(key),
            _mission_metric_status(metric),
            _mission_metric_detail(metric),
        )
    console.print(table)

    operator_focus = [str(item) for item in payload.get("operator_focus", []) if item]
    if operator_focus:
        focus_table = Table(title="Operator Focus")
        focus_table.add_column("Item")
        for item in operator_focus:
            focus_table.add_row(item)
        console.print(focus_table)


def _edge_target_assessments(contract_or_decision: dict[str, Any]) -> list[dict[str, Any]]:
    assessments = contract_or_decision.get("target_assessments")
    if not isinstance(assessments, list):
        return []
    return [item for item in assessments if isinstance(item, dict)]


def _edge_runtime_decision_trace(proof: dict[str, Any]) -> dict[str, Any]:
    trace = (
        proof.get("runtime_decision_trace")
        if isinstance(proof.get("runtime_decision_trace"), dict)
        else {}
    )
    if trace:
        return trace

    workbench = (
        proof.get("runtime_workbench")
        if isinstance(proof.get("runtime_workbench"), dict)
        else {}
    )
    targets = workbench.get("targets") if isinstance(workbench.get("targets"), list) else []
    rows = [
        _edge_runtime_decision_trace_row(target)
        for target in targets
        if isinstance(target, dict)
    ]
    rows = [row for row in rows if row.get("runtime_target_id")]
    commands = [
        row["remediation_command"]
        for row in rows
        if isinstance(row.get("remediation_command"), dict)
    ]
    summary = workbench.get("summary") if isinstance(workbench.get("summary"), dict) else {}
    target_selection = (
        workbench.get("target_selection")
        if isinstance(workbench.get("target_selection"), dict)
        else {}
    )
    return {
        "schema_version": "temms-runtime-decision-trace/v1",
        "source": "derived-from-runtime-workbench",
        "source_schema_version": workbench.get("schema_version"),
        "checked_at": workbench.get("checked_at"),
        "status": workbench.get("status"),
        "recommended_action": workbench.get("recommended_action"),
        "selected_runtime_target_id": workbench.get("selected_runtime_target_id"),
        "best_runtime_target_id": workbench.get("best_runtime_target_id"),
        "selected_is_best": summary.get("selected_is_best"),
        "target_count": summary.get("target_count", len(rows)),
        "eligible_target_count": summary.get(
            "eligible_target_count",
            sum(1 for row in rows if row.get("eligible") is True),
        ),
        "blocked_target_count": summary.get(
            "blocked_target_count",
            sum(1 for row in rows if row.get("status") == "blocked"),
        ),
        "target_selection_status": target_selection.get("status"),
        "selected_rank": target_selection.get("selected_rank"),
        "selected_score": target_selection.get("selected_score"),
        "best_score": target_selection.get("best_score"),
        "score_delta": target_selection.get("score_delta"),
        "rows": rows,
        "commands": commands,
    }


def _edge_runtime_decision_trace_row(target: dict[str, Any]) -> dict[str, Any]:
    runtime_target_id = str(target.get("runtime_target_id") or "")
    if not runtime_target_id:
        return {}
    proof = target.get("proof") if isinstance(target.get("proof"), dict) else {}
    remediation = target.get("remediation") if isinstance(target.get("remediation"), dict) else {}
    runtime_lane = target.get("runtime_lane") if isinstance(target.get("runtime_lane"), dict) else {}
    artifact_lane = target.get("artifact_lane") if isinstance(target.get("artifact_lane"), dict) else {}
    row = {
        "runtime_target_id": runtime_target_id,
        "rank": target.get("rank"),
        "status": target.get("status"),
        "eligible": target.get("eligible"),
        "selected": target.get("selected") is True,
        "best": target.get("best") is True,
        "score": target.get("score"),
        "tier": target.get("tier"),
        "detail": target.get("detail"),
        "runtime_lane": runtime_lane,
        "artifact_lane": {
            "status": artifact_lane.get("status"),
            "state": artifact_lane.get("state"),
            "detail": artifact_lane.get("detail"),
            "model_format": artifact_lane.get("model_format"),
            "lane_id": artifact_lane.get("lane_id"),
        },
        "proof_components": {
            "runtime_validation": {
                "status": proof.get("runtime_validation_status"),
                "state": proof.get("runtime_validation_state"),
                "evidence_id": proof.get("validation_id"),
            },
            "benchmark": {
                "status": proof.get("performance_status"),
                "state": proof.get("performance_state"),
                "evidence_id": proof.get("benchmark_id"),
                "latency_ms_p95": proof.get("latency_ms_p95"),
                "throughput_ips": proof.get("throughput_ips"),
            },
            "resource": {
                "status": proof.get("resource_status"),
                "state": proof.get("resource_state"),
            },
            "telemetry": {
                "status": proof.get("telemetry_status"),
                "state": proof.get("telemetry_state"),
            },
            "capability_lock": {
                "status": proof.get("capability_lock_status"),
                "capability_sha256": proof.get("capability_sha256"),
            },
        },
        "capability_lock": {
            "status": proof.get("capability_lock_status"),
            "capability_sha256": proof.get("capability_sha256"),
            "telemetry_state": proof.get("telemetry_state"),
            "telemetry_status": proof.get("telemetry_status"),
        },
        "validation_id": proof.get("validation_id"),
        "benchmark_id": proof.get("benchmark_id"),
        "latency_ms_p95": proof.get("latency_ms_p95"),
        "throughput_ips": proof.get("throughput_ips"),
        "reasons": target.get("reasons") if isinstance(target.get("reasons"), list) else [],
        "penalties": target.get("penalties") if isinstance(target.get("penalties"), list) else [],
        "remediation": {
            "action": remediation.get("action"),
            "label": remediation.get("label"),
            "detail": remediation.get("detail"),
            "requires_edge_execution": remediation.get("requires_edge_execution"),
        },
    }
    command = _edge_target_remediation_command(target)
    if command is not None:
        row["remediation_command"] = command
    return row


def _edge_runtime_decision_trace_consistency(
    proof: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any]:
    workbench = (
        proof.get("runtime_workbench")
        if isinstance(proof.get("runtime_workbench"), dict)
        else {}
    )
    if not workbench:
        return {
            "schema_version": "temms-runtime-decision-trace-consistency/v1",
            "status": "not_checked",
            "errors": [],
        }

    expected = _edge_runtime_decision_trace({"runtime_workbench": workbench})
    errors: list[str] = []
    if trace.get("schema_version") != "temms-runtime-decision-trace/v1":
        errors.append(
            "schema_version is "
            f"{trace.get('schema_version') or 'missing'}, expected temms-runtime-decision-trace/v1"
        )

    top_level_fields = (
        "selected_runtime_target_id",
        "best_runtime_target_id",
        "selected_is_best",
        "target_count",
        "eligible_target_count",
        "blocked_target_count",
        "target_selection_status",
        "selected_rank",
        "selected_score",
        "best_score",
        "score_delta",
    )
    for field in top_level_fields:
        if not _edge_trace_values_equal(trace.get(field), expected.get(field)):
            errors.append(
                f"{field} is {trace.get(field)!r}, expected {expected.get(field)!r}"
            )

    trace_rows = _edge_runtime_trace_rows_by_id(trace)
    expected_rows = _edge_runtime_trace_rows_by_id(expected)
    trace_ids = set(trace_rows)
    expected_ids = set(expected_rows)
    for runtime_target_id in sorted(expected_ids - trace_ids):
        errors.append(f"missing row for runtime target {runtime_target_id}")
    for runtime_target_id in sorted(trace_ids - expected_ids):
        errors.append(f"unexpected row for runtime target {runtime_target_id}")

    for runtime_target_id in sorted(trace_ids & expected_ids):
        errors.extend(
            _edge_runtime_decision_trace_row_errors(
                runtime_target_id,
                trace_rows[runtime_target_id],
                expected_rows[runtime_target_id],
            )
        )

    trace_commands = _edge_runtime_trace_commands_by_id(trace)
    expected_commands = _edge_runtime_trace_commands_by_id(expected)
    for runtime_target_id in sorted(set(expected_commands) - set(trace_commands)):
        errors.append(f"missing remediation command for runtime target {runtime_target_id}")
    for runtime_target_id in sorted(set(trace_commands) - set(expected_commands)):
        errors.append(
            f"unexpected remediation command for runtime target {runtime_target_id}"
        )
    for runtime_target_id in sorted(set(trace_commands) & set(expected_commands)):
        trace_command = trace_commands[runtime_target_id]
        expected_command = expected_commands[runtime_target_id]
        for field in ("action", "label", "kind", "requires_edge_execution", "command_text"):
            if not _edge_trace_values_equal(
                trace_command.get(field),
                expected_command.get(field),
            ):
                errors.append(
                    f"command {runtime_target_id}.{field} is "
                    f"{trace_command.get(field)!r}, expected {expected_command.get(field)!r}"
                )

    return {
        "schema_version": "temms-runtime-decision-trace-consistency/v1",
        "status": "consistent" if not errors else "mismatch",
        "checked_rows": len(expected_rows),
        "checked_commands": len(expected_commands),
        "errors": errors,
    }


def _edge_execution_manifest_consistency(proof: dict[str, Any]) -> dict[str, Any]:
    manifest = (
        proof.get("edge_execution_manifest")
        if isinstance(proof.get("edge_execution_manifest"), dict)
        else {}
    )
    if not manifest:
        return {
            "schema_version": "temms-edge-execution-manifest-consistency/v1",
            "status": "not_present",
            "checked_fields": 0,
            "errors": [],
        }

    errors: list[str] = []
    checked_fields = 0
    if manifest.get("schema_version") != "temms-edge-execution-manifest/v1":
        errors.append(
            "schema_version is "
            f"{manifest.get('schema_version') or 'missing'}, expected "
            "temms-edge-execution-manifest/v1"
        )

    selection = proof.get("selection") if isinstance(proof.get("selection"), dict) else {}
    mission = (
        proof.get("edge_runtime_mission")
        if isinstance(proof.get("edge_runtime_mission"), dict)
        else {}
    )
    mission_path = mission.get("path") if isinstance(mission.get("path"), dict) else {}
    contract = (
        proof.get("edge_execution_contract")
        if isinstance(proof.get("edge_execution_contract"), dict)
        else {}
    )
    contract_path = contract.get("path") if isinstance(contract.get("path"), dict) else {}
    workbench = (
        proof.get("runtime_workbench")
        if isinstance(proof.get("runtime_workbench"), dict)
        else {}
    )
    selected_runtime_target_id = str(
        workbench.get("selected_runtime_target_id")
        or selection.get("runtime_target_id")
        or contract_path.get("runtime_target_id")
        or ""
    )
    selected_target = _edge_runtime_workbench_selected_target(
        workbench,
        selected_runtime_target_id=selected_runtime_target_id,
    )
    selected_proof = (
        selected_target.get("proof")
        if isinstance(selected_target.get("proof"), dict)
        else {}
    )
    selected_runtime_ref = (
        selected_target.get("runtime_target")
        if isinstance(selected_target.get("runtime_target"), dict)
        else {}
    )
    selected_lane = _edge_manifest_source_dict(
        contract.get("selected_runtime_lane"),
        selected_target.get("runtime_lane"),
    )
    artifact_lane = _edge_manifest_source_dict(
        contract.get("artifact_lane"),
        selected_target.get("artifact_lane"),
    )
    capability_lock = _edge_manifest_source_dict(
        contract.get("runtime_capability_lock"),
        {
            "status": selected_proof.get("capability_lock_status"),
            "capability_sha256": selected_proof.get("capability_sha256"),
            "telemetry_state": selected_proof.get("telemetry_state"),
            "telemetry_status": selected_proof.get("telemetry_status"),
        },
    )
    target_selection = _edge_manifest_source_dict(
        contract.get("target_selection"),
        workbench.get("target_selection"),
    )
    production_admission = _edge_manifest_source_dict(
        contract.get("production_admission"),
        workbench.get("production_admission"),
    )
    trace = _edge_runtime_decision_trace(proof)
    trace_commands = _edge_runtime_trace_commands_by_id(trace)
    selected_command = trace_commands.get(selected_runtime_target_id, {})

    source_path = {
        **contract_path,
        **mission_path,
        **selection,
        "runtime_target_id": selected_runtime_target_id,
    }
    path = manifest.get("path") if isinstance(manifest.get("path"), dict) else {}
    for field in ("package_id", "model_id", "device_id", "runtime_target_id", "slot", "rollout_id"):
        checked_fields += _edge_manifest_compare(
            f"path.{field}",
            path.get(field),
            source_path.get(field),
            errors,
        )
    checked_fields += _edge_manifest_compare(
        "path.label",
        path.get("label"),
        _edge_manifest_path_label(source_path),
        errors,
    )

    model = manifest.get("model") if isinstance(manifest.get("model"), dict) else {}
    model_expectations = {
        "package_id": path.get("package_id"),
        "model_id": path.get("model_id"),
        "slot": path.get("slot"),
        "artifact_format": artifact_lane.get("model_format"),
        "artifact_state": artifact_lane.get("state"),
        "artifact_lane_id": artifact_lane.get("lane_id"),
        "artifact_detail": artifact_lane.get("detail"),
    }
    for field, expected in model_expectations.items():
        checked_fields += _edge_manifest_compare(
            f"model.{field}",
            model.get(field),
            expected,
            errors,
        )

    execution = (
        manifest.get("execution")
        if isinstance(manifest.get("execution"), dict)
        else {}
    )
    execution_expectations = {
        "runtime_target_id": selected_runtime_target_id,
        "runtime_image": selected_runtime_ref.get("image"),
        "runtime_registry": selected_runtime_ref.get("registry"),
        "runtime_os": selected_runtime_ref.get("os"),
        "runtime_arch": selected_runtime_ref.get("arch"),
        "runtime_device_profiles": selected_runtime_ref.get("device_profiles"),
        "target_status": selected_target.get("status"),
        "target_score": selected_target.get("score"),
        "target_tier": selected_target.get("tier"),
        "selected_is_best": (
            workbench.get("summary", {}).get("selected_is_best")
            if isinstance(workbench.get("summary"), dict)
            else None
        ),
        "best_runtime_target_id": workbench.get("best_runtime_target_id"),
    }
    for field, expected in execution_expectations.items():
        checked_fields += _edge_manifest_compare(
            f"execution.{field}",
            execution.get(field),
            expected,
            errors,
        )
    execution_lane = (
        execution.get("runtime_lane")
        if isinstance(execution.get("runtime_lane"), dict)
        else {}
    )
    for field in (
        "lane_id",
        "label",
        "execution_engine",
        "acceleration",
        "providers",
        "accelerators",
        "optimization_goal",
    ):
        checked_fields += _edge_manifest_compare(
            f"execution.runtime_lane.{field}",
            execution_lane.get(field),
            selected_lane.get(field),
            errors,
        )

    edge = manifest.get("edge") if isinstance(manifest.get("edge"), dict) else {}
    manifest_lock = (
        edge.get("capability_lock")
        if isinstance(edge.get("capability_lock"), dict)
        else {}
    )
    for field in ("status", "capability_sha256"):
        checked_fields += _edge_manifest_compare(
            f"edge.capability_lock.{field}",
            manifest_lock.get(field),
            capability_lock.get(field),
            errors,
        )
    telemetry = edge.get("telemetry") if isinstance(edge.get("telemetry"), dict) else {}
    checked_fields += _edge_manifest_compare(
        "edge.telemetry.status",
        telemetry.get("status"),
        selected_proof.get("telemetry_status"),
        errors,
    )
    checked_fields += _edge_manifest_compare(
        "edge.telemetry.state",
        telemetry.get("state"),
        selected_proof.get("telemetry_state"),
        errors,
    )

    evidence = (
        manifest.get("evidence")
        if isinstance(manifest.get("evidence"), dict)
        else {}
    )
    evidence_expectations = {
        "runtime_validation_id": selected_proof.get("validation_id"),
        "benchmark_id": selected_proof.get("benchmark_id"),
        "latency_ms_p95": selected_proof.get("latency_ms_p95"),
        "throughput_ips": selected_proof.get("throughput_ips"),
        "resource_status": selected_proof.get("resource_status"),
        "resource_state": selected_proof.get("resource_state"),
    }
    for field, expected in evidence_expectations.items():
        checked_fields += _edge_manifest_compare(
            f"evidence.{field}",
            evidence.get(field),
            expected,
            errors,
        )

    admission = (
        manifest.get("admission")
        if isinstance(manifest.get("admission"), dict)
        else {}
    )
    admission_expectations = {
        "gate_status": proof.get("gate_status"),
        "gate_policy": proof.get("gate_policy") if isinstance(proof.get("gate_policy"), dict) else {},
        "gate_failures": proof.get("gate_failures") if isinstance(proof.get("gate_failures"), list) else [],
        "production_status": production_admission.get("status"),
        "apply_allowed": production_admission.get("apply_allowed"),
        "target_selection_status": target_selection.get("status"),
        "recommended_action": contract.get("recommended_action") or workbench.get("recommended_action"),
    }
    for field, expected in admission_expectations.items():
        checked_fields += _edge_manifest_compare(
            f"admission.{field}",
            admission.get(field),
            expected,
            errors,
        )

    manifest_command = (
        manifest.get("selected_remediation_command")
        if isinstance(manifest.get("selected_remediation_command"), dict)
        else {}
    )
    for field in ("runtime_target_id", "action", "label", "kind", "requires_edge_execution", "command_text", "note"):
        checked_fields += _edge_manifest_compare(
            f"selected_remediation_command.{field}",
            manifest_command.get(field),
            selected_command.get(field),
            errors,
        )

    return {
        "schema_version": "temms-edge-execution-manifest-consistency/v1",
        "status": "consistent" if not errors else "mismatch",
        "checked_fields": checked_fields,
        "errors": errors,
    }


def _edge_runtime_workbench_selected_target(
    workbench: dict[str, Any],
    *,
    selected_runtime_target_id: str,
) -> dict[str, Any]:
    selected_target = (
        workbench.get("selected_target")
        if isinstance(workbench.get("selected_target"), dict)
        else {}
    )
    if selected_target:
        return selected_target
    targets = workbench.get("targets") if isinstance(workbench.get("targets"), list) else []
    for target in targets:
        if not isinstance(target, dict):
            continue
        if target.get("selected") is True:
            return target
        if selected_runtime_target_id and str(target.get("runtime_target_id") or "") == selected_runtime_target_id:
            return target
    return {}


def _edge_manifest_source_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


def _edge_manifest_path_label(path: dict[str, Any]) -> str:
    model = str(path.get("model_id") or "model")
    runtime = str(path.get("runtime_target_id") or "runtime")
    device = str(path.get("device_id") or "edge")
    return f"{model} -> {runtime} -> {device}"


def _edge_manifest_compare(
    field: str,
    actual: Any,
    expected: Any,
    errors: list[str],
) -> int:
    if _edge_manifest_empty(actual) and _edge_manifest_empty(expected):
        return 0
    if not _edge_manifest_values_equal(actual, expected):
        errors.append(f"{field} is {actual!r}, expected {expected!r}")
    return 1


def _edge_manifest_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _edge_manifest_values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return left == right
    return _edge_trace_values_equal(left, right)


def _edge_runtime_trace_rows_by_id(trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = trace.get("rows") if isinstance(trace.get("rows"), list) else []
    rows_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        runtime_target_id = str(row.get("runtime_target_id") or "")
        if runtime_target_id:
            rows_by_id[runtime_target_id] = row
    return rows_by_id


def _edge_runtime_trace_commands_by_id(trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    commands = trace.get("commands") if isinstance(trace.get("commands"), list) else []
    commands_by_id: dict[str, dict[str, Any]] = {}
    for command in commands:
        if not isinstance(command, dict):
            continue
        runtime_target_id = str(command.get("runtime_target_id") or "")
        if runtime_target_id:
            commands_by_id[runtime_target_id] = command
    return commands_by_id


def _edge_runtime_decision_trace_row_errors(
    runtime_target_id: str,
    trace_row: dict[str, Any],
    expected_row: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    fields = (
        "rank",
        "status",
        "eligible",
        "selected",
        "best",
        "score",
        "tier",
        "detail",
        "validation_id",
        "benchmark_id",
        "latency_ms_p95",
        "throughput_ips",
    )
    for field in fields:
        if not _edge_trace_values_equal(trace_row.get(field), expected_row.get(field)):
            errors.append(
                f"row {runtime_target_id}.{field} is "
                f"{trace_row.get(field)!r}, expected {expected_row.get(field)!r}"
            )

    trace_lock = (
        trace_row.get("capability_lock")
        if isinstance(trace_row.get("capability_lock"), dict)
        else {}
    )
    expected_lock = (
        expected_row.get("capability_lock")
        if isinstance(expected_row.get("capability_lock"), dict)
        else {}
    )
    for field in ("status", "capability_sha256", "telemetry_state", "telemetry_status"):
        if not _edge_trace_values_equal(trace_lock.get(field), expected_lock.get(field)):
            errors.append(
                f"row {runtime_target_id}.capability_lock.{field} is "
                f"{trace_lock.get(field)!r}, expected {expected_lock.get(field)!r}"
            )

    trace_components = (
        trace_row.get("proof_components")
        if isinstance(trace_row.get("proof_components"), dict)
        else {}
    )
    expected_components = (
        expected_row.get("proof_components")
        if isinstance(expected_row.get("proof_components"), dict)
        else {}
    )
    for component_name, fields_to_check in {
        "runtime_validation": ("status", "state", "evidence_id"),
        "benchmark": ("status", "state", "evidence_id", "latency_ms_p95", "throughput_ips"),
        "resource": ("status", "state"),
        "telemetry": ("status", "state"),
        "capability_lock": ("status", "capability_sha256"),
    }.items():
        trace_component = (
            trace_components.get(component_name)
            if isinstance(trace_components.get(component_name), dict)
            else {}
        )
        expected_component = (
            expected_components.get(component_name)
            if isinstance(expected_components.get(component_name), dict)
            else {}
        )
        for field in fields_to_check:
            if not _edge_trace_values_equal(
                trace_component.get(field),
                expected_component.get(field),
            ):
                errors.append(
                    f"row {runtime_target_id}.proof_components.{component_name}.{field} "
                    f"is {trace_component.get(field)!r}, expected "
                    f"{expected_component.get(field)!r}"
                )

    return errors


def _edge_trace_values_equal(left: Any, right: Any) -> bool:
    if left == right:
        return True
    if left in (None, "") and right in (None, ""):
        return True
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def _edge_target_assessment_blocked(assessment: dict[str, Any]) -> bool:
    status = str(assessment.get("status") or "").lower()
    return status == "blocked" or assessment.get("blocked") is True


def _edge_target_assessment_eligible(assessment: dict[str, Any]) -> bool:
    status = str(assessment.get("status") or "").lower()
    if status == "eligible" or assessment.get("eligible") is True:
        return True
    return bool(status) and not _edge_target_assessment_blocked(assessment)


def _edge_target_runtime_coverage(contract_or_decision: dict[str, Any]) -> dict[str, Any]:
    assessments = _edge_target_assessments(contract_or_decision)
    selected = next(
        (
            str(item.get("runtime_target_id"))
            for item in assessments
            if item.get("selected") is True and item.get("runtime_target_id")
        ),
        None,
    )
    best = next(
        (
            str(item.get("runtime_target_id"))
            for item in assessments
            if item.get("best") is True and item.get("runtime_target_id")
        ),
        None,
    )
    requires_edge_execution = []
    commands = []
    for item in assessments:
        remediation = item.get("remediation") if isinstance(item.get("remediation"), dict) else {}
        if remediation.get("requires_edge_execution") is True and item.get("runtime_target_id"):
            requires_edge_execution.append(str(item["runtime_target_id"]))
        command = _edge_target_remediation_command(item)
        if command is not None:
            commands.append(command)

    return {
        "schema_version": "temms-target-runtime-coverage/v1",
        "assessed": len(assessments),
        "eligible": sum(1 for item in assessments if _edge_target_assessment_eligible(item)),
        "blocked": sum(1 for item in assessments if _edge_target_assessment_blocked(item)),
        "selected_runtime_target_id": selected,
        "best_runtime_target_id": best,
        "requires_edge_execution": requires_edge_execution,
        "commands": commands,
    }


def _edge_target_score_text(score: Any) -> str:
    if score in (None, ""):
        return ""
    try:
        return f"{float(score):g}/100"
    except (TypeError, ValueError):
        return str(score)


def _edge_target_lane_text(assessment: dict[str, Any]) -> str:
    lane = assessment.get("runtime_lane") if isinstance(assessment.get("runtime_lane"), dict) else {}
    return str(
        lane.get("label")
        or lane.get("lane_id")
        or assessment.get("runtime_target_id")
        or ""
    )


def _edge_target_state_text(assessment: dict[str, Any]) -> str:
    status = str(assessment.get("status") or "").strip()
    if not status:
        status = "blocked" if _edge_target_assessment_blocked(assessment) else "eligible"
    flags = []
    if assessment.get("selected") is True:
        flags.append("selected")
    if assessment.get("best") is True:
        flags.append("best")
    return " ".join([status, *flags])


def _edge_target_component_text(assessment: dict[str, Any]) -> str:
    lock = (
        assessment.get("runtime_capability_lock")
        if isinstance(assessment.get("runtime_capability_lock"), dict)
        else {}
    )
    if lock:
        status = str(lock.get("status") or "unknown")
        digest = str(lock.get("capability_sha256") or "")
        return f"{status} {digest[:12]}".strip()

    component_states = (
        assessment.get("component_states")
        if isinstance(assessment.get("component_states"), dict)
        else {}
    )
    labels = (
        ("compatibility", "compat"),
        ("runtime_validation", "valid"),
        ("performance", "perf"),
        ("resource", "res"),
        ("telemetry", "tel"),
    )
    parts = []
    for key, label in labels:
        component = component_states.get(key)
        if not isinstance(component, dict):
            continue
        state = component.get("state") or component.get("status")
        if state:
            parts.append(f"{label}:{state}")
    return ", ".join(parts) if parts else "not reported"


def _edge_target_remediation_text(assessment: dict[str, Any]) -> str:
    remediation = (
        assessment.get("remediation")
        if isinstance(assessment.get("remediation"), dict)
        else {}
    )
    if not remediation:
        return "Review target evidence"
    label = str(remediation.get("label") or remediation.get("action") or "Review")
    detail = str(remediation.get("detail") or "").strip()
    suffix = " [edge-run]" if remediation.get("requires_edge_execution") is True else ""
    return f"{label}: {detail}{suffix}" if detail else f"{label}{suffix}"


def _edge_target_remediation_command(assessment: dict[str, Any]) -> dict[str, Any] | None:
    remediation = (
        assessment.get("remediation")
        if isinstance(assessment.get("remediation"), dict)
        else {}
    )
    if not remediation:
        return None

    command_record = (
        remediation.get("command") if isinstance(remediation.get("command"), dict) else {}
    )
    edge_command_text = str(
        remediation.get("edge_command_text")
        or command_record.get("edge_command_text")
        or ""
    ).strip()
    operator_command_text = str(
        remediation.get("operator_command_text")
        or command_record.get("operator_command_text")
        or ""
    ).strip()
    edge_command = _edge_target_command_text_from_list(
        remediation.get("edge_command") or command_record.get("edge_command")
    )
    operator_command = _edge_target_command_text_from_list(
        remediation.get("operator_command") or command_record.get("operator_command")
    )

    command_text = edge_command_text or operator_command_text or edge_command or operator_command
    if not command_text:
        return None

    kind = "edge" if edge_command_text or edge_command else "operator"
    note = str(
        remediation.get(f"{kind}_command_note")
        or command_record.get(f"{kind}_command_note")
        or ""
    ).strip()
    return {
        "runtime_target_id": str(assessment.get("runtime_target_id") or ""),
        "action": str(remediation.get("action") or ""),
        "label": str(remediation.get("label") or remediation.get("action") or "Review"),
        "kind": kind,
        "requires_edge_execution": remediation.get("requires_edge_execution") is True,
        "command_text": command_text,
        "note": note,
    }


def _edge_target_command_text_from_list(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    command = [str(part) for part in value if part not in (None, "")]
    return shlex.join(command) if command else ""


def _print_edge_target_runtime_coverage(contract_or_decision: dict[str, Any]) -> None:
    assessments = _edge_target_assessments(contract_or_decision)
    if not assessments:
        return

    coverage = _edge_target_runtime_coverage(contract_or_decision)
    commands = coverage.get("commands") if isinstance(coverage.get("commands"), list) else []
    console.print(
        "Target coverage: "
        f"{coverage['eligible']} eligible / "
        f"{coverage['blocked']} blocked "
        f"({coverage['assessed']} assessed)"
    )
    for assessment in assessments:
        runtime_target_id = str(assessment.get("runtime_target_id") or "runtime")
        console.print(
            f"Target runtime {runtime_target_id}: "
            f"{_edge_target_state_text(assessment)}; "
            f"lane {_edge_target_lane_text(assessment)}; "
            f"next {_edge_target_remediation_text(assessment)}"
        )
    for command in commands:
        if not isinstance(command, dict):
            continue
        console.print(
            "Target remediation command "
            f"{command.get('runtime_target_id') or 'runtime'} "
            f"({command.get('kind') or 'operator'}): "
            f"{command.get('command_text') or ''}"
        )

    table = Table(title="Target Runtime Coverage")
    table.add_column("Runtime")
    table.add_column("Lane")
    table.add_column("State")
    table.add_column("Score")
    table.add_column("Capability Proof")
    table.add_column("Next")
    for assessment in assessments:
        table.add_row(
            str(assessment.get("runtime_target_id") or ""),
            _edge_target_lane_text(assessment),
            _edge_target_state_text(assessment),
            _edge_target_score_text(assessment.get("score")),
            _edge_target_component_text(assessment),
            _edge_target_remediation_text(assessment),
        )
    console.print(table)

    command_rows = [command for command in commands if isinstance(command, dict)]
    if command_rows:
        command_table = Table(title="Target Remediation Commands")
        command_table.add_column("Runtime")
        command_table.add_column("Kind")
        command_table.add_column("Action")
        command_table.add_column("Command")
        for command in command_rows:
            command_table.add_row(
                str(command.get("runtime_target_id") or ""),
                str(command.get("kind") or ""),
                str(command.get("label") or command.get("action") or ""),
                str(command.get("command_text") or ""),
            )
        console.print(command_table)


def _print_edge_runtime_decision_trace(trace: dict[str, Any]) -> None:
    rows = trace.get("rows") if isinstance(trace.get("rows"), list) else []
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return

    console.print(
        "Runtime decision trace: "
        f"{trace.get('eligible_target_count', 0)} eligible / "
        f"{trace.get('blocked_target_count', 0)} blocked "
        f"({trace.get('target_count', len(rows))} targets)"
    )
    table = Table(title="Runtime Decision Trace")
    table.add_column("Runtime")
    table.add_column("Rank")
    table.add_column("State")
    table.add_column("Score")
    table.add_column("Proof")
    table.add_column("Reason")
    table.add_column("Next")
    for row in rows:
        table.add_row(
            str(row.get("runtime_target_id") or ""),
            _edge_runtime_trace_rank(row),
            _edge_runtime_trace_state(row),
            _edge_target_score_text(row.get("score")),
            _edge_runtime_trace_proof_text(row),
            _edge_runtime_trace_reason(row),
            _edge_runtime_trace_next(row),
        )
    console.print(table)


def _edge_runtime_trace_rank(row: dict[str, Any]) -> str:
    rank = row.get("rank")
    if rank not in (None, ""):
        return str(rank)
    if row.get("selected") is True and row.get("best") is True:
        return "selected/best"
    if row.get("selected") is True:
        return "selected"
    if row.get("best") is True:
        return "best"
    return ""


def _edge_runtime_trace_state(row: dict[str, Any]) -> str:
    state = str(row.get("status") or "")
    flags = []
    if row.get("selected") is True:
        flags.append("selected")
    if row.get("best") is True:
        flags.append("best")
    return " ".join([state, *flags]).strip()


def _edge_runtime_trace_proof_text(row: dict[str, Any]) -> str:
    components = (
        row.get("proof_components")
        if isinstance(row.get("proof_components"), dict)
        else {}
    )
    parts = []
    labels = (
        ("runtime_validation", "valid"),
        ("benchmark", "bench"),
        ("resource", "res"),
        ("telemetry", "tel"),
        ("capability_lock", "cap"),
    )
    for key, label in labels:
        component = components.get(key) if isinstance(components.get(key), dict) else {}
        state = component.get("status") or component.get("state")
        if state:
            parts.append(f"{label}:{state}")
    capability_lock = (
        row.get("capability_lock")
        if isinstance(row.get("capability_lock"), dict)
        else {}
    )
    digest = str(capability_lock.get("capability_sha256") or "")
    if digest:
        parts.append(digest[:12])
    return ", ".join(parts) if parts else "not retained"


def _edge_runtime_trace_reason(row: dict[str, Any]) -> str:
    for key in ("reasons", "penalties"):
        values = row.get(key)
        if isinstance(values, list):
            for value in values:
                if value:
                    return str(value)
    remediation = row.get("remediation") if isinstance(row.get("remediation"), dict) else {}
    return str(remediation.get("detail") or row.get("detail") or "")


def _edge_runtime_trace_next(row: dict[str, Any]) -> str:
    command = (
        row.get("remediation_command")
        if isinstance(row.get("remediation_command"), dict)
        else {}
    )
    remediation = row.get("remediation") if isinstance(row.get("remediation"), dict) else {}
    label = str(command.get("label") or remediation.get("label") or remediation.get("action") or "")
    kind = str(command.get("kind") or ("edge" if remediation.get("requires_edge_execution") else "operator"))
    return f"{label} ({kind})" if label else ""


def _print_edge_runtime_proof_verification(payload: dict[str, Any]) -> None:
    """Print local edge proof verification in an operator-readable format."""
    valid = bool(payload.get("valid"))
    color = "green" if valid else "red"
    verdict = "valid" if valid else "invalid"
    console.print(f"[bold {color}]Edge mission proof: {verdict}[/bold {color}]")
    console.print(f"Proof: {payload.get('proof_path', '')}")

    path = payload.get("path") if isinstance(payload.get("path"), dict) else {}
    if path:
        console.print(
            "Path: "
            f"{path.get('model_id') or 'model'} -> "
            f"{path.get('runtime_target_id') or 'runtime'} -> "
            f"{path.get('device_id') or 'edge'}"
        )

    console.print(f"Mission status: {payload.get('status') or 'unknown'}")
    runtime_fit_score = payload.get("runtime_fit_score")
    if runtime_fit_score is not None:
        try:
            score_text = f"{float(runtime_fit_score):g}/100"
        except (TypeError, ValueError):
            score_text = f"{runtime_fit_score}/100"
        console.print(f"Runtime fit: {score_text}")
    console.print(f"Recorded gate: {payload.get('gate_status') or 'unknown'}")
    console.print(f"Requested gate: {payload.get('requested_gate_status') or 'unknown'}")
    proof_freshness = (
        payload.get("proof_freshness")
        if isinstance(payload.get("proof_freshness"), dict)
        else {}
    )
    if proof_freshness:
        freshness_status = proof_freshness.get("status") or "unknown"
        age = proof_freshness.get("age_seconds")
        max_age = proof_freshness.get("max_age_seconds")
        freshness_detail = ""
        if age is not None:
            try:
                freshness_detail = f" age {float(age):g}s"
            except (TypeError, ValueError):
                freshness_detail = f" age {age}s"
        if max_age is not None:
            try:
                freshness_detail += f" / max {float(max_age):g}s"
            except (TypeError, ValueError):
                freshness_detail += f" / max {max_age}s"
        console.print(f"Proof freshness: {freshness_status}{freshness_detail}")
    path_expectations = (
        payload.get("path_expectations")
        if isinstance(payload.get("path_expectations"), dict)
        else {}
    )
    if path_expectations and path_expectations.get("status") != "not_requested":
        console.print(f"Path binding: {path_expectations.get('status') or 'unknown'}")

    runtime_decision = (
        payload.get("runtime_decision")
        if isinstance(payload.get("runtime_decision"), dict)
        else {}
    )
    if runtime_decision:
        target_selection = (
            runtime_decision.get("target_selection")
            if isinstance(runtime_decision.get("target_selection"), dict)
            else {}
        )
        selected = target_selection.get("selected_runtime_target_id") or path.get(
            "runtime_target_id"
        )
        best = target_selection.get("best_runtime_target_id")
        decision_status = target_selection.get("status") or runtime_decision.get(
            "readiness_status"
        )
        console.print(
            "Runtime decision: "
            f"{decision_status or 'unknown'}"
            f" ({runtime_decision.get('recommended_action') or 'review'})"
        )
        if selected or best:
            console.print(
                "Runtime target: "
                f"{selected or 'selected unknown'}"
                + (f" / best {best}" if best else "")
            )

    edge_execution_contract = (
        payload.get("edge_execution_contract")
        if isinstance(payload.get("edge_execution_contract"), dict)
        else {}
    )
    if edge_execution_contract:
        target_selection = (
            edge_execution_contract.get("target_selection")
            if isinstance(edge_execution_contract.get("target_selection"), dict)
            else {}
        )
        capability_lock = (
            edge_execution_contract.get("runtime_capability_lock")
            if isinstance(edge_execution_contract.get("runtime_capability_lock"), dict)
            else {}
        )
        console.print(
            "Execution contract: "
            f"{edge_execution_contract.get('status') or 'unknown'}"
            f" ({edge_execution_contract.get('recommended_action') or 'review'})"
        )
        if target_selection.get("selected_runtime_target_id"):
            console.print(
                "Contract runtime: "
                f"{target_selection.get('selected_runtime_target_id')}"
                + (
                    f" / best {target_selection.get('best_runtime_target_id')}"
                    if target_selection.get("best_runtime_target_id")
                    else ""
                )
            )
        if capability_lock:
            digest = str(capability_lock.get("capability_sha256") or "")
            console.print(
                "Capability lock: "
                f"{capability_lock.get('status') or 'unknown'}"
                + (f" {digest[:12]}" if digest else "")
            )

    _print_edge_target_runtime_coverage(edge_execution_contract or runtime_decision)
    runtime_decision_trace = (
        payload.get("runtime_decision_trace")
        if isinstance(payload.get("runtime_decision_trace"), dict)
        else {}
    )
    _print_edge_runtime_decision_trace(runtime_decision_trace)
    trace_consistency = (
        payload.get("runtime_decision_trace_consistency")
        if isinstance(payload.get("runtime_decision_trace_consistency"), dict)
        else {}
    )
    if trace_consistency:
        console.print(
            "Runtime trace consistency: "
            f"{trace_consistency.get('status') or 'unknown'}"
        )
    manifest_consistency = (
        payload.get("edge_execution_manifest_consistency")
        if isinstance(payload.get("edge_execution_manifest_consistency"), dict)
        else {}
    )
    if manifest_consistency:
        console.print(
            "Execution manifest: "
            f"{manifest_consistency.get('status') or 'unknown'}"
        )
    component_digest_consistency = (
        payload.get("component_digest_consistency")
        if isinstance(payload.get("component_digest_consistency"), dict)
        else {}
    )
    if component_digest_consistency:
        console.print(
            "Component digests: "
            f"{component_digest_consistency.get('status') or 'unknown'}"
        )

    integrity = payload.get("integrity") if isinstance(payload.get("integrity"), dict) else {}
    recorded_hash = integrity.get("recorded_payload_sha256")
    if recorded_hash:
        console.print(f"Payload SHA256: {recorded_hash}")

    attestation = payload.get("attestation") if isinstance(payload.get("attestation"), dict) else {}
    if attestation:
        console.print(f"Attestation: {attestation.get('status') or 'unknown'}")
        if attestation.get("key_fingerprint"):
            console.print(f"Attestation key: {attestation['key_fingerprint']}")
        if attestation.get("signer"):
            console.print(f"Attestation signer: {attestation['signer']}")

    for error in payload.get("errors", []) or []:
        console.print(f"[red]Proof invalid:[/red] {error}")
    for failure in payload.get("gate_failures", []) or []:
        console.print(f"[yellow]Recorded gate failed:[/yellow] {failure}")
    for failure in payload.get("requested_gate_failures", []) or []:
        console.print(f"[red]Requested gate failed:[/red] {failure}")


def _mission_metric_label(key: str) -> str:
    labels = {
        "runtime_fit": "Runtime fit",
        "target_selection": "Target selection",
        "runtime_lane": "Runtime lane",
        "artifact_fit": "Artifact fit",
        "live_inventory": "Live inventory",
        "performance": "Performance SLO",
        "resources": "Resource envelope",
        "runtime_validation": "Runtime validation",
        "production_admission": "Production admission",
        "ddil_repair": "DDIL repair",
    }
    return labels.get(key, key.replace("_", " ").title())


def _mission_metric_status(metric: dict[str, Any]) -> str:
    status = metric.get("status") or metric.get("state") or ""
    score = metric.get("score")
    tier = metric.get("tier")
    if score is not None:
        suffix = f"score {score}"
        if tier:
            suffix += f", {tier}"
        return f"{status} ({suffix})" if status else suffix
    if metric.get("apply_allowed") is not None:
        allowed = "allowed" if metric.get("apply_allowed") else "blocked"
        return f"{status} ({allowed})" if status else allowed
    return str(status)


def _mission_metric_detail(metric: dict[str, Any]) -> str:
    if metric.get("detail"):
        return str(metric["detail"])
    if metric.get("label"):
        lane = str(metric["label"])
        engine = metric.get("execution_engine")
        acceleration = metric.get("acceleration")
        parts = [lane]
        if engine:
            parts.append(str(engine))
        if acceleration:
            parts.append(str(acceleration))
        return " / ".join(parts)
    if metric.get("state"):
        return str(metric["state"])
    if metric.get("best_runtime_target_id"):
        return f"best target {metric['best_runtime_target_id']}"
    return ""


def _hub_status_color(status: str) -> str:
    if status == "go":
        return "green"
    if status == "blocked":
        return "red"
    if status == "attention":
        return "yellow"
    return "white"


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
