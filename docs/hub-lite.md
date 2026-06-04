# Hub Lite

Hub Lite is the MVP control-plane layer for multi-VM TEMMS deployments. It is intentionally small: one JSON-backed store, exposed through the TEMMS API, with enough state to coordinate devices and rollouts without replacing MLflow.

Hub Lite responsibilities:

- Device enrollment
- Heartbeat and inventory
- Package catalog
- Runtime target catalog
- Rollout assignment
- Rollout lifecycle status
- Deployment status snapshots
- Air-gap export/import

Hub Lite does not train models and does not replace MLflow. MLflow remains the registry. TEMMS packages remain the deployment unit.

## Online Edge Sync

Each edge VM can sync to a central Hub Lite API by setting environment variables in `/etc/temms/temms.env`:

```ini
TEMMS_HUB_URL=http://hub-vm:8080
TEMMS_HUB_TOKEN=change-me
TEMMS_DEVICE_ID=edge-1
TEMMS_DEVICE_PROFILE=x86_64-cpu
TEMMS_HUB_SYNC_INTERVAL_S=30
```

When enabled, the daemon periodically enrolls the device, sends heartbeat/inventory/deployment status, mirrors central rollout assignments for its device ID into the local Hub Lite store, downloads package archives for assigned rollouts, and replays local rollout state transitions back to the hub. That replay preserves edge-side lifecycle history such as `downloading`, `imported`, and `activated`, not just the final state. Downloaded archives are cached beside the edge Hub Lite state and the local package catalog path is rewritten before apply. Leave `TEMMS_HUB_URL` unset for a fully local or air-gapped edge VM.

On each online sync, the edge preserves a previously downloaded package artifact when its `source_sha256` still matches the central catalog. Before reuse, it verifies the cached artifact SHA. If the artifact digest has drifted, TEMMS emits `hub.package_cache_mismatch` telemetry and downloads a fresh copy from Hub Lite. If the central `source_sha256` changes, the old cached artifact is discarded and the new package is fetched.

To let the edge VM execute assigned rollouts without a separate operator call, enable signed auto-apply:

```ini
TEMMS_HUB_AUTO_APPLY=true
TEMMS_ROLLOUT_REQUIRE_SIGNATURE=true
TEMMS_PACKAGE_SIGNING_KEY_FILE=/etc/temms/hub-signing.key
```

Auto-apply only acts on local rollouts in `assigned` state. If signature verification is required and no package signing key is configured, the local rollout is marked `failed` and that state is pushed back to Hub Lite on the next successful sync.

## API

Hub Lite routes live under `/v1/hub/*`. When `TEMMS_API_TOKEN` is configured, these routes require the same token as `/v1/control/*`. Web UI write actions use that same protection for slot overrides, condition injection, override clearing, and package import; UI package import also inherits the daemon package signature policy.

When the API is running inside the TEMMS daemon, `/v1/hub/packages/from-mlflow`, `/v1/hub/packages/register`, and `/v1/hub/rollouts/{id}/apply` inherit `TEMMS_ROLLOUT_REQUIRE_SIGNATURE` plus `TEMMS_PACKAGE_SIGNING_KEY` or `TEMMS_PACKAGE_SIGNING_KEY_FILE`. MLflow packaging and package registration sign artifacts with the daemon key before cataloging whenever signatures are required and a key is configured, then verify the resulting artifact metadata. Package registration and Hub-side MLflow packaging run strict production metadata validation by default, so catalog metadata records whether schemas, provenance, runtime constraints, and benchmark metadata were checked. Rollout assignment refuses catalog entries that do not carry verified signature metadata and, when daemon signature policy is enabled, strict metadata validation. Operators can still pass a signing key or set `strict_metadata` false in the request for one-off lab calls, but deployed agents use the daemon signature policy and strict catalog posture by default.

