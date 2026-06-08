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

## Step 3: Start the Docker sim environment

This launches two containers:

| Service | URL | What it does |
|---------|-----|-------------|
| TEMMS Daemon | http://localhost:8080 | Edge runtime + inference server |
| MLflow | http://localhost:5001 | Model registry UI |

```bash
make docker-up
```

Wait 15-20 seconds for initialization. Check if it's ready:

```bash
curl http://localhost:8080/v1/health
# {"status":"ok","timestamp":"..."}
```

Open the TEMMS dashboard: http://localhost:8080/ui/

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

Or use the Web UI at http://localhost:8080/ui/conditions — there's an injection form.

## Step 5: Inspect the decision log

Every model switch is logged with the full condition snapshot:

Open http://localhost:8080/ui/decisions or:

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
- [Policy reference](policy-reference.md) — full YAML schema for writing policies
- [examples/policies/](https://github.com/LewisJor/temms/tree/main/examples/policies) — real policy files you can study
- Bring your own ONNX models — drop them in `examples/package-example/models/` and update `manifest.json`
