"""
Unified inference runtime with hot-swap and fallback support.

Manages model loading and inference across multiple slots with:
- Per-slot model instances
- Hot-swap capability (load new while old serves)
- Thread-safe inference with copy-on-read locking
- Fallback chain execution
- Model preloading for fast activation
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from temms.core.loader import ModelLoader, RuntimeType, ModelRuntime
from temms.core.cache import ModelCache, CachedModel, ModelFormat
from temms.core.storage import ModelStorage

logger = logging.getLogger(__name__)


@dataclass
class LoadedModel:
    """Represents a loaded model instance."""
    model_id: str
    runtime: ModelRuntime
    model_info: CachedModel
    loaded_at: datetime
    inference_count: int = 0
    last_inference: Optional[datetime] = None


@dataclass
class SlotRuntime:
    """Runtime state for a single slot."""
    slot_name: str
    loaded_model: Optional[LoadedModel] = None
    loading_model: Optional[str] = None  # Model ID currently being loaded
    lock: threading.RLock = field(default_factory=threading.RLock)


class InferenceRuntime:
    """
    Manages model inference across multiple slots.

    Features:
    - Per-slot model management
    - Hot-swap: load new model while old serves requests
    - Thread-safe inference with copy-on-read locking
    - Fallback chain execution on load failure
    - Model preloading for fast activation
    """

    def __init__(
        self,
        model_cache: ModelCache,
        model_storage: ModelStorage,
        max_workers: int = 4,
    ):
        """
        Initialize inference runtime.

        Args:
            model_cache: ModelCache for model metadata lookup
            model_storage: ModelStorage for model file access
            max_workers: Thread pool size for inference
        """
        self.model_cache = model_cache
        self.model_storage = model_storage
        self._slots: Dict[str, SlotRuntime] = {}
        self._global_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._preloaded: Dict[str, LoadedModel] = {}  # model_id -> LoadedModel

    def _get_slot_runtime(self, slot_name: str) -> SlotRuntime:
        """Get or create slot runtime."""
        with self._global_lock:
            if slot_name not in self._slots:
                self._slots[slot_name] = SlotRuntime(slot_name=slot_name)
            return self._slots[slot_name]

    def _format_to_runtime_type(self, format: ModelFormat) -> RuntimeType:
        """Convert model format to runtime type."""
        mapping = {
            ModelFormat.ONNX: RuntimeType.ONNX,
            ModelFormat.TFLITE: RuntimeType.TFLITE,
            ModelFormat.TORCHSCRIPT: RuntimeType.TORCHSCRIPT,
            ModelFormat.TENSORRT: RuntimeType.TENSORRT,
        }
        return mapping.get(format, RuntimeType.ONNX)

    async def load_model(self, slot_name: str, model_id: str) -> bool:
        """
        Load a model into a slot.

        Performs hot-swap if a model is already loaded:
        1. Load new model outside the lock (or use preloaded)
        2. Briefly lock for atomic pointer swap
        3. Unload old model outside the lock

        Args:
            slot_name: Target slot
            model_id: Model ID to load

        Returns:
            True if successful

        Raises:
            ValueError: If model not found
            RuntimeError: If load fails
        """
        slot_runtime = self._get_slot_runtime(slot_name)

        # Get model info from cache
        model_info = self.model_cache.get_model(model_id)
        if model_info is None:
            raise ValueError(f"Model not found in cache: {model_id}")

        # Get model file path
        model_dir = self.model_storage.get_model_path(model_id)
        if model_dir is None:
            raise ValueError(f"Model files not found: {model_id}")

        # Find model file in directory
        model_path = self._find_model_file(model_dir, model_info.format)
        if model_path is None:
            raise ValueError(f"Model file not found in {model_dir}")

        # Load in thread pool to avoid blocking
        def _load():
            # Mark as loading (brief lock)
            with slot_runtime.lock:
                slot_runtime.loading_model = model_id

            try:
                # Check if model is preloaded (skip expensive load step)
                if model_id in self._preloaded:
                    new_loaded = self._preloaded.pop(model_id)
                    logger.info(
                        f"Using preloaded model {model_id} for slot {slot_name}"
                    )
                else:
                    # Create loader and load model OUTSIDE the lock
                    loader = ModelLoader()
                    runtime_type = self._format_to_runtime_type(model_info.format)
                    runtime = loader.load_model(model_path, runtime_type)

                    new_loaded = LoadedModel(
                        model_id=model_id,
                        runtime=runtime,
                        model_info=model_info,
                        loaded_at=datetime.now(),
                    )

                # Brief lock for atomic swap
                with slot_runtime.lock:
                    old_loaded = slot_runtime.loaded_model
                    slot_runtime.loaded_model = new_loaded
                    slot_runtime.loading_model = None

                # Unload old model outside lock
                if old_loaded is not None:
                    try:
                        old_loaded.runtime.unload()
                        logger.info(
                            f"Unloaded old model {old_loaded.model_id} from slot {slot_name}"
                        )
                    except Exception as e:
                        logger.warning(f"Error unloading old model: {e}")

                logger.info(
                    f"Loaded model {model_id} into slot {slot_name}"
                )
                return True

            except Exception as e:
                with slot_runtime.lock:
                    slot_runtime.loading_model = None
                raise RuntimeError(f"Failed to load model {model_id}: {e}") from e

        # Run in executor
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _load)

    def _find_model_file(self, model_dir: Path, format: ModelFormat) -> Optional[Path]:
        """Find the model file in a directory based on format."""
        extensions = {
            ModelFormat.ONNX: [".onnx"],
            ModelFormat.TFLITE: [".tflite"],
            ModelFormat.TORCHSCRIPT: [".pt", ".pth"],
            ModelFormat.TENSORRT: [".engine", ".plan"],
        }

        for ext in extensions.get(format, []):
            files = list(model_dir.glob(f"*{ext}"))
            if files:
                return files[0]

        # Fallback: any file
        files = list(model_dir.glob("*"))
        for f in files:
            if f.is_file() and not f.name.startswith("."):
                return f

        return None

    async def infer(
        self,
        slot_name: str,
        model_id: str,
        input_data: bytes,
        content_type: str = "application/octet-stream",
    ) -> List[Any]:
        """
        Run inference on a slot's loaded model.

        Uses copy-on-read locking: grabs a reference to the loaded model
        under a brief lock, then releases the lock and runs inference on
        the reference. This allows hot-swap to proceed concurrently.

        Args:
            slot_name: Slot to run inference on
            model_id: Expected model ID (for validation)
            input_data: Raw input bytes
            content_type: MIME type of input

        Returns:
            List of predictions

        Raises:
            RuntimeError: If slot has no model or wrong model loaded
        """
        slot_runtime = self._get_slot_runtime(slot_name)

        def _infer():
            # Brief lock to grab reference
            with slot_runtime.lock:
                loaded = slot_runtime.loaded_model

                if loaded is None:
                    raise RuntimeError(f"No model loaded in slot {slot_name}")

                if loaded.model_id != model_id:
                    raise RuntimeError(
                        f"Model mismatch: expected {model_id}, got {loaded.model_id}"
                    )

            # Run inference WITHOUT holding the lock
            # Get the actual input name from the runtime session
            input_name = self._get_model_input_name(loaded.runtime)

            processed_input = self._preprocess_input(
                input_data, content_type, loaded.model_info, input_name=input_name
            )

            outputs = loaded.runtime.infer(processed_input)

            # Update stats
            loaded.inference_count += 1
            loaded.last_inference = datetime.now()

            # Postprocess outputs
            return self._postprocess_output(outputs)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _infer)

    def _get_model_input_name(self, runtime: ModelRuntime) -> str:
        """
        Get the input tensor name from a loaded model runtime.

        For ONNX models, reads session.get_inputs()[0].name.
        Falls back to "input" for other runtimes.
        """
        try:
            # ONNX Runtime: session has get_inputs()
            if hasattr(runtime, "session") and runtime.session is not None:
                inputs = runtime.session.get_inputs()
                if inputs:
                    return inputs[0].name
        except Exception as e:
            logger.debug(f"Could not get input name from runtime: {e}")

        return "input"

    def _preprocess_input(
        self,
        input_data: bytes,
        content_type: str,
        model_info: CachedModel,
        input_name: str = "input",
    ) -> Any:
        """
        Preprocess input data for inference.

        Args:
            input_data: Raw bytes
            content_type: MIME type
            model_info: Model metadata for shape/type info
            input_name: Name of the model's input tensor

        Returns:
            Preprocessed input suitable for model
        """
        import numpy as np

        # Get input shape from metadata
        input_shape = model_info.metadata.get("input_shape", [1, 3, 224, 224])
        _ = model_info.metadata.get("input_dtype", "float32")  # Reserved for future use

        if content_type.startswith("image/"):
            # Image preprocessing
            try:
                from PIL import Image
                import io

                image = Image.open(io.BytesIO(input_data))
                image = image.convert("RGB")

                # Resize to expected shape
                if len(input_shape) >= 2:
                    h, w = input_shape[-2], input_shape[-1]
                    image = image.resize((w, h))

                # Convert to numpy
                arr = np.array(image, dtype=np.float32)

                # Normalize to [0, 1]
                arr = arr / 255.0

                # HWC -> CHW
                if len(arr.shape) == 3:
                    arr = np.transpose(arr, (2, 0, 1))

                # Add batch dimension
                arr = np.expand_dims(arr, 0)

                return {input_name: arr.astype(np.float32)}

            except ImportError:
                logger.warning("PIL not available, using raw bytes")
                return {input_name: np.frombuffer(input_data, dtype=np.float32).reshape(input_shape)}

        else:
            # Generic binary data - assume numpy array
            arr = np.frombuffer(input_data, dtype=np.float32)
            if input_shape:
                arr = arr.reshape(input_shape)
            return {input_name: arr}

    def _postprocess_output(self, outputs: Any) -> List[Any]:
        """
        Postprocess model outputs.

        Args:
            outputs: Raw model outputs

        Returns:
            List of predictions in standard format
        """
        import numpy as np

        if isinstance(outputs, list):
            return [
                o.tolist() if isinstance(o, np.ndarray) else o
                for o in outputs
            ]
        elif isinstance(outputs, np.ndarray):
            return outputs.tolist()
        else:
            return [outputs]

    async def unload_model(self, slot_name: str) -> bool:
        """
        Unload model from a slot.

        Args:
            slot_name: Slot to unload

        Returns:
            True if model was unloaded, False if no model loaded
        """
        slot_runtime = self._get_slot_runtime(slot_name)

        def _unload():
            with slot_runtime.lock:
                if slot_runtime.loaded_model is None:
                    return False

                try:
                    slot_runtime.loaded_model.runtime.unload()
                except Exception as e:
                    logger.warning(f"Error unloading model: {e}")

                slot_runtime.loaded_model = None
                logger.info(f"Unloaded model from slot {slot_name}")
                return True

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _unload)

    async def try_fallback_chain(
        self,
        slot_name: str,
        fallback_chain: List[str],
    ) -> Optional[str]:
        """
        Try loading models from fallback chain until one succeeds.

        Args:
            slot_name: Target slot
            fallback_chain: List of model names to try in order

        Returns:
            Model ID that was successfully loaded, or None
        """
        for model_name in fallback_chain:
            model = self.model_cache.find_model(model_name)
            if model is None:
                logger.warning(f"Fallback model not found: {model_name}")
                continue

            try:
                await self.load_model(slot_name, model.id)
                logger.info(f"Fallback successful: loaded {model.id} into {slot_name}")
                return model.id
            except Exception as e:
                logger.warning(f"Fallback model {model_name} failed: {e}")
                continue

        logger.error(f"All fallback models failed for slot {slot_name}")
        return None

    async def preload_model(self, slot_name: str, model_id: str) -> bool:
        """
        Preload a model into memory without activating it.

        When load_model is later called for this model_id, it will use
        the preloaded instance instead of loading from disk, making
        the switch near-instantaneous.

        Args:
            slot_name: Slot this model is intended for (used for validation)
            model_id: Model ID to preload

        Returns:
            True if preloaded successfully

        Raises:
            ValueError: If model not found
            RuntimeError: If load fails
        """
        # Skip if already preloaded
        if model_id in self._preloaded:
            logger.debug(f"Model {model_id} already preloaded")
            return True

        # Get model info from cache
        model_info = self.model_cache.get_model(model_id)
        if model_info is None:
            raise ValueError(f"Model not found in cache: {model_id}")

        # Get model file path
        model_dir = self.model_storage.get_model_path(model_id)
        if model_dir is None:
            raise ValueError(f"Model files not found: {model_id}")

        # Find model file in directory
        model_path = self._find_model_file(model_dir, model_info.format)
        if model_path is None:
            raise ValueError(f"Model file not found in {model_dir}")

        def _preload():
            loader = ModelLoader()
            runtime_type = self._format_to_runtime_type(model_info.format)
            runtime = loader.load_model(model_path, runtime_type)

            loaded = LoadedModel(
                model_id=model_id,
                runtime=runtime,
                model_info=model_info,
                loaded_at=datetime.now(),
            )

            self._preloaded[model_id] = loaded
            self.clear_preloaded()

            logger.info(f"Preloaded model {model_id} for slot {slot_name}")
            return True

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _preload)

    def clear_preloaded(self, max_preloaded: int = 3) -> None:
        """
        Evict oldest preloaded models if over limit.

        Keeps at most max_preloaded models in the preload cache,
        evicting the oldest entries first (by loaded_at timestamp).

        Args:
            max_preloaded: Maximum number of preloaded models to keep
        """
        if len(self._preloaded) <= max_preloaded:
            return

        # Sort by loaded_at, evict oldest
        sorted_entries = sorted(
            self._preloaded.items(),
            key=lambda item: item[1].loaded_at,
        )

        to_evict = len(self._preloaded) - max_preloaded
        for model_id, loaded in sorted_entries[:to_evict]:
            try:
                loaded.runtime.unload()
                logger.info(f"Evicted preloaded model {model_id}")
            except Exception as e:
                logger.warning(f"Error unloading preloaded model {model_id}: {e}")
            del self._preloaded[model_id]

    def get_slot_info(self, slot_name: str) -> Dict[str, Any]:
        """Get information about a slot's runtime state."""
        slot_runtime = self._get_slot_runtime(slot_name)

        with slot_runtime.lock:
            loaded = slot_runtime.loaded_model

            return {
                "slot_name": slot_name,
                "has_model": loaded is not None,
                "model_id": loaded.model_id if loaded else None,
                "model_name": loaded.model_info.name if loaded else None,
                "loaded_at": loaded.loaded_at.isoformat() if loaded else None,
                "inference_count": loaded.inference_count if loaded else 0,
                "last_inference": loaded.last_inference.isoformat() if loaded and loaded.last_inference else None,
                "loading_model": slot_runtime.loading_model,
            }

    def get_all_slots_info(self) -> Dict[str, Dict[str, Any]]:
        """Get information about all slots."""
        with self._global_lock:
            return {
                name: self.get_slot_info(name)
                for name in self._slots
            }

    def shutdown(self) -> None:
        """Shutdown runtime, unload all models and preloaded models."""
        logger.info("Shutting down inference runtime")

        # Unload preloaded models
        for model_id, loaded in self._preloaded.items():
            try:
                loaded.runtime.unload()
                logger.info(f"Unloaded preloaded model {model_id}")
            except Exception as e:
                logger.warning(f"Error unloading preloaded model {model_id}: {e}")
        self._preloaded.clear()

        with self._global_lock:
            for slot_name, slot_runtime in self._slots.items():
                with slot_runtime.lock:
                    if slot_runtime.loaded_model is not None:
                        try:
                            slot_runtime.loaded_model.runtime.unload()
                        except Exception as e:
                            logger.warning(f"Error unloading {slot_name}: {e}")
                        slot_runtime.loaded_model = None

            self._slots.clear()

        self._executor.shutdown(wait=True)
        logger.info("Inference runtime shutdown complete")
