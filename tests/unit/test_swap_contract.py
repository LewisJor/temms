"""Swap-contract Tier 1 tests: in-flight hot-swap semantics.

These exercise the guarantees in docs/swap-contract.md:
- a request is always served by whichever model is loaded when it is admitted,
  never erroring because a swap is in progress;
- the model instance a request began on is never unloaded mid-flight;
- results are attributed to the model that actually served them;
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


def _fire(runtime, model="model-a"):
    """Submit one inference request against the vision slot."""
    return runtime.infer_result("vision", model, INPUT, "application/octet-stream")

pytestmark = pytest.mark.asyncio

_SIM_CAPS = SimpleNamespace(
    device_profile="x86_64-cpu",
    runtimes={"onnxruntime": {"available": True, "providers": ["CPUExecutionProvider"]}},
    accelerators={},
)


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


async def test_infer_attributes_to_currently_loaded_model(sim_runtime):
    await sim_runtime.load_model("vision", "model-a")
    await sim_runtime.load_model("vision", "model-b")

    # Caller still believes model-a is active, but model-b now serves. The
    # request must succeed and be attributed to the model that served it.
    result = await sim_runtime.infer_result("vision", "model-a", INPUT, "application/octet-stream")
    assert result.model_id == "model-b"
    assert result.expected_model_id == "model-a"
    assert result.swapped_during_request is True


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

    # Fire a burst of concurrent requests while the swap is mid-flight.
    during = await asyncio.gather(
        *[_fire(sim_runtime) for _ in range(25)]
    )
    # Old model still serves; nothing errored.
    assert all(r.model_id == "model-a" for r in during)

    gate.set()
    await swap

    after = await asyncio.gather(
        *[_fire(sim_runtime) for _ in range(25)]
    )
    assert all(r.model_id == "model-b" for r in after)


async def test_in_flight_request_drains_before_old_model_unload(sim_runtime, monkeypatch):
    await sim_runtime.load_model("vision", "model-a")
    slot_rt = sim_runtime._get_slot_runtime("vision")
    old = slot_rt.loaded_model
    assert old is not None

    unloaded = threading.Event()
    infer_started = threading.Event()
    release_infer = threading.Event()

    original_unload = old.runtime.unload

    def tracked_unload():
        unloaded.set()
        return original_unload()

    monkeypatch.setattr(old.runtime, "unload", tracked_unload)

    original_infer = old.runtime.infer

    def slow_infer(processed):
        infer_started.set()
        assert release_infer.wait(timeout=5), "infer gate never released"
        return original_infer(processed)

    monkeypatch.setattr(old.runtime, "infer", slow_infer)

    # Start a request on model-a and let it enter the (blocked) inference.
    req = asyncio.create_task(
        sim_runtime.infer_result("vision", "model-a", INPUT, "application/octet-stream")
    )
    assert await asyncio.get_running_loop().run_in_executor(None, infer_started.wait, 5)

    # Swap to model-b while the model-a request is still executing.
    await sim_runtime.load_model("vision", "model-b")
    assert old.retired is True
    # model-a must NOT have been unloaded yet — a request is still in flight.
    assert not unloaded.is_set()

    # Let the in-flight request finish; only then may model-a be unloaded.
    release_infer.set()
    result = await req
    assert result.model_id == "model-a"
    assert await asyncio.get_running_loop().run_in_executor(None, unloaded.wait, 5)
