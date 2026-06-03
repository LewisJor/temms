"""
Integration tests with real ONNX models.

Tests the full pipeline using generated ONNX models that ONNX Runtime
can actually load and run inference on.

Requires: onnx, onnxruntime, pillow
"""

import asyncio
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

# Skip all tests in this module if onnxruntime is not installed
onnxruntime = pytest.importorskip("onnxruntime", reason="onnxruntime required")
onnx_pkg = pytest.importorskip("onnx", reason="onnx package required")

from temms.core.cache import ModelCache, ModelFormat
from temms.core.storage import ModelStorage
from temms.core.loader import ONNXRuntime, ModelLoader, RuntimeType
from temms.core.package import PackageImporter
from temms.core.package_catalog import package_source_sha256
from temms.core.signing import sign_package, validate_package
from temms.inference.runtime import InferenceRuntime
from temms.slots.manager import SlotManager, SlotState
from temms.conditions.store import ConditionStore
from temms.policy.engine import PolicyEngine
from temms.policy.schema import SlotPolicy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_onnx_model(tmp_path):
    """Generate a small real ONNX model for testing."""
    from onnx import helper, TensorProto, numpy_helper, checker

    rng = np.random.RandomState(42)

    # Conv weights (8 filters, 3 channels, 3x3 kernel)
    conv_w = rng.randn(8, 3, 3, 3).astype(np.float32) * 0.1
    conv_b = np.zeros(8, dtype=np.float32)

    # FC weights (10 classes, 8 features)
    fc_w = rng.randn(10, 8).astype(np.float32) * 0.1
    fc_b = np.zeros(10, dtype=np.float32)

    conv_w_init = numpy_helper.from_array(conv_w, name="conv_w")
    conv_b_init = numpy_helper.from_array(conv_b, name="conv_b")
    fc_w_init = numpy_helper.from_array(fc_w, name="fc_w")
    fc_b_init = numpy_helper.from_array(fc_b, name="fc_b")

    graph = helper.make_graph(
        nodes=[
            helper.make_node(
                "Conv",
                ["input", "conv_w", "conv_b"],
                ["conv_out"],
                kernel_shape=[3, 3],
                pads=[1, 1, 1, 1],
            ),
            helper.make_node("Relu", ["conv_out"], ["relu_out"]),
            helper.make_node("GlobalAveragePool", ["relu_out"], ["gap_out"]),
            helper.make_node("Flatten", ["gap_out"], ["flat_out"], axis=1),
            helper.make_node("Gemm", ["flat_out", "fc_w", "fc_b"], ["output"], transB=1),
        ],
        name="test_model",
        inputs=[helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 32, 32])],
        outputs=[helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 10])],
        initializer=[conv_w_init, conv_b_init, fc_w_init, fc_b_init],
    )

    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    checker.check_model(model)

    model_path = tmp_path / "test_model.onnx"
    with open(model_path, "wb") as f:
        f.write(model.SerializeToString())

    return model_path


@pytest.fixture
def two_real_models(tmp_path):
    """Generate two different real ONNX models for hot-swap testing."""
    from onnx import helper, TensorProto, numpy_helper, checker

    models = {}
    for name, seed in [("model_a", 42), ("model_b", 123)]:
        rng = np.random.RandomState(seed)

        conv_w = rng.randn(8, 3, 3, 3).astype(np.float32) * 0.1
        conv_b = np.zeros(8, dtype=np.float32)
        fc_w = rng.randn(10, 8).astype(np.float32) * 0.1
        fc_b = np.zeros(10, dtype=np.float32)

        graph = helper.make_graph(
            nodes=[
                helper.make_node(
                    "Conv",
                    ["input", "conv_w", "conv_b"],
                    ["conv_out"],
                    kernel_shape=[3, 3],
                    pads=[1, 1, 1, 1],
                ),
                helper.make_node("Relu", ["conv_out"], ["relu_out"]),
                helper.make_node("GlobalAveragePool", ["relu_out"], ["gap_out"]),
                helper.make_node("Flatten", ["gap_out"], ["flat_out"], axis=1),
                helper.make_node("Gemm", ["flat_out", "fc_w", "fc_b"], ["output"], transB=1),
            ],
            name=name,
            inputs=[helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 32, 32])],
            outputs=[helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 10])],
            initializer=[
                numpy_helper.from_array(conv_w, "conv_w"),
                numpy_helper.from_array(conv_b, "conv_b"),
                numpy_helper.from_array(fc_w, "fc_w"),
                numpy_helper.from_array(fc_b, "fc_b"),
            ],
        )

        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 8
        checker.check_model(model)

        model_path = tmp_path / f"{name}.onnx"
        with open(model_path, "wb") as f:
            f.write(model.SerializeToString())
        models[name] = model_path

    return models


