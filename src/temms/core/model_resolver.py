"""Registry-agnostic model source resolution (issue #43, slice 1).

A ``mission.yaml`` references each model by a ``source: scheme://…`` URI. The
scheme is the abstraction seam: ``mlflow://`` today, ``sagemaker://`` / ``s3://``
later, each a drop-in resolver. Crucially the registry is a **build-time input,
never a runtime dependency** — the compiler resolves a URI to concrete artifact
bytes plus a registry-neutral provenance block, and the edge only ever sees the
resolved, signed artifact. Swapping registries has zero blast radius past the
build machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from temms.core.signing import sha256_file


class ModelResolutionError(ValueError):
    """A model source URI could not be resolved to an artifact."""


@dataclass
class ResolvedModel:
    """A model artifact resolved from a source URI, ready to package.

    ``provenance`` is registry-neutral on purpose: it records *where* the
    artifact came from (registry, ref, run/version, a metrics pointer) without
    binding the package to any one registry's API.
    """

    artifact_path: Path
    sha256: str
    provenance: dict[str, Any] = field(default_factory=dict)


class ModelResolver(Protocol):
    """Resolves a ``scheme://`` model source to a local artifact + provenance."""

    scheme: str

    def resolve(self, uri: str) -> ResolvedModel: ...


class FileModelResolver:
    """Resolve ``file://path`` or a bare local path.

    The registry-agnostic baseline: no external service, no run linkage. Used for
    local artifacts and as the resolver in tests that must not depend on a live
    registry.
    """

    scheme = "file"

    def __init__(self, base_dir: Path | None = None) -> None:
        # Relative sources resolve against the mission.yaml's directory.
        self.base_dir = base_dir

    def resolve(self, uri: str) -> ResolvedModel:
        parsed = urlparse(uri)
        if parsed.scheme in ("", "file"):
            raw = parsed.path if parsed.scheme == "file" else uri
            # file://relative/path leaves the first segment in netloc.
            if parsed.scheme == "file" and parsed.netloc:
                raw = f"{parsed.netloc}{parsed.path}"
        else:
            raise ModelResolutionError(f"FileModelResolver cannot handle {uri!r}")

        path = Path(raw)
        if not path.is_absolute() and self.base_dir is not None:
            path = self.base_dir / path
        if not path.is_file():
            raise ModelResolutionError(f"model artifact not found: {path}")

        return ResolvedModel(
            artifact_path=path,
            sha256=sha256_file(path),
            provenance={
                "registry": "file",
                "resolved_uri": uri,
                "ref": str(path),
            },
        )


class MLflowModelResolver:
    """Resolve ``mlflow://models/<name>/<version>`` (or ``@alias``).

    Downloads the registered model's artifact from the MLflow registry at build
    time and records the run id + a metrics pointer as provenance. MLflow is
    imported lazily so the resolver seam does not force the dependency on anyone
    who only uses ``file://``.
    """

    scheme = "mlflow"

    def __init__(self, tracking_uri: str | None = None) -> None:
        self.tracking_uri = tracking_uri

    @staticmethod
    def _parse(uri: str) -> tuple[str, str | None, str | None]:
        """Return (name, version, alias) from an mlflow:// URI."""
        parsed = urlparse(uri)
        if parsed.scheme != "mlflow":
            raise ModelResolutionError(f"not an mlflow URI: {uri!r}")
        # mlflow://models/<name>/<version>  → netloc="models", path="/name/version"
        segments = [parsed.netloc, *parsed.path.split("/")]
        segments = [s for s in segments if s]
        if not segments or segments[0] != "models":
            raise ModelResolutionError(
                f"expected mlflow://models/<name>/<version>, got {uri!r}"
            )
        rest = segments[1:]
        if not rest:
            raise ModelResolutionError(f"mlflow URI is missing a model name: {uri!r}")
        name = rest[0]
        if "@" in name:
            base, alias = name.split("@", 1)
            return base, None, alias
        version = rest[1] if len(rest) > 1 else None
        return name, version, None

    def resolve(self, uri: str) -> ResolvedModel:
        name, version, alias = self._parse(uri)
        try:
            import mlflow
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ModelResolutionError(
                "resolving mlflow:// sources requires MLflow (install temms[mlflow])"
            ) from exc

        tracking_uri = (
            self.tracking_uri
            or os.environ.get("MLFLOW_TRACKING_URI")
            or "http://localhost:5000"
        )
        mlflow.set_tracking_uri(tracking_uri)
        client = mlflow.tracking.MlflowClient()

        if version:
            model_version = client.get_model_version(name, version)
        elif alias:
            model_version = client.get_model_version_by_alias(name, alias)
        else:
            versions = client.get_latest_versions(name)
            if not versions:
                raise ModelResolutionError(f"no versions for MLflow model {name!r}")
            model_version = versions[0]

        local_dir = Path(mlflow.artifacts.download_artifacts(model_version.source))
        artifact = _first_model_artifact(local_dir)
        if artifact is None:
            raise ModelResolutionError(
                f"no model artifact found under {model_version.source}"
            )

        return ResolvedModel(
            artifact_path=artifact,
            sha256=sha256_file(artifact),
            provenance={
                "registry": "mlflow",
                "resolved_uri": uri,
                "ref": f"models:/{name}/{model_version.version}",
                "run_id": model_version.run_id,
                "model_version": str(model_version.version),
                "metrics_uri": f"{tracking_uri}/#/experiments",
            },
        )


_MODEL_EXTENSIONS = {".onnx", ".tflite", ".pt", ".pth", ".engine", ".plan", ".bin"}


def _first_model_artifact(root: Path) -> Path | None:
    """Pick the model file from a downloaded artifact directory."""
    if root.is_file():
        return root
    candidates = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _MODEL_EXTENSIONS
    )
    return candidates[0] if candidates else None


def resolve_model_source(
    uri: str,
    *,
    base_dir: Path | None = None,
    tracking_uri: str | None = None,
) -> ResolvedModel:
    """Resolve a model source URI using the resolver for its scheme."""
    scheme = urlparse(uri).scheme
    if scheme == "mlflow":
        return MLflowModelResolver(tracking_uri=tracking_uri).resolve(uri)
    if scheme in ("", "file"):
        return FileModelResolver(base_dir=base_dir).resolve(uri)
    raise ModelResolutionError(
        f"unsupported model source scheme {scheme!r} in {uri!r} "
        "(supported: mlflow://, file://, or a local path)"
    )
