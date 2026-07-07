#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-connected-lab}"

HUB_URL="${HUB_URL:-}"
ONLINE_EDGE_URL="${ONLINE_EDGE_URL:-}"
AIRGAP_EDGE_URL="${AIRGAP_EDGE_URL:-}"

SLOT="${SLOT:-vision}"
ONLINE_DEVICE_ID="${ONLINE_DEVICE_ID:-edge-online}"
AIRGAP_DEVICE_ID="${AIRGAP_DEVICE_ID:-edge-airgap}"
ONLINE_DEVICE_PROFILE="${ONLINE_DEVICE_PROFILE:-x86_64-cpu}"
AIRGAP_DEVICE_PROFILE="${AIRGAP_DEVICE_PROFILE:-rpi5-tflite}"
ONLINE_PACKAGE_ID="${ONLINE_PACKAGE_ID:-}"
AIRGAP_PACKAGE_ID="${AIRGAP_PACKAGE_ID:-}"
ONLINE_PACKAGE_PATH="${ONLINE_PACKAGE_PATH:-}"
AIRGAP_PACKAGE_PATH="${AIRGAP_PACKAGE_PATH:-}"
ONLINE_PACKAGE_HOST_PATH="${ONLINE_PACKAGE_HOST_PATH:-${ONLINE_PACKAGE_PATH}}"
AIRGAP_PACKAGE_HOST_PATH="${AIRGAP_PACKAGE_HOST_PATH:-${AIRGAP_PACKAGE_PATH}}"
ONLINE_ROLLOUT_ID="${ONLINE_ROLLOUT_ID:-rollout-online}"
AIRGAP_ROLLOUT_ID="${AIRGAP_ROLLOUT_ID:-rollout-airgap}"

ACCEPTANCE_DIR="${ACCEPTANCE_DIR:-./temms-mvp-acceptance}"
BUNDLE_PATH="${BUNDLE_PATH:-${ACCEPTANCE_DIR}/hub-lite-airgap-bundle.json}"
ONLINE_EVIDENCE_PATH="${ONLINE_EVIDENCE_PATH:-${ACCEPTANCE_DIR}/online-edge-evidence.json}"
AIRGAP_EVIDENCE_PATH="${AIRGAP_EVIDENCE_PATH:-${ACCEPTANCE_DIR}/airgap-edge-evidence.json}"
DEPLOYMENT_STATUS_PATH="${DEPLOYMENT_STATUS_PATH:-${ACCEPTANCE_DIR}/central-deployment-status.json}"
SUMMARY_PATH="${SUMMARY_PATH:-${ACCEPTANCE_DIR}/acceptance-summary.json}"
WAIT_SECONDS="${WAIT_SECONDS:-120}"
POLL_SECONDS="${POLL_SECONDS:-5}"

AUTH_TOKEN="${AUTH_TOKEN:-${TEMMS_API_TOKEN:-}}"
HUB_TOKEN="${HUB_TOKEN:-${TEMMS_HUB_TOKEN:-${AUTH_TOKEN}}}"
ONLINE_EDGE_TOKEN="${ONLINE_EDGE_TOKEN:-${AUTH_TOKEN}}"
AIRGAP_EDGE_TOKEN="${AIRGAP_EDGE_TOKEN:-${AUTH_TOKEN}}"

