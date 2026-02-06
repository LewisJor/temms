"""
Model file storage with SHA256 verification.
"""

import hashlib
import shutil
from pathlib import Path
from typing import Optional


class ModelStorage:
    """Manages model file storage and verification."""

    def __init__(self, model_dir: Path):
        """Initialize storage with model directory."""
        self.model_dir = model_dir
        self.model_dir.mkdir(parents=True, exist_ok=True)

    def store_model(self, source_path: Path, model_id: str, verify: bool = True) -> tuple[Path, str, int]:
        """
        Store a model file.

        Args:
            source_path: Source model file path
            model_id: Unique model identifier
            verify: Whether to compute SHA256 hash

        Returns:
            Tuple of (destination_path, sha256_hash, size_bytes)
        """
        if not source_path.exists():
            raise FileNotFoundError(f"Source model not found: {source_path}")

        # Create model-specific directory
        model_storage_dir = self.model_dir / model_id
        model_storage_dir.mkdir(parents=True, exist_ok=True)

        # Determine destination path
        dest_path = model_storage_dir / source_path.name

        # Compute hash while copying
        sha256_hash = hashlib.sha256()
        size_bytes = 0

        with open(source_path, "rb") as src, open(dest_path, "wb") as dst:
            while chunk := src.read(8192):
                size_bytes += len(chunk)
                if verify:
                    sha256_hash.update(chunk)
                dst.write(chunk)

        hash_value = sha256_hash.hexdigest() if verify else ""

        return dest_path, hash_value, size_bytes

    def verify_model(self, model_path: Path, expected_sha256: str) -> bool:
        """
        Verify model file integrity.

        Args:
            model_path: Path to model file
            expected_sha256: Expected SHA256 hash

        Returns:
            True if hash matches, False otherwise
        """
        if not model_path.exists():
            return False

        sha256_hash = hashlib.sha256()
        with open(model_path, "rb") as f:
            while chunk := f.read(8192):
                sha256_hash.update(chunk)

        return sha256_hash.hexdigest() == expected_sha256

    def delete_model(self, model_id: str) -> bool:
        """
        Delete model files.

        Args:
            model_id: Model identifier

        Returns:
            True if deleted, False if not found
        """
        model_dir = self.model_dir / model_id
        if model_dir.exists():
            shutil.rmtree(model_dir)
            return True
        return False

    def get_model_path(self, model_id: str) -> Optional[Path]:
        """
        Get model directory path.

        Args:
            model_id: Model identifier

        Returns:
            Path to model directory or None if not found
        """
        model_dir = self.model_dir / model_id
        return model_dir if model_dir.exists() else None

    def get_storage_stats(self) -> dict:
        """
        Get storage statistics.

        Returns:
            Dictionary with storage stats
        """
        total_size = 0
        model_count = 0

        for model_dir in self.model_dir.iterdir():
            if model_dir.is_dir():
                model_count += 1
                for file_path in model_dir.rglob("*"):
                    if file_path.is_file():
                        total_size += file_path.stat().st_size

        return {
            "total_size_bytes": total_size,
            "model_count": model_count,
            "storage_path": str(self.model_dir),
        }