@pytest.fixture
def real_package(tmp_path):
    """Create a complete TEMMS package with real ONNX models."""
    import hashlib
    from onnx import helper, TensorProto, numpy_helper, checker

    pkg_dir = tmp_path / "test-package"
    models_dir = pkg_dir / "models"
    policies_dir = pkg_dir / "policies"
    models_dir.mkdir(parents=True)
    policies_dir.mkdir(parents=True)

    model_entries = []
    for name, model_id, seed in [
        ("model-alpha", "model-alpha-001", 42),
        ("model-beta", "model-beta-001", 123),
    ]:
        rng = np.random.RandomState(seed)

        conv_w = rng.randn(8, 3, 3, 3).astype(np.float32) * 0.1
        conv_b = np.zeros(8, dtype=np.float32)
        fc_w = rng.randn(10, 8).astype(np.float32) * 0.1
        fc_b = np.zeros(10, dtype=np.float32)

        graph = helper.make_graph(
            nodes=[
                helper.make_node(
                    "Conv",
                    ["input", "conv_w", "conv_b"],
                    ["conv_out"],
                    kernel_shape=[3, 3],
                    pads=[1, 1, 1, 1],
                ),
                helper.make_node("Relu", ["conv_out"], ["relu_out"]),
                helper.make_node("GlobalAveragePool", ["relu_out"], ["gap_out"]),
                helper.make_node("Flatten", ["gap_out"], ["flat_out"], axis=1),
                helper.make_node("Gemm", ["flat_out", "fc_w", "fc_b"], ["output"], transB=1),
            ],
            name=name,
            inputs=[helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 32, 32])],
            outputs=[helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 10])],
            initializer=[
                numpy_helper.from_array(conv_w, "conv_w"),
                numpy_helper.from_array(conv_b, "conv_b"),
                numpy_helper.from_array(fc_w, "fc_w"),
                numpy_helper.from_array(fc_b, "fc_b"),
            ],
        )

        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 8
        checker.check_model(model)

        filename = f"{name}.onnx"
        model_bytes = model.SerializeToString()
        model_path = models_dir / filename
        model_path.write_bytes(model_bytes)

        sha256 = hashlib.sha256(model_bytes).hexdigest()

        model_entries.append(
            {
                "id": model_id,
                "name": name,
                "version": "1.0.0",
                "format": "onnx",
                "filename": filename,
                "sha256": sha256,
                "size_bytes": len(model_bytes),
                "metadata": {
                    "input_shape": [1, 3, 32, 32],
                    "classes": 10,
                },
            }
        )

    # Write policy
    policy_yaml = """apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: test-adaptive
  description: Test policy for integration tests
spec:
  slot: vision
  default_model: model-alpha
  rules:
    - name: switch-on-temp
      priority: 80
      conditions:
        all:
          - metric: platform.compute.cpu_temp_c
            operator: gte
            value: 75
      action:
        switch_to: model-beta
  fallback_chain:
    - model-alpha
    - model-beta
"""
    (policies_dir / "test-adaptive.yaml").write_text(policy_yaml)

    # Write manifest
    manifest = {
        "schema_version": "v1",
        "package_id": "pkg-test-integration",
        "name": "test-integration-package",
        "version": "1.0.0",
        "description": "Integration test package with real ONNX models",
        "created_at": "2024-01-15T10:00:00Z",
        "created_by": "test",
        "models": model_entries,
        "policies": [
            {"name": "test-adaptive", "filename": "test-adaptive.yaml", "slot": "vision"},
        ],
    }
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    return pkg_dir


