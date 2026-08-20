"""
Unified model loader supporting multiple ML frameworks.
"""

import logging
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class RuntimeType(str, Enum):
    """Supported ML runtime types."""
    ONNX = "onnx"
    TFLITE = "tflite"
    TORCHSCRIPT = "torchscript"
    TENSORRT = "tensorrt"


class ModelRuntime(Protocol):
    """Protocol for model runtime implementations."""

    def load(self, model_path: Path) -> Any:
        """Load model from path."""
        ...

    def infer(self, input_data: Any) -> Any:
        """Run inference."""
        ...

    def unload(self) -> None:
        """Unload model and free resources."""
        ...


class ONNXRuntime:
    """ONNX Runtime wrapper."""

    def __init__(self, providers: list[str] | None = None):
        self.session: Any | None = None
        self.providers = providers

    def load(self, model_path: Path) -> Any:
        """Load ONNX model."""
        try:
            import onnxruntime as ort
            kwargs = {}
            if self.providers:
                available = set(ort.get_available_providers())
                selected = [provider for provider in self.providers if provider in available]
                if selected:
                    kwargs["providers"] = selected
                else:
                    raise RuntimeError(
                        "None of the requested ONNX providers are available: "
                        + ", ".join(self.providers)
                    )
            self.session = ort.InferenceSession(str(model_path), **kwargs)
            logger.info(
                "Loaded ONNX model from %s providers=%s",
                model_path,
                getattr(self.session, "get_providers", lambda: [])(),
            )
            return self.session
        except ImportError:
            raise RuntimeError("onnxruntime not installed. Install with: pip install onnxruntime")

    def infer(self, input_data: dict) -> list:
        """Run ONNX inference."""
        if self.session is None:
            raise RuntimeError("Model not loaded")

        outputs = self.session.run(None, input_data)
        return outputs

    def unload(self) -> None:
        """Unload ONNX model."""
        self.session = None
        logger.info("Unloaded ONNX model")








class ModelLoader:
    """Unified model loader with hot-swap support."""

    def __init__(self):
        self.current_model: ModelRuntime | None = None
        self.current_runtime_type: RuntimeType | None = None
        self.current_path: Path | None = None

    def load_model(
        self,
        model_path: Path,
        runtime_type: RuntimeType,
        runtime_options: dict[str, Any] | None = None,
    ) -> ModelRuntime:
        """
        Load a model with specified runtime.

        Args:
            model_path: Path to model file
            runtime_type: Runtime type to use
            runtime_options: Runtime-specific options such as ONNX providers

        Returns:
            Loaded model runtime
        """
        # Unload current model if any
        if self.current_model is not None:
            self.unload_current()

        # Create runtime instance
        # ONNX Runtime is the only supported engine (direction: integrate ORT,
        # do not build a runtime zoo). Other RuntimeType values fail explicitly.
        runtime_map = {
            RuntimeType.ONNX: ONNXRuntime,
        }

        runtime_class = runtime_map.get(runtime_type)
        if runtime_class is None:
            raise ValueError(f"Unsupported runtime type: {runtime_type}")

        runtime_options = runtime_options or {}
        runtime = runtime_class(**runtime_options)
        runtime.load(model_path)

        self.current_model = runtime
        self.current_runtime_type = runtime_type
        self.current_path = model_path

        return runtime

    def unload_current(self) -> None:
        """Unload currently loaded model."""
        if self.current_model is not None:
            self.current_model.unload()
            self.current_model = None
            self.current_runtime_type = None
            self.current_path = None

    def get_current_model(self) -> ModelRuntime | None:
        """Get currently loaded model."""
        return self.current_model

    def is_loaded(self) -> bool:
        """Check if a model is currently loaded."""
        return self.current_model is not None
