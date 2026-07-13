#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/deploy/docker-compose.acceptance.yml}"
ACCEPTANCE_DIR="${ACCEPTANCE_DIR:-${ROOT_DIR}/temms-mvp-acceptance}"
PACKAGE_DIR="${TEMMS_ACCEPTANCE_PACKAGE_DIR:-${ROOT_DIR}/dist/acceptance-packages}"
SIGNING_KEY="${TEMMS_ACCEPTANCE_SIGNING_KEY:-temms-acceptance-secret}"
TOKEN="${TEMMS_ACCEPTANCE_TOKEN:-temms-acceptance-token}"
HUB_PORT="${TEMMS_ACCEPTANCE_HUB_PORT:-18080}"
ONLINE_PORT="${TEMMS_ACCEPTANCE_ONLINE_PORT:-18081}"
AIRGAP_PORT="${TEMMS_ACCEPTANCE_AIRGAP_PORT:-18082}"
ONLINE_PACKAGE_ID="${ONLINE_PACKAGE_ID:-pkg-online-x86}"
AIRGAP_PACKAGE_ID="${AIRGAP_PACKAGE_ID:-pkg-airgap-rpi}"
ONLINE_MODEL_ID="${ONLINE_MODEL_ID:-model-online-v1}"
AIRGAP_MODEL_ID="${AIRGAP_MODEL_ID:-model-airgap-v1}"
ONLINE_ROLLOUT_ID="${ONLINE_ROLLOUT_ID:-rollout-online}"
AIRGAP_ROLLOUT_ID="${AIRGAP_ROLLOUT_ID:-rollout-airgap}"
PYTHON_CMD_TEXT="${PYTHON_CMD:-python3}"
KEEP_CONTAINERS="${KEEP_CONTAINERS:-false}"

read -r -a PYTHON_CMD <<< "${PYTHON_CMD_TEXT}"

cleanup() {
    if [ "${KEEP_CONTAINERS}" != "true" ]; then
        docker compose -f "${COMPOSE_FILE}" down >/dev/null
    fi
}

reset_stack() {
    docker compose -f "${COMPOSE_FILE}" down -v >/dev/null
}

require_docker() {
    if ! docker compose version >/dev/null 2>&1; then
        echo "error: Docker Compose is not available. Install Docker with the compose plugin and rerun make docker-acceptance." >&2
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "error: Docker daemon is not reachable. Start Docker Desktop or the Docker daemon and rerun make docker-acceptance." >&2
        exit 1
    fi
}

wait_health() {
    local url="$1"
    local name="$2"
    local elapsed=0
    local timeout="${WAIT_SECONDS:-180}"
    local sleep_s="${POLL_SECONDS:-5}"
    while [ "${elapsed}" -le "${timeout}" ]; do
        if curl -fsS "${url}/v1/health" >/dev/null 2>&1; then
            echo "${name} healthy at ${url}"
            return 0
        fi
        sleep "${sleep_s}"
        elapsed=$((elapsed + sleep_s))
    done
    echo "error: ${name} did not become healthy at ${url}" >&2
    return 1
}

