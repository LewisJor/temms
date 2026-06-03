"""
Hardware-aware local benchmarking for cached edge models.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from temms.core.cache import ModelCache, CachedModel
from temms.core.runtime_profiles import detect_runtime_capabilities
from temms.core.storage import ModelStorage
from temms.inference.runtime import InferenceRuntime


def build_synthetic_input(model: CachedModel) -> bytes:
    """Create zero-filled float32 input bytes from cached model metadata."""
    import numpy as np

    shape = _input_shape(model.metadata)
    dtype = model.metadata.get("input_dtype", "float32")
    array = np.zeros(shape, dtype=dtype)
    return array.tobytes()


async def benchmark_cached_model(
    model_cache: ModelCache,
    model_storage: ModelStorage,
    model_id_or_name: str,
    slot_name: str = "benchmark",
    samples: int = 5,
    warmup: int = 1,
    content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """Load and benchmark a cached model on the local device."""
    model = model_cache.get_model(model_id_or_name) or model_cache.find_model(model_id_or_name)
    if model is None:
        raise ValueError(f"Model not found in cache: {model_id_or_name}")

    runtime = InferenceRuntime(model_cache, model_storage, max_workers=1)
    input_data = build_synthetic_input(model)
    total_runs = max(samples, 1) + max(warmup, 0)
    latencies: list[float] = []
    load_started = time.perf_counter()

    try:
        await runtime.load_model(slot_name, model.id)
        load_latency_ms = (time.perf_counter() - load_started) * 1000
        runtime_info = runtime.get_slot_info(slot_name)

        for index in range(total_runs):
            started = time.perf_counter()
            await runtime.infer(
                slot_name=slot_name,
                model_id=model.id,
                input_data=input_data,
                content_type=content_type,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            if index >= warmup:
                latencies.append(latency_ms)
    finally:
        runtime.shutdown()

    return {
        "schema_version": "temms-benchmark/v1",
        "model_id": model.id,
        "model_name": model.name,
        "model_version": model.version,
        "model_format": model.format.value,
        "slot": slot_name,
        "samples": len(latencies),
        "warmup": warmup,
        "input_shape": _input_shape(model.metadata),
        "load_latency_ms": load_latency_ms,
        "latency_ms": _latency_stats(latencies),
        "throughput": _throughput_stats(latencies),
        "runtime": {
            "type": runtime_info.get("runtime_type"),
            "options": runtime_info.get("runtime_options", {}),
        },
        "capabilities": detect_runtime_capabilities().to_dict(),
        "created_at": datetime.utcnow().isoformat() + "Z",
    }


def write_benchmark_result(result: dict[str, Any], output_path: Path) -> Path:
    """Write benchmark result JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return output_path


def run_benchmark_sync(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Synchronous wrapper for CLI callers."""
    return asyncio.run(benchmark_cached_model(*args, **kwargs))


def _input_shape(metadata: dict[str, Any]) -> list[int]:
    if metadata.get("input_shape"):
        return [int(value) for value in metadata["input_shape"]]
    schema = metadata.get("input_schema") or {}
    if isinstance(schema, dict):
        shape = schema.get("shape")
        if shape:
            return [int(value) for value in shape]
    return [1, 3, 224, 224]


def _latency_stats(latencies: list[float]) -> dict[str, float]:
    if not latencies:
        return {"min": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}

    sorted_latencies = sorted(latencies)
    p95_index = min(len(sorted_latencies) - 1, int(len(sorted_latencies) * 0.95))
    return {
        "min": min(latencies),
        "mean": statistics.fmean(latencies),
        "p50": statistics.median(latencies),
        "p95": sorted_latencies[p95_index],
        "max": max(latencies),
    }


def _throughput_stats(latencies: list[float]) -> dict[str, float | int]:
    samples = len(latencies)
    total_latency_ms = sum(latencies)
    if samples == 0 or total_latency_ms <= 0:
        inferences_per_second = 0.0
    else:
        inferences_per_second = samples / (total_latency_ms / 1000)
    return {
        "samples": samples,
        "total_latency_ms": total_latency_ms,
        "inferences_per_second": inferences_per_second,
    }