Package catalog registration plus rollout assignment, status, apply, and rollback history records include an `actor` field. Operators can send it as `X-TEMMS-Actor` or in the JSON body; online edge sync uses `edge:<device-id>`. TEMMS never derives actors from the bearer token itself, so evidence bundles can identify the operator or edge agent without storing secrets.

## CLI

The same MVP workflow is available through `temms hub`:

```bash
temms hub enroll \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --device-profile x86_64-cpu \
  --label site=lab \
  --inventory runtime=onnx

temms hub register-package ./dist/mlflow-detector-7.temms.tar.zst \
  --hub-url http://hub-vm:8080 \
  --token "$TEMMS_HUB_TOKEN"

temms hub package-from-mlflow models:/detector/7 \
  --hub-url http://hub-vm:8080 \
  --token "$TEMMS_HUB_TOKEN" \
  --slot vision \
  --tracking-uri http://mlflow.example:5000 \
  --device-profile x86_64-cpu \
  --runtime onnxruntime \
  --provider CPUExecutionProvider \
  --actor operator:alice

temms hub register-runtime \
  --hub-url http://hub-vm:8080 \
  --runtime-target-id customer-orin \
  --image registry.example.com/customer/orin-runtime:2026.06 \
  --device-profile orin-tensorrt \
  --runtime onnxruntime \
  --runtime tensorrt \
  --provider CUDAExecutionProvider \
  --accelerator nvidia \
  --actor operator:alice

temms hub validate-runtime ./dist/mlflow-detector-7.temms.tar.zst \
  --hub-url http://hub-vm:8080 \
  --runtime-target-id customer-orin \
  --signing-key-file ./hub-signing.key \
  --pull-image

temms hub validate-runtime ./dist/mlflow-detector-7.temms.tar.zst \
  --hub-url http://hub-vm:8080 \
  --runtime-target-id customer-orin \
  --allow-unsigned-package \
  --no-strict-metadata \
  --dry-run \
  --json

temms hub assign \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --package-id mlflow-detector-7 \
  --slot vision \
  --rollout-id rollout-vision-001 \
  --runtime-target-id customer-orin \
  --actor operator:alice

temms hub export \
  --hub-url http://hub-vm:8080 \
  --include-packages \
  --output hub-lite-package-bundle.json

temms hub import hub-lite-package-bundle.json \
  --hub-url http://edge-vm:8080

temms hub apply rollout-vision-001 \
  --hub-url http://edge-vm:8080 \
  --actor edge:edge-1
```

Use `temms hub devices`, `temms hub packages`, `temms hub runtime-targets`, `temms hub rollouts`, and `temms hub status` to inspect fleet state. Add `--json` to any command for scriptable output.
Hub CLI package registration, Hub MLflow packaging, runtime validation, and rollout apply require production metadata and signature verification by default; use `--no-strict-metadata` or `--allow-unsigned-package` only for isolated labs.
Use `preview-compatibility` before assignment to check package/device/runtime fit without creating a rollout:

```bash
temms hub preview-compatibility \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --package-id pkg-vision-1 \
  --runtime-target-id temms-x86_64-cpu
```

`register-package`, `package-from-mlflow`, and `validate-runtime` all use strict metadata by default. `validate-runtime` fetches the selected runtime target from Hub Lite and runs `temms package validate --check-runtime --strict-metadata` inside that target's container image. Use `--dry-run` to see the exact `docker run` command before executing it. Use `--no-strict-metadata` only for lab packages that predate the production metadata contract.
Each validation or dry-run is written back to Hub Lite as runtime validation evidence with the target image, package path or package ID, pass/fail state, actor, timestamp, and a redacted command. Inspect those records with `temms hub runtime-validations`; evidence exports include them in `runtime_validations` and in the merged audit timeline.
For stricter rollout control, pass `--package-id` when validating and `--require-runtime-validation` when assigning. Hub Lite then requires a non-dry-run passing validation record for that exact package artifact and runtime target before it creates the rollout.

For a local x86 VM or laptop rehearsal, build the same image tag used by the built-in `temms-x86_64-cpu` runtime target:

