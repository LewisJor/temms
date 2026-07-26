# Mission Packages (`mission.yaml`)

A **mission package** is defined by one authored file — `mission.yaml` — which is
the source of truth for the whole package. `manifest.json` is *compiled* from it
and signed; you edit the YAML, never the manifest. This is the first slice of
[best-feasible model control](model-control.md) (issue
[#43](https://github.com/LewisJor/temms/issues/43)): the model **portfolio** and
its targeting live in one place.

## Anatomy

```yaml
apiVersion: temms/v1
kind: MissionPackage

metadata:
  name: vision-portfolio
  version: 1.0.0

# The portfolio. Each member is an operating point: the conditions it is FOR
# (optimal_when) and what it REQUIRES to run. Sources are scheme:// URIs.
models:
  - id: yolov8-daylight
    source: mlflow://models/yolov8-daylight/3   # resolved from the registry at build
    format: onnx
    provides: object-detection
    optimal_when: { light: bright }
    requires: { memory_mb: 512 }
  - id: mobilenet-tiny
    source: file://models/mobilenet-tiny.onnx   # a local artifact
    format: onnx
    optimal_when: {}          # the floor — cheapest, last to become infeasible
    requires: { memory_mb: 96 }

slot:
  name: vision
  policy: policies/weather-adaptive.yaml

target:
  device_profiles: [x86_64-cpu, arm64-cpu, rpi5-tflite]
  runtime: onnxruntime
```

`optimal_when` and `requires` are the operating envelope. Slice 1 carries them
into the manifest; best-feasible dispatch consumes them in a later slice. Until
then, switching still runs on the existing policy engine.

## Model sources are registry-agnostic

`source:` is a `scheme://` URI resolved by a `ModelResolver`. The registry is a
**build-time input only** — the compiler bakes resolved artifact bytes, a digest,
and a registry-neutral provenance block into the package, and the edge never
talks to a registry.

| Scheme | Resolves to | Provenance |
|---|---|---|
| `mlflow://models/<name>/<version>` (or `@alias`) | the registered model's artifact | registry, run id, model version |
| `file://path` or a bare path | a local artifact (relative to the mission.yaml) | file ref |

Additional registries (`sagemaker://`, `s3://`, …) are drop-in resolvers; nothing
else in the pipeline changes when one is added.

## The check that makes fallback provable

Validation **fails the build** if the slot's policy references a model the
portfolio does not carry — its `default_model`, any rule's `switch_to`, or
anything in the `fallback_chain`:

```text
policy references model(s) the portfolio does not carry: ['mobilenet-tiny'].
Every model a policy can switch to (including its fallback chain) must be
declared under models[], or the edge would fail to switch to it offline.
```

This converts a silent, worst-moment "model not found" on a disconnected edge
into a build error on the laptop. It is the single most important property of the
mission-package format.

## CLI

```bash
# Validate without building (includes the policy-reference check)
temms mission validate mission.yaml

# Compile into a package directory
temms mission build mission.yaml --out dist

# Compile and sign in one step
temms mission build mission.yaml --out dist --sign fleet.private.pem

# Resolve mlflow:// sources against a specific registry
temms mission build mission.yaml --out dist --mlflow-uri http://localhost:5001
```

The signature covers the **whole portfolio** — every model artifact, the policy,
and the manifest — so tampering with any one model fails verification (see
[package signing](package-signing.md)). A single-model mission is simply a
one-member portfolio, so existing single-model workflows keep working.

## What the compiler writes

A self-contained, offline-verifiable package:

```text
vision-portfolio-1-0-0/
├── manifest.json                 # compiled from mission.yaml (+ envelopes, provenance)
├── models/
│   ├── yolov8-daylight/…onnx
│   └── mobilenet-tiny/…onnx
├── policies/weather-adaptive.yaml
└── signature.json                # when --sign is used; covers the whole tree
```

See [best-feasible model control](model-control.md) for where this is heading:
the envelopes compiled here become the inputs to feasibility-first selection.