usage() {
    cat <<'EOF'
TEMMS multi-VM MVP acceptance harness.

Modes:
  connected-lab   Run central Hub + online edge + air-gap edge over reachable URLs.
  export-airgap   Register/enroll/assign on Hub and write the air-gap bundle.
  import-airgap   Import a pre-staged bundle on an edge VM, apply rollout, export evidence.

Required for connected-lab/export-airgap:
  HUB_URL
  ONLINE_PACKAGE_ID, ONLINE_PACKAGE_PATH
  AIRGAP_PACKAGE_ID, AIRGAP_PACKAGE_PATH

Required for connected-lab:
  ONLINE_EDGE_URL, AIRGAP_EDGE_URL

Required for import-airgap:
  AIRGAP_EDGE_URL
  AIRGAP_PACKAGE_ID

Optional:
  AUTH_TOKEN, HUB_TOKEN, ONLINE_EDGE_TOKEN, AIRGAP_EDGE_TOKEN
  ONLINE_PACKAGE_HOST_PATH, AIRGAP_PACKAGE_HOST_PATH
  ONLINE_DEVICE_ID=edge-online, AIRGAP_DEVICE_ID=edge-airgap
  ONLINE_DEVICE_PROFILE=x86_64-cpu, AIRGAP_DEVICE_PROFILE=rpi5-tflite
  ONLINE_ROLLOUT_ID=rollout-online, AIRGAP_ROLLOUT_ID=rollout-airgap
  SLOT=vision, ACCEPTANCE_DIR=./temms-mvp-acceptance
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

require_var() {
    local name="$1"
    local value="${!name:-}"
    if [ -z "${value}" ]; then
        die "${name} is required"
    fi
}

base_url() {
    local url="${1%/}"
    url="${url%/v1/hub}"
    url="${url%/v1}"
    printf '%s' "${url}"
}

hub_api() {
    printf '%s/v1/hub' "$(base_url "$1")"
}

curl_json() {
    local method="$1"
    local url="$2"
    local token="$3"
    local data="${4:-}"
    local args=(-sS -X "${method}" -H "Content-Type: application/json")
    local response_file
    local status
    response_file="$(mktemp)"
    if [ -n "${token}" ]; then
        args+=(-H "X-TEMMS-Token: ${token}")
    fi
    if [ -n "${data}" ]; then
        args+=(-d "${data}")
    fi
    status="$(curl "${args[@]}" -w "%{http_code}" -o "${response_file}" "${url}")"
    if [ "${status}" -lt 200 ] || [ "${status}" -ge 300 ]; then
        echo "error: ${method} ${url} returned HTTP ${status}" >&2
        cat "${response_file}" >&2
        echo >&2
        rm -f "${response_file}"
        return 22
    fi
    cat "${response_file}"
    rm -f "${response_file}"
}

curl_json_file() {
    local method="$1"
    local url="$2"
    local token="$3"
    local path="$4"
    local args=(-sS -X "${method}" -H "Content-Type: application/json")
    local response_file
    local status
    response_file="$(mktemp)"
    if [ -n "${token}" ]; then
        args+=(-H "X-TEMMS-Token: ${token}")
    fi
    args+=("--data-binary" "@${path}")
    status="$(curl "${args[@]}" -w "%{http_code}" -o "${response_file}" "${url}")"
    if [ "${status}" -lt 200 ] || [ "${status}" -ge 300 ]; then
        echo "error: ${method} ${url} returned HTTP ${status}" >&2
        cat "${response_file}" >&2
        echo >&2
        rm -f "${response_file}"
        return 22
    fi
    cat "${response_file}"
    rm -f "${response_file}"
}

json_payload() {
    python3 - "$@" <<'PY'
import json
import sys

payload = {}
for item in sys.argv[1:]:
    key, value = item.split("=", 1)
    if value == "true":
        payload[key] = True
    elif value == "false":
        payload[key] = False
    elif value.startswith("[") or value.startswith("{"):
        payload[key] = json.loads(value)
    else:
        payload[key] = value
print(json.dumps(payload))
PY
}

health_check() {
    local url="$1"
    curl -fsS "$(base_url "${url}")/v1/health" >/dev/null
}

rollout_state() {
    local url="$1"
    local token="$2"
    local rollout_id="$3"
    curl_json GET "$(hub_api "${url}")/rollouts" "${token}" |
        python3 -c 'import json,sys; rid=sys.argv[1]; data=json.load(sys.stdin); print(next((r.get("state","") for r in data.get("rollouts",[]) if r.get("rollout_id")==rid), ""))' "${rollout_id}"
}

wait_rollout_state() {
    local url="$1"
    local token="$2"
    local rollout_id="$3"
    local expected="$4"
    local elapsed=0
    local state=""
    while [ "${elapsed}" -le "${WAIT_SECONDS}" ]; do
        state="$(rollout_state "${url}" "${token}" "${rollout_id}")"
        if [ "${state}" = "${expected}" ]; then
            echo "rollout ${rollout_id} reached ${expected}"
            return 0
        fi
        sleep "${POLL_SECONDS}"
        elapsed=$((elapsed + POLL_SECONDS))
    done
    die "rollout ${rollout_id} did not reach ${expected}; last state=${state}"
}

register_package() {
    local hub="$1"
    local package_id="$2"
    local package_path="$3"
    local profile="$4"
    local host_path="$5"
    [ -f "${host_path}" ] || die "package does not exist on this VM: ${host_path}"
    local profiles
    profiles="$(python3 -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "${profile}")"
    local payload
    payload="$(json_payload \
        "package_path=${package_path}" \
        "require_signature=true" \
        "sign=false" \
        "device_profiles=${profiles}")"
    curl_json POST "$(hub_api "${hub}")/packages/register" "${HUB_TOKEN}" "${payload}" >/dev/null
    echo "registered package ${package_id} from ${package_path}"
}

promote_package() {
    local hub="$1"
    local package_id="$2"
    local state
    local payload
    for state in validated approved released; do
        payload="$(json_payload \
            "state=${state}" \
            "actor=operator:acceptance" \
            "reason=package ${state} for acceptance rollout")"
        curl_json POST "$(hub_api "${hub}")/packages/${package_id}/promote" "${HUB_TOKEN}" "${payload}" >/dev/null
    done
    echo "promoted package ${package_id} to released"
}

enroll_device() {
    local hub="$1"
    local device_id="$2"
    local profile="$3"
    local mode="$4"
    local labels
    local inventory
    labels="$(python3 -c 'import json,sys; print(json.dumps({"mode": sys.argv[1]}))' "${mode}")"
    case "${profile}" in
        x86_64-cpu)
            inventory='{"schema_version":"temms-device-inventory/v1","simulated":true,"device_profile":"x86_64-cpu","os":"linux","arch":"amd64","runtimes":{"onnxruntime":{"available":true,"providers":["CPUExecutionProvider"]}},"accelerators":{}}'
            ;;
        rpi5-tflite)
            inventory='{"schema_version":"temms-device-inventory/v1","simulated":true,"device_profile":"rpi5-tflite","os":"linux","arch":"arm64","runtimes":{"onnxruntime":{"available":true,"providers":["CPUExecutionProvider"]},"tflite_runtime":{"available":true,"options":{"num_threads":4}}},"accelerators":{}}'
            ;;
        *)
            inventory='{"schema_version":"temms-device-inventory/v1","simulated":true,"runtimes":{},"accelerators":{}}'
            ;;
    esac
    local payload
    payload="$(json_payload \
        "device_id=${device_id}" \
        "profile=${profile}" \
        "labels=${labels}" \
        "inventory=${inventory}")"
    curl_json POST "$(hub_api "${hub}")/devices/enroll" "${HUB_TOKEN}" "${payload}" >/dev/null
    echo "enrolled ${device_id} (${profile})"
}