@pytest.fixture
def system_with_storage(tmp_path):
    """Create full TEMMS system with real storage paths."""
    db_path = tmp_path / "temms.db"
    model_dir = tmp_path / "models"
    cache_dir = tmp_path / "cache"
    model_dir.mkdir()
    cache_dir.mkdir()

    model_cache = ModelCache(db_path)
    model_storage = ModelStorage(model_dir)
    slot_manager = SlotManager(db_path)
    condition_store = ConditionStore(db_path)
    policy_engine = PolicyEngine(condition_store)
    inference_runtime = InferenceRuntime(model_cache, model_storage)

    return {
        "db_path": db_path,
        "model_dir": model_dir,
        "cache_dir": cache_dir,
        "model_cache": model_cache,
        "model_storage": model_storage,
        "slot_manager": slot_manager,
        "condition_store": condition_store,
        "policy_engine": policy_engine,
        "inference_runtime": inference_runtime,
    }


# ---------------------------------------------------------------------------
# Tests: Direct ONNX Loading
# ---------------------------------------------------------------------------


class TestRealONNXLoading:
    """Test loading real ONNX models directly."""

    def test_load_real_onnx_model(self, real_onnx_model):
        """Test ONNXRuntime can load a real model."""
        runtime = ONNXRuntime()
        session = runtime.load(real_onnx_model)

        assert session is not None
        inputs = session.get_inputs()
        assert len(inputs) == 1
        assert inputs[0].name == "input"
        assert inputs[0].shape == [1, 3, 32, 32]

        runtime.unload()

    def test_infer_with_real_model(self, real_onnx_model):
        """Test running inference with a real ONNX model."""
        runtime = ONNXRuntime()
        runtime.load(real_onnx_model)

        # Create dummy input matching expected shape
        dummy_input = np.random.randn(1, 3, 32, 32).astype(np.float32)
        outputs = runtime.infer({"input": dummy_input})

        assert isinstance(outputs, list)
        assert len(outputs) == 1
        assert outputs[0].shape == (1, 10)

        runtime.unload()

    def test_model_loader_with_real_model(self, real_onnx_model):
        """Test ModelLoader loads real ONNX model."""
        loader = ModelLoader()
        runtime = loader.load_model(real_onnx_model, RuntimeType.ONNX)

        dummy_input = np.random.randn(1, 3, 32, 32).astype(np.float32)
        outputs = runtime.infer({"input": dummy_input})

        assert outputs[0].shape == (1, 10)
        loader.unload_current()

    def test_different_models_produce_different_outputs(self, two_real_models):
        """Test that models with different weights produce different outputs."""
        dummy_input = np.random.randn(1, 3, 32, 32).astype(np.float32)

        runtime_a = ONNXRuntime()
        runtime_a.load(two_real_models["model_a"])
        output_a = runtime_a.infer({"input": dummy_input})

        runtime_b = ONNXRuntime()
        runtime_b.load(two_real_models["model_b"])
        output_b = runtime_b.infer({"input": dummy_input})

        # Different weights should produce different outputs
        assert not np.allclose(output_a[0], output_b[0])

        runtime_a.unload()
        runtime_b.unload()


# ---------------------------------------------------------------------------
# Tests: InferenceRuntime with Real Models
# ---------------------------------------------------------------------------


