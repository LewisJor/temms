# Edge Operations MVP

This is the minimum operator flow for running TEMMS across multiple edge VMs with different hardware profiles.

## 1. Detect VM capabilities

Run diagnostics on every target VM:

```bash
temms doctor --config /etc/temms/temms.yaml
temms doctor --config /etc/temms/temms.yaml --json > temms-doctor.json
```

Record the reported device profile, board model, runtimes, ONNX providers, accelerators, writable path status, API port status, security readiness, and model cache health. Writable path status is backed by an actual create/write/delete probe, which helps catch read-only mounts or ownership drift before rollout apply. Security readiness reports whether the control API token, Hub token source, rollout signature enforcement, and package signing key source are configured without printing secrets. Cache health should be `healthy`; a `degraded` result means at least one cached model file is missing, has the wrong size, or no longer matches its recorded SHA256. Canonical MVP profiles are:

- `x86_64-cpu`
- `arm64-jetson`
- `rpi5-tflite`
- `orin-tensorrt`

TEMMS normalizes common aliases such as `amd64-cpu`, `aarch64-jetson`, `raspberry_pi_5`, and `jetson-orin` to these canonical profiles before package compatibility checks. Set `TEMMS_DEVICE_PROFILE` explicitly on a VM when automatic board detection is not precise enough.

## 2. Build and sign packages from MLflow

Build deployment packages from MLflow registry entries:

```bash
temms package from-mlflow models:/detector/7 \
  --slot vision \
  --tracking-uri http://mlflow.example:5000 \
  --device-profile x86_64-cpu \
  --signing-key-file ./temms-signing.key \
  --output ./dist \
  --archive
```

For Hub-managed deployments, keep the production signing key on the Hub VM and
let Hub build from MLflow via `/v1/hub/packages/from-mlflow`, or register a
prebuilt artifact there with `/v1/hub/packages/register`. Both paths sign with
signer `temms-hub-lite` before cataloging when the Hub daemon has
`TEMMS_PACKAGE_SIGNING_KEY_FILE` or `TEMMS_PACKAGE_SIGNING_KEY` configured. The
Hub catalog path also runs strict metadata validation by default and records the
result beside signature verification; use `--no-strict-metadata` only for
isolated lab artifacts. The local `--signing-key-file` path is still useful for
development, direct edge imports, and pre-signing in CI.

Package metadata can include `runtime_constraints` such as required runtimes, ONNX providers, accelerators, or device profiles. It can also include `runtime_options`, for example an ONNX provider order:

```bash
temms package from-mlflow models:/detector/7 \
  --slot vision \
  --runtime-constraint 'runtimes=["onnx"]' \
  --runtime-constraint 'accelerators=["nvidia"]' \
  --runtime-option 'providers=["CUDAExecutionProvider","CPUExecutionProvider"]'
```

```json
{
  "runtime_constraints": {
    "runtimes": ["onnx"],
    "providers": ["CPUExecutionProvider"]
  },
  "runtime_options": {
    "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"]
  }
}
```

Hub Lite checks package/device compatibility at assignment time, rollout apply checks package-level and model-level runtime constraints before loading the model, and the edge runtime uses the selected provider options during model load. If a package does not declare provider options, TEMMS applies device-profile defaults: CPU VMs use `CPUExecutionProvider`, Jetson profiles prefer CUDA-capable ONNX providers, Orin prefers TensorRT then CUDA then CPU, and Raspberry Pi 5 defaults TFLite to four threads.

## 3. Import on an edge VM

```bash
temms import ./dist/mlflow-detector-7.temms.tar.zst \
  --signing-key-file ./temms-signing.key \
  --device-profile x86_64-cpu \
  --config /etc/temms/temms.yaml
```

Imports require signature verification and strict production metadata by default and are idempotent. Policies included in the package are promoted into the active policy directory. On systemd installs, `/etc/temms/policies` is owned by the `temms` service user so daemon-driven rollout apply and UI import can promote policies without root. Direct edge imports also enforce package device-profile and runtime constraints before anything is copied into the model cache. Use `--allow-unsigned-package --allow-lab-metadata` only for isolated labs.

The same signing-first default applies when validating or inspecting packages
before distribution:

```bash
temms package validate ./dist/mlflow-detector-7.temms.tar.zst \
  --signing-key-file ./temms-signing.key \
  --device-profile x86_64-cpu \
  --check-runtime \
  --strict-metadata
```

Use `--strict-metadata` for production gates so a package cannot pass release
validation without input/output schemas, MLflow provenance, runtime constraints,
and benchmark metadata.

## 4. Benchmark locally

After import, measure the model on the actual target hardware:

```bash
temms benchmark detector-7 \
  --config /etc/temms/temms.yaml \
  --samples 20 \
  --warmup 3 \
  --output /var/lib/temms/benchmarks/detector-7-x86_64-cpu.json \
  --hub-url http://hub-vm:8080 \
  --device-id edge-1 \
  --package-id pkg-detector-7 \
  --runtime-target-id temms-x86_64-cpu
```

The benchmark JSON includes load latency, inference latency percentiles, derived throughput, input shape, model metadata, the selected runtime type/options, and detected runtime capabilities. When `--hub-url` is set, TEMMS also records benchmark evidence in Hub Lite so operators can compare package/runtime performance across edge VMs with `temms hub benchmarks`.

## Runtime selection

TEMMS chooses the runtime from the imported model format:

- `onnx` uses ONNX Runtime and honors `runtime_options.providers`, `runtime_constraints.provider_order`, or `runtime_constraints.preferred_providers`.
- `tflite` uses `tflite_runtime` when installed, otherwise TensorFlow Lite, and honors `num_threads`.
- `torchscript` uses PyTorch TorchScript.
- `tensorrt` loads serialized TensorRT engines when NVIDIA TensorRT bindings are present. The edge runtime rejects TensorRT packages before load when the local runtime target does not expose TensorRT, which keeps VM simulations and real edge devices aligned. Generic TensorRT inference still requires device-specific I/O bindings, so production TensorRT deployments should ship a runtime plugin or adapter for the target profile.

When provider options are absent from package metadata, the detected or configured device profile supplies runtime defaults. The defaults are filtered against locally available ONNX Runtime providers before load, so an Orin package can prefer TensorRT/CUDA while still falling back to CPU when that is the only installed provider.

## 5. Roll out with Hub Lite

Enroll each VM, catalog packages, assign rollouts, then apply on the target edge agent. Hub CLI package registration and rollout apply require signature verification by default; use `--allow-unsigned-package` only for isolated labs.

```bash
curl -X POST http://edge-vm:8080/v1/hub/rollouts/rollout-1/apply \
  -H "Content-Type: application/json" \
  -d '{"require_signature": true}'
```

Configure `TEMMS_PACKAGE_SIGNING_KEY_FILE` or `TEMMS_PACKAGE_SIGNING_KEY` in the edge daemon environment so signature verification uses the VM-local key instead of a key sent in request JSON. If the package declares constraints the device cannot satisfy, or if the local package path no longer matches the cataloged source SHA256, apply fails before import or activation and the rollout state becomes `failed`.

To roll back a Hub-managed rollout to the previous known-good model for its slot:

```bash
temms hub rollback rollout-1 \
  --hub-url http://edge-vm:8080 \
  --actor operator:alice \
  --reason "latency regression"
```

The edge agent reloads the previous model, records the rollback decision, emits telemetry, and moves the targeted rollout to `rolled_back`.

## 6. Export post-mission evidence

TEMMS buffers operator actions, rollouts, rollbacks, deploy requests, and inference summaries in a local JSONL telemetry file.

For the richest post-mission review, export the evidence bundle. It includes Hub Lite fleet state, deployment state, doctor-style diagnostics, slots, runtime state, condition snapshot, package/model metadata, package import audit events, rollout history with actors, decision logs with package/provenance metadata, signature verification context, telemetry, local benchmark JSON files, and a merged timeline. Policy-driven switch decisions also include the matched policy, matched rule, rule priority, action, and per-condition evidence with actual value, source, priority, confidence, and match result:

```bash
curl -X POST http://edge-vm:8080/v1/hub/evidence/export \
  -H "Content-Type: application/json" \
  -d '{"decision_limit": 500, "telemetry_limit": 5000, "include_benchmarks": true}' \
  > temms-evidence-bundle.json
```

To inspect only the audit timeline for a slot:

```bash
curl "http://edge-vm:8080/v1/control/audit/timeline?slot=vision&limit=100"
```

You can still export the raw telemetry bundle when you only need replayable events:

```bash
curl -X POST http://edge-vm:8080/v1/control/telemetry/export \
  -H "Content-Type: application/json" \
  -d '{"limit": 5000}' > telemetry-bundle.json
```

After transferring the bundle to Hub Lite, replay it into the central audit store:

```bash
temms hub replay-telemetry telemetry-bundle.json \
  --hub-url http://hub-vm:8080 \
  --device-id edge-vm \
  --actor operator:post-mission
```

Then mark it replayed and clear the local edge buffer:

```bash
curl -X POST http://edge-vm:8080/v1/control/telemetry/replay \
  -H "Content-Type: application/json" \
  -d '{"clear": true}'
```