assign_rollout() {
    local hub="$1"
    local device_id="$2"
    local package_id="$3"
    local rollout_id="$4"
    local payload
    payload="$(json_payload \
        "device_id=${device_id}" \
        "package_id=${package_id}" \
        "slot=${SLOT}" \
        "rollout_id=${rollout_id}")"
    curl_json POST "$(hub_api "${hub}")/rollouts" "${HUB_TOKEN}" "${payload}" >/dev/null
    echo "assigned ${rollout_id}: ${package_id} -> ${device_id}/${SLOT}"
}

prepare_hub() {
    require_var HUB_URL
    require_var ONLINE_PACKAGE_ID
    require_var AIRGAP_PACKAGE_ID
    require_var ONLINE_PACKAGE_PATH
    require_var AIRGAP_PACKAGE_PATH
    health_check "${HUB_URL}"
    enroll_device "${HUB_URL}" "${ONLINE_DEVICE_ID}" "${ONLINE_DEVICE_PROFILE}" "online"
    enroll_device "${HUB_URL}" "${AIRGAP_DEVICE_ID}" "${AIRGAP_DEVICE_PROFILE}" "airgap"
    register_package "${HUB_URL}" "${ONLINE_PACKAGE_ID}" "${ONLINE_PACKAGE_PATH}" "${ONLINE_DEVICE_PROFILE}" "${ONLINE_PACKAGE_HOST_PATH}"
    register_package "${HUB_URL}" "${AIRGAP_PACKAGE_ID}" "${AIRGAP_PACKAGE_PATH}" "${AIRGAP_DEVICE_PROFILE}" "${AIRGAP_PACKAGE_HOST_PATH}"
    promote_package "${HUB_URL}" "${ONLINE_PACKAGE_ID}"
    promote_package "${HUB_URL}" "${AIRGAP_PACKAGE_ID}"
    assign_rollout "${HUB_URL}" "${ONLINE_DEVICE_ID}" "${ONLINE_PACKAGE_ID}" "${ONLINE_ROLLOUT_ID}"
    assign_rollout "${HUB_URL}" "${AIRGAP_DEVICE_ID}" "${AIRGAP_PACKAGE_ID}" "${AIRGAP_ROLLOUT_ID}"
}

