"""
Model cache health diagnostics.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def model_cache_health(models: list[Any]) -> dict[str, Any]:
    """Return file-level integrity health for cached model records."""
    issues: list[dict[str, Any]] = []
    checked = 0

    for model in models:
        checked += 1
        path = Path(model.path)
        issue_base = {
            "model_id": model.id,
            "package_id": model.package_id,
            "path": str(path),
        }
        if not path.exists():
            issues.append({**issue_base, "type": "missing_file"})
            continue
        if not path.is_file():
            issues.append({**issue_base, "type": "not_a_file"})
            continue
        actual_size = path.stat().st_size
        if model.size_bytes is not None and actual_size != model.size_bytes:
            issues.append(
                {
                    **issue_base,
                    "type": "size_mismatch",
                    "expected": model.size_bytes,
                    "actual": actual_size,
                }
            )
        actual_sha256 = sha256_path(path)
        if model.sha256 and actual_sha256 != model.sha256:
            issues.append(
                {
                    **issue_base,
                    "type": "sha256_mismatch",
                    "expected": model.sha256,
                    "actual": actual_sha256,
                }
            )

    return {
        "status": "healthy" if not issues else "degraded",
        "checked_models": checked,
        "issues": issues,
    }


def sha256_path(path: Path) -> str:
    """Compute SHA256 for a local file path."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