```bash
make docker-build-runtime

temms hub validate-runtime ./dist/pkg-vision.temms.tar.zst \
  --hub-url http://localhost:8080 \
  --package-id pkg-vision-1 \
  --runtime-target-id temms-x86_64-cpu \
  --allow-unsigned-package
```

The TEMMS container entrypoint executes explicit commands directly, so the validation container runs `temms package validate` instead of starting the daemon. Customer runtime images should follow the same contract: when Hub passes a command, execute it as the container process.

## HTTP API

Enroll a device:

```bash
curl -X POST http://localhost:8080/v1/hub/devices/enroll \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "edge-1",
    "profile": "x86_64-cpu",
    "labels": {"site": "lab"},
    "inventory": {"python": "3.11", "runtime": "onnx"}
  }'
```

Send a heartbeat:

```bash
curl -X POST http://localhost:8080/v1/hub/devices/edge-1/heartbeat \
  -H "Content-Type: application/json" \
  -d '{
    "status": "online",
    "inventory": {"runtime": "onnx"},
    "deployment_status": {"state": "READY"}
  }'
```

Build, Hub-sign, and register a package from the MLflow registry:

```bash
curl -X POST http://localhost:8080/v1/hub/packages/from-mlflow \
  -H "Content-Type: application/json" \
  -d '{
    "model_uri": "models:/detector/7",
    "slot": "vision",
    "tracking_uri": "http://mlflow.example:5000",
    "device_profile": "x86_64-cpu",
    "runtime_constraints": {"runtimes": ["onnx"]},
    "runtime_options": {"providers": ["CPUExecutionProvider"]},
    "archive": true
  }'
```

Hub writes generated artifacts under its local `packages/` state directory by default, signs them with signer `temms-hub-lite` when daemon signature policy is enabled, validates strict production metadata unless `"strict_metadata": false` is set for a lab package, and returns both the catalog entry and package path. Existing `models:/name/version` outputs are immutable unless the request sets `"overwrite": true`, which should be reserved for isolated lab rebuilds before registration or distribution.

Register and Hub-sign a package artifact directly from its manifest:

```bash
curl -X POST http://localhost:8080/v1/hub/packages/register \
  -H "Content-Type: application/json" \
  -d '{
    "package_path": "/packages/pkg-vision-1.temms.tar.zst",
    "require_signature": true
  }'
```

This derives `package_id`, name, version, source SHA256, compatible device profiles, model metadata, policy metadata, provenance, signature verification status, and strict metadata validation status from the package itself. If the Hub daemon is configured with `TEMMS_PACKAGE_SIGNING_KEY_FILE` or `TEMMS_PACKAGE_SIGNING_KEY` and signatures are required, registration first writes or replaces `signature.json` using signer `temms-hub-lite`, then catalogs the verified artifact. The catalog keeps `source_sha256` for the registered package source even when a directory package is later streamed as an archive. On deployed Hub VMs, keep the signing key in the daemon environment so the key does not travel in request JSON.

Edges that use online sync fetch assigned package bytes from:

```bash
curl -o pkg-vision-1.temms.tar.zst \
  http://localhost:8080/v1/hub/packages/pkg-vision-1/artifact
```

The response includes `X-TEMMS-Package-Filename`, `X-TEMMS-Package-SHA256`, `X-TEMMS-Package-Artifact-SHA256`, and `X-TEMMS-Package-Source-SHA256` headers. Directory packages are streamed as `.temms.tar.zst` archives, so the source SHA and artifact SHA may differ; both are carried into edge cache metadata and air-gap bundles for audit.

Before Hub Lite serves an online package artifact, embeds one in an air-gap bundle, or applies a rollout from a local package path, it re-checks the cataloged source SHA256. If the package file or directory changed after registration, distribution or apply fails with a conflict instead of using a different artifact under the old catalog entry.

You can still register a package catalog entry manually:

