"""Swap-contract tests: minimal in-flight hot-swap semantics.

The swap is a thin primitive over the runtime (see docs/swap-contract.md): build
the new instance, warm it, atomically switch the slot pointer, and let the old
instance be freed once its in-flight requests finish. These tests pin the
behavior that must hold for any in-process adapter:

- a request is served by whichever model is loaded when it is admitted, never
  erroring because a swap is in progress;
- a request that began before a swap completes successfully (its instance is not
  torn down mid-flight);
- a new model is warmed before it becomes the serving instance.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from temms.core.cache import ModelFormat
from temms.inference.runtime import InferenceRuntime, SimulatedModelRuntime

# 48 float32 values = model input_shape (1, 3, 4, 4); raw-binary path, no PIL.
INPUT = b"\x00" * (48 * 4)

pytestmark = pytest.mark.asyncio

_SIM_CAPS = SimpleNamespace(
    device_profile="x86_64-cpu",
    runtimes={"onnxruntime": {"available": True, "providers": ["CPUExecutionProvider"]}},
    accelerators={},
)


def _fire(runtime, model="model-a"):
    """Submit one inference request against the vision slot."""
    return runtime.infer("vision", model, INPUT, "application/octet-stream")


def _add_model(model_cache, model_storage, temp_dir, model_id, shape=(1, 3, 4, 4)):
    model_file = temp_dir / f"{model_id}.onnx"
    model_file.write_bytes(b"onnx-" + model_id.encode())
    dest_path, sha256, size = model_storage.store_model(model_file, model_id)
    model_cache.add_cached_model(
        model_id=model_id,
        name=model_id,
        version="1",
        format=ModelFormat.ONNX,
        path=dest_path,
        sha256=sha256,
        size_bytes=size,
        package_id="pkg",
        metadata={
            "input_shape": list(shape),
            "runtime_constraints": {
                "device_profiles": ["x86_64-cpu"],
                "runtimes": ["onnxruntime"],
            },
        },
    )


@pytest.fixture
def sim_runtime(model_cache, model_storage, temp_dir, monkeypatch):
    monkeypatch.setenv("TEMMS_INFERENCE_SIMULATE_RUNTIME", "1")
    monkeypatch.setattr(
        "temms.inference.runtime.detect_runtime_capabilities", lambda: _SIM_CAPS
    )
    _add_model(model_cache, model_storage, temp_dir, "model-a")
    _add_model(model_cache, model_storage, temp_dir, "model-b")
    return InferenceRuntime(model_cache, model_storage, max_workers=8)


async def test_load_warms_model_before_serving(sim_runtime):
    await sim_runtime.load_model("vision", "model-a")
    loaded = sim_runtime._get_slot_runtime("vision").loaded_model
    assert loaded is not None
    assert loaded.warmed is True
    # The warmup inference ran against the model instance.
    assert loaded.inference_count >= 1


async def test_infer_serves_loaded_model_after_swap(sim_runtime):
    await sim_runtime.load_model("vision", "model-a")
    await sim_runtime.load_model("vision", "model-b")

    # A caller still passing the stale expected id must be served, not errored.
    predictions = await _fire(sim_runtime, "model-a")
    assert predictions == []  # simulated runtime returns no predictions
    info = sim_runtime.get_slot_info("vision")
    assert info["model_id"] == "model-b"


async def test_no_error_window_while_swap_in_progress(sim_runtime, monkeypatch):
    await sim_runtime.load_model("vision", "model-a")

    gate = threading.Event()
    original_load = SimulatedModelRuntime.load

    def gated_load(self, model_path):
        # Block only while loading the incoming model-b so the swap is
        # observably in progress while we fire requests at the slot.
        if "model-b" in str(model_path):
            assert gate.wait(timeout=5), "load gate never released"
        return original_load(self, model_path)

    monkeypatch.setattr(SimulatedModelRuntime, "load", gated_load)

    swap = asyncio.create_task(sim_runtime.load_model("vision", "model-b"))
    await asyncio.sleep(0.05)  # let the swap begin and block inside load

    # 25 concurrent requests fired while the swap is mid-flight — none may error.
    during = await asyncio.gather(*[_fire(sim_runtime) for _ in range(25)])
    assert all(p == [] for p in during)  # served, no exceptions

    gate.set()
    await swap
    assert sim_runtime.get_slot_info("vision")["model_id"] == "model-b"

    after = await asyncio.gather(*[_fire(sim_runtime) for _ in range(25)])
    assert all(p == [] for p in after)


async def test_preload_honors_simulation_runtime(sim_runtime):
    # preload_model shares the model-construction path with load_model, so it
    # honors TEMMS_INFERENCE_SIMULATE_RUNTIME (before this was fixed it always
    # used the real loader and errored on simulated models).
    await sim_runtime.preload_model("vision", "model-a")
    preloaded = sim_runtime._preloaded["model-a"]
    assert isinstance(preloaded.runtime, SimulatedModelRuntime)
    assert preloaded.warmed is True

    # Activation consumes the preloaded, already-warm instance.
    await sim_runtime.load_model("vision", "model-a")
    assert "model-a" not in sim_runtime._preloaded
    assert sim_runtime.get_slot_info("vision")["model_id"] == "model-a"


async def test_in_flight_request_completes_across_swap(sim_runtime, monkeypatch):
    await sim_runtime.load_model("vision", "model-a")
    old = sim_runtime._get_slot_runtime("vision").loaded_model
    assert old is not None

    infer_started = threading.Event()
    release_infer = threading.Event()
    original_infer = old.runtime.infer

    def slow_infer(processed):
        infer_started.set()
        assert release_infer.wait(timeout=5), "infer gate never released"
        return original_infer(processed)

    monkeypatch.setattr(old.runtime, "infer", slow_infer)

    # Start a request on model-a and let it enter the (blocked) inference.
    req = asyncio.create_task(_fire(sim_runtime, "model-a"))
    assert await asyncio.get_running_loop().run_in_executor(None, infer_started.wait, 5)

    # Swap to model-b while the model-a request is still executing.
    await sim_runtime.load_model("vision", "model-b")

    # The in-flight request must still complete cleanly on model-a's instance,
    # which was not torn down by the swap.
    release_infer.set()
    predictions = await req
    assert predictions == []
    assert sim_runtime.get_slot_info("vision")["model_id"] == "model-b"
