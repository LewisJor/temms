# TEMMS

TEMMS is an edge runtime for adaptive inference. It runs next to your inference
application, watches local operating conditions, and switches the active model
for each inference slot when policy says another model is a better fit.

It is designed for disconnected or degraded edge systems where model choice
depends on local state: visibility, lighting, battery, thermals, available
runtime, mission phase, or operator input.

## Features

- Slot-based inference endpoints
- YAML policies for condition-based model selection
- Local condition store with source priority
- Operator overrides
- Hot-swap model activation
- Fallback chains when a selected model fails to load
- Offline mode with buffered control operations
- Decision log and evidence bundle export
- Docker simulation with example ONNX models

## Install

```bash
git clone https://github.com/LewisJor/temms.git
cd temms
pip install -e ".[dev,sim]"
```

Run the test suite:

```bash
make test
```

## Quick Start

Start the Docker environment:

```bash
make docker-up
```

Services:

```text
TEMMS UI    http://localhost:8080/ui/
TEMMS API   http://localhost:8080/v1/health
API docs    http://localhost:8080/docs
MLflow UI   http://localhost:5001
```

Run a headless scenario:

```bash
make sim-headless
```

Run the visual simulator:

```bash
pip install -e ".[dev,sim-visual]"
make docker-up
make sim-visual
```

## How It Works

TEMMS has two layers:

- **TEMMS Hub** manages candidate models before they reach a device. It packages
  models and policies, signs artifacts, and runs targeted container tests
  against the runtimes or device profiles that will consume them.
- **TEMMS Daemon** runs on the edge device. It imports signed packages, evaluates
  local conditions and policies, switches models, falls back when needed, and
  records decision evidence.

The daemon has four main pieces:

- **Slots** are named inference endpoints, such as `vision` or `navigation`.
- **Conditions** are local facts, such as battery level or visibility.
- **Policies** map conditions to model choices.
- **The controller** evaluates a slot, chooses a model, applies the switch, and
  falls back if the selected model cannot load.

The controller can run in the daemon loop or be invoked directly through the API:

```bash
curl -X POST http://localhost:8080/v1/control/slots/vision/evaluate \
  -H "Content-Type: application/json" \
  -d '{"apply": true}'
```

Use `{"apply": false}` to preview the decision without changing the active
model.

## Slots

Create and inspect slots with the CLI:

```bash
temms slot create vision --required --default-model yolov8-daylight
temms slot list
temms slot status vision
temms slot decisions --slot vision
```

Each slot tracks its active model, runtime state, candidates, optional operator
override, and decision history.

## Conditions

Set or inspect local conditions:

```bash
temms condition set environmental.atmospheric.visibility_m 50
temms condition set platform.power.battery_percent 18
temms condition snapshot
```

Conditions have priorities. Higher-priority values override lower-priority
values for the same path. Operator-provided values use high priority by default.

## Policies

Policies are slot-scoped YAML files.

```yaml
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: weather-adaptive-vision
spec:
  slot: vision
  default_model: yolov8-daylight

  rules:
    - name: fog-conditions
      priority: 80
      conditions:
        any:
          - metric: environmental.atmospheric.visibility_m
            operator: lte
            value: 100
      action:
        switch_to: yolov8-lowlight

  fallback_chain:
    - yolov8-daylight
    - yolov8-lowlight
    - mobilenet-tiny
```

Load a policy:

```bash
temms policy load examples/policies/weather-adaptive.yaml
```

Rules are evaluated by priority. If the selected model fails to load, TEMMS tries
the policy fallback chain in order.

## API

Common endpoints:

```text
GET    /v1/health
GET    /v1/status
GET    /v1/evidence?slot=vision

GET    /v1/slots/{slot}/status
POST   /v1/slots/{slot}/infer

POST   /v1/control/slots/{slot}/evaluate
POST   /v1/control/slots/{slot}/model

POST   /v1/control/conditions
DELETE /v1/control/conditions/overrides

POST   /v1/control/offline
POST   /v1/control/online
POST   /v1/control/sync
POST   /v1/control/deploy
```

Inject a condition:

```bash
curl -X POST http://localhost:8080/v1/control/conditions \
  -H "Content-Type: application/json" \
  -d '{"conditions": {"environmental.atmospheric.visibility_m": 50}}'
```

Export evidence:

```bash
curl http://localhost:8080/v1/evidence?slot=vision | python -m json.tool
```

## CLI

Local setup:

```bash
temms init --config ./local.temms.yaml --data-dir ./local-data
temms daemon start --foreground --config ./local.temms.yaml
```

Package import:

```bash
temms import ./examples/package-example --config ./local.temms.yaml
```

Slots:

```bash
temms slot create vision --required --default-model yolov8-daylight --config ./local.temms.yaml
temms slot list --config ./local.temms.yaml
temms slot status vision --config ./local.temms.yaml
temms slot set vision yolov8-lowlight --reason "operator override" --config ./local.temms.yaml
temms slot decisions --slot vision --config ./local.temms.yaml
```

Conditions:

```bash
temms condition set environmental.atmospheric.visibility_m 50 --config ./local.temms.yaml
temms condition list --config ./local.temms.yaml
temms condition snapshot --config ./local.temms.yaml
temms condition clear-overrides --config ./local.temms.yaml
```

Policies and evidence:

```bash
temms policy load examples/policies/weather-adaptive.yaml --config ./local.temms.yaml
temms policy list --config ./local.temms.yaml
temms evidence --slot vision --output evidence.json --config ./local.temms.yaml
```

## Offline Mode

Offline mode keeps local control working while buffering operations for later
sync.

```bash
curl -X POST http://localhost:8080/v1/control/offline
curl -X POST http://localhost:8080/v1/control/online
curl -X POST http://localhost:8080/v1/control/sync
```

Condition updates and operator overrides still apply locally while offline.

## Evidence Bundles

Evidence bundles are JSON documents with recent slot decisions, condition
snapshots, model metadata, package manifests, loaded policies, offline state,
pending operations, and a bundle SHA256.

```bash
temms evidence --slot vision --output evidence.json
```

Schema version:

```text
temms-evidence-bundle/v1
```

## Package Format

TEMMS imports model packages from local directories. Those packages can be
created by TEMMS Hub, a registry export, CI job, or air-gap transfer workflow.
Hub is intended to give individuals and agents a repeatable path from candidate
models to signed, tested packages that the daemon can consume.

```text
my-package/
├── manifest.json
├── models/
│   ├── yolov8-daylight.onnx
│   ├── yolov8-lowlight.onnx
│   └── mobilenet-tiny.onnx
└── policies/
    └── weather-adaptive.yaml
```

Generate example model files:

```bash
python scripts/generate_real_models.py
temms import ./examples/package-example
```

## Project Layout

```text
src/temms/
├── controller.py       # Adaptive model selection and fallback
├── daemon/             # Async daemon loops and deployment state
├── inference/          # FastAPI app and runtime model loading
├── policy/             # YAML policy schema and evaluator
├── conditions/         # Condition store and collectors
├── slots/              # Slot state and decision log
├── core/               # Model cache, package import, storage
├── ui/                 # Local web UI
├── sim/                # Simulation helpers
└── cli/                # Typer CLI
```

## Development

```bash
pip install -e ".[dev,sim]"

make test
make test-sim
make test-e2e

make format
make lint
```

Useful focused test command:

```bash
uv run pytest tests/unit/test_controller.py tests/integration/test_inference_flow.py -q
```

## Scope

TEMMS is focused on local runtime control. It does not provide model training,
labeling, experiment tracking, feature stores, fleet orchestration, or container
scheduling.

## License

Apache 2.0