```bash
curl -X POST http://localhost:8080/v1/hub/packages \
  -H "Content-Type: application/json" \
  -d '{
    "package_id": "pkg-vision-1",
    "name": "vision-models",
    "version": "1.0.0",
    "path": "/packages/pkg-vision-1.temms",
    "device_profiles": ["x86_64-cpu"]
  }'
```

List runtime targets:

```bash
curl http://localhost:8080/v1/hub/runtime-targets
```

Hub Lite starts with default runtime targets for `x86_64-cpu`, `arm64-jetson`, `rpi5-tflite`, and `orin-tensorrt`. A runtime target is the container execution environment used for simulation or validation: image reference, OS, architecture, compatible device profiles, available runtimes, ONNX providers, accelerators, and optional labels. Customers can bring their own runtime images without changing package metadata:

```bash
curl -X POST http://localhost:8080/v1/hub/runtime-targets \
  -H "Content-Type: application/json" \
  -d '{
    "runtime_target_id": "customer-orin",
    "name": "Customer Orin Runtime",
    "image": "registry.example.com/customer/orin-runtime:2026.06",
    "os": "linux",
    "arch": "arm64",
    "device_profiles": ["orin-tensorrt"],
    "runtimes": {
      "onnxruntime": {
        "available": true,
        "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"]
      },
      "tensorrt": {"available": true}
    },
    "accelerators": {"nvidia": {"available": true}},
    "runtime_constraints": {
      "device_profiles": ["orin-tensorrt"],
      "runtimes": ["onnxruntime", "tensorrt"],
      "preferred_providers": ["CUDAExecutionProvider"],
      "accelerators": ["nvidia"]
    },
    "labels": {"customer": "acme"}
  }'
```

Assign a rollout:

```bash
curl -X POST http://localhost:8080/v1/hub/compatibility/preview \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "edge-1",
    "package_id": "pkg-vision-1",
    "runtime_target_id": "temms-x86_64-cpu"
  }'

curl -X POST http://localhost:8080/v1/hub/rollouts \
  -H "Content-Type: application/json" \
  -d '{
    "rollout_id": "rollout-1",
    "device_id": "edge-1",
    "package_id": "pkg-vision-1",
    "slot": "vision",
    "runtime_target_id": "temms-x86_64-cpu",
    "require_runtime_validation": true
  }'
```

If the package catalog entry declares `device_profiles`, Hub Lite checks that the enrolled device profile is included before creating the assignment. When the assignment names a `runtime_target_id`, Hub Lite checks package constraints against that container target's declared runtimes, ONNX providers, accelerators, OS/arch metadata, and compatible device profiles. When `require_runtime_validation` is true, Hub Lite also requires a passing non-dry-run validation record for the selected package/runtime target and embeds a compact validation summary in the rollout. Without a runtime target, Hub Lite falls back to the device heartbeat inventory. Rollout apply repeats package-level and model-level runtime constraint checks on the edge before import or activation, which keeps air-gap/manual paths aligned with online assignment policy. This keeps `orin-tensorrt`, `tflite`, or customer-provided runtime images from being assigned to incompatible VMs by accident.

Update rollout state:

```bash
curl -X POST http://localhost:8080/v1/hub/rollouts/rollout-1/status \
  -H "Content-Type: application/json" \
  -d '{"state": "activated", "detail": "loaded on edge-1"}'
```

Valid rollout states:

- `assigned`
- `downloading`
- `imported`
- `activated`
- `failed`
- `rolled_back`

Apply a rollout on the edge agent:

```bash
curl -X POST http://localhost:8080/v1/hub/rollouts/rollout-1/apply \
  -H "Content-Type: application/json" \
  -d '{
    "require_signature": true
  }'
```

Apply reads the package catalog entry, imports the TEMMS package from its local path, promotes package policies, loads the selected model into the rollout slot, activates the slot, and moves rollout state through:

```text
assigned -> downloading -> imported -> activated
```

