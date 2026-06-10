# TEMMS

Manage ML models on devices that can't always phone home.

TEMMS (Tactical Edge Model Management System) is a daemon that runs on edge hardware (Jetson Nano, Raspberry Pi, etc.) and automatically switches between ML models based on environmental conditions. Fog rolls in? TEMMS loads the low-visibility model. Battery dying? TEMMS drops to the lightweight fallback. Operator says use thermal? Done — and logged.

Existing tools (AWS Greengrass, Azure IoT Edge) assume your device is online. TEMMS assumes it isn't.

```
┌─────────────────────────────────────────────────────────┐
│                    TEMMS Daemon                          │
│                                                          │
│   Conditions ──→ Policy Engine ──→ Model Loader          │
│   (fog? heat?      (YAML rules)     (hot-swap, no        │
│    battery?)                          downtime)           │
│                        │                                  │
│                        ▼                                  │
│              Inference Server (HTTP)                      │
│              POST /v1/slots/vision/infer                  │
│                                                          │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│   │ yolov8   │  │ yolov8   │  │ mobilenet│              │
│   │ daylight │  │ lowlight │  │ tiny     │              │
│   │ (active) │  │          │  │ (fallbck)│              │
│   └──────────┘  └──────────┘  └──────────┘              │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

### Install and run (3 commands)

```bash
git clone https://github.com/yourusername/temms.git && cd temms
pip install -e ".[dev,sim]"
make test  # 268 tests, should all pass
```

### Run the visual simulation

The fastest way to see what TEMMS does. This generates synthetic driving frames, applies weather effects (fog, rain, night), sends them through inference, and shows a live dashboard:

```bash
# Install with GUI support
pip install -e ".[dev,sim-visual]"

# Start the daemon (terminal 1)
make docker-up

# Run the visual sim (terminal 2)
make sim-visual
```

You'll see a window with two panels — clean image on the left, weather-augmented on the right — and a status bar showing which model TEMMS selected. As fog rolls in, watch the model switch from `yolov8-daylight` to `yolov8-lowlight`.

No webcam needed. No GPU needed. Runs on any laptop.

### Docker sim environment

If you just want the daemon running in Docker with MLflow-backed Hub import:

```bash
make docker-up
# TEMMS UI:   http://localhost:8080/ui/
# TEMMS API:  http://localhost:8080/v1/health
# API Docs:   http://localhost:8080/docs

# Run headless sim scenario
make sim-headless

# Check the logs
make docker-logs

# Tear it all down
make docker-clean
```

## How It Works

TEMMS has three concepts: **slots**, **conditions**, and **policies**.

### Slots

A slot is a named inference endpoint. An autonomous robot might have a `vision` slot, a `targeting` slot, and a `navigation` slot — each running a different model, each switchable independently.

```bash
temms slot create vision --required --default-model yolov8-daylight
temms slot create targeting --default-model rgb-tracker-v1
temms slot list
```

### Conditions

Conditions are the state of the world. Temperature, visibility, battery level, time of day, mission phase. They come from sensors, from derived calculations, or from operator injection.

```bash
# Operator says visibility is 50 meters
temms condition set environmental.atmospheric.visibility_m 50

# System automatically collects CPU temp, memory, time
temms condition list
```

Conditions have priorities. Operator overrides (priority 1000) beat sensors (100) beat cached values (10).

### Policies

Policies are YAML files that say "when X, use model Y":

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

    - name: critical-low-visibility
      priority: 150
      conditions:
        all:
          - metric: environmental.atmospheric.visibility_m
            operator: lte
            value: 20
          - metric: operational.mission.priority
            operator: eq
            value: critical
      action:
        switch_to: mobilenet-tiny

  fallback_chain:
    - yolov8-daylight
    - yolov8-lowlight
    - mobilenet-tiny
```

The policy engine evaluates rules by priority (highest wins), checks for operator overrides (they always win), and executes the fallback chain if a model fails to load.

Every model switch is logged with the full condition snapshot, so you can audit exactly why the system made every decision.

## Architecture

Three-tier design. You only need Tier 3 to get started.

```
Tier 1: MLflow ──────── Model registry (versioning, experiments, UI)
                │        Standard MLflow. Not modified.
                │
Tier 2: Hub ────────── DDIL packaging layer (delta updates, fleet sync)
                │        Packages models for edge consumption.
                │
Tier 3: TEMMS Daemon ── Edge runtime (this repo)
                         Policy engine, inference, offline-first.
```

