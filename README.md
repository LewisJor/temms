# TEMMS - Tactical Edge Model Management System

**Offline-first ML model management for edge devices in DDIL (Denied, Degraded, Intermittent, Limited) environments.**

Target platforms: NVIDIA Jetson (Nano/Orin), Raspberry Pi 4/5, and similar ARM64/x86 edge hardware.

## Core Value Proposition

**"Manage ML models on devices that can't always phone home."**

TEMMS enables autonomous systems to:
- **Switch models** based on environmental conditions (weather, lighting, temperature)
- **Operate offline** with pre-packaged model updates via USB/SD card
- **Manage multiple models** concurrently across different slots (vision, targeting, navigation)
- **Make deterministic decisions** with full audit trails
- **Degrade gracefully** when conditions change or models fail

## Architecture: Three-Tier Design

```
┌──────────────────────────────────────────────────────────────┐
│            MLflow (Local or Cloud - Your Choice)             │
│  - Standard model registry (not modified)                     │
│  - Can run completely offline (SQLite + local storage)       │
│  - Teams use familiar MLflow UI                              │
│  - Track experiments, versions, metrics                       │
└────────────────┬─────────────────────────────────────────────┘
                 │
                 │ Local network or same machine
                 │
┌────────────────▼─────────────────────────────────────────────┐
│              TEMMS Hub (Your IP - Local or Cloud)            │
│  - DDIL sync layer                                           │
│  - Packages models from MLflow into TEMMS packages           │
│  - Delta updates, fleet management                           │
│  - Works with local or cloud MLflow                          │
└────────────────┬─────────────────────────────────────────────┘
                 │
                 │ USB/SD card or local network transfer
                 │
┌────────────────▼─────────────────────────────────────────────┐
│                TEMMS Daemon (Edge Device)                    │
│  - NO registry logic (cache only)                            │
│  - Receives pre-packaged models                              │
│  - Multi-slot management                                     │
│  - Policy-driven switching                                   │
│  - Runs completely offline                                   │
└──────────────────────────────────────────────────────────────┘
```

### Offline Capability

**All three tiers can run completely offline:**

- **MLflow**: Run locally with SQLite backend and file storage
- **Hub**: Reads from local MLflow, outputs packages to filesystem/USB
- **Daemon**: Imports packages, runs with zero network dependencies

See [Local MLflow Setup](docs/LOCAL_MLFLOW_SETUP.md) for full offline configuration.

### What TEMMS Daemon Does (Build Now)

- **Package Import**: Accept pre-validated model packages
- **Multi-Slot Management**: Run multiple models concurrently (vision, targeting, navigation)
- **Condition System**: Monitor environment, platform, and operational state
- **Policy Engine**: Evaluate rules and switch models automatically
- **Fallback Chains**: Deterministic degradation when models fail
- **Inference Serving**: gRPC/HTTP API for model inference
- **Decision Audit**: Log every model switch with full context

### What TEMMS Hub Does (Build Later)

- Package models from MLflow into TEMMS format
- Delta updates (only send changed models)
- Fleet-wide sync coordination
- Offline distribution manifest generation

## Quick Start

### Installation

```bash
# Clone repository
git clone https://github.com/yourusername/temms.git
cd temms

# Install TEMMS Daemon
pip install -e ".[onnx]"  # With ONNX Runtime support

# Optional: Install with all ML runtimes
pip install -e ".[all-runtimes]"

# Optional: Install MLflow for local model registry
pip install -e ".[mlflow]"
```

### Set Up Local MLflow (Optional but Recommended)

For completely offline operation:

```bash
# Quick setup script
bash scripts/setup-local-mlflow.sh

# Or manual setup
pip install mlflow
mlflow server \
  --backend-store-uri sqlite:///~/mlflow-local/mlflow.db \
  --default-artifact-root ~/mlflow-local/artifacts \
  --host 127.0.0.1 \
  --port 5000

# Access UI at http://localhost:5000
```

See [Local MLflow Setup](docs/LOCAL_MLFLOW_SETUP.md) for detailed instructions.

### Initialize TEMMS

```bash
# Initialize configuration
temms init

# Or specify custom paths
temms init --config ./local.temms.yaml --data-dir ./local-data
```

### Import a Model Package

```bash
# Import pre-packaged models and policies
temms import ./path/to/temms-package/

# Skip hash verification (faster, less safe)
temms import ./path/to/package/ --no-verify
```

### Configure Slots

```bash
# Create a slot for vision models
temms slot create vision \
  --description "Primary perception model" \
  --required \
  --default yolov8-daylight \
  --candidates "yolov8-daylight,yolov8-lowlight,yolov8-fog"

# List all slots
temms slot list

# Check slot status
temms slot status vision
```

### Manage Conditions

```bash
# Set operator override (highest priority)
temms condition set weather.visibility_m 50

# Set mission parameters
temms condition set operational.mission.phase patrol
temms condition set operational.mission.priority normal

# View all conditions
temms condition list

# View nested condition snapshot
temms condition snapshot
```

### Manual Model Switching

```bash
# Activate specific model in a slot
temms slot set vision yolov8-fog --reason "Heavy fog detected"

# View decision history
temms slot decisions --slot vision --limit 10
```

### Check System Status

```bash
temms status
```

## Package Format

TEMMS expects pre-packaged model bundles created by the Hub:

```
temms-package/
├── manifest.json          # Package metadata
├── models/                # Pre-validated model files
│   ├── yolov8-daylight.onnx
│   ├── yolov8-lowlight.onnx
│   └── mobilenet-tiny.onnx
└── policies/              # Policy YAML files
    ├── weather-adaptive.yaml
    └── thermal-adaptive.yaml
```

