"""
Local model cache (not a registry - just tracks imported packages).
"""

import sqlite3
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from dataclasses import dataclass
from enum import Enum


class ModelFormat(str, Enum):
    """Supported model formats."""
    ONNX = "onnx"
    TFLITE = "tflite"
    TORCHSCRIPT = "torchscript"
    TENSORRT = "tensorrt"


@dataclass
class CachedModel:
    """Cached model metadata (from imported package)."""
    id: str
    name: str
    version: str
    format: ModelFormat
    path: Path
    sha256: str
    size_bytes: int
    metadata: Dict[str, Any]
    package_id: str  # Which package this came from
    imported_at: datetime

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "format": self.format.value,
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "metadata": self.metadata,
            "package_id": self.package_id,
            "imported_at": self.imported_at.isoformat(),
        }


@dataclass
class ImportedPackage:
    """Imported TEMMS package."""
    id: str
    name: str
    version: str
    source: str  # USB path, URL, etc.
    imported_at: datetime
    manifest: Dict[str, Any]


class ModelCache:
    """Local cache of imported models (not a full registry)."""

    def __init__(self, db_path: Path):
        """Initialize cache with database path."""
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            # Imported packages
            conn.execute("""
                CREATE TABLE IF NOT EXISTS packages (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source TEXT NOT NULL,
                    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    manifest JSON NOT NULL
                )
            """)

            # Cached models (from packages)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cached_models (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    format TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER,
                    metadata JSON,
                    package_id TEXT REFERENCES packages(id),
                    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def add_package(
        self,
        package_id: str,
        name: str,
        version: str,
        source: str,
        manifest: Dict[str, Any],
    ) -> ImportedPackage:
        """Record imported package."""
        imported_at = datetime.now()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO packages (id, name, version, source, imported_at, manifest)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (package_id, name, version, source, imported_at, json.dumps(manifest)),
            )
            conn.commit()

        return ImportedPackage(
            id=package_id,
            name=name,
            version=version,
            source=source,
            imported_at=imported_at,
            manifest=manifest,
        )

    def add_cached_model(
        self,
        model_id: str,
        name: str,
        version: str,
        format: ModelFormat,
        path: Path,
        sha256: str,
        size_bytes: int,
        package_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CachedModel:
        """Add model to cache (from package import)."""
        imported_at = datetime.now()
        metadata = metadata or {}

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO cached_models
                (id, name, version, format, path, sha256, size_bytes, metadata, package_id, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_id,
                    name,
                    version,
                    format.value,
                    str(path),
                    sha256,
                    size_bytes,
                    json.dumps(metadata),
                    package_id,
                    imported_at,
                ),
            )
            conn.commit()

        return CachedModel(
            id=model_id,
            name=name,
            version=version,
            format=format,
            path=path,
            sha256=sha256,
            size_bytes=size_bytes,
            metadata=metadata,
            package_id=package_id,
            imported_at=imported_at,
        )

    def get_model(self, model_id: str) -> Optional[CachedModel]:
        """Get cached model by ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM cached_models WHERE id = ?", (model_id,)
            )
            row = cursor.fetchone()

        if not row:
            return None

        return CachedModel(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            format=ModelFormat(row["format"]),
            path=Path(row["path"]),
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            metadata=json.loads(row["metadata"]),
            package_id=row["package_id"],
            imported_at=datetime.fromisoformat(row["imported_at"]),
        )

    def list_models(self) -> List[CachedModel]:
        """List all cached models."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM cached_models ORDER BY imported_at DESC")
            rows = cursor.fetchall()

        return [
            CachedModel(
                id=row["id"],
                name=row["name"],
                version=row["version"],
                format=ModelFormat(row["format"]),
                path=Path(row["path"]),
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                metadata=json.loads(row["metadata"]),
                package_id=row["package_id"],
                imported_at=datetime.fromisoformat(row["imported_at"]),
            )
            for row in rows
        ]

    def find_model(self, name: str, version: Optional[str] = None) -> Optional[CachedModel]:
        """Find cached model by name and optional version."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if version:
                cursor = conn.execute(
                    "SELECT * FROM cached_models WHERE name = ? AND version = ?",
                    (name, version),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM cached_models WHERE name = ? ORDER BY imported_at DESC LIMIT 1",
                    (name,),
                )
            row = cursor.fetchone()

        if not row:
            return None

        return CachedModel(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            format=ModelFormat(row["format"]),
            path=Path(row["path"]),
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            metadata=json.loads(row["metadata"]),
            package_id=row["package_id"],
            imported_at=datetime.fromisoformat(row["imported_at"]),
        )

    def list_packages(self) -> List[ImportedPackage]:
        """List all imported packages."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM packages ORDER BY imported_at DESC")
            rows = cursor.fetchall()

        return [
            ImportedPackage(
                id=row["id"],
                name=row["name"],
                version=row["version"],
                source=row["source"],
                imported_at=datetime.fromisoformat(row["imported_at"]),
                manifest=json.loads(row["manifest"]),
            )
            for row in rows
        ]
