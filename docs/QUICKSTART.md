# Quickstart

This gets you from zero to watching TEMMS switch models in under 5 minutes.

## Prerequisites

- Python 3.11+
- Node.js 20+ and npm (for the React Hub product UI)
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

## Product UI: Mission Package Workbench

The Hub product UI is a Vite + React + TypeScript app served by the daemon at
`/ui/hub`. The UI package lives in `ui/`, but the repo root exposes npm scripts
for the normal demo workflow:

```bash
npm --prefix ui install
npm run typecheck
npm run build
npm run smoke:workbench
```

Use `npm run ui:ci` when you want the CI-equivalent shortcut for typecheck,
build, and smoke in one command.

The equivalent Makefile targets are `make ui-install`, `make ui-typecheck`,
`make ui-build`, `make ui-smoke`, and the CI-equivalent `make ui-ci`.

The first screen is **Mission Package Workbench**. It follows the demo path
**Mission -> Model Plan -> Runtime Fit -> Sensor Handling -> Package Handoff ->
Edge Deploy -> Field Ops**. The primary path turns a mission spec/YAML into a
selected model, target runtime, sensor/model handling policy, signed mission
package, and edge rollout intent. The first viewport is a **Mission workflow
cockpit**: the operator path rail chooses the stage, the stage focus panel shows
the current decision and next action, package path signals show mission/model/
runtime/handling/package state, and **Live context** keeps inventory, rollout,
evidence, and DDIL telemetry available without turning the hub into a dashboard
dump. Setup-only controls such as package registration and edge enrollment are
under **Advanced intake**, while direct rollout forms are under **Manual controls**.

The same handoff can run from a mission YAML file in the CLI:

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

For a seeded local UI rehearsal, use the functional testing checklist:

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
an online `edge-sim` node, so the Hub opens with model inventory ready for a
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

Verify the live daemon is serving the current Mission Package Workbench
contract, including explicit JSON planning, YAML-only mission planning, and
package download:

```bash
make docker-product-smoke
```

Open the TEMMS Hub product UI: http://localhost:8080/ui/hub

The first screen should be **Mission Package Workbench**. It keeps the
mission-to-package path in front with the **Mission workflow cockpit** and
places inventory, rollout, evidence, and DDIL health under **Live context** so
the demo starts with the edge packaging workflow, not a telemetry dump.

Hub-enabled daemons also redirect http://localhost:8080/ui/ to the Hub product
UI.

Open the MLflow UI: http://localhost:5001

If that port is already in use, choose another host port:

```bash
MLFLOW_HOST_PORT=5050 make docker-up
```

## Step 4: See model switching in action

### Option A: Visual simulation (recommended)

This opens a live window showing weather effects + model switching in real time.

```bash
# Install with GUI support (has opencv with display)
pip install -e ".[dev,sim-visual]"

# Run the fog scenario
make sim-visual
```

What you'll see:
1. **Left panel**: Original synthetic driving scene
2. **Right panel**: Same scene with weather effects applied
3. **Status bar**: Active model, latency, current conditions

Watch as fog rolls in → TEMMS switches from `yolov8-daylight` to `yolov8-lowlight`.

**Keyboard controls:**
- `q` — quit
- `s` — skip to next scenario step
- `p` — pause/resume

Other scenarios:
```bash
make sim-visual-night    # Day → night → dawn
make sim-visual-rain     # Clear → downpour → clearing
make sim-visual-stress   # Multi-factor stress test
```

### Option B: Headless simulation

If you don't have a display (or you're SSH'd into a server):

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

In standalone agent mode, the diagnostic Web UI at
http://localhost:8080/ui/conditions still exposes an injection form. In
Hub-enabled demos, diagnostic UI paths redirect to the product cockpit at
`/ui/hub` so the demo stays on the mission-to-edge flow.

## Step 5: Inspect the decision log

Every model switch is logged with the full condition snapshot:

In standalone agent mode, open the diagnostic decision log at
http://localhost:8080/ui/decisions. In Hub-enabled demos, use the **Field Ops**
step in `/ui/hub`, or inspect the API directly:

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
- [Functional testing](functional-testing.md) — local product UI and acceptance
  checklist
- [Policy reference](policy-reference.md) — full YAML schema for writing policies
- [examples/policies/](https://github.com/LewisJor/temms/tree/main/examples/policies) — real policy files you can study
- Bring your own ONNX models — drop them in `examples/package-example/models/` and update `manifest.json`
