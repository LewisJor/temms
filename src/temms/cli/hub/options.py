"""Shared option types for the Hub sub-commands.

Every Hub sub-command needs ``--hub-url``, ``--token``, ``--json`` and most
need ``--output``. Declaring them as annotated aliases keeps one definition of
each flag's name, default and help text, so thirty-five signatures cannot
drift apart -- while still letting each sub-command declare only the options it
actually accepts.

This is the difference from the old single ``hub()``: there, every option was
in scope for every action, so ``temms hub devices --archive`` was accepted and
silently ignored. Here an option a sub-command does not name is a usage error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

# --- Connection -----------------------------------------------------------

HubUrl = Annotated[
    str, typer.Option("--hub-url", help="TEMMS Hub Lite API base URL")
]
Token = Annotated[
    str | None,
    typer.Option(
        "--token", help="Hub API token; defaults to TEMMS_HUB_TOKEN or TEMMS_API_TOKEN"
    ),
]

DEFAULT_HUB_URL = "http://127.0.0.1:8080"

# --- Output ---------------------------------------------------------------

JsonOutput = Annotated[bool, typer.Option("--json", help="Emit raw JSON")]
Output = Annotated[
    Path | None, typer.Option("--output", "-o", help="Write the result to this path")
]

# --- Identity -------------------------------------------------------------

DeviceId = Annotated[str | None, typer.Option("--device-id", help="Target device ID")]
PackageId = Annotated[str | None, typer.Option("--package-id", help="Package ID")]
ModelId = Annotated[
    str | None, typer.Option("--model-id", help="Model ID within the package")
]
RuntimeTargetId = Annotated[
    str | None, typer.Option("--runtime-target-id", help="Runtime target ID")
]
Slot = Annotated[str | None, typer.Option("--slot", help="Slot name")]
DeviceProfile = Annotated[
    str | None, typer.Option("--device-profile", help="Device profile identifier")
]

# --- Attribution ----------------------------------------------------------

Actor = Annotated[
    str | None, typer.Option("--actor", help="Operator recorded in the audit trail")
]
Reason = Annotated[
    str | None, typer.Option("--reason", help="Reason recorded in the audit trail")
]

# --- Rollout shaping ------------------------------------------------------

BatchSize = Annotated[
    int | None, typer.Option("--batch-size", help="Devices per rollout batch")
]
RequireRuntimeValidation = Annotated[
    bool,
    typer.Option(
        "--require-runtime-validation",
        help="Refuse to proceed without a recorded runtime validation",
    ),
]
RequireApproval = Annotated[
    bool, typer.Option("--require-approval", help="Hold the rollout for approval")
]

# --- Signing --------------------------------------------------------------

RequireSignature = Annotated[
    bool,
    typer.Option(
        "--require-signature/--allow-unsigned-package", help="Refuse unsigned packages"
    ),
]
RequireSchema = Annotated[
    bool,
    typer.Option(
        "--require-schema/--allow-missing-schema", help="Refuse models without a schema"
    ),
]
Archive = Annotated[
    bool,
    typer.Option("--archive/--directory-package", help="Emit a tarball rather than a directory"),
]
SigningKey = Annotated[
    str | None, typer.Option("--signing-key", help="Ed25519 signing key (inline)")
]
SigningKeyFile = Annotated[
    Path | None, typer.Option("--signing-key-file", help="Ed25519 signing key file")
]

# --- Runtime capability ---------------------------------------------------

Runtimes = Annotated[
    list[str] | None, typer.Option("--runtime", help="Runtime (repeatable)")
]
Providers = Annotated[
    list[str] | None,
    typer.Option("--provider", help="ONNX Runtime execution provider (repeatable)"),
]
Accelerators = Annotated[
    list[str] | None, typer.Option("--accelerator", help="Accelerator (repeatable)")
]

# --- Proof gates ----------------------------------------------------------

RequireGo = Annotated[
    bool, typer.Option("--require-go", help="Fail unless the decision is GO")
]
MinRuntimeFit = Annotated[
    float | None,
    typer.Option("--min-runtime-fit", help="Fail below this runtime fit score"),
]
RequireBestRuntime = Annotated[
    bool,
    typer.Option(
        "--require-best-runtime", help="Fail unless the best runtime target was selected"
    ),
]
RequireCapabilityLock = Annotated[
    bool,
    typer.Option(
        "--require-capability-lock", help="Fail unless runtime capabilities are locked"
    ),
]

# --- Metadata -------------------------------------------------------------

StrictMetadata = Annotated[
    bool, typer.Option("--strict-metadata/--no-strict-metadata", help="Enforce metadata rules")
]