export_airgap_bundle() {
    mkdir -p "${ACCEPTANCE_DIR}"
    curl_json POST "$(hub_api "${HUB_URL}")/airgap/export" "${HUB_TOKEN}" '{"include_packages":true}' > "${BUNDLE_PATH}"
    echo "wrote air-gap bundle: ${BUNDLE_PATH}"
}

import_airgap_bundle() {
    require_var AIRGAP_EDGE_URL
    require_var AIRGAP_PACKAGE_ID
    [ -f "${BUNDLE_PATH}" ] || die "bundle does not exist: ${BUNDLE_PATH}"
    health_check "${AIRGAP_EDGE_URL}"
    curl_json_file POST "$(hub_api "${AIRGAP_EDGE_URL}")/airgap/import" "${AIRGAP_EDGE_TOKEN}" "${BUNDLE_PATH}" >/dev/null
    local apply_payload
    apply_payload="$(json_payload "require_signature=true")"
    curl_json POST "$(hub_api "${AIRGAP_EDGE_URL}")/rollouts/${AIRGAP_ROLLOUT_ID}/apply" "${AIRGAP_EDGE_TOKEN}" "${apply_payload}" >/dev/null
    curl_json POST "$(hub_api "${AIRGAP_EDGE_URL}")/evidence/export" "${AIRGAP_EDGE_TOKEN}" '{}' > "${AIRGAP_EVIDENCE_PATH}"
    echo "air-gap edge applied ${AIRGAP_ROLLOUT_ID}; evidence: ${AIRGAP_EVIDENCE_PATH}"
}