**Tier 3 (TEMMS Daemon)** runs completely offline. It receives pre-packaged models from the Hub (or from a USB drive), manages a local model cache, and makes all switching decisions locally.

See [docs/architecture.md](docs/architecture.md) for the full design.

## Project Structure

```
temms/
├── src/temms/
│   ├── core/           # Model cache, storage, package import
│   ├── slots/          # Multi-slot lifecycle management
│   ├── conditions/     # Condition store + collectors (CPU, time, etc.)
│   ├── policy/         # Policy engine (YAML → model switch decisions)
│   ├── inference/      # FastAPI server + ONNX/TFLite runtime
│   ├── daemon/         # Main daemon loop (condition → policy → switch)
│   ├── sim/            # Visual simulation (weather effects + runner)
│   ├── ui/             # Web dashboard (Jinja2 + HTMX)
│   └── cli/            # CLI commands (typer)
├── tests/              # 268 tests, ~60% coverage
├── examples/
│   ├── package-example/  # Sample model package with real ONNX models
│   └── policies/         # Example policy YAML files
├── scripts/            # Sim scripts, Docker entrypoint, model generator
├── docs/               # Architecture, quickstart, policy reference
└── docker-compose.yml  # Full sim environment (MLflow + TEMMS)
```

## CLI Reference

```bash
# System
temms init                               # Initialize TEMMS
temms status                             # System health check
temms daemon start --foreground          # Start daemon

# Models
temms import ./path/to/package/          # Import model package
temms model list                         # List cached models

# Slots
temms slot create vision --required      # Create a slot
temms slot list                          # List all slots
temms slot status vision                 # Slot details
temms slot set vision yolov8-lowlight    # Manual model switch

# Conditions
temms condition set weather.vis 50       # Inject condition
temms condition list                     # List all conditions
temms condition snapshot                 # Nested condition view

# Policies
temms policy load ./policy.yaml          # Load a policy

# MLflow registry bridge (optional)
temms mlflow list                        # List upstream registry models
temms mlflow pull <model> <version>      # Pull from MLflow into a package
```

## API Endpoints

```
GET  /v1/health                          # Liveness probe
GET  /v1/status                          # Full system status
GET  /v1/slots/{name}/status             # Slot status
POST /v1/slots/{name}/infer              # Run inference (file upload)
POST /v1/control/slots/{name}/model      # Operator override
POST /v1/control/conditions              # Inject conditions
DELETE /v1/control/conditions/overrides  # Clear overrides

# Web UI
GET  /ui/                                # Dashboard
GET  /ui/slots                           # Slot management
GET  /ui/conditions                      # Condition viewer + injection
GET  /ui/decisions                       # Decision audit log
GET  /ui/models                          # Hub registry, import, deploy flow
GET  /ui/import                          # Compatibility alias for Hub import
```

Interactive API docs at `http://localhost:8080/docs` (Swagger UI).

## Visual Simulation

TEMMS includes a built-in simulation engine that applies weather effects to video frames in real-time and shows model switching as it happens.

```bash
# Four built-in scenarios
make sim-visual            # Fog rollout (default)
make sim-visual-night      # Day → night → dawn cycle
make sim-visual-rain       # Clear → downpour → clearing
make sim-visual-stress     # Combined: fog + night + battery + thermal

# Custom options
python -m temms.sim.runner \
  --scenario fog_rollout \
  --source webcam \
  --daemon-url http://localhost:8080

# Headless mode (for Docker / CI)
python -m temms.sim.runner --scenario fog_rollout --headless
```

The weather engine applies fog, rain, snow, darkness, and sun flare effects using pure OpenCV — no additional dependencies. Each effect maps to a TEMMS condition (visibility → fog intensity, ambient light → darkness, etc.).

## Model Package Format

Models arrive as pre-packaged directories. In production, the Hub creates these. For development, use `scripts/generate_real_models.py`.

```
my-package/
├── manifest.json       # Package metadata + model checksums
├── models/
│   ├── yolov8-daylight.onnx
│   ├── yolov8-lowlight.onnx
│   └── mobilenet-tiny.onnx
└── policies/
    └── weather-adaptive.yaml
```

Hub uses the package manifest as the operator evidence contract. Import always
records hash validation; model builders can add evidence fields for signed,
simulated, and tested status:

