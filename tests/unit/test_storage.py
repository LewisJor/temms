"""
Tests for local model storage.
"""

import hashlib
from pathlib import Path

import pytest

from temms.core.storage import ModelStorage


def test_store_model_replaces_existing_model_atomically(temp_dir):
    storage = ModelStorage(temp_dir / "models")
    source = temp_dir / "model.onnx"

    source.write_bytes(b"model-v1")
    dest_path, sha256, size = storage.store_model(source, "model-1")

    assert dest_path.read_bytes() == b"model-v1"
    assert sha256 == hashlib.sha256(b"model-v1").hexdigest()
    assert size == len(b"model-v1")

    source.write_bytes(b"model-v2")
    replaced_path, replaced_sha256, replaced_size = storage.store_model(source, "model-1")

    assert replaced_path == dest_path
    assert dest_path.read_bytes() == b"model-v2"
    assert replaced_sha256 == hashlib.sha256(b"model-v2").hexdigest()
    assert replaced_size == len(b"model-v2")


def test_store_model_preserves_existing_model_when_replace_fails(temp_dir, monkeypatch):
    storage = ModelStorage(temp_dir / "models")
    source = temp_dir / "model.onnx"

    source.write_bytes(b"known-good-model")
    dest_path, _, _ = storage.store_model(source, "model-1")

    source.write_bytes(b"replacement-model")
    original_replace = Path.replace

    def fail_temp_model_replace(self, target, *args, **kwargs):
        if self.parent == dest_path.parent and self.name.startswith(f".{dest_path.name}-"):
            raise OSError("simulated model replace failure")
        return original_replace(self, target, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", fail_temp_model_replace)

    with pytest.raises(OSError, match="simulated model replace failure"):
        storage.store_model(source, "model-1")

    assert dest_path.read_bytes() == b"known-good-model"
    assert list(dest_path.parent.glob(f".{dest_path.name}-*")) == []


def test_store_model_rejects_unsafe_model_id(temp_dir):
    storage = ModelStorage(temp_dir / "models")
    source = temp_dir / "model.onnx"
    source.write_bytes(b"model")

    with pytest.raises(ValueError, match="Unsafe model_id"):
        storage.store_model(source, "../escape")

    assert not (temp_dir / "escape").exists()
