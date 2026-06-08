"""
Tests for the adaptive inference controller.
"""

from unittest.mock import AsyncMock

import pytest

from temms.controller import AdaptiveInferenceController
from temms.core.cache import ModelFormat


def _add_model(model_cache, model_storage, sample_model_file, model_id, name):
    dest_path, sha256, size = model_storage.store_model(
        sample_model_file,
        model_id,
        verify=True,
    )
    return model_cache.add_cached_model(
        model_id=model_id,
        name=name,
        version="1.0.0",
        format=ModelFormat.ONNX,
        path=dest_path,
        sha256=sha256,
        size_bytes=size,
        package_id="test-package",
    )


@pytest.fixture
def controller_system(
    model_cache,
    model_storage,
    slot_manager,
    condition_store,
    policy_engine,
    sample_model_file,
    sample_policy_yaml,
):
    _add_model(model_cache, model_storage, sample_model_file, "model-normal-v1", "test-model")
    _add_model(model_cache, model_storage, sample_model_file, "model-tiny-v1", "test-model-tiny")
    policy_engine.load_policy_from_file(sample_policy_yaml)
    slot_manager.create_slot(
        name="vision",
        description="Vision",
        required=True,
        default_model="test-model",
    )
    slot_manager.activate_model(
        slot_name="vision",
        model_id="model-normal-v1",
        trigger_type="startup",
        trigger_detail="default_model",
    )
    condition_store.set(
        path="platform.compute.cpu_temp_c",
        value=81,
        source="sensor",
        priority=100,
    )
    runtime = AsyncMock()
    runtime.load_model = AsyncMock(return_value=True)
    controller = AdaptiveInferenceController(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
        inference_runtime=runtime,
    )
    return {
        "controller": controller,
        "runtime": runtime,
        "slot_manager": slot_manager,
    }


@pytest.mark.asyncio
async def test_controller_applies_policy_selected_model(controller_system):
    decision = await controller_system["controller"].evaluate_slot("vision", apply=True)

    assert decision.status == "activated"
    assert decision.selected_model == "model-tiny-v1"
    assert decision.activated_model == "model-tiny-v1"
    assert decision.applied is True
    assert controller_system["slot_manager"].get_slot("vision").active_model_id == "model-tiny-v1"
    controller_system["runtime"].load_model.assert_awaited_with("vision", "model-tiny-v1")


@pytest.mark.asyncio
async def test_controller_previews_without_applying(controller_system):
    decision = await controller_system["controller"].evaluate_slot("vision", apply=False)

    assert decision.status == "selected"
    assert decision.selected_model == "model-tiny-v1"
    assert decision.applied is False
    assert controller_system["slot_manager"].get_slot("vision").active_model_id == "model-normal-v1"
    controller_system["runtime"].load_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_controller_falls_back_when_selected_model_fails(controller_system):
    async def load_model(slot_name, model_id):
        if model_id == "model-tiny-v1":
            raise RuntimeError("runtime missing accelerator")
        return True

    controller_system["runtime"].load_model.side_effect = load_model

    decision = await controller_system["controller"].evaluate_slot("vision", apply=True)

    assert decision.status == "fallback_activated"
    assert decision.selected_model == "model-tiny-v1"
    assert decision.activated_model == "model-normal-v1"
    assert decision.fallback_attempted == ["model-normal-v1"]
    assert controller_system["slot_manager"].get_slot("vision").active_model_id == "model-normal-v1"
