"""Registry-agnostic model source resolution (#43 slice 1)."""

from __future__ import annotations

import hashlib

import pytest

from temms.core.model_resolver import (
    FileModelResolver,
    MLflowModelResolver,
    ModelResolutionError,
    resolve_model_source,
)


@pytest.fixture
def artifact(tmp_path):
    path = tmp_path / "model.onnx"
    path.write_bytes(b"onnx-bytes")
    return path


def test_file_resolver_bare_path(artifact):
    resolved = FileModelResolver().resolve(str(artifact))
    assert resolved.artifact_path == artifact
    assert resolved.sha256 == hashlib.sha256(b"onnx-bytes").hexdigest()
    assert resolved.provenance["registry"] == "file"


def test_file_resolver_relative_to_base_dir(artifact):
    resolved = FileModelResolver(base_dir=artifact.parent).resolve("file://model.onnx")
    assert resolved.artifact_path == artifact


def test_file_resolver_missing_artifact_is_clear(tmp_path):
    with pytest.raises(ModelResolutionError, match="not found"):
        FileModelResolver(base_dir=tmp_path).resolve("file://absent.onnx")


def test_dispatch_selects_file_resolver(artifact):
    resolved = resolve_model_source(str(artifact))
    assert resolved.provenance["registry"] == "file"


def test_dispatch_rejects_unknown_scheme():
    with pytest.raises(ModelResolutionError, match="unsupported model source scheme"):
        resolve_model_source("s3://bucket/model.onnx")


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("mlflow://models/yolov8/3", ("yolov8", "3", None)),
        ("mlflow://models/yolov8", ("yolov8", None, None)),
        ("mlflow://models/yolov8@prod", ("yolov8", None, "prod")),
    ],
)
def test_mlflow_uri_parsing(uri, expected):
    assert MLflowModelResolver._parse(uri) == expected


def test_mlflow_uri_requires_models_prefix():
    with pytest.raises(ModelResolutionError, match="expected mlflow://models"):
        MLflowModelResolver._parse("mlflow://runs/abc/model")


def test_mlflow_uri_requires_a_name():
    with pytest.raises(ModelResolutionError, match="missing a model name"):
        MLflowModelResolver._parse("mlflow://models")