### Example manifest.json

```json
{
  "schema_version": "v1",
  "package_id": "pkg-vision-20240115",
  "name": "vision-models",
  "version": "1.0.0",
  "created_by": "mlflow-packager",
  "models": [
    {
      "id": "model-yolov8-daylight-001",
      "name": "yolov8-daylight",
      "version": "1.0.0",
      "format": "onnx",
      "filename": "yolov8-daylight.onnx",
      "sha256": "abc123...",
      "size_bytes": 12345678
    }
  ],
  "policies": [
    {
      "name": "weather-adaptive",
      "filename": "weather-adaptive.yaml",
      "slot": "vision"
    }
  ]
}
```

## Policy-Driven Model Switching

Policies define rules for automatic model switching based on conditions:

```yaml
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: weather-adaptive-vision
spec:
  slot: vision

  rules:
    - name: fog-conditions
      priority: 80
      conditions:
        any:
          - metric: environmental.atmospheric.visibility_m
            operator: lte
            value: 100
          - metric: environmental.atmospheric.precipitation
            operator: in
            value: [fog, mist]
      action:
        switch_to: yolov8-fog

  fallback_chain:
    - yolov8-daylight
    - yolov8-lowlight
    - mobilenet-minimal
```

See `examples/policies/` for more examples.

## Condition System

TEMMS monitors conditions from multiple sources with priority levels:

| Priority | Source | Example |
|----------|--------|---------|
| 1000 | Operator override | Manual condition injection via CLI/API |
| 100 | Onboard sensors | CPU temp, battery, memory |
| 90 | Derived/computed | Time of day, sun position |
| 50 | External data | Weather API (when connected) |
| 10 | Cached | Last-known-good values |

### Condition Categories

- **Environmental**: weather, lighting, terrain, electromagnetic
- **Operational**: mission phase, threat level, coordination
- **Platform**: power, compute, sensors, mobility
- **Operator**: overrides, authority level, attention state

## Multi-Slot Architecture

Autonomous systems run multiple models concurrently:

```
┌─────────────────────────────────────────────────────┐
│              TEMMS Daemon Slots                     │
├─────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────┐  │
│  │ vision       │  │ targeting    │  │ nav      │  │
│  │ yolov8-fog   │  │ thermal-v2   │  │ lidar    │  │
│  │ (running)    │  │ (running)    │  │ (running)│  │
│  └──────────────┘  └──────────────┘  └──────────┘  │
└─────────────────────────────────────────────────────┘
```

Each slot:
- Runs independently
- Has its own policies
- Tracks activation history
- Can be required or optional

## Decision Audit

Every model switch is logged with full context:

```bash
$ temms slot decisions --slot vision

Timestamp            From           To             Trigger
2024-01-15 14:30:00  yolov8-day     yolov8-fog     policy: fog-conditions
2024-01-15 14:45:00  yolov8-fog     yolov8-thermal operator: manual override
2024-01-15 15:00:00  yolov8-thermal yolov8-day     policy: clear-weather
```

## Development

### Project Structure

```
temms/
├── src/temms/
│   ├── core/           # Cache, storage, package import
│   ├── slots/          # Multi-slot management
│   ├── conditions/     # Condition store and collectors
│   ├── policy/         # Policy engine
│   ├── inference/      # Inference server (TODO)
│   ├── daemon/         # Main daemon loop (TODO)
│   └── cli/            # CLI commands
├── tests/
├── examples/
│   ├── policies/       # Example policy files
│   └── package-example/ # Example package structure
└── docs/
```

### Running Tests

```bash
make test
```

### Code Quality

```bash
make format  # Format with black
make lint    # Lint with ruff and mypy
```

## Roadmap

### ✅ Phase 1: Core Infrastructure (Complete)
- [x] Package import and cache
- [x] Multi-slot management
- [x] Condition system with priorities
- [x] Policy engine with slot-awareness
- [x] CLI for import, slot, condition management
- [x] Decision audit logging

### 🚧 Phase 2: Runtime & Inference (In Progress)
- [ ] Inference server (gRPC/HTTP)
- [ ] Model loader with hot-swap
- [ ] Daemon with policy evaluation loop
- [ ] Condition collectors (system metrics, time-based)
- [ ] Fallback chain execution

### 📋 Phase 3: Hub Integration (Planned)
- [ ] Hub: MLflow to TEMMS package converter
- [ ] Hub: Delta update generation
- [ ] Hub: Fleet sync coordination
- [ ] Opportunistic cloud sync in daemon

### 🎯 Phase 4: Advanced Features
- [ ] Model ensemble/voting
- [ ] Predictive model pre-loading
- [ ] Cross-slot dependencies
- [ ] Swarm condition sharing
- [ ] Graceful degradation profiles

## Why TEMMS?

Existing edge ML tools (AWS Greengrass, Azure IoT Edge) assume connectivity. TEMMS is designed for:

- **Military/defense** - Autonomous systems in contested environments
- **Remote operations** - Oil rigs, mines, agricultural robots
- **Disaster response** - Drones and robots in areas without infrastructure
- **Space/maritime** - Extended offline operation periods

## License

Apache 2.0

## Contributing

Contributions welcome! Please see CONTRIBUTING.md for guidelines.

## References

- [ONNX Runtime](https://onnxruntime.ai/)
- [NVIDIA Jetson Documentation](https://docs.nvidia.com/jetson/)
- [MLflow](https://mlflow.org/)