On failure the rollout is moved to `failed` with the error detail. If no slot is set on the rollout, apply stops after import and leaves the rollout in `imported`.

Export an air-gap bundle:

```bash
curl -X POST http://localhost:8080/v1/hub/airgap/export > hub-lite-bundle.json
```

Export an air-gap bundle that embeds signed package archives:

```bash
curl -X POST http://localhost:8080/v1/hub/airgap/export \
  -H "Content-Type: application/json" \
  -d '{"include_packages": true}' \
  > hub-lite-package-bundle.json
```

When `include_packages` is true, Hub Lite embeds package artifacts from catalog entries that have a readable `path`. Directory packages are archived as `.temms.tar.zst` for transfer. On import, artifacts are written under the receiving Hub Lite state directory in `packages/`, package catalog paths are rewritten to those local files, and SHA256 is checked before the bundle is accepted. Bundle import is conflict-aware: records missing locally are added, newer incoming records replace older local records, but stale bundle records do not overwrite newer local rollout, deployment, or package artifact state. Rollout histories are merged so an edge can import an older central assignment bundle after local activation without losing its `activated` audit trail.

Import an air-gap bundle:

```bash
curl -X POST http://localhost:8080/v1/hub/airgap/import \
  -H "Content-Type: application/json" \
  --data-binary @hub-lite-bundle.json
```

Export a post-mission evidence bundle:

```bash
curl -X POST http://localhost:8080/v1/hub/evidence/export \
  -H "Content-Type: application/json" \
  -d '{"decision_limit": 500, "telemetry_limit": 5000, "include_benchmarks": true}' \
  > temms-evidence-bundle.json
```

The evidence bundle combines Hub Lite fleet state, current deployment status, centrally replayed telemetry, doctor-style diagnostics, slots, runtime state, condition snapshot, imported package/model metadata, package import audit events, rollout history with actors, decision logs with package/provenance metadata and package signature verification context, local telemetry events, local benchmark artifacts, Hub-recorded benchmark evidence, and a merged timeline. Policy-driven decision logs include the matched policy, matched rule, rule priority, action, and per-condition evidence with actual value, source, priority, confidence, and match result. Diagnostics include write-probed path health and model cache health, including missing cached model files, size mismatches, and SHA256 mismatches.
Runtime target validation records are included as first-class evidence so operators can prove that a package was preflighted against a customer or default runtime image before assignment or deployment.
Hardware benchmark records are also first-class Hub evidence. Publish them from an edge with `temms benchmark ... --hub-url http://hub-vm:8080 --device-id edge-1 --package-id pkg-vision-1 --runtime-target-id temms-x86_64-cpu`, then inspect central results with `temms hub benchmarks`.
The Hub UI `Preview Evidence` action renders the same bundle as an operator summary before the raw JSON: fleet counts, package signature and strict metadata posture, recent "why models switched" cards with matched condition evidence, and a merged mission timeline.

Air-gapped edges can also replay raw telemetry bundles into Hub Lite after a mission:

```bash
temms hub replay-telemetry telemetry-bundle.json \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --actor operator:post-mission
```

## Edge Rollback

Rollback is exposed on the edge agent because the agent owns local slot state:

```bash
curl -X POST http://localhost:8080/v1/control/slots/vision/rollback
```

For Hub-managed rollouts, target the rollout directly:

```bash
curl -X POST http://localhost:8080/v1/hub/rollouts/rollout-1/rollback \
  -H "Content-Type: application/json" \
  -d '{"reason": "operator requested rollback"}'
```

The same operation is available from the CLI:

```bash
temms hub rollback rollout-1 --hub-url http://edge-vm:8080
```

The agent selects the previous model from the slot decision log, loads it, activates it, records a `rollback` decision, emits telemetry, and moves the targeted Hub Lite rollout to `rolled_back`.

## Storage

The daemon stores Hub Lite state beside the local TEMMS database as `hub_lite.json`. For non-root/local test runs, this path follows the configured data directory rather than assuming `/var/lib/temms`.
