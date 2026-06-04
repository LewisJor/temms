"""
Evidence bundle tests.
"""

import json

from temms.core.cache import ModelFormat
from temms.evidence import EvidenceBundleBuilder


def test_evidence_bundle_enriches_decision_with_model_and_package(
    model_cache,
    model_storage,
    slot_manager,
    condition_store,
    policy_engine,
    sample_model_file,
):
    package = model_cache.add_package(
        package_id="pkg-vision",
        name="vision-package",
        version="1.0.0",
        source="/mnt/usb/pkg-vision",
        manifest={"package_id": "pkg-vision", "signature_verified": True},
    )
    dest_path, sha256, size = model_storage.store_model(
        sample_model_file,
        "model-lowlight-v1",
        verify=True,
    )
    model_cache.add_cached_model(
        model_id="model-lowlight-v1",
        name="lowlight",
        version="1.0.0",
        format=ModelFormat.ONNX,
        path=dest_path,
        sha256=sha256,
        size_bytes=size,
        package_id=package.id,
        metadata={"runtime_constraints": {"runtimes": ["onnxruntime"]}},
    )
    slot_manager.create_slot(
        name="vision",
        description="Vision",
        required=True,
        default_model="daylight",
    )
    condition_store.set(
        path="environmental.visibility_m",
        value=40,
        source="operator",
        priority=1000,
    )
    slot_manager.activate_model(
        slot_name="vision",
        model_id="model-lowlight-v1",
        trigger_type="policy",
        trigger_detail="weather-adaptive/fog",
        conditions=condition_store.get_snapshot(),
    )

    bundle = EvidenceBundleBuilder(
        slot_manager=slot_manager,
        condition_store=condition_store,
        policy_engine=policy_engine,
        model_cache=model_cache,
    ).build(slot_name="vision")

    assert bundle["schema_version"] == "temms-evidence-bundle/v1"
    assert bundle["integrity"]["payload_sha256"]
    assert len(bundle["decisions"]) == 1
    decision = bundle["decisions"][0]
    assert decision["to_model"] == "model-lowlight-v1"
    assert decision["conditions_snapshot"]["environmental"]["visibility_m"] == 40
    assert decision["model_evidence"]["to_model"]["sha256"] == sha256
    assert decision["model_evidence"]["to_package"]["manifest"]["signature_verified"] is True

    # The bundle is portable JSON, not a Python-only object graph.
    json.dumps(bundle)