class TestRealInferenceRuntime:
    """Test InferenceRuntime with real ONNX models."""

    def _setup_model_in_system(self, system, model_path, model_id, model_name):
        """Helper to add a model to the cache and storage."""
        # Use store_model to copy file into storage directory
        dest_path, sha256, size_bytes = system["model_storage"].store_model(
            source_path=model_path,
            model_id=model_id,
            verify=True,
        )

        # Add to cache database (path is the directory containing the model)
        cached = system["model_cache"].add_cached_model(
            model_id=model_id,
            name=model_name,
            version="1.0.0",
            format=ModelFormat.ONNX,
            path=dest_path.parent,
            sha256=sha256,
            size_bytes=size_bytes,
            package_id="test-pkg",
            metadata={"input_shape": [1, 3, 32, 32], "classes": 10},
        )
        return cached

    @pytest.mark.asyncio
    async def test_load_real_model_into_slot(self, system_with_storage, real_onnx_model):
        """Test loading a real model into a slot via InferenceRuntime."""
        system = system_with_storage

        # Set up model
        self._setup_model_in_system(system, real_onnx_model, "test-model-001", "test-model")

        # Create slot
        system["slot_manager"].create_slot(name="vision", description="Vision", required=True)

        # Load model into slot
        result = await system["inference_runtime"].load_model("vision", "test-model-001")
        assert result is True

    @pytest.mark.asyncio
    async def test_real_inference_through_runtime(self, system_with_storage, real_onnx_model):
        """Test full inference through InferenceRuntime with real model."""
        system = system_with_storage

        self._setup_model_in_system(system, real_onnx_model, "test-model-001", "test-model")

        system["slot_manager"].create_slot(name="vision", description="Vision", required=True)

        await system["inference_runtime"].load_model("vision", "test-model-001")

        # Create dummy input as raw bytes (non-image content type)
        dummy_input = np.random.randn(1, 3, 32, 32).astype(np.float32)
        input_bytes = dummy_input.tobytes()

        predictions = await system["inference_runtime"].infer(
            slot_name="vision",
            model_id="test-model-001",
            input_data=input_bytes,
            content_type="application/octet-stream",
        )

        assert isinstance(predictions, list)
        assert len(predictions) == 1
        # Output is [[10 floats]] (batch dim + classes)
        assert len(predictions[0][0]) == 10

    @pytest.mark.asyncio
    async def test_hot_swap_real_models(self, system_with_storage, two_real_models):
        """Test hot-swapping between two real models."""
        system = system_with_storage

        self._setup_model_in_system(system, two_real_models["model_a"], "model-a-001", "model-a")
        self._setup_model_in_system(system, two_real_models["model_b"], "model-b-001", "model-b")

        system["slot_manager"].create_slot(name="vision", description="Vision", required=True)

        # Load model A
        await system["inference_runtime"].load_model("vision", "model-a-001")

        # Run inference with model A
        dummy_input = np.random.randn(1, 3, 32, 32).astype(np.float32)
        input_bytes = dummy_input.tobytes()

        output_a = await system["inference_runtime"].infer(
            "vision", "model-a-001", input_bytes, "application/octet-stream"
        )

        # Hot-swap to model B
        await system["inference_runtime"].load_model("vision", "model-b-001")

        output_b = await system["inference_runtime"].infer(
            "vision", "model-b-001", input_bytes, "application/octet-stream"
        )

        # Should have same shape but different values
        assert len(output_a[0][0]) == len(output_b[0][0]) == 10
        assert output_a[0][0] != output_b[0][0]