create_acceptance_packages() {
    mkdir -p "${PACKAGE_DIR}"
    PACKAGE_DIR="${PACKAGE_DIR}" \
    ROOT_DIR="${ROOT_DIR}" \
    SIGNING_KEY="${SIGNING_KEY}" \
    ONLINE_PACKAGE_ID="${ONLINE_PACKAGE_ID}" \
    AIRGAP_PACKAGE_ID="${AIRGAP_PACKAGE_ID}" \
    ONLINE_MODEL_ID="${ONLINE_MODEL_ID}" \
    AIRGAP_MODEL_ID="${AIRGAP_MODEL_ID}" \
    "${PYTHON_CMD[@]}" - <<'PY'
import hashlib
import json
import os
import shutil
from pathlib import Path

from temms.core.package_archive import create_package_archive
from temms.core.signing import sign_package


def package(package_dir, *, package_id, model_id, profile, model_format):
    if package_dir.exists():
        shutil.rmtree(package_dir)
    models_dir = package_dir / "models"
    models_dir.mkdir(parents=True)
    ext = "tflite" if model_format == "tflite" else "onnx"
    filename = f"model.{ext}"
    if model_format == "onnx":
        example_models = Path(os.environ["ROOT_DIR"]) / "examples" / "package-example" / "models"
        source_model = (
            example_models / "mobilenet-v2-tiny.onnx"
            if "airgap" in package_id
            else example_models / "yolov8n-daylight.onnx"
        )
        model_bytes = source_model.read_bytes()
    else:
        model_bytes = f"{package_id}:{model_id}".encode("utf-8")
    model_sha256 = hashlib.sha256(model_bytes).hexdigest()
    runtime_device_profiles = [profile]
    if profile in {"x86_64-cpu", "rpi5-tflite"}:
        runtime_device_profiles.append("arm64-cpu")
    (models_dir / filename).write_bytes(model_bytes)
    manifest = {
        "schema_version": "v1",
        "package_id": package_id,
        "name": package_id,
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "compatibility": {"device_profiles": [profile]},
        "models": [
            {
                "id": model_id,
                "name": model_id,
                "version": "1.0.0",
                "format": model_format,
                "filename": filename,
                "sha256": model_sha256,
                "size_bytes": len(model_bytes),
                "input_schema": {
                    "shape": [1, 3, 224, 224],
                    "dtype": "float32",
                    "source": "docker-acceptance",
                },
                "output_schema": {
                    "shape": [1, 1000],
                    "dtype": "float32",
                    "source": "docker-acceptance",
                },
                "runtime_constraints": {
                    "device_profiles": runtime_device_profiles,
                    "runtimes": [
                        "onnxruntime" if model_format == "onnx" else "tflite_runtime"
                    ],
                },
                "benchmark": {
                    "available": False,
                    "reason": "acceptance package uses static fixture model",
                    "source": "docker-acceptance",
                },
                "provenance": {
                    "source": "docker-acceptance",
                    "run_id": f"acceptance-{package_id}",
                    "artifact_sha256": model_sha256,
                    "artifact_path": f"models/{filename}",
                },
            }
        ],
        "policies": [],
    }
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    sign_package(package_dir, os.environ["SIGNING_KEY"], signer="temms-acceptance")
    archive = create_package_archive(package_dir)
    return archive


root = Path(os.environ["PACKAGE_DIR"])
online = package(
    root / f"{os.environ['ONLINE_PACKAGE_ID']}.temms",
    package_id=os.environ["ONLINE_PACKAGE_ID"],
    model_id=os.environ["ONLINE_MODEL_ID"],
    profile="x86_64-cpu",
    model_format="onnx",
)
airgap = package(
    root / f"{os.environ['AIRGAP_PACKAGE_ID']}.temms",
    package_id=os.environ["AIRGAP_PACKAGE_ID"],
    model_id=os.environ["AIRGAP_MODEL_ID"],
    profile="rpi5-tflite",
    model_format="tflite",
)
print(online)
print(airgap)
PY
}

main() {
    cd "${ROOT_DIR}"
    require_docker
    mkdir -p "${ACCEPTANCE_DIR}"
    create_acceptance_packages
    reset_stack

    TEMMS_ACCEPTANCE_PACKAGE_DIR="${PACKAGE_DIR}" \
    TEMMS_ACCEPTANCE_SIGNING_KEY="${SIGNING_KEY}" \
    TEMMS_ACCEPTANCE_TOKEN="${TOKEN}" \
    TEMMS_ACCEPTANCE_HUB_PORT="${HUB_PORT}" \
    TEMMS_ACCEPTANCE_ONLINE_PORT="${ONLINE_PORT}" \
    TEMMS_ACCEPTANCE_AIRGAP_PORT="${AIRGAP_PORT}" \
    docker compose -f "${COMPOSE_FILE}" up --build -d

    trap cleanup EXIT

    wait_health "http://localhost:${HUB_PORT}" "hub"
    wait_health "http://localhost:${ONLINE_PORT}" "online edge"
    wait_health "http://localhost:${AIRGAP_PORT}" "airgap edge"

    HUB_URL="http://localhost:${HUB_PORT}" \
    ONLINE_EDGE_URL="http://localhost:${ONLINE_PORT}" \
    AIRGAP_EDGE_URL="http://localhost:${AIRGAP_PORT}" \
    ONLINE_PACKAGE_ID="${ONLINE_PACKAGE_ID}" \
    AIRGAP_PACKAGE_ID="${AIRGAP_PACKAGE_ID}" \
    ONLINE_MODEL_ID="${ONLINE_MODEL_ID}" \
    AIRGAP_MODEL_ID="${AIRGAP_MODEL_ID}" \
    ONLINE_PACKAGE_PATH="/acceptance-packages/${ONLINE_PACKAGE_ID}.temms.tar.zst" \
    AIRGAP_PACKAGE_PATH="/acceptance-packages/${AIRGAP_PACKAGE_ID}.temms.tar.zst" \
    ONLINE_ROLLOUT_ID="${ONLINE_ROLLOUT_ID}" \
    AIRGAP_ROLLOUT_ID="${AIRGAP_ROLLOUT_ID}" \
    AUTH_TOKEN="${TOKEN}" \
    ACCEPTANCE_DIR="${ACCEPTANCE_DIR}" \
    ONLINE_PACKAGE_HOST_PATH="${PACKAGE_DIR}/${ONLINE_PACKAGE_ID}.temms.tar.zst" \
    AIRGAP_PACKAGE_HOST_PATH="${PACKAGE_DIR}/${AIRGAP_PACKAGE_ID}.temms.tar.zst" \
    "${ROOT_DIR}/deploy/multi-vm-acceptance.sh" connected-lab

    echo "container acceptance summary: ${ACCEPTANCE_DIR}/acceptance-summary.json"
}

main "$@"
