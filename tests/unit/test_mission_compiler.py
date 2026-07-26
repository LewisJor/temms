"""Compile mission.yaml -> signed package (#43 slice 1)."""

from __future__ import annotations

import json
import textwrap

import pytest
import yaml

from temms.core import signing
from temms.core.mission_compiler import compile_mission_package
from temms.core.mission_spec import MissionSpecError

POLICY = textwrap.dedent(
    """
    apiVersion: temms/v1
    kind: SlotPolicy
    metadata: { name: weather-adaptive }
    spec:
      slot: vision
      default_model: daylight
      rules: []
      fallback_chain: [daylight, tiny]
    """
)


@pytest.fixture
def mission(tmp_path):
    """A self-contained mission.yaml with file:// artifacts and a policy."""
    for mid in ("daylight", "tiny"):
        (tmp_path / f"{mid}.onnx").write_bytes(f"onnx-{mid}".encode())
    (tmp_path / "weather.yaml").write_text(POLICY)
    spec = {
        "kind": "MissionPackage",
        "metadata": {"name": "vision", "version": "2.1.0", "description": "test"},
        "models": [
            {
                "id": "daylight",
                "source": "file://daylight.onnx",
                "format": "onnx",
                "provides": "object-detection",
                "optimal_when": {"light": "bright"},
                "requires": {"memory_mb": 512},
            },
            {
                "id": "tiny",
                "source": "file://tiny.onnx",
                "format": "onnx",
                "optimal_when": {},
                "requires": {"memory_mb": 96},
            },
        ],
        "slot": {"name": "vision", "policy": "weather.yaml"},
        "target": {"device_profiles": ["x86_64-cpu", "arm64-cpu"], "runtime": "onnxruntime"},
    }
    path = tmp_path / "mission.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def test_compiles_all_models_with_digests(mission, tmp_path):
    pkg = compile_mission_package(mission, tmp_path / "out")
    manifest = json.loads((pkg / "manifest.json").read_text())

    assert manifest["package_id"] == "vision-2-1-0"
    ids = [m["id"] for m in manifest["models"]]
    assert ids == ["daylight", "tiny"]
    for entry in manifest["models"]:
        artifact = pkg / entry["filename"]
        assert artifact.is_file()
        assert entry["sha256"] == signing.sha256_file(artifact)
        assert entry["size_bytes"] == artifact.stat().st_size


def test_carries_envelopes_and_provenance(mission, tmp_path):
    pkg = compile_mission_package(mission, tmp_path / "out")
    manifest = json.loads((pkg / "manifest.json").read_text())
    daylight = next(m for m in manifest["models"] if m["id"] == "daylight")

    assert daylight["provides"] == "object-detection"
    assert daylight["optimal_when"] == {"light": "bright"}
    assert daylight["requires"] == {"memory_mb": 512}
    assert daylight["provenance"]["registry"] == "file"


def test_target_and_policy_are_recorded(mission, tmp_path):
    pkg = compile_mission_package(mission, tmp_path / "out")
    manifest = json.loads((pkg / "manifest.json").read_text())

    assert manifest["target"]["device_profiles"] == ["x86_64-cpu", "arm64-cpu"]
    assert manifest["policies"][0]["slot"] == "vision"
    assert (pkg / manifest["policies"][0]["filename"]).is_file()


def test_signed_package_verifies_over_the_whole_set(mission, tmp_path):
    private_pem, public_pem, _ = signing.generate_ed25519_keypair()
    pkg = compile_mission_package(mission, tmp_path / "out", signing_key=private_pem)

    meta = signing.verify_package_signature(pkg, public_pem)
    assert meta["algorithm"] == "Ed25519"

    # The signature covers every model artifact: tampering with any fails verify.
    tampered = next((pkg / "models").rglob("*.onnx"))
    tampered.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hashes do not match"):
        signing.verify_package_signature(pkg, public_pem)


def test_invalid_spec_leaves_no_partial_package(tmp_path):
    """Validation runs before anything is written."""
    (tmp_path / "d.onnx").write_bytes(b"x")
    (tmp_path / "weather.yaml").write_text(POLICY)  # references daylight + tiny
    spec = {
        "kind": "MissionPackage",
        "metadata": {"name": "vision", "version": "1.0.0"},
        "models": [{"id": "daylight", "source": "file://d.onnx", "format": "onnx"}],
        "slot": {"name": "vision", "policy": "weather.yaml"},  # 'tiny' not carried
    }
    mission = tmp_path / "mission.yaml"
    mission.write_text(yaml.safe_dump(spec))
    out = tmp_path / "out"

    with pytest.raises(MissionSpecError):
        compile_mission_package(mission, out)
    assert not out.exists() or not any(out.iterdir())


def test_overwrite_required_to_replace(mission, tmp_path):
    out = tmp_path / "out"
    compile_mission_package(mission, out)
    with pytest.raises(FileExistsError):
        compile_mission_package(mission, out)
    compile_mission_package(mission, out, overwrite=True)  # succeeds
