"""
Package builder/admin commands.
"""

import json
import os
from pathlib import Path

import typer
from rich.console import Console

from temms.core.package import generate_ed25519_keypair, sign_package_manifest

app = typer.Typer(help="Manage package signing")
console = Console()


@app.command("keygen")
def keygen(
    key_id: str = typer.Option(..., "--key-id", help="Trusted signing key identifier"),
    private_key_path: Path = typer.Option(
        ...,
        "--private-key",
        help="Path where the private signing key should be written",
    ),
    trusted_keys_path: Path | None = typer.Option(
        None,
        "--trusted-keys",
        help="Optional trusted-key JSON file to create or update",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing key files"),
):
    """Generate an Ed25519 keypair for signing TEMMS packages."""
    if private_key_path.exists() and not force:
        console.print(f"[red]Private key already exists: {private_key_path}[/red]")
        raise typer.Exit(1)

    try:
        keypair = generate_ed25519_keypair()
    except Exception as exc:
        console.print(f"[red]Could not generate keypair: {exc}[/red]")
        raise typer.Exit(1)

    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key_path.write_text(keypair["private_key"] + "\n", encoding="utf-8")
    try:
        os.chmod(private_key_path, 0o600)
    except OSError:
        pass

    console.print(f"[green]✓ Wrote private key:[/green] {private_key_path}")
    console.print(f"Public key ({key_id}): {keypair['public_key']}")

    if trusted_keys_path is not None:
        trusted_keys = _load_trusted_key_map(trusted_keys_path)
        trusted_keys[key_id] = keypair["public_key"]
        trusted_keys_path.parent.mkdir(parents=True, exist_ok=True)
        trusted_keys_path.write_text(
            json.dumps(trusted_keys, indent=2) + "\n",
            encoding="utf-8",
        )
        console.print(f"[green]✓ Updated trusted keys:[/green] {trusted_keys_path}")


@app.command("sign")
def sign(
    package_path: Path = typer.Argument(..., help="Path to TEMMS package directory"),
    key_id: str = typer.Option(..., "--key-id", help="Signing key identifier"),
    private_key_path: Path = typer.Option(
        ...,
        "--private-key",
        help="Path to the private signing key",
    ),
):
    """Sign a TEMMS package manifest in place."""
    if not package_path.exists():
        console.print(f"[red]Package not found: {package_path}[/red]")
        raise typer.Exit(1)
    if not private_key_path.exists():
        console.print(f"[red]Private key not found: {private_key_path}[/red]")
        raise typer.Exit(1)

    try:
        signature = sign_package_manifest(
            package_path,
            key_id=key_id,
            private_key_material=private_key_path.read_text(encoding="utf-8").strip(),
        )
    except Exception as exc:
        console.print(f"[red]Could not sign package: {exc}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]✓ Signed package:[/green] {package_path}")
    console.print(f"Key: {signature['key_id']}")


def _load_trusted_key_map(path: Path) -> dict[str, str]:
    """Load an existing trusted-key JSON object."""
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        console.print(f"[red]Trusted-key file is not valid JSON: {exc}[/red]")
        raise typer.Exit(1)

    if not isinstance(payload, dict):
        console.print("[red]Trusted-key file must be a JSON object[/red]")
        raise typer.Exit(1)

    return {
        str(key): str(value)
        for key, value in payload.items()
    }
