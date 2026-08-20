# Quickstart

This gets you from zero to watching TEMMS switch models in under 5 minutes.

## Prerequisites

- Python 3.11+
- Docker and Docker Compose (for sim environment)
- ~2GB disk space (Docker images + ONNX models)

## Step 1: Clone and install

```bash
git clone https://github.com/LewisJor/temms.git
cd temms

# Create a virtualenv (recommended)
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# Install with all development + simulation dependencies
pip install -e ".[dev,sim]"
```

Verify the install:

```bash
temms --help
# Should print the CLI help
```

## Step 2: Run the tests

```bash
make test
# Expected: local unit/integration tests should pass; Docker E2E tests skip unless the daemon is running.
```

If any tests fail, check your Python version (`python --version` — need 3.11+) and that numpy/onnxruntime installed correctly.

## Fast path: run the canonical product demo

This runs the TEMMS control loop without Docker or a separate daemon. It builds
and signs a demo package, catalogs it in Hub Lite, records runtime validation,
coordinates a staged rollout plan, records rollout approval, applies the first
batch on a local edge runtime, simulates fog, low battery, model-load failure,
serves an inference request while offline, rolls back, and applies an operator
override, then exports an evidence bundle and ingests it back into Hub Lite for
central evidence aggregation.

```bash
make product-demo
```

Expected output includes the active model changes and writes:

```text
temms-canonical-evidence.json
```

Replay the evidence as an operator-readable summary:

```bash
temms evidence --input temms-canonical-evidence.json --summary
```

Export a chronological mission replay artifact:

```bash
temms evidence --input temms-canonical-evidence.json --replay
```

When the daemon is running, the same replay shape is available from the API:

```bash
curl "http://localhost:8080/v1/evidence?summary=true&summary_limit=20" | python -m json.tool
curl "http://localhost:8080/v1/evidence?replay=true&replay_limit=50" | python -m json.tool
```

The canonical demo also records the rollout plan, approval gate, Hub evidence
ingest, and Hub-side mission replay phase, so the exported evidence shows that
rollout coordination and policy approval happened before edge apply and that
post-mission evidence can be aggregated centrally.

## Mission package handoff from the CLI

The `temms hub` CLI follows the demo path
**Mission -> Model Plan -> Runtime Fit -> Sensor Handling -> Package Handoff ->
Edge Deploy -> Field Ops**. The primary path turns a mission spec/YAML into a
selected model, target runtime, sensor/model handling policy, signed mission
package, and edge rollout intent.

Run the handoff from a mission YAML file:

```bash
uv run temms hub mission-package-plan ./mission.yaml --hub-url http://localhost:8080 --json
uv run temms hub mission-package-download ./mission.yaml --hub-url http://localhost:8080 \
  --output /tmp/temms-edge-mission-package.json
uv run temms hub mission-package-stage /tmp/temms-edge-mission-package.json \
  --hub-url http://localhost:8080 --actor operator:cli-demo
```

The downloaded `temms-edge-mission-package/v1` artifact includes an
`edge_handoff` block with schema `temms-edge-mission-package-handoff/v1` and
mode `stage_approve_apply`, so the file itself carries the package stage,
approval, rollout apply, and digest-verification runbook for the edge handoff.

For a seeded local rehearsal, use the functional testing checklist:

```text
docs/functional-testing.md
```

## Step 3: Start the Docker sim environment

This launches two containers:

| Service | URL | What it does |
|---------|-----|-------------|
| TEMMS Daemon | http://localhost:8080 | Edge runtime + inference server |
| MLflow | http://localhost:5001 | Model registry UI |

```bash
make docker-up
```

The Docker entrypoint seeds Hub Lite with a signed, released demo package and
an online `edge-sim` node, so Hub Lite starts with model inventory ready for a
rollout walkthrough. In Docker demo mode, the local daemon heartbeat keeps
`edge-sim` on a healthy simulated memory/storage envelope while still reporting
real runtime/provider availability. That keeps the default smoke deterministic;
resource-drift drills can still post constrained heartbeat inventory to show the
same readiness gate blocking behavior a real edge would trigger.

Wait 15-20 seconds for initialization. Check if it's ready:

```bash
curl http://localhost:8080/v1/health
# {"status":"ok","timestamp":"..."}
```

Verify the live daemon is serving the current mission package contract,
including explicit JSON planning, YAML-only mission planning, and package
download:

```bash
uv run python scripts/mission_package_smoke.py --hub-url http://localhost:8080
```

Open the MLflow UI: http://localhost:5001

If that port is already in use, choose another host port:

```bash
MLFLOW_HOST_PORT=5050 make docker-up
```

## Step 4: See model switching in action

The headless scenario runner steps a DDIL scenario against the daemon and prints each model-selection decision:

```bash
make sim-headless
```

This prints scenario progress and model switch decisions to stdout.

### Option C: Manual condition injection

Drive the model switching yourself:

```bash
# Set visibility to 50 meters (should trigger fog policy)
curl -X POST http://localhost:8080/v1/control/conditions \
  -H "Content-Type: application/json" \
  -d '{"conditions": {"environmental.atmospheric.visibility_m": 50}}'

# Check what model is active now
curl http://localhost:8080/v1/status | python -m json.tool

# Clear overrides
curl -X DELETE http://localhost:8080/v1/control/conditions/overrides
```

## Step 5: Inspect the decision log

Every model switch is logged with the full condition snapshot. Inspect the API
directly:

```bash
curl http://localhost:8080/v1/status | python -m json.tool
```

## Step 6: Cleanup

```bash
make docker-down       # Stop containers (keep data)
make docker-clean      # Stop + remove all data (fresh start)
```

## What just happened?

1. **Generated 3 real ONNX models** (~8KB each) — small Conv→ReLU→Pool→FC networks
2. **Created a model package** with manifest.json + SHA256 checksums
3. **Imported the package** into TEMMS cache with integrity verification
4. **Created a `vision` slot** with `yolov8-daylight` as default
5. **Loaded weather-adaptive policy** from YAML
6. **Started the daemon** — condition loop + policy loop + inference server

When fog conditions were injected, the policy engine matched the `fog-conditions` rule (visibility ≤ 100m), triggered a model switch, logged the decision, and hot-swapped the inference runtime — all in under 100ms.

## Next steps

- [Architecture overview](architecture.md) — how the three tiers fit together
- [Functional testing](functional-testing.md) — local product and acceptance
  checklist
- [Policy reference](policy-reference.md) — full YAML schema for writing policies
- [examples/policies/](https://github.com/LewisJor/temms/tree/main/examples/policies) — real policy files you can study
- Bring your own ONNX models — drop them in `examples/package-example/models/` and update `manifest.json`
