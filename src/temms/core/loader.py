"""
Unified model loader supporting multiple ML frameworks.
"""

from pathlib import Path
from typing import Any, Optional, Protocol
from enum import Enum
import logging

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

    def __init__(self):
        self.session: Optional[Any] = None

    def load(self, model_path: Path) -> Any:
        """Load ONNX model."""
        try:
            import onnxruntime as ort
            self.session = ort.InferenceSession(str(model_path))
            logger.info(f"Loaded ONNX model from {model_path}")
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


class TFLiteRuntime:
    """TensorFlow Lite runtime wrapper."""

    def __init__(self):
        self.interpreter: Optional[Any] = None

    def load(self, model_path: Path) -> Any:
        """Load TFLite model."""
        try:
            import tensorflow as tf
            self.interpreter = tf.lite.Interpreter(model_path=str(model_path))
            self.interpreter.allocate_tensors()
            logger.info(f"Loaded TFLite model from {model_path}")
            return self.interpreter
        except ImportError:
            raise RuntimeError("tensorflow not installed. Install with: pip install tensorflow")

    def infer(self, input_data: Any) -> Any:
        """Run TFLite inference."""
        if self.interpreter is None:
            raise RuntimeError("Model not loaded")

        input_details = self.interpreter.get_input_details()
        output_details = self.interpreter.get_output_details()

        self.interpreter.set_tensor(input_details[0]["index"], input_data)
        self.interpreter.invoke()

        output_data = self.interpreter.get_tensor(output_details[0]["index"])
        return output_data

    def unload(self) -> None:
        """Unload TFLite model."""
        self.interpreter = None
        logger.info("Unloaded TFLite model")


class TorchScriptRuntime:
    """PyTorch TorchScript runtime wrapper."""

    def __init__(self):
        self.model: Optional[Any] = None

    def load(self, model_path: Path) -> Any:
        """Load TorchScript model."""
        try:
            import torch
            self.model = torch.jit.load(str(model_path))
            self.model.eval()
            logger.info(f"Loaded TorchScript model from {model_path}")
            return self.model
        except ImportError:
            raise RuntimeError("torch not installed. Install with: pip install torch")

    def infer(self, input_data: Any) -> Any:
        """Run TorchScript inference."""
        if self.model is None:
            raise RuntimeError("Model not loaded")

        import torch
        with torch.no_grad():
            output = self.model(input_data)
        return output

    def unload(self) -> None:
        """Unload TorchScript model."""
        self.model = None
        logger.info("Unloaded TorchScript model")


class ModelLoader:
    """Unified model loader with hot-swap support."""

    def __init__(self):
        self.current_model: Optional[ModelRuntime] = None
        self.current_runtime_type: Optional[RuntimeType] = None
        self.current_path: Optional[Path] = None

    def load_model(self, model_path: Path, runtime_type: RuntimeType) -> ModelRuntime:
        """
        Load a model with specified runtime.

        Args:
            model_path: Path to model file
            runtime_type: Runtime type to use

        Returns:
            Loaded model runtime
        """
        # Unload current model if any
        if self.current_model is not None:
            self.unload_current()

        # Create runtime instance
        runtime_map = {
            RuntimeType.ONNX: ONNXRuntime,
            RuntimeType.TFLITE: TFLiteRuntime,
            RuntimeType.TORCHSCRIPT: TorchScriptRuntime,
        }

        runtime_class = runtime_map.get(runtime_type)
        if runtime_class is None:
            raise ValueError(f"Unsupported runtime type: {runtime_type}")

        runtime = runtime_class()
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

    def get_current_model(self) -> Optional[ModelRuntime]:
        """Get currently loaded model."""
        return self.current_model

    def is_loaded(self) -> bool:
        """Check if a model is currently loaded."""
        return self.current_model is not None
