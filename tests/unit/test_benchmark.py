"""
Tests for hardware-aware benchmark output.
"""

from types import SimpleNamespace

from temms.benchmark import run_benchmark_sync


def test_benchmark_records_throughput_and_selected_runtime(
    model_cache,
    model_storage,
    sample_cached_model,
    monkeypatch,
):
    class FakeInferenceRuntime:
        def __init__(self, model_cache, model_storage, max_workers=1):
            self.slot_name = None
            self.model_id = None
            self.shutdown_called = False

        async def load_model(self, slot_name, model_id):
            self.slot_name = slot_name
            self.model_id = model_id
            return True

        async def infer(self, slot_name, model_id, input_data, content_type):
            assert slot_name == self.slot_name
            assert model_id == self.model_id
            assert input_data
            assert content_type == "application/octet-stream"
            return [{"ok": True}]

        def get_slot_info(self, slot_name):
            assert slot_name == self.slot_name
            return {
                "runtime_type": "onnx",
                "runtime_options": {"providers": ["CPUExecutionProvider"]},
            }

        def shutdown(self):
            self.shutdown_called = True

    monkeypatch.setattr("temms.benchmark.InferenceRuntime", FakeInferenceRuntime)
    monkeypatch.setattr(
        "temms.benchmark.detect_runtime_capabilities",
        lambda: SimpleNamespace(
            to_dict=lambda: {
                "device_profile": "x86_64-cpu",
                "runtimes": {"onnxruntime": {"available": True}},
            }
        ),
    )

    result = run_benchmark_sync(
        model_cache,
        model_storage,
        sample_cached_model.id,
        samples=2,
        warmup=1,
    )

    assert result["schema_version"] == "temms-benchmark/v1"
    assert result["samples"] == 2
    assert result["throughput"]["samples"] == 2
    assert result["throughput"]["total_latency_ms"] > 0
    assert result["throughput"]["inferences_per_second"] > 0
    assert result["runtime"] == {
        "type": "onnx",
        "options": {"providers": ["CPUExecutionProvider"]},
    }
    assert result["capabilities"]["device_profile"] == "x86_64-cpu"
