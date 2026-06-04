"""
Tests for TEMMS package archive safety.
"""

import io
import json
import tarfile
import hashlib
import os

import pytest

from temms.core.package_archive import (
    _compress_zstd,
    create_package_archive,
    extract_package_archive,
)
from temms.core.package_catalog import package_source_sha256
from temms.core.signing import sign_package, validate_package


class TestPackageArchiveSafety:
    """Validate archive extraction rejects unsafe tar member types."""

    def test_create_archive_is_deterministic(self, temp_dir):
        first_pkg = _minimal_package(temp_dir / "deterministic.temms", b"stable-model")
        second_pkg = _minimal_package(temp_dir / "copy" / "deterministic.temms", b"stable-model")
        os.utime(first_pkg / "models" / "model.onnx", (1_700_000_000, 1_700_000_000))
        os.utime(second_pkg / "models" / "model.onnx", (1_800_000_000, 1_800_000_000))

        first_archive = create_package_archive(first_pkg, temp_dir / "first.temms.tar.zst")
        second_archive = create_package_archive(second_pkg, temp_dir / "second.temms.tar.zst")

        assert first_archive.read_bytes() == second_archive.read_bytes()

    def test_create_archive_rejects_links(self, temp_dir):
        if not hasattr(os, "symlink"):
            pytest.skip("symlinks are not supported on this platform")
        pkg = _minimal_package(temp_dir / "linked.temms", b"linked-model")
        (pkg / "models" / "linked.onnx").symlink_to(pkg / "models" / "model.onnx")

        with pytest.raises(ValueError, match="regular files or directories"):
            create_package_archive(pkg, temp_dir / "linked.temms.tar.zst")

    def test_extract_rejects_special_file_members(self, temp_dir):
        tar_path = temp_dir / "malicious.temms.tar"
        archive_path = temp_dir / "malicious.temms.tar.zst"
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-malicious",
            "name": "pkg-malicious",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [],
            "policies": [],
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")

        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
            package_dir = tarfile.TarInfo("malicious.temms")
            package_dir.type = tarfile.DIRTYPE
            tar.addfile(package_dir)

            manifest_info = tarfile.TarInfo("malicious.temms/manifest.json")
            manifest_info.size = len(manifest_bytes)
            tar.addfile(manifest_info, io.BytesIO(manifest_bytes))

            fifo = tarfile.TarInfo("malicious.temms/models/fifo")
            fifo.type = tarfile.FIFOTYPE
            tar.addfile(fifo)

        _compress_zstd(tar_path, archive_path)

        with pytest.raises(ValueError, match="regular files or directories"):
            extract_package_archive(archive_path, temp_dir / "extracted")

    def test_validate_rejects_manifest_model_path_traversal(self, temp_dir):
        pkg = temp_dir / "unsafe-model.temms"
        (pkg / "models").mkdir(parents=True)
        (pkg / "policies").mkdir()
        outside_model = pkg / "outside.onnx"
        outside_model.write_bytes(b"outside-model")
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-unsafe-model",
            "name": "pkg-unsafe-model",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "unsafe-model",
                    "name": "unsafe-model",
                    "version": "1",
                    "format": "onnx",
                    "filename": "../outside.onnx",
                    "sha256": hashlib.sha256(outside_model.read_bytes()).hexdigest(),
                    "size_bytes": outside_model.stat().st_size,
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any("Unsafe model filename" in error for error in result.errors)
        assert any("path traversal" in error for error in result.errors)

    def test_validate_rejects_unsafe_package_id(self, temp_dir):
        pkg = temp_dir / "unsafe-package-id.temms"
        (pkg / "models").mkdir(parents=True)
        (pkg / "policies").mkdir()
        manifest = {
            "schema_version": "v1",
            "package_id": "../pkg-escape",
            "name": "pkg-escape",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any("Unsafe package_id" in error for error in result.errors)

    def test_validate_rejects_unsafe_model_id(self, temp_dir):
        pkg = temp_dir / "unsafe-model-id.temms"
        model_dir = pkg / "models"
        model_dir.mkdir(parents=True)
        (pkg / "policies").mkdir()
        model_file = model_dir / "model.onnx"
        model_file.write_bytes(b"model")
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-safe",
            "name": "pkg-safe",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "../model-escape",
                    "name": "model-escape",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
                    "size_bytes": model_file.stat().st_size,
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any("Unsafe model id" in error for error in result.errors)

    def test_validate_rejects_manifest_policy_paths(self, temp_dir):
        pkg = temp_dir / "unsafe-policy.temms"
        (pkg / "models").mkdir(parents=True)
        nested_policy = pkg / "policies" / "nested" / "policy.yaml"
        nested_policy.parent.mkdir(parents=True)
        nested_policy.write_text("policies: []")
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-unsafe-policy",
            "name": "pkg-unsafe-policy",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [],
            "policies": [
                {
                    "name": "unsafe-policy",
                    "filename": "nested/policy.yaml",
                    "slot": "vision",
                }
            ],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any("Unsafe policy filename" in error for error in result.errors)
        assert any("must be a file name" in error for error in result.errors)

    def test_validate_rejects_model_size_mismatch(self, temp_dir):
        pkg = temp_dir / "wrong-size.temms"
        model_dir = pkg / "models"
        model_dir.mkdir(parents=True)
        (pkg / "policies").mkdir()
        model_bytes = b"real-model-bytes"
        model_file = model_dir / "model.onnx"
        model_file.write_bytes(model_bytes)
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-wrong-size",
            "name": "pkg-wrong-size",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "wrong-size-model",
                    "name": "wrong-size-model",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes) + 1,
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any("Size mismatch for models/model.onnx" in error for error in result.errors)

    def test_validate_rejects_manifest_missing_required_fields(self, temp_dir):
        pkg = temp_dir / "missing-fields.temms"
        (pkg / "models").mkdir(parents=True)
        (pkg / "policies").mkdir()
        manifest = {
            "schema_version": "v1",
            "name": "missing-fields",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any("Invalid package manifest" in error for error in result.errors)
        assert any("package_id" in error for error in result.errors)

    def test_validate_rejects_unsupported_model_format(self, temp_dir):
        pkg = temp_dir / "bad-format.temms"
        model_dir = pkg / "models"
        model_dir.mkdir(parents=True)
        (pkg / "policies").mkdir()
        model_bytes = b"bad-format-model"
        model_file = model_dir / "model.bin"
        model_file.write_bytes(model_bytes)
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-bad-format",
            "name": "pkg-bad-format",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "bad-format-model",
                    "name": "bad-format-model",
                    "version": "1",
                    "format": "pickle",
                    "filename": "model.bin",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any("Unsupported model format" in error for error in result.errors)

    def test_validate_warns_for_incomplete_production_metadata(self, temp_dir):
        pkg = _minimal_package(temp_dir / "metadata-warnings.temms", b"metadata-warning-model")

        result = validate_package(pkg)

        assert result.valid is True
        assert any("Model metadata incomplete" in warning for warning in result.warnings)
        assert any("input_schema" in warning for warning in result.warnings)

    def test_strict_metadata_rejects_incomplete_production_metadata(self, temp_dir):
        pkg = _minimal_package(temp_dir / "strict-metadata.temms", b"strict-metadata-model")

        result = validate_package(pkg, strict_metadata=True)

        assert result.valid is False
        assert any("Model metadata incomplete" in error for error in result.errors)
        assert any("runtime_constraints" in error for error in result.errors)
        assert any("benchmark" in error for error in result.errors)

    def test_validate_rejects_duplicate_model_ids_and_files(self, temp_dir):
        pkg = temp_dir / "duplicate-models.temms"
        model_dir = pkg / "models"
        model_dir.mkdir(parents=True)
        (pkg / "policies").mkdir()
        model_bytes = b"duplicate-model"
        model_file = model_dir / "model.onnx"
        model_file.write_bytes(model_bytes)
        model_entry = {
            "id": "duplicate-model",
            "name": "duplicate-model",
            "version": "1",
            "format": "onnx",
            "filename": "model.onnx",
            "sha256": hashlib.sha256(model_bytes).hexdigest(),
            "size_bytes": len(model_bytes),
        }
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-duplicate-models",
            "name": "pkg-duplicate-models",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [model_entry, dict(model_entry)],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any("Duplicate model id" in error for error in result.errors)
        assert any("Duplicate model filename" in error for error in result.errors)

    def test_validate_rejects_duplicate_policy_names_and_files(self, temp_dir):
        pkg = temp_dir / "duplicate-policies.temms"
        (pkg / "models").mkdir(parents=True)
        policies_dir = pkg / "policies"
        policies_dir.mkdir()
        (policies_dir / "policy.yaml").write_text("policies: []")
        policy_entry = {
            "name": "duplicate-policy",
            "filename": "policy.yaml",
            "slot": "vision",
        }
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-duplicate-policies",
            "name": "pkg-duplicate-policies",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [],
            "policies": [policy_entry, dict(policy_entry)],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any("Duplicate policy name" in error for error in result.errors)
        assert any("Duplicate policy filename" in error for error in result.errors)

    def test_validate_rejects_undeclared_model_files(self, temp_dir):
        pkg = temp_dir / "undeclared-model.temms"
        model_dir = pkg / "models"
        model_dir.mkdir(parents=True)
        (pkg / "policies").mkdir()
        model_bytes = b"declared-model"
        model_file = model_dir / "model.onnx"
        model_file.write_bytes(model_bytes)
        (model_dir / "extra.onnx").write_bytes(b"hidden-model")
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-undeclared-model",
            "name": "pkg-undeclared-model",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [
                {
                    "id": "declared-model",
                    "name": "declared-model",
                    "version": "1",
                    "format": "onnx",
                    "filename": "model.onnx",
                    "sha256": hashlib.sha256(model_bytes).hexdigest(),
                    "size_bytes": len(model_bytes),
                }
            ],
            "policies": [],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any(
            "Undeclared model file in package: models/extra.onnx" in error
            for error in result.errors
        )

    def test_validate_rejects_undeclared_policy_files(self, temp_dir):
        pkg = temp_dir / "undeclared-policy.temms"
        (pkg / "models").mkdir(parents=True)
        policies_dir = pkg / "policies"
        nested_dir = policies_dir / "nested"
        nested_dir.mkdir(parents=True)
        (policies_dir / "policy.yaml").write_text("policies: []")
        (nested_dir / "extra.yaml").write_text("policies: []")
        manifest = {
            "schema_version": "v1",
            "package_id": "pkg-undeclared-policy",
            "name": "pkg-undeclared-policy",
            "version": "1",
            "created_at": "2024-01-01T00:00:00Z",
            "models": [],
            "policies": [
                {
                    "name": "declared-policy",
                    "filename": "policy.yaml",
                    "slot": "vision",
                }
            ],
        }
        (pkg / "manifest.json").write_text(json.dumps(manifest))

        result = validate_package(pkg)

        assert result.valid is False
        assert any(
            "Undeclared policy file in package: policies/nested/extra.yaml" in error
            for error in result.errors
        )

    def test_validate_rejects_directory_package_links(self, temp_dir):
        if not hasattr(os, "symlink"):
            pytest.skip("symlinks are not supported on this platform")
        pkg = _minimal_package(temp_dir / "linked-dir.temms", b"linked-dir-model")
        (pkg / "docs").mkdir()
        (pkg / "docs" / "manifest-link.json").symlink_to(pkg / "manifest.json")

        result = validate_package(pkg)

        assert result.valid is False
        assert any(
            "Package links are not allowed: docs/manifest-link.json" in error
            for error in result.errors
        )

    def test_signing_rejects_directory_package_links(self, temp_dir):
        if not hasattr(os, "symlink"):
            pytest.skip("symlinks are not supported on this platform")
        pkg = _minimal_package(temp_dir / "linked-sign.temms", b"linked-sign-model")
        (pkg / "models" / "model-link.onnx").symlink_to(pkg / "models" / "model.onnx")

        with pytest.raises(ValueError, match="Package links are not allowed"):
            sign_package(pkg, "secret")

    def test_directory_source_sha_rejects_links(self, temp_dir):
        if not hasattr(os, "symlink"):
            pytest.skip("symlinks are not supported on this platform")
        pkg = _minimal_package(temp_dir / "linked-sha.temms", b"linked-sha-model")
        (pkg / "models" / "model-link.onnx").symlink_to(pkg / "models" / "model.onnx")

        with pytest.raises(ValueError, match="Package links are not allowed"):
            package_source_sha256(pkg)


def _minimal_package(package_dir, model_bytes: bytes):
    model_dir = package_dir / "models"
    model_dir.mkdir(parents=True)
    (package_dir / "policies").mkdir()
    model_file = model_dir / "model.onnx"
    model_file.write_bytes(model_bytes)
    manifest = {
        "schema_version": "v1",
        "package_id": "pkg-deterministic",
        "name": "pkg-deterministic",
        "version": "1",
        "created_at": "2024-01-01T00:00:00Z",
        "models": [
            {
                "id": "model-deterministic",
                "name": "model-deterministic",
                "version": "1",
                "format": "onnx",
                "filename": "model.onnx",
                "sha256": hashlib.sha256(model_bytes).hexdigest(),
                "size_bytes": len(model_bytes),
            }
        ],
        "policies": [],
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return package_dir
