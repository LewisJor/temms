"""mission.yaml parsing + the keystone policy-reference check (#43 slice 1)."""

from __future__ import annotations

import textwrap

import pytest
import yaml

from temms.core.mission_spec import MissionSpecError, load_mission_spec

POLICY = textwrap.dedent(
    """
    apiVersion: temms/v1
    kind: SlotPolicy
    metadata: { name: weather-adaptive }
    spec:
      slot: vision
      default_model: daylight
      rules:
        - name: low-light
          priority: 70
          conditions:
            all:
              - metric: environmental.celestial.ambient
                operator: in
                value: [low, dark]
          action:
            switch_to: lowlight
      fallback_chain: [daylight, tiny]
    """
)


def _write_mission(tmp_path, models, *, with_policy=True, slot="vision"):
    if with_policy:
        (tmp_path / "weather.yaml").write_text(POLICY)
    spec = {
        "apiVersion": "temms/v1",
        "kind": "MissionPackage",
        "metadata": {"name": "vision", "version": "1.0.0"},
        "models": models,
        "slot": {"name": slot, **({"policy": "weather.yaml"} if with_policy else {})},
        "target": {"device_profiles": ["x86_64-cpu"], "runtime": "onnxruntime"},
    }
    path = tmp_path / "mission.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def _model(mid):
    return {"id": mid, "source": f"file://{mid}.onnx", "format": "onnx"}


def test_valid_spec_parses(tmp_path):
    path = _write_mission(tmp_path, [_model("daylight"), _model("lowlight"), _model("tiny")])
    spec = load_mission_spec(path)
    assert spec.model_ids() == ["daylight", "lowlight", "tiny"]
    assert spec.slot.name == "vision"


def test_envelope_fields_are_carried(tmp_path):
    models = [
        {
            "id": "daylight",
            "source": "file://d.onnx",
            "format": "onnx",
            "provides": "object-detection",
            "optimal_when": {"light": "bright"},
            "requires": {"memory_mb": 512},
        },
        _model("lowlight"),
        _model("tiny"),
    ]
    spec = load_mission_spec(_write_mission(tmp_path, models))
    daylight = spec.models[0]
    assert daylight.provides == "object-detection"
    assert daylight.optimal_when == {"light": "bright"}
    assert daylight.requires == {"memory_mb": 512}


def test_keystone_rejects_policy_referencing_absent_model(tmp_path):
    # 'tiny' is in the policy fallback_chain but not in models[].
    path = _write_mission(tmp_path, [_model("daylight"), _model("lowlight")])
    with pytest.raises(MissionSpecError, match="does not carry.*tiny"):
        load_mission_spec(path)


def test_keystone_passes_when_all_referenced_models_present(tmp_path):
    path = _write_mission(tmp_path, [_model("daylight"), _model("lowlight"), _model("tiny")])
    load_mission_spec(path)  # no raise


def test_policy_for_wrong_slot_is_rejected(tmp_path):
    path = _write_mission(
        tmp_path, [_model("daylight"), _model("lowlight"), _model("tiny")], slot="nav"
    )
    with pytest.raises(MissionSpecError, match="policy is for slot"):
        load_mission_spec(path)


def test_no_models_is_rejected(tmp_path):
    path = _write_mission(tmp_path, [], with_policy=False)
    with pytest.raises(MissionSpecError, match="at least one model"):
        load_mission_spec(path)


def test_duplicate_model_ids_rejected(tmp_path):
    path = _write_mission(
        tmp_path, [_model("daylight"), _model("daylight")], with_policy=False
    )
    with pytest.raises(MissionSpecError, match="duplicate model id"):
        load_mission_spec(path)


def test_wrong_kind_rejected(tmp_path):
    path = tmp_path / "mission.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "kind": "SlotPolicy",
                "metadata": {"name": "x"},
                "models": [_model("a")],
                "slot": {"name": "vision"},
            }
        )
    )
    with pytest.raises(MissionSpecError, match="kind must be MissionPackage"):
        load_mission_spec(path)


def test_missing_policy_file_is_reported(tmp_path):
    spec = {
        "kind": "MissionPackage",
        "metadata": {"name": "vision", "version": "1.0.0"},
        "models": [_model("a")],
        "slot": {"name": "vision", "policy": "absent.yaml"},
    }
    path = tmp_path / "mission.yaml"
    path.write_text(yaml.safe_dump(spec))
    with pytest.raises(MissionSpecError, match="slot policy not found"):
        load_mission_spec(path)


def test_no_policy_is_allowed(tmp_path):
    path = _write_mission(tmp_path, [_model("a")], with_policy=False)
    spec = load_mission_spec(path)
    assert spec.slot.policy is None
