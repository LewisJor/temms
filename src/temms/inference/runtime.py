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
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from temms.core.loader import ModelLoader, RuntimeType, ModelRuntime
from temms.core.cache import ModelCache, CachedModel, ModelFormat
from temms.core.runtime_profiles import (
    RuntimeCapabilities,
    detect_runtime_capabilities,
    runtime_constraints_satisfied,
    runtime_defaults_for_profile,
)
from temms.core.storage import ModelStorage

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _simulate_runtime_loads() -> bool:
    """Return whether model loads should use the simulation runtime."""
    return _env_bool("TEMMS_INFERENCE_SIMULATE_RUNTIME")


class SimulatedModelRuntime:
    """No-op runtime for explicit Docker/VM acceptance simulations."""

    def __init__(self, runtime_type: RuntimeType):
        self.runtime_type = runtime_type
        self.model_path: Optional[Path] = None

    def load(self, model_path: Path) -> "SimulatedModelRuntime":
        self.model_path = model_path
        logger.info("Simulated %s model load from %s", self.runtime_type.value, model_path)
        return self

    def infer(self, input_data: Any) -> list[Any]:
        return []

    def unload(self) -> None:
        self.model_path = None


@dataclass
class LoadedModel:
    """Represents a loaded model instance."""
    model_id: str
    runtime: ModelRuntime
    model_info: CachedModel
    loaded_at: datetime
    runtime_type: RuntimeType
    runtime_options: Dict[str, Any] = field(default_factory=dict)
    inference_count: int = 0
    last_inference: Optional[datetime] = None
    warmed: bool = False


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
    - Per-slot model activation control
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
        model_info, model_path = self._resolve_model_source(model_id)

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
                    # Build the instance OUTSIDE the lock (loads + warms). A
                    # warmup failure means the model cannot run: it propagates,
                    # so the swap is aborted, the old model keeps serving, and
                    # the controller can fall back.
                    new_loaded = self._construct_loaded_model(model_info, model_path)

                # Brief lock for an atomic pointer swap. The old instance is
                # NOT explicitly unloaded here: any request already executing
                # holds its own reference to it, so it is freed (and its native
                # session released) only once the last in-flight request
                # returns. This drains in-flight requests without a bespoke
                # refcount subsystem — CPython reference counting is the
                # mechanism (swap-contract: in-flight completion).
                with slot_runtime.lock:
                    slot_runtime.loaded_model = new_loaded
                    slot_runtime.loading_model = None

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

    def _resolve_model_source(self, model_id: str) -> tuple[CachedModel, Path]:
        """Resolve a model's cache entry and on-disk file, or raise ValueError.

        Runs eagerly (before any executor dispatch) so a missing model surfaces
        as ValueError to the caller rather than a wrapped load error.
        """
        model_info = self.model_cache.get_model(model_id)
        if model_info is None:
            raise ValueError(f"Model not found in cache: {model_id}")
        model_dir = self.model_storage.get_model_path(model_id)
        if model_dir is None:
            raise ValueError(f"Model files not found: {model_id}")
        model_path = self._find_model_file(model_dir, model_info.format)
        if model_path is None:
            raise ValueError(f"Model file not found in {model_dir}")
        return model_info, model_path

    def _construct_loaded_model(
        self, model_info: CachedModel, model_path: Path
    ) -> LoadedModel:
        """Load a model instance from disk and warm it.

        Shared by activation and preload so both honor the simulation runtime
        flag and warm identically. Runs outside any slot lock.
        """
        runtime_type = self._format_to_runtime_type(model_info.format)
        runtime_options = self._runtime_options_for_model(model_info)
        if _simulate_runtime_loads():
            runtime = SimulatedModelRuntime(runtime_type).load(model_path)
        else:
            runtime = ModelLoader().load_model(model_path, runtime_type, runtime_options)

        loaded = LoadedModel(
            model_id=model_info.id,
            runtime=runtime,
            model_info=model_info,
            loaded_at=datetime.now(),
            runtime_type=runtime_type,
            runtime_options=runtime_options,
        )
        self._warmup(loaded)
        return loaded

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

    def _runtime_options_for_model(self, model_info: CachedModel) -> Dict[str, Any]:
        """Build runtime-specific loader options from package metadata."""
        constraints = model_info.metadata.get("runtime_constraints", {}) or {}
        runtime_options = model_info.metadata.get("runtime_options", {}) or {}
        options: Dict[str, Any] = {}
        capabilities = detect_runtime_capabilities()
        capability_dict = self._capabilities_to_dict(capabilities)
        if _simulate_runtime_loads():
            capability_dict = self._simulated_capabilities_for_model(
                capability_dict,
                constraints,
            )
        defaults = runtime_defaults_for_profile(
            capability_dict.get("device_profile"),
            capability_dict,
        )

        constraints_ok, reasons = runtime_constraints_satisfied(
            constraints,
            capability_dict,
        )
        if not constraints_ok:
            raise RuntimeError(
                "Runtime constraints are not satisfied for "
                f"{model_info.id}: {'; '.join(reasons)}"
            )

        if model_info.format == ModelFormat.ONNX:
            providers = (
                runtime_options.get("providers")
                or constraints.get("provider_order")
                or constraints.get("preferred_providers")
                or constraints.get("providers")
                or defaults.get("onnx_providers")
            )
            if providers:
                available = capability_dict.get("runtimes", {}).get("onnxruntime", {}).get(
                    "providers", []
                )
                selected = [provider for provider in providers if provider in available]
                if not selected:
                    raise RuntimeError(
                        "No requested ONNX providers are available for "
                        f"{model_info.id}: {providers}"
                    )
                options["providers"] = selected

        if model_info.format == ModelFormat.TFLITE:
            num_threads = (
                runtime_options.get("num_threads")
                or constraints.get("num_threads")
                or defaults.get("tflite_num_threads")
            )
            if num_threads:
                options["num_threads"] = int(num_threads)

        if model_info.format == ModelFormat.TENSORRT:
            tensorrt_status = capability_dict.get("runtimes", {}).get("tensorrt", {})
            if not tensorrt_status.get("available", False):
                raise RuntimeError(
                    "TensorRT runtime is not available for "
                    f"{model_info.id}. Install NVIDIA TensorRT bindings or deploy "
                    "the package to a TensorRT-capable runtime target."
                )

        return options

    def _simulated_capabilities_for_model(
        self,
        capabilities: Dict[str, Any],
        constraints: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Overlay required model runtimes for explicit simulation-only loads."""
        simulated = dict(capabilities)
        runtimes = dict(simulated.get("runtimes") or {})
        required_runtimes = constraints.get("runtimes") or []
        for runtime_name in required_runtimes:
            runtime_key = str(runtime_name)
            current = dict(runtimes.get(runtime_key) or {})
            current["available"] = True
            if runtime_key in {"onnx", "onnxruntime"}:
                current.setdefault("providers", ["CPUExecutionProvider"])
                runtimes.setdefault("onnxruntime", current)
                runtimes.setdefault("onnx", current)
            else:
                runtimes[runtime_key] = current
        simulated["runtimes"] = runtimes

        profiles = constraints.get("device_profiles") or []
        if profiles:
            simulated["device_profile"] = str(profiles[0])
        return simulated

    def _capabilities_to_dict(
        self,
        capabilities: RuntimeCapabilities | Dict[str, Any] | Any,
    ) -> Dict[str, Any]:
        """Return runtime capability data as a plain dict for compatibility checks."""
        if isinstance(capabilities, RuntimeCapabilities):
            return capabilities.to_dict()
        if isinstance(capabilities, dict):
            return capabilities
        return {
            "device_profile": getattr(capabilities, "device_profile", None),
            "runtimes": getattr(capabilities, "runtimes", {}),
            "accelerators": getattr(capabilities, "accelerators", {}),
        }

    async def infer(
        self,
        slot_name: str,
        model_id: Optional[str],
        input_data: bytes,
        content_type: str = "application/octet-stream",
    ) -> List[Any]:
        """
        Run inference on a slot's currently-serving model.

        Copy-on-read: under a brief lock it grabs a reference to the loaded
        model, then releases the lock and runs inference on that reference. A
        concurrent hot-swap only reassigns the slot's pointer, so:

        - A request is served by whichever model is loaded when it is admitted;
          it never errors because a swap is in progress (the old model serves
          until the atomic pointer swap, the new one after).
        - The instance a request began on stays alive for the whole request:
          the local reference below keeps it (and its native session) from being
          freed until the request returns.

        ``model_id`` is an attribution hint, not a hard gate — the request is
        served by whatever is loaded.

        Raises:
            RuntimeError: If the slot has no model loaded at all.
        """
        slot_runtime = self._get_slot_runtime(slot_name)

        def _infer() -> List[Any]:
            with slot_runtime.lock:
                loaded = slot_runtime.loaded_model
                if loaded is None:
                    raise RuntimeError(f"No model loaded in slot {slot_name}")

            # Run inference WITHOUT holding the lock. `loaded` keeps the instance
            # alive across a concurrent swap.
            input_name = self._get_model_input_name(loaded.runtime)
            processed_input = self._preprocess_input(
                input_data,
                content_type,
                loaded.model_info,
                input_name=input_name,
            )
            outputs = loaded.runtime.infer(processed_input)
            loaded.inference_count += 1
            loaded.last_inference = datetime.now()
            return self._postprocess_output(outputs)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _infer)

    def _warmup(self, loaded: LoadedModel) -> None:
        """Run a single warmup inference so the model serves warm.

        Builds a synthetic input from the runtime's declared input shape (or the
        model metadata) and runs it through the runtime. Best-effort: a warmup
        that cannot be constructed or executed (e.g. a synthetic-input shape the
        model rejects) is logged and skipped rather than aborting activation.
        The first real request then pays the cold-start cost, and a genuinely
        broken model is caught by the normal inference fallback path.
        """
        import numpy as np

        input_name = self._get_model_input_name(loaded.runtime)
        shape = self._warmup_input_shape(loaded)
        try:
            arr = np.zeros(shape, dtype=np.float32)
            processed = self._format_processed_input(arr, loaded.model_info, input_name)
            loaded.runtime.infer(processed)
        except Exception as e:  # best-effort warmup
            logger.warning(
                f"Warmup inference skipped for {loaded.model_id} (shape {shape}): {e}"
            )
            return
        loaded.warmed = True
        loaded.inference_count += 1
        logger.info(f"Warmed model {loaded.model_id} with shape {shape}")

    def _warmup_input_shape(self, loaded: LoadedModel) -> List[int]:
        """Best-effort concrete input shape for a warmup inference.

        Prefers the runtime session's declared input shape (replacing dynamic
        dimensions with 1), falling back to the model's ``input_shape`` metadata
        and finally a generic NCHW image shape.
        """
        runtime = loaded.runtime
        try:
            session = getattr(runtime, "session", None)
            if session is not None:
                declared = session.get_inputs()[0].shape
                shape = [int(d) if isinstance(d, int) and d > 0 else 1 for d in declared]
                if shape:
                    return shape
        except Exception:
            pass
        declared = loaded.model_info.metadata.get("input_shape") or [1, 3, 224, 224]
        try:
            return [int(d) if int(d) > 0 else 1 for d in declared]
        except (TypeError, ValueError):
            return [1, 3, 224, 224]

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

                # ONNX/Torch models commonly use NCHW; TFLite commonly uses NHWC.
                input_layout = model_info.metadata.get("input_layout")
                use_nchw = input_layout == "NCHW" or (
                    input_layout is None and model_info.format != ModelFormat.TFLITE
                )
                if use_nchw and len(arr.shape) == 3:
                    arr = np.transpose(arr, (2, 0, 1))

                # Add batch dimension
                arr = np.expand_dims(arr, 0)

                return self._format_processed_input(
                    arr.astype(np.float32),
                    model_info,
                    input_name,
                )

            except ImportError:
                logger.warning("PIL not available, using raw bytes")
                arr = np.frombuffer(input_data, dtype=np.float32).reshape(input_shape)
                return self._format_processed_input(arr, model_info, input_name)

        else:
            # Generic binary data - assume numpy array
            arr = np.frombuffer(input_data, dtype=np.float32)
            if input_shape:
                arr = arr.reshape(input_shape)
            return self._format_processed_input(arr, model_info, input_name)

    def _format_processed_input(
        self,
        array: Any,
        model_info: CachedModel,
        input_name: str,
    ) -> Any:
        """Adapt a numpy array to the selected runtime's expected input shape."""
        if model_info.format == ModelFormat.ONNX:
            return {input_name: array}
        if model_info.format == ModelFormat.TORCHSCRIPT:
            try:
                import torch

                return torch.from_numpy(array)
            except ImportError:
                return array
        return array

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

        model_info, model_path = self._resolve_model_source(model_id)

        def _preload():
            # Build + warm exactly as activation does (honors the simulation
            # runtime flag), so a preloaded swap and a direct load are identical.
            loaded = self._construct_loaded_model(model_info, model_path)
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
                "runtime_type": loaded.runtime_type.value if loaded else None,
                "runtime_options": loaded.runtime_options if loaded else {},
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
