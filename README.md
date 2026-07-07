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
- Hub product UI for model inventory, runtime compatibility, rollout approval,
  activation, and evidence export
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

Run the canonical control-loop demo:

```bash
make product-demo
```

This builds and signs a demo package, catalogs it in Hub Lite, records runtime
validation, coordinates a staged rollout plan, records rollout approval, applies
the first batch on a local edge runtime, simulates fog, low battery, model-load
failure, offline inference serving, rollback, and operator override, then writes
`temms-canonical-evidence.json` and ingests that evidence back into Hub Lite for
central aggregation and mission replay.

Replay the evidence as an operator-readable summary:

```bash
temms evidence --input temms-canonical-evidence.json --summary
temms evidence --input temms-canonical-evidence.json --replay
```

For a local product UI rehearsal with seeded models, rollout state, and evidence,
follow [Functional Testing](docs/functional-testing.md).

Start the Docker environment:

```bash
make docker-up
```

The local Docker daemon seeds Hub Lite with a signed, released example package,
an online `edge-sim` target, and a demo signing key. That makes
`/ui/hub` open directly into a model deployment workflow instead of an empty
catalog. Docker demo mode also publishes a stable simulated resource envelope
for `edge-sim` so the first-open mission package path starts green; explicit
heartbeat/resource-drift tests still exercise the same readiness blockers used
for real constrained edges.

Services:

```text
TEMMS Hub   http://localhost:8080/ui/hub
TEMMS API   http://localhost:8080/v1/health
API docs    http://localhost:8080/docs
MLflow UI   http://localhost:5001
```

Build or run the React Hub UI from the repo root:

```bash
npm --prefix ui install
npm run typecheck
npm run build
npm run smoke:workbench

# Optional: Vite dev server with /v1 proxied to the local daemon
npm run dev
```

The Makefile exposes the same workflow as `make ui-install`, `make ui-build`,
`make ui-smoke`, `make ui-ci`, and `make ui-dev` for shell sessions that prefer Make.

When the Docker stack is running, verify that the live daemon and UI agree on
the current Mission Package Workbench contract, including explicit JSON and
YAML-only mission package planning:

```bash
make docker-product-smoke
```

The same package handoff is available without the browser:

```bash
uv run temms hub mission-package-plan ./mission.yaml --hub-url http://localhost:8080 --json
uv run temms hub mission-package-download ./mission.yaml --hub-url http://localhost:8080 \
  --output /tmp/temms-edge-mission-package.json
uv run temms hub mission-package-stage /tmp/temms-edge-mission-package.json \
  --hub-url http://localhost:8080 --actor operator:cli-demo
```

The production Hub app is served by the daemon at `http://localhost:8080/ui/hub`.
Hub-enabled daemons also redirect `http://localhost:8080/ui/` to the React Hub.
The Hub opens as **Mission Package Workbench**: a product cockpit for signed model
inventory, targeted runtime selection, edge rollout status, DDIL readiness, and
mission evidence. The first viewport now opens as a **Mission workflow cockpit**:
an operator path rail, a focused current-stage decision panel, package path
signals, and a compact **Live context** drawer for inventory, rollout, evidence,
and DDIL telemetry. Its first operator pass now follows
**Mission -> Model Plan -> Runtime Fit -> Sensor Handling -> Package Handoff ->
Edge Deploy -> Field Ops**:
define the mission spec or YAML, choose models, rank the target runtime, set
sensor/model-switch handling, package the edge handoff, stage deployment, and
operate through DDIL/evidence proof. Setup-only controls such as package
registration and edge enrollment are under **Advanced intake**, and direct
rollout forms are under **Manual controls** so the demo path stays focused on
the mission package handoff. Package planning now separates the stable package
identity hash from the exact downloaded payload hash, so repeated plan/download
actions can be audited as the same mission/runtime package even when artifact
timestamps differ. The deployment intent also carries mission-contract,
runtime-capability-lock, and runtime-plan digests that staging verifies before
creating the edge rollout.
**Model Plan** owns model selection and package release
context; **Runtime Fit** preserves that selected model as locked context, lets
the operator choose the edge node and target runtime, ranks available runtime
targets by fit, validation, benchmark, and live inventory state, and then
exposes a runtime proof artifact lane that can generate a
`temms-edge-runtime-proof/v1` payload through Hub and download the exact
server-backed JSON proof for offline handoff. The same proof includes the
canonical `temms-runtime-workbench/v1` contract used by the UI, CLI, API, and
DDIL retarget checks to agree on selected target, best target, capability lock,
benchmark, telemetry, and blocked-runtime reasons. When the
daemon has a package signing key, that proof carries an attestation with the
payload hash, signer, and key fingerprint, and the local `verify-edge-proof`
command can fail closed with `--require-proof-signature`. It keeps copyable
`edge-runtime-mission` plus local verification commands for the selected
model/runtime/device path. The legacy diagnostic pages remain available for
standalone agent debugging, but Hub-enabled daemons redirect those diagnostic
GET pages back to `/ui/hub` so they are not competing demo paths.

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
curl "http://localhost:8080/v1/evidence?summary=true&summary_limit=20" | python -m json.tool
curl "http://localhost:8080/v1/evidence?replay=true&replay_limit=50" | python -m json.tool
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
temms evidence --input evidence.json --summary
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
