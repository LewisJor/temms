# MLflow to TEMMS Edge Packages

Production edge deployments should package MLflow registry models before they reach an edge VM. The edge agent should import TEMMS packages, not depend on live MLflow access.

`temms mlflow pull` remains available only as a local-development shortcut and
requires `--allow-dev-pull`. Production operators should use
`temms package from-mlflow`, sign the output, and distribute the resulting TEMMS
package through Hub Lite or an air-gap bundle.

Packages created by the legacy direct pull path are marked
`metadata.development_only=true`. Normal validation warns on that marker, and
strict production metadata validation rejects it.

Package import does not register models back into MLflow by default, even when
the `mlflow` Python package is installed in the edge image. That keeps rollout
apply deterministic on disconnected VMs. For local development only, set
`TEMMS_MLFLOW_AUTO_REGISTER=true` before import to mirror imported models into
the configured MLflow tracking server.

## Build From MLflow

```bash
temms package from-mlflow models:/detector/7 \
  --slot vision \
  --policy ./policies/vision.yaml \
  --output ./dist \
  --tracking-uri http://mlflow.example:5000 \
  --device-profile x86_64-cpu \
  --runtime-constraint 'runtimes=["onnx"]' \
  --runtime-option 'providers=["CPUExecutionProvider"]' \
  --signing-key-file ./hub-signing.key \
  --archive
```

With `--archive`, this creates an immutable archive such as:

```text
dist/mlflow-detector-7.temms.tar.zst
```

Package outputs are immutable by default. If `dist/mlflow-detector-7.temms/`
or `dist/mlflow-detector-7.temms.tar.zst` already exists, packaging fails
instead of replacing a previously published artifact. Use `--overwrite` only
for isolated lab rebuilds before a package has been registered or distributed.

Without `--archive`, this creates a directory package such as:

```text
dist/mlflow-detector-7.temms/
├── manifest.json
├── signature.json
├── models/
│   └── model.onnx
└── policies/
    └── vision.yaml
```

The generated manifest captures:

- MLflow provenance: registry URI, requested model URI, resolved `models:/name/version` URI, model version or alias, lifecycle status/stage, registry source path, run ID, run name, run artifact URI, artifact path, artifact SHA256, and stable run parameter/tag fingerprints
- Model integrity: SHA256 and size
- Required schemas from MLflow run params: `input_schema`, `output_schema`; when those params are absent, TEMMS falls back to the model's `MLmodel` signature
- Runtime constraints and loader options from MLflow run params or packaging-time CLI flags such as `--runtime-constraint accelerators='["nvidia"]'` and `--runtime-option providers='["CUDAExecutionProvider","CPUExecutionProvider"]'`
- Benchmark metadata from MLflow run metrics, normalized from common aliases such as `avg_latency_ms`, `p95_latency_ms`, `fps`, `requests_per_second`, `peak_memory_mb`, `accuracy`, and `f1_score`; the manifest records which MLflow metric key supplied each normalized field, a fingerprint of the run metrics, and an explicit `available: false` state when no recognized benchmark metrics were present
- Compatibility metadata such as target slot and device profile

`temms package from-mlflow` requires both input and output schema metadata by
default so edge packages are executable and auditable without reaching back to
MLflow. Use `--allow-missing-schema` only for isolated lab packages.

It also requires runtime constraints by default. Supply them through MLflow run
params, `--runtime-constraint`, or `--device-profile` so Hub and edge agents know
which runtime image, OS/arch, provider stack, or hardware profile the package is
allowed to run against. Use `--allow-missing-runtime-constraints` only for
isolated lab packages that should not be treated as production-ready.

When an MLflow download contains more than one supported model artifact, TEMMS
uses the file declared by `MLmodel` flavor metadata when that declaration is
unambiguous. Otherwise packaging fails clearly instead of guessing. Pass
`--model-artifact relative/path/model.onnx` to select the exact artifact to
ship.

## Sign and Validate

Packages can be signed after creation:

```bash
temms package sign ./dist/mlflow-detector-7.temms \
  --signing-key-file ./hub-signing.key
```

Directory packages can be archived after creation:

```bash
temms package archive ./dist/mlflow-detector-7.temms
```

Archives can also be signed in place:

```bash
temms package sign ./dist/mlflow-detector-7.temms.tar.zst \
  --signing-key-file ./hub-signing.key
```

Validate structure, hashes, and signature:

```bash
temms package validate ./dist/mlflow-detector-7.temms.tar.zst \
  --signing-key-file ./hub-signing.key \
  --device-profile x86_64-cpu \
  --check-runtime \
  --strict-metadata
```

Use `--strict-metadata` in CI and Hub release gates. It rejects packages that
are structurally valid but missing production edge metadata: input/output
schemas, MLflow provenance, runtime constraints, and benchmark metadata.
Hub Lite package registration and `/v1/hub/packages/from-mlflow` run this
strict metadata check by default and record the result in the package catalog;
use `--no-strict-metadata` or `"strict_metadata": false` only for isolated lab
packages that should not be assigned under production daemon policy.

Inspect the Hub-ready catalog entry derived from the package:

```bash
temms package inspect ./dist/mlflow-detector-7.temms.tar.zst \
  --signing-key-file ./hub-signing.key \
  --json
```

Package validation, inspection, Hub registration, rollout apply, and edge import require signature verification by default. Use `--allow-unsigned-package` only for isolated labs. The current MVP signature envelope is `signature.json` with `HMAC-SHA256`. It records the signer name and a stable SHA256 signing-key fingerprint so validation output, import audit records, and evidence bundles can identify which key verified a package without exposing the key itself. This keeps signing dependency-free for air-gapped VMs. Hub Lite can later replace the algorithm with asymmetric signing while preserving the same validation flow.

## Import on an Edge VM

```bash
temms import ./dist/mlflow-detector-7.temms.tar.zst \
  --config /etc/temms/temms.yaml \
  --signing-key-file ./hub-signing.key
```

The import path accepts directory packages and `.temms.tar.zst` archives. Archive extraction rejects path traversal, links, and special tar members; only regular files and directories are accepted. Manifest-declared model and policy filenames are also checked so they cannot escape the package directories, and active policy promotion only accepts flat policy filenames. TEMMS verifies package structure, manifest-declared model hashes, package signatures, production metadata, device-profile compatibility, and runtime constraints before copying models into the local cache. Signature verification and strict production metadata are required by default; use `--allow-unsigned-package --allow-lab-metadata` only for isolated labs. Policies included in the package are promoted into the configured active policy directory. Re-importing the same package refreshes cached policy files and removes previously promoted active policy files that are no longer declared by the package manifest.

Every import records an `_temms_import` audit envelope in the local package cache. That envelope captures the source path, source type, archive SHA256 when applicable, whether model hashes were checked, whether a signature was required and verified, signer and signing-key fingerprint metadata, the checked device profile, validation warnings, and the import timestamp. Evidence exports include this cache record as `package_imports` and as `package_import` entries in the merged timeline.
