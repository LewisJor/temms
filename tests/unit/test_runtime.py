"""
Unit tests for the inference runtime.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
import numpy as np
from pathlib import Path
from types import SimpleNamespace

from temms.inference.runtime import (
    InferenceRuntime,
    LoadedModel,
    SimulatedModelRuntime,
    SlotRuntime,
)
from temms.core.cache import CachedModel, ModelFormat
from temms.core.loader import ModelLoader, RuntimeType
from temms.core.runtime_profiles import (
    RuntimeCapabilities,
    _infer_device_profile,
    detect_runtime_capabilities,
    known_device_profiles,
    normalize_device_profile,
    package_runtime_constraints,
    runtime_constraints_satisfied,
    runtime_defaults_for_profile,
)


class TestInferenceRuntime:
    """Tests for InferenceRuntime class."""

    def test_init(self, model_cache, model_storage):
        """Test runtime initialization."""
        runtime = InferenceRuntime(model_cache, model_storage)

        assert runtime.model_cache == model_cache
        assert runtime.model_storage == model_storage
        assert runtime._slots == {}

    def test_get_slot_runtime_creates_new(self, model_cache, model_storage):
        """Test that _get_slot_runtime creates new slot runtime."""
        runtime = InferenceRuntime(model_cache, model_storage)

        slot_runtime = runtime._get_slot_runtime("test-slot")

        assert slot_runtime is not None
        assert slot_runtime.slot_name == "test-slot"
        assert slot_runtime.loaded_model is None
        assert "test-slot" in runtime._slots

    def test_get_slot_runtime_returns_existing(self, model_cache, model_storage):
        """Test that _get_slot_runtime returns existing slot runtime."""
        runtime = InferenceRuntime(model_cache, model_storage)

        slot_runtime1 = runtime._get_slot_runtime("test-slot")
        slot_runtime2 = runtime._get_slot_runtime("test-slot")

        assert slot_runtime1 is slot_runtime2

    def test_format_to_runtime_type(self, model_cache, model_storage):
        """Test format to runtime type conversion."""
        from temms.core.loader import RuntimeType

        runtime = InferenceRuntime(model_cache, model_storage)

        assert runtime._format_to_runtime_type(ModelFormat.ONNX) == RuntimeType.ONNX
        assert runtime._format_to_runtime_type(ModelFormat.TFLITE) == RuntimeType.TFLITE
        assert runtime._format_to_runtime_type(ModelFormat.TORCHSCRIPT) == RuntimeType.TORCHSCRIPT
        assert runtime._format_to_runtime_type(ModelFormat.TENSORRT) == RuntimeType.TENSORRT

    def test_find_model_file_supports_tensorrt_engine(
        self,
        model_cache,
        model_storage,
        temp_dir,
    ):
        """Test serialized TensorRT engine selection."""
        runtime = InferenceRuntime(model_cache, model_storage)
        engine_path = temp_dir / "detector.engine"
        plan_path = temp_dir / "detector.plan"
        plan_path.write_bytes(b"plan")
        engine_path.write_bytes(b"engine")

        assert runtime._find_model_file(temp_dir, ModelFormat.TENSORRT) == engine_path

    def test_get_slot_info_no_model(self, model_cache, model_storage):
        """Test get_slot_info with no model loaded."""
        runtime = InferenceRuntime(model_cache, model_storage)
        runtime._get_slot_runtime("test-slot")

        info = runtime.get_slot_info("test-slot")

        assert info["slot_name"] == "test-slot"
        assert info["has_model"] is False
        assert info["model_id"] is None
        assert info["inference_count"] == 0

    def test_postprocess_output_list(self, model_cache, model_storage):
        """Test output postprocessing for list."""
        runtime = InferenceRuntime(model_cache, model_storage)

        outputs = [np.array([1, 2, 3]), np.array([4, 5, 6])]
        result = runtime._postprocess_output(outputs)

        assert result == [[1, 2, 3], [4, 5, 6]]

    def test_postprocess_output_single_array(self, model_cache, model_storage):
        """Test output postprocessing for single array."""
        runtime = InferenceRuntime(model_cache, model_storage)

        outputs = np.array([1, 2, 3])
        result = runtime._postprocess_output(outputs)

        assert result == [1, 2, 3]

    def test_shutdown(self, model_cache, model_storage):
        """Test runtime shutdown."""
        runtime = InferenceRuntime(model_cache, model_storage)
        runtime._get_slot_runtime("test-slot")

        runtime.shutdown()

        assert runtime._slots == {}

    def test_runtime_options_select_available_onnx_provider(
        self,
        model_cache,
        model_storage,
        monkeypatch,
    ):
        """Test ONNX provider selection comes from model metadata."""
        runtime = InferenceRuntime(model_cache, model_storage)
        model = CachedModel(
            id="model-1",
            name="model",
            version="1",
            format=ModelFormat.ONNX,
            path=Path("/tmp/model"),
            sha256="abc",
            size_bytes=1,
            metadata={
                "runtime_constraints": {
                    "preferred_providers": [
                        "CUDAExecutionProvider",
                        "CPUExecutionProvider",
                    ]
                }
            },
            package_id="pkg",
            imported_at=__import__("datetime").datetime.now(),
        )
        monkeypatch.setattr(
            "temms.inference.runtime.detect_runtime_capabilities",
            lambda: SimpleNamespace(
                runtimes={
                    "onnxruntime": {
                        "providers": ["CPUExecutionProvider"],
                    }
                }
            ),
        )

        assert runtime._runtime_options_for_model(model) == {"providers": ["CPUExecutionProvider"]}

    def test_runtime_options_use_device_profile_provider_defaults(
        self,
        model_cache,
        model_storage,
        monkeypatch,
    ):
        """Test profile defaults choose edge-appropriate ONNX providers."""
        runtime = InferenceRuntime(model_cache, model_storage)
        model = CachedModel(
            id="model-orin",
            name="model",
            version="1",
            format=ModelFormat.ONNX,
            path=Path("/tmp/model"),
            sha256="abc",
            size_bytes=1,
            metadata={},
            package_id="pkg",
            imported_at=__import__("datetime").datetime.now(),
        )
        monkeypatch.setattr(
            "temms.inference.runtime.detect_runtime_capabilities",
            lambda: SimpleNamespace(
                device_profile="orin-tensorrt",
                runtimes={
                    "onnxruntime": {
                        "available": True,
                        "providers": [
                            "CPUExecutionProvider",
                            "CUDAExecutionProvider",
                            "TensorrtExecutionProvider",
                        ],
                    }
                },
                accelerators={},
            ),
        )

        assert runtime._runtime_options_for_model(model) == {
            "providers": [
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            ]
        }

    def test_explicit_runtime_options_override_device_profile_defaults(
        self,
        model_cache,
        model_storage,
        monkeypatch,
    ):
        """Test package metadata wins over profile defaults."""
        runtime = InferenceRuntime(model_cache, model_storage)
        model = CachedModel(
            id="model-explicit",
            name="model",
            version="1",
            format=ModelFormat.ONNX,
            path=Path("/tmp/model"),
            sha256="abc",
            size_bytes=1,
            metadata={"runtime_options": {"providers": ["CPUExecutionProvider"]}},
            package_id="pkg",
            imported_at=__import__("datetime").datetime.now(),
        )
        monkeypatch.setattr(
            "temms.inference.runtime.detect_runtime_capabilities",
            lambda: SimpleNamespace(
                device_profile="orin-tensorrt",
                runtimes={
                    "onnxruntime": {
                        "available": True,
                        "providers": [
                            "CPUExecutionProvider",
                            "CUDAExecutionProvider",
                            "TensorrtExecutionProvider",
                        ],
                    }
                },
                accelerators={},
            ),
        )

        assert runtime._runtime_options_for_model(model) == {"providers": ["CPUExecutionProvider"]}

    def test_tflite_runtime_options_use_profile_num_threads(
        self,
        model_cache,
        model_storage,
        monkeypatch,
    ):
        """Test Raspberry Pi profile supplies TFLite thread defaults."""
        runtime = InferenceRuntime(model_cache, model_storage)
        model = CachedModel(
            id="model-rpi",
            name="model",
            version="1",
            format=ModelFormat.TFLITE,
            path=Path("/tmp/model"),
            sha256="abc",
            size_bytes=1,
            metadata={},
            package_id="pkg",
            imported_at=__import__("datetime").datetime.now(),
        )
        monkeypatch.setattr(
            "temms.inference.runtime.detect_runtime_capabilities",
            lambda: SimpleNamespace(
                device_profile="rpi5-tflite",
                runtimes={"tflite_runtime": {"available": True}},
                accelerators={},
            ),
        )

        assert runtime._runtime_options_for_model(model) == {"num_threads": 4}

    def test_tensorrt_runtime_options_require_local_tensorrt(
        self,
        model_cache,
        model_storage,
        monkeypatch,
    ):
        """Test TensorRT packages fail early when the runtime is absent."""
        runtime = InferenceRuntime(model_cache, model_storage)
        model = CachedModel(
            id="model-trt",
            name="model",
            version="1",
            format=ModelFormat.TENSORRT,
            path=Path("/tmp/model"),
            sha256="abc",
            size_bytes=1,
            metadata={},
            package_id="pkg",
            imported_at=__import__("datetime").datetime.now(),
        )
        monkeypatch.setattr(
            "temms.inference.runtime.detect_runtime_capabilities",
            lambda: SimpleNamespace(
                device_profile="x86_64-cpu",
                runtimes={"tensorrt": {"available": False}},
                accelerators={},
            ),
        )

        with pytest.raises(RuntimeError, match="TensorRT runtime is not available"):
            runtime._runtime_options_for_model(model)

    def test_tensorrt_runtime_options_accept_available_runtime(
        self,
        model_cache,
        model_storage,
        monkeypatch,
    ):
        """Test TensorRT packages can load when the local runtime is present."""
        runtime = InferenceRuntime(model_cache, model_storage)
        model = CachedModel(
            id="model-trt",
            name="model",
            version="1",
            format=ModelFormat.TENSORRT,
            path=Path("/tmp/model"),
            sha256="abc",
            size_bytes=1,
            metadata={},
            package_id="pkg",
            imported_at=__import__("datetime").datetime.now(),
        )
        monkeypatch.setattr(
            "temms.inference.runtime.detect_runtime_capabilities",
            lambda: SimpleNamespace(
                device_profile="orin-tensorrt",
                runtimes={"tensorrt": {"available": True}},
                accelerators={"nvidia": {"available": True}},
            ),
        )

        assert runtime._runtime_options_for_model(model) == {}

    def test_tflite_preprocess_returns_array(self, model_cache, model_storage):
        """Test TFLite preprocessing returns an array instead of ONNX dict input."""
        runtime = InferenceRuntime(model_cache, model_storage)
        model = CachedModel(
            id="model-1",
            name="model",
            version="1",
            format=ModelFormat.TFLITE,
            path=Path("/tmp/model"),
            sha256="abc",
            size_bytes=1,
            metadata={"input_shape": [1, 2]},
            package_id="pkg",
            imported_at=__import__("datetime").datetime.now(),
        )

        processed = runtime._preprocess_input(
            np.zeros([1, 2], dtype=np.float32).tobytes(),
            "application/octet-stream",
            model,
        )

        assert isinstance(processed, np.ndarray)
        assert processed.shape == (1, 2)


class TestModelLoader:
    """Tests for low-level runtime loader selection."""

    def test_model_loader_passes_onnx_providers(self, temp_dir, monkeypatch):
        """Test ONNX provider options reach the inference session."""
        calls = {}

        class FakeSession:
            def __init__(self, path, **kwargs):
                calls["path"] = path
                calls["kwargs"] = kwargs

            def get_providers(self):
                return calls["kwargs"].get("providers", [])

        fake_ort = SimpleNamespace(
            InferenceSession=FakeSession,
            get_available_providers=lambda: [
                "CPUExecutionProvider",
                "CUDAExecutionProvider",
            ],
        )
        monkeypatch.setitem(__import__("sys").modules, "onnxruntime", fake_ort)
        model_path = temp_dir / "model.onnx"
        model_path.write_bytes(b"fake")

        loader = ModelLoader()
        loader.load_model(
            model_path,
            RuntimeType.ONNX,
            {"providers": ["CUDAExecutionProvider"]},
        )

        assert calls["kwargs"]["providers"] == ["CUDAExecutionProvider"]


class TestSlotRuntime:
    """Tests for SlotRuntime dataclass."""

    def test_init(self):
        """Test slot runtime initialization."""
        slot = SlotRuntime(slot_name="vision")

        assert slot.slot_name == "vision"
        assert slot.loaded_model is None
        assert slot.loading_model is None


class TestRuntimeProfiles:
    """Tests for device/runtime compatibility helpers."""

    def test_mvp_device_profiles_are_registered(self):
        profiles = known_device_profiles()

        assert {"x86_64-cpu", "arm64-jetson", "rpi5-tflite", "orin-tensorrt"}.issubset(profiles)

    def test_runtime_defaults_filter_to_available_providers(self):
        defaults = runtime_defaults_for_profile(
            "orin-tensorrt",
            {
                "runtimes": {
                    "onnxruntime": {
                        "available": True,
                        "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
                    }
                }
            },
        )

        assert defaults["onnx_providers"] == [
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]

    def test_device_profile_aliases_normalize_to_mvp_profiles(self):
        assert normalize_device_profile("amd64-cpu") == "x86_64-cpu"
        assert normalize_device_profile("aarch64-jetson") == "arm64-jetson"
        assert normalize_device_profile("raspberry_pi_5") == "rpi5-tflite"
        assert normalize_device_profile("jetson-orin") == "orin-tensorrt"

    def test_runtime_capabilities_report_canonical_arch(self):
        capabilities = RuntimeCapabilities(
            os="Linux",
            machine="aarch64",
            python="3.11",
            device_profile="arm64-cpu",
        )

        assert capabilities.to_dict()["arch"] == "arm64"

    def test_detect_runtime_capabilities_honors_device_profile_env(self, monkeypatch):
        monkeypatch.setenv("TEMMS_DEVICE_PROFILE", "rpi5-tflite")

        capabilities = detect_runtime_capabilities()

        assert capabilities.device_profile == "rpi5-tflite"

    def test_infer_device_profile_for_mvp_edge_boards(self):
        assert (
            _infer_device_profile(
                "aarch64",
                {"tflite_runtime": {"available": True}},
                {"jetson": {"available": False}, "nvidia": {"available": False}},
                board_model="Raspberry Pi 5 Model B Rev 1.0",
            )
            == "rpi5-tflite"
        )
        assert (
            _infer_device_profile(
                "aarch64",
                {"tensorrt": {"available": False}},
                {"jetson": {"available": True}, "nvidia": {"available": True}},
                board_model="NVIDIA Jetson AGX Xavier",
            )
            == "arm64-jetson"
        )
        assert (
            _infer_device_profile(
                "aarch64",
                {"tensorrt": {"available": True}},
                {"jetson": {"available": True}, "nvidia": {"available": True}},
                board_model="NVIDIA Jetson Orin",
            )
            == "orin-tensorrt"
        )

    def test_runtime_constraints_satisfied_with_aliases(self):
        capabilities = {
            "device_profile": "amd64-cpu",
            "runtimes": {"onnxruntime": {"available": True, "providers": ["CPUExecutionProvider"]}},
            "accelerators": {},
        }

        ok, reasons = runtime_constraints_satisfied(
            {
                "device_profiles": ["x86_64-cpu"],
                "runtimes": ["onnx"],
                "providers": ["CPUExecutionProvider"],
            },
            capabilities,
        )

        assert ok is True
        assert reasons == []

    def test_runtime_constraints_accept_provider_order_with_available_fallback(self):
        capabilities = {
            "device_profile": "x86_64-cpu",
            "runtimes": {"onnxruntime": {"available": True, "providers": ["CPUExecutionProvider"]}},
            "accelerators": {},
        }

        ok, reasons = runtime_constraints_satisfied(
            {
                "runtimes": ["onnx"],
                "provider_order": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            },
            capabilities,
        )

        assert ok is True
        assert reasons == []

    def test_runtime_constraints_reject_provider_order_when_none_are_available(self):
        capabilities = {
            "device_profile": "x86_64-cpu",
            "runtimes": {"onnxruntime": {"available": True, "providers": ["CPUExecutionProvider"]}},
            "accelerators": {},
        }

        ok, reasons = runtime_constraints_satisfied(
            {
                "runtimes": ["onnx"],
                "provider_order": ["CUDAExecutionProvider", "TensorrtExecutionProvider"],
            },
            capabilities,
        )

        assert ok is False
        assert any("none of the preferred ONNX providers" in reason for reason in reasons)

    def test_runtime_constraints_accept_canonical_profile_aliases(self):
        capabilities = {
            "device_profile": "raspberry_pi_5",
            "runtimes": {"tflite_runtime": {"available": True}},
            "accelerators": {},
        }

        ok, reasons = runtime_constraints_satisfied(
            {"device_profiles": ["rpi5-tflite"], "runtimes": ["tflite"]},
            capabilities,
        )

        assert ok is True
        assert reasons == []

    def test_runtime_constraints_report_missing_runtime(self):
        capabilities = {
            "device_profile": "arm64-cpu",
            "runtimes": {"onnxruntime": {"available": False}},
            "accelerators": {},
        }

        ok, reasons = runtime_constraints_satisfied(
            {"device_profiles": ["x86_64-cpu"], "runtimes": ["onnx"]},
            capabilities,
        )

        assert ok is False
        assert any("device profile" in reason for reason in reasons)
        assert any("missing runtimes" in reason for reason in reasons)

    def test_package_runtime_constraints_include_compatibility_metadata(self):
        manifest = {
            "compatibility": {
                "runtime_constraints": {
                    "runtimes": ["tflite"],
                    "providers": ["CPUExecutionProvider"],
                }
            },
            "models": [
                {
                    "id": "model-a",
                    "runtime_constraints": {"accelerators": ["nvidia"]},
                }
            ],
        }

        constraints = package_runtime_constraints(manifest)

        assert constraints == [
            (
                "model-a",
                {
                    "runtimes": ["tflite"],
                    "providers": ["CPUExecutionProvider"],
                    "accelerators": ["nvidia"],
                },
            )
        ]


@pytest.mark.asyncio
class TestInferenceRuntimeAsync:
    """Async tests for InferenceRuntime."""

    async def test_load_model_not_found(self, model_cache, model_storage):
        """Test loading non-existent model raises error."""
        runtime = InferenceRuntime(model_cache, model_storage)

        with pytest.raises(ValueError, match="Model not found"):
            await runtime.load_model("test-slot", "nonexistent-model")

    async def test_unload_model_no_model(self, model_cache, model_storage):
        """Test unloading when no model loaded."""
        runtime = InferenceRuntime(model_cache, model_storage)
        runtime._get_slot_runtime("test-slot")

        result = await runtime.unload_model("test-slot")

        assert result is False

    async def test_simulated_runtime_load_accepts_explicit_missing_tflite(
        self,
        model_cache,
        model_storage,
        monkeypatch,
        temp_dir,
    ):
        """Test acceptance simulations can load a target runtime not installed locally."""
        monkeypatch.setenv("TEMMS_INFERENCE_SIMULATE_RUNTIME", "1")
        monkeypatch.setattr(
            "temms.inference.runtime.detect_runtime_capabilities",
            lambda: SimpleNamespace(
                device_profile="x86_64-cpu",
                runtimes={"onnxruntime": {"available": True}},
                accelerators={},
            ),
        )
        model_file = temp_dir / "model.tflite"
        model_file.write_bytes(b"simulated-tflite")
        dest_path, sha256, size = model_storage.store_model(model_file, "model-rpi")
        model_cache.add_cached_model(
            model_id="model-rpi",
            name="model-rpi",
            version="1",
            format=ModelFormat.TFLITE,
            path=dest_path,
            sha256=sha256,
            size_bytes=size,
            package_id="pkg-rpi",
            metadata={
                "runtime_constraints": {
                    "device_profiles": ["rpi5-tflite"],
                    "runtimes": ["tflite_runtime"],
                }
            },
        )
        runtime = InferenceRuntime(model_cache, model_storage)

        assert await runtime.load_model("vision", "model-rpi") is True

        loaded = runtime._get_slot_runtime("vision").loaded_model
        assert loaded is not None
        assert loaded.runtime_type == RuntimeType.TFLITE
        assert isinstance(loaded.runtime, SimulatedModelRuntime)

    async def test_try_fallback_chain_empty(self, model_cache, model_storage):
        """Test fallback chain with empty list."""
        runtime = InferenceRuntime(model_cache, model_storage)

        result = await runtime.try_fallback_chain("test-slot", [])

        assert result is None

    async def test_try_fallback_chain_all_missing(self, model_cache, model_storage):
        """Test fallback chain when all models are missing."""
        runtime = InferenceRuntime(model_cache, model_storage)

        result = await runtime.try_fallback_chain("test-slot", ["missing1", "missing2", "missing3"])

        assert result is None
