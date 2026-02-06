"""
Unit tests for the inference runtime.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
import numpy as np

from temms.inference.runtime import InferenceRuntime, LoadedModel, SlotRuntime
from temms.core.cache import ModelFormat


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


class TestSlotRuntime:
    """Tests for SlotRuntime dataclass."""

    def test_init(self):
        """Test slot runtime initialization."""
        slot = SlotRuntime(slot_name="vision")

        assert slot.slot_name == "vision"
        assert slot.loaded_model is None
        assert slot.loading_model is None


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

    async def test_try_fallback_chain_empty(self, model_cache, model_storage):
        """Test fallback chain with empty list."""
        runtime = InferenceRuntime(model_cache, model_storage)

        result = await runtime.try_fallback_chain("test-slot", [])

        assert result is None

    async def test_try_fallback_chain_all_missing(self, model_cache, model_storage):
        """Test fallback chain when all models are missing."""
        runtime = InferenceRuntime(model_cache, model_storage)

        result = await runtime.try_fallback_chain(
            "test-slot",
            ["missing1", "missing2", "missing3"]
        )

        assert result is None