# ---------------------------------------------------------------------------
# Tests: Full Pipeline (Import → Policy → Switch → Infer)
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Test the complete pipeline from import to inference."""

    @pytest.mark.asyncio
    async def test_import_to_inference(self, system_with_storage, real_package):
        """Test importing a package and running inference on imported models."""
        system = system_with_storage

        # 1. Import package
        importer = PackageImporter(
            system["cache_dir"],
            system["model_cache"],
            system["model_storage"],
            require_signature=False,
        )
        result = importer.import_package(real_package, verify=True)

        assert len(result.models) == 2
        assert result.models[0].name == "model-alpha"
        assert result.models[1].name == "model-beta"

        # 2. Create slot
        system["slot_manager"].create_slot(
            name="vision",
            description="Vision",
            required=True,
            default_model="model-alpha",
            candidates=["model-alpha", "model-beta"],
        )

        # 3. Load default model
        await system["inference_runtime"].load_model("vision", "model-alpha-001")

        # 4. Run inference
        dummy_input = np.random.randn(1, 3, 32, 32).astype(np.float32)
        input_bytes = dummy_input.tobytes()

        predictions = await system["inference_runtime"].infer(
            "vision", "model-alpha-001", input_bytes, "application/octet-stream"
        )

        assert isinstance(predictions, list)
        assert len(predictions) == 1
        # Output is [[10 floats]] (batch dim + classes)
        assert len(predictions[0][0]) == 10

    @pytest.mark.asyncio
    async def test_import_is_idempotent_and_activates_policies(
        self, system_with_storage, real_package, tmp_path
    ):
        """Test repeated imports update cache records and promote policies."""
        system = system_with_storage
        active_policy_dir = tmp_path / "active-policies"

        importer = PackageImporter(
            system["cache_dir"],
            system["model_cache"],
            system["model_storage"],
            active_policy_dir=active_policy_dir,
            require_signature=False,
        )

        first = importer.import_package(real_package, verify=True)
        second = importer.import_package(real_package, verify=True)

        assert len(first.models) == 2
        assert len(second.models) == 2
        assert len(system["model_cache"].list_models()) == 2
        assert len(system["model_cache"].list_packages()) == 1
        audit = second.package.manifest["_temms_import"]
        assert audit["source_type"] == "directory"
        assert audit["source_sha256"] == package_source_sha256(real_package)
        assert audit["directory_sha256"] == audit["source_sha256"]
        assert audit["archive_sha256"] is None
        active_policy_path = active_policy_dir / "pkg-test-integration-test-adaptive.yaml"
        assert active_policy_path.exists()

        manifest_path = real_package / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["policies"] = []
        manifest_path.write_text(json.dumps(manifest, indent=2))
        (real_package / "policies" / "test-adaptive.yaml").unlink()

        third = importer.import_package(real_package, verify=True)

        assert third.policies == []
        assert not active_policy_path.exists()
        assert list((system["cache_dir"] / "policies" / "pkg-test-integration").iterdir()) == []

    @pytest.mark.asyncio
    async def test_failed_policy_promotion_preserves_active_policy(
        self, system_with_storage, real_package, tmp_path, monkeypatch
    ):
        """Test active package policies survive a failed re-import promotion."""
        system = system_with_storage
        active_policy_dir = tmp_path / "active-policies"

        importer = PackageImporter(
            system["cache_dir"],
            system["model_cache"],
            system["model_storage"],
            active_policy_dir=active_policy_dir,
            require_signature=False,
        )

        importer.import_package(real_package, verify=True)
        active_policy_path = active_policy_dir / "pkg-test-integration-test-adaptive.yaml"
        cached_policy_path = (
            system["cache_dir"] / "policies" / "pkg-test-integration" / "test-adaptive.yaml"
        )
        active_policy_before = active_policy_path.read_text()
        cached_policy_before = cached_policy_path.read_text()

        source_policy_path = real_package / "policies" / "test-adaptive.yaml"
        source_policy_path.write_text(active_policy_before + "\n# updated policy\n")

        import temms.core.package as package_module

        original_copy = package_module.shutil.copy

        def fail_active_policy_copy(src, dst, *args, **kwargs):
            destination = Path(dst)
            if destination.parent == active_policy_dir and destination.name.startswith(
                ".pkg-test-integration-test-adaptive.yaml-"
            ):
                raise OSError("simulated active policy copy failure")
            return original_copy(src, dst, *args, **kwargs)

        monkeypatch.setattr(package_module.shutil, "copy", fail_active_policy_copy)

        with pytest.raises(OSError, match="simulated active policy copy failure"):
            importer.import_package(real_package, verify=True)

        assert active_policy_path.read_text() == active_policy_before
        assert cached_policy_path.read_text() == cached_policy_before
        assert list(active_policy_dir.iterdir()) == [active_policy_path]

    @pytest.mark.asyncio
    async def test_failed_active_policy_replace_restores_cached_policy(
        self, system_with_storage, real_package, tmp_path, monkeypatch
    ):
        """Test cached and active policies survive a late active replace failure."""
        system = system_with_storage
        active_policy_dir = tmp_path / "active-policies"

        importer = PackageImporter(
            system["cache_dir"],
            system["model_cache"],
            system["model_storage"],
            active_policy_dir=active_policy_dir,
            require_signature=False,
        )

        importer.import_package(real_package, verify=True)
        active_policy_path = active_policy_dir / "pkg-test-integration-test-adaptive.yaml"
        cached_policy_path = (
            system["cache_dir"] / "policies" / "pkg-test-integration" / "test-adaptive.yaml"
        )
        active_policy_before = active_policy_path.read_text()
        cached_policy_before = cached_policy_path.read_text()

        source_policy_path = real_package / "policies" / "test-adaptive.yaml"
        source_policy_path.write_text(active_policy_before + "\n# replacement policy\n")

        import temms.core.package as package_module

        original_replace = package_module.Path.replace

        def fail_active_policy_replace(self, target, *args, **kwargs):
            if self.parent == active_policy_dir and self.name.startswith(
                ".pkg-test-integration-test-adaptive.yaml-"
            ):
                raise OSError("simulated active policy replace failure")
            return original_replace(self, target, *args, **kwargs)

        monkeypatch.setattr(package_module.Path, "replace", fail_active_policy_replace)

        with pytest.raises(OSError, match="simulated active policy replace failure"):
            importer.import_package(real_package, verify=True)

        assert active_policy_path.read_text() == active_policy_before
        assert cached_policy_path.read_text() == cached_policy_before
        assert list(active_policy_dir.iterdir()) == [active_policy_path]

    @pytest.mark.asyncio
    async def test_signed_package_validation_and_required_import(
        self, system_with_storage, real_package
    ):
        """Test package signatures can be created, verified, and required."""
        system = system_with_storage
        key = "test-signing-key"

        sign_package(real_package, key, signer="test-hub")
        validation = validate_package(real_package, require_signature=True, signing_key=key)

        assert validation.valid is True
        assert validation.signature_verified is True

        importer = PackageImporter(
            system["cache_dir"],
            system["model_cache"],
            system["model_storage"],
            require_signature=True,
            signing_key=key,
        )
        result = importer.import_package(real_package, verify=True)

        assert len(result.models) == 2

    @pytest.mark.asyncio
    async def test_policy_driven_model_switch(self, system_with_storage, real_package):
        """Test complete policy-driven model switching with real models."""
        system = system_with_storage

        # 1. Import package (includes policy)
        importer = PackageImporter(
            system["cache_dir"],
            system["model_cache"],
            system["model_storage"],
            require_signature=False,
        )
        result = importer.import_package(real_package, verify=True)

        # 2. Load policy
        policy_path = (
            system["cache_dir"] / "policies" / "pkg-test-integration" / "test-adaptive.yaml"
        )
        system["policy_engine"].load_policy_from_file(policy_path)

        # 3. Create slot and load default model
        system["slot_manager"].create_slot(
            name="vision",
            description="Vision",
            required=True,
            default_model="model-alpha",
            candidates=["model-alpha", "model-beta"],
        )
        await system["inference_runtime"].load_model("vision", "model-alpha-001")

        # 4. Evaluate policy with normal conditions (no rules match)
        system["condition_store"].set(
            path="platform.compute.cpu_temp_c",
            value=50.0,
            source="test",
            priority=100,
        )
        eval_result = system["policy_engine"].evaluate_slot("vision")
        # Should return default model since no rules matched
        assert eval_result.switch_to == "model-alpha"
        assert eval_result.is_default is True

        # 5. Set high temperature condition -> should trigger switch to model-beta
        system["condition_store"].set(
            path="platform.compute.cpu_temp_c",
            value=80.0,
            source="test",
            priority=100,
        )
        eval_result = system["policy_engine"].evaluate_slot("vision")

        assert eval_result.switch_to == "model-beta"
        assert eval_result.is_default is False
        assert "switch-on-temp" in eval_result.triggered_by

        # 6. Execute the switch
        await system["inference_runtime"].load_model("vision", "model-beta-001")
        system["slot_manager"].activate_model(
            slot_name="vision",
            model_id="model-beta-001",
            trigger_type="policy",
            trigger_detail=eval_result.triggered_by,
            conditions=system["condition_store"].get_snapshot(),
        )

        # 7. Run inference on new model
        dummy_input = np.random.randn(1, 3, 32, 32).astype(np.float32)
        predictions = await system["inference_runtime"].infer(
            "vision", "model-beta-001", dummy_input.tobytes(), "application/octet-stream"
        )

        assert len(predictions[0][0]) == 10

        # 8. Verify decision was logged
        decisions = system["slot_manager"].get_decision_log("vision", limit=1)
        assert len(decisions) == 1
        assert decisions[0]["to_model"] == "model-beta-001"
        assert decisions[0]["trigger_type"] == "policy"