```json
{
  "signature": {
    "algorithm": "ed25519",
    "key_id": "builder-key",
    "signature": "base64-ed25519-signature"
  },
  "validation": {
    "sim_passed": true,
    "sim_evidence": {
      "source": "temms-sim",
      "scenario": "fog-regression",
      "run_id": "sim-42"
    },
    "tests_passed": true,
    "test_evidence": {
      "source": "pytest",
      "suite": "unit-readiness",
      "run_id": "ci-99"
    }
  }
}
```

For signature verification, sign the canonical JSON manifest with top-level
`signature` and `signatures` fields omitted. Trust keys are local operator
configuration, not package-provided trust. Set
`TEMMS_TRUSTED_SIGNATURE_KEYS='{"builder-key":"base64-ed25519-public-key"}'`
or point `TEMMS_TRUSTED_SIGNATURE_KEYS_FILE` at the same JSON object. Hub only
marks `Signed` as passed after import verifies the signature against one of
those trusted keys. Signature verification uses Python `cryptography`; the
Docker/sim install path includes it through the MLflow stack. If it is not
available, signed packages remain unverified rather than being trusted.
When the manifest is signed, Sim/Test evidence details are covered by that
signature too, so Hub can show where the evidence came from without making the
main operator table noisy.

```bash
# Builder/admin setup
temms package keygen \
  --key-id builder-key \
  --private-key ./builder.key \
  --trusted-keys ./trusted-keys.json

# Sign a package before Hub import
temms package sign ./my-package/ \
  --key-id builder-key \
  --private-key ./builder.key

# Operator-side trust config for Hub/daemon imports
export TEMMS_TRUSTED_SIGNATURE_KEYS_FILE=./trusted-keys.json
```

If evidence is missing, Hub shows that state as unknown or needing attention
instead of implying a model is ready. By default, Hub requires every readiness
check before a model can move to an edge/sim slot:
`Signed`, `Sim`, `Test`, and `Val`. For local smoke tests, set
`TEMMS_HUB_REQUIRED_EVIDENCE=val` to permit hash-validated-only deployment.

```bash
# Import a package
temms import ./my-package/

# Generate example models (real ONNX, ~8KB each)
python scripts/generate_real_models.py
```

## Development

```bash
# Install dev + sim dependencies
pip install -e ".[dev,sim]"

# Run tests
make test                  # All 268 tests
make test-sim              # Just simulation tests
make test-e2e              # E2E tests (requires docker-up)

# Code quality
make format                # black
make lint                  # ruff + mypy
```

### Test breakdown

| Suite | Tests | What it covers |
|-------|-------|----------------|
| Core | 152 | Cache, storage, package import, slots, conditions |
| Policy | 49 | Policy parsing, evaluation, operator overrides |
| Sim | 58 | Weather effects, scenarios, condition mapping |
| Integration | 9 | Real ONNX model loading, inference, hot-swap |

## Target Hardware

| Device | CPU | RAM | Status |
|--------|-----|-----|--------|
| NVIDIA Jetson Nano | ARM64 | 4GB | Primary target |
| NVIDIA Jetson Orin Nano | ARM64 | 8GB | Supported |
| Raspberry Pi 4/5 | ARM64 | 4-8GB | Supported |
| Any Linux x86_64 | x86_64 | 4GB+ | Supported |
| MacBook (Docker) | ARM64 | 8GB+ | Development/sim |

## Roadmap

- [x] **Phase 1**: Core infrastructure — cache, slots, conditions, policy engine, CLI
- [x] **Phase 2**: Runtime — inference server, model loader, daemon, operator overrides
- [x] **Phase 3**: Sim environment — Docker, real ONNX models, Web UI, MLflow bridge, visual sim
- [ ] **Phase 4**: Hub — MLflow packaging, delta updates, fleet sync, air-gap export
- [ ] **Phase 5**: Advanced — model ensembles, predictive preloading, swarm condition sharing

## Why TEMMS?

| Feature | Greengrass | Azure IoT Edge | TEMMS |
|---------|-----------|---------------|-------|
| Offline-first | ❌ | ❌ | ✅ |
| Policy-driven model switching | ❌ | ❌ | ✅ |
| Deterministic fallback chains | ❌ | ❌ | ✅ |
| ML model versioning (first-class) | ❌ | ❌ | ✅ |
| No Kubernetes dependency | ❌ | ❌ | ✅ |
| USB/air-gap model updates | ❌ | ❌ | ✅ |
| Decision audit trail | ❌ | ❌ | ✅ |
| Runs on 4GB Jetson | ⚠️ | ⚠️ | ✅ |

## License

Apache 2.0