write_summary() {
    local mode="$1"
    mkdir -p "${ACCEPTANCE_DIR}"
    export ACCEPTANCE_MODE="${mode}"
    export HUB_URL ONLINE_EDGE_URL AIRGAP_EDGE_URL
    export ONLINE_DEVICE_ID AIRGAP_DEVICE_ID
    export ONLINE_DEVICE_PROFILE AIRGAP_DEVICE_PROFILE
    export ONLINE_PACKAGE_ID AIRGAP_PACKAGE_ID
    export ONLINE_ROLLOUT_ID AIRGAP_ROLLOUT_ID
    export BUNDLE_PATH ONLINE_EVIDENCE_PATH AIRGAP_EVIDENCE_PATH
    export DEPLOYMENT_STATUS_PATH SUMMARY_PATH
    python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def read_json(path):
    candidate = Path(path)
    if not candidate.exists():
        return None
    return json.loads(candidate.read_text(encoding="utf-8"))


def rollout_state_from_evidence(evidence, rollout_id):
    if not evidence:
        return None
    rollouts = (evidence.get("hub_lite") or {}).get("rollouts") or {}
    rollout = rollouts.get(rollout_id)
    if isinstance(rollout, dict):
        return rollout.get("state")
    return None


def rollout_state_from_central(deployment_status, rollout_id):
    rollouts = (deployment_status or {}).get("rollouts") or {}
    rollout = rollouts.get(rollout_id)
    if isinstance(rollout, dict):
        return rollout.get("state")
    return None


def check(name, actual, expected):
    if expected is None:
        return {
            "name": name,
            "actual": actual,
            "expected": None,
            "passed": actual is not None,
        }
    return {
        "name": name,
        "actual": actual,
        "expected": expected,
        "passed": actual == expected,
    }


deployment_status = read_json(os.environ["DEPLOYMENT_STATUS_PATH"])
online_evidence = read_json(os.environ["ONLINE_EVIDENCE_PATH"])
airgap_evidence = read_json(os.environ["AIRGAP_EVIDENCE_PATH"])
online_rollout_id = os.environ["ONLINE_ROLLOUT_ID"]
airgap_rollout_id = os.environ["AIRGAP_ROLLOUT_ID"]

mode = os.environ["ACCEPTANCE_MODE"]
online_state = (
    rollout_state_from_evidence(online_evidence, online_rollout_id)
    or rollout_state_from_central(deployment_status, online_rollout_id)
)
airgap_state = (
    rollout_state_from_evidence(airgap_evidence, airgap_rollout_id)
    or rollout_state_from_central(deployment_status, airgap_rollout_id)
)
expected_online = "rolled_back" if mode == "connected-lab" else None
expected_airgap = "activated" if mode in {"connected-lab", "import-airgap"} else None
checks = [
    check("online_rollout_state", online_state, expected_online),
    check("airgap_rollout_state", airgap_state, expected_airgap),
]

summary = {
    "schema_version": "temms-mvp-acceptance/v1",
    "mode": mode,
    "result": "passed" if all(item["passed"] for item in checks) else "needs_attention",
    "checks": checks,
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "hub_url": os.environ.get("HUB_URL") or None,
    "edges": {
        "online": {
            "url": os.environ.get("ONLINE_EDGE_URL") or None,
            "device_id": os.environ["ONLINE_DEVICE_ID"],
            "device_profile": os.environ["ONLINE_DEVICE_PROFILE"],
            "package_id": os.environ.get("ONLINE_PACKAGE_ID") or None,
            "rollout_id": online_rollout_id,
            "state": online_state,
            "evidence_path": (
                os.environ["ONLINE_EVIDENCE_PATH"]
                if Path(os.environ["ONLINE_EVIDENCE_PATH"]).exists()
                else None
            ),
        },
        "airgap": {
            "url": os.environ.get("AIRGAP_EDGE_URL") or None,
            "device_id": os.environ["AIRGAP_DEVICE_ID"],
            "device_profile": os.environ["AIRGAP_DEVICE_PROFILE"],
            "package_id": os.environ.get("AIRGAP_PACKAGE_ID") or None,
            "rollout_id": airgap_rollout_id,
            "state": airgap_state,
            "evidence_path": (
                os.environ["AIRGAP_EVIDENCE_PATH"]
                if Path(os.environ["AIRGAP_EVIDENCE_PATH"]).exists()
                else None
            ),
        },
    },
    "artifacts": {
        "airgap_bundle": (
            os.environ["BUNDLE_PATH"] if Path(os.environ["BUNDLE_PATH"]).exists() else None
        ),
        "central_deployment_status": (
            os.environ["DEPLOYMENT_STATUS_PATH"]
            if Path(os.environ["DEPLOYMENT_STATUS_PATH"]).exists()
            else None
        ),
        "online_edge_evidence": (
            os.environ["ONLINE_EVIDENCE_PATH"]
            if Path(os.environ["ONLINE_EVIDENCE_PATH"]).exists()
            else None
        ),
        "airgap_edge_evidence": (
            os.environ["AIRGAP_EVIDENCE_PATH"]
            if Path(os.environ["AIRGAP_EVIDENCE_PATH"]).exists()
            else None
        ),
    },
}

Path(os.environ["SUMMARY_PATH"]).write_text(
    json.dumps(summary, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
    echo "acceptance summary: ${SUMMARY_PATH}"
}

run_connected_lab() {
    require_var ONLINE_EDGE_URL
    require_var AIRGAP_EDGE_URL
    mkdir -p "${ACCEPTANCE_DIR}"
    prepare_hub
    health_check "${ONLINE_EDGE_URL}"
    health_check "${AIRGAP_EDGE_URL}"
    echo "waiting for online edge auto-apply; ensure TEMMS_HUB_AUTO_APPLY=true on ${ONLINE_DEVICE_ID}"
    wait_rollout_state "${HUB_URL}" "${HUB_TOKEN}" "${ONLINE_ROLLOUT_ID}" "activated"
    curl_json POST "$(hub_api "${ONLINE_EDGE_URL}")/rollouts/${ONLINE_ROLLOUT_ID}/rollback" "${ONLINE_EDGE_TOKEN}" '{"reason":"multi-vm acceptance"}' >/dev/null
    wait_rollout_state "${HUB_URL}" "${HUB_TOKEN}" "${ONLINE_ROLLOUT_ID}" "rolled_back"
    export_airgap_bundle
    import_airgap_bundle
    curl_json GET "$(hub_api "${HUB_URL}")/deployment-status" "${HUB_TOKEN}" > "${DEPLOYMENT_STATUS_PATH}"
    curl_json POST "$(hub_api "${ONLINE_EDGE_URL}")/evidence/export" "${ONLINE_EDGE_TOKEN}" '{}' > "${ONLINE_EVIDENCE_PATH}"
    write_summary "connected-lab"
    echo "central deployment status: ${DEPLOYMENT_STATUS_PATH}"
    echo "online edge evidence: ${ONLINE_EVIDENCE_PATH}"
    echo "TEMMS multi-VM MVP acceptance completed"
}

case "${MODE}" in
    connected-lab)
        run_connected_lab
        ;;
    export-airgap)
        prepare_hub
        export_airgap_bundle
        ;;
    import-airgap)
        mkdir -p "${ACCEPTANCE_DIR}"
        import_airgap_bundle
        write_summary "import-airgap"
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        die "unknown mode: ${MODE}"
        ;;
esac
