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

    def __init__(self, providers: Optional[list[str]] = None):
        self.session: Optional[Any] = None
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


class TFLiteRuntime:
    """TensorFlow Lite runtime wrapper."""

    def __init__(self, num_threads: Optional[int] = None):
        self.interpreter: Optional[Any] = None
        self.num_threads = num_threads

    def load(self, model_path: Path) -> Any:
        """Load TFLite model."""
        try:
            try:
                from tflite_runtime.interpreter import Interpreter
            except ImportError:
                import tensorflow as tf

                Interpreter = tf.lite.Interpreter
            kwargs = {"model_path": str(model_path)}
            if self.num_threads:
                kwargs["num_threads"] = self.num_threads
            self.interpreter = Interpreter(**kwargs)
            self.interpreter.allocate_tensors()
            logger.info(
                "Loaded TFLite model from %s num_threads=%s",
                model_path,
                self.num_threads,
            )
            return self.interpreter
        except ImportError:
            raise RuntimeError(
                "TFLite runtime not installed. Install tensorflow or tflite_runtime."
            )

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


class TensorRTRuntime:
    """TensorRT serialized engine runtime wrapper."""

    def __init__(self):
        self.engine: Optional[Any] = None
        self.context: Optional[Any] = None

    def load(self, model_path: Path) -> Any:
        """Load a serialized TensorRT engine."""
        try:
            import tensorrt as trt

            logger_obj = trt.Logger(trt.Logger.WARNING)
            with trt.Runtime(logger_obj) as runtime:
                self.engine = runtime.deserialize_cuda_engine(model_path.read_bytes())
            if self.engine is None:
                raise RuntimeError(f"Could not deserialize TensorRT engine: {model_path}")
            self.context = self.engine.create_execution_context()
            logger.info("Loaded TensorRT engine from %s", model_path)
            return self.context
        except ImportError:
            raise RuntimeError("tensorrt not installed. Install NVIDIA TensorRT bindings.")

    def infer(self, input_data: Any) -> Any:
        """Run TensorRT inference."""
        raise RuntimeError(
            "Generic TensorRT inference requires deployment-specific I/O bindings. "
            "Load the engine through a TEMMS runtime plugin for this device profile."
        )

    def unload(self) -> None:
        """Unload TensorRT engine."""
        self.context = None
        self.engine = None
        logger.info("Unloaded TensorRT engine")


class ModelLoader:
    """Unified model loader with hot-swap support."""

    def __init__(self):
        self.current_model: Optional[ModelRuntime] = None
        self.current_runtime_type: Optional[RuntimeType] = None
        self.current_path: Optional[Path] = None

    def load_model(
        self,
        model_path: Path,
        runtime_type: RuntimeType,
        runtime_options: Optional[dict[str, Any]] = None,
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
        runtime_map = {
            RuntimeType.ONNX: ONNXRuntime,
            RuntimeType.TFLITE: TFLiteRuntime,
            RuntimeType.TORCHSCRIPT: TorchScriptRuntime,
            RuntimeType.TENSORRT: TensorRTRuntime,
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

    def get_current_model(self) -> Optional[ModelRuntime]:
        """Get currently loaded model."""
        return self.current_model

    def is_loaded(self) -> bool:
        """Check if a model is currently loaded."""
        return self.current_model is not None
