# TEMMS Quick Start Guide

## Installation

### Prerequisites

- Python 3.10 or higher
- pip and venv
- Linux system (Ubuntu 20.04/22.04 recommended)
- Target hardware: NVIDIA Jetson, Raspberry Pi, or x86 Linux

### Install TEMMS

```bash
# Clone repository
git clone https://github.com/yourusername/temms.git
cd temms

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install TEMMS with ONNX Runtime support
pip install -e ".[onnx]"

# Or install all runtime support
pip install -e ".[all-runtimes]"
```

## Initialize System

```bash
# Initialize with default paths (/etc/temms, /var/lib/temms)
# Note: May require sudo for system paths
sudo temms init

# Or initialize in local directory for development
temms init --config ./local.temms.yaml --data-dir ./local-data
```

This creates:
- Configuration file
- Model storage directories
- Policy directories
- SQLite databases

## Create Your First Slot

Slots represent different model purposes (vision, targeting, navigation, etc.):

```bash
temms slot create vision \
  --description "Primary perception model" \
  --required \
  --default yolov8-daylight \
  --candidates "yolov8-daylight,yolov8-lowlight,yolov8-fog,mobilenet-tiny"
```

View slots:
```bash
temms slot list
```

## Import a Model Package

### Option 1: Use Example Package (Development)

The repository includes an example package structure:

```bash
# View example package
ls examples/package-example/

# Note: You'll need to add actual model files and update hashes
# For now, this will fail without real models
temms import examples/package-example/ --no-verify
```

### Option 2: Create Your Own Package

Create a package directory:

```bash
mkdir -p my-package/{models,policies}
```

Create `my-package/manifest.json`:

```json
{
  "schema_version": "v1",
  "package_id": "pkg-test-001",
  "name": "test-models",
  "version": "1.0.0",
  "created_at": "2024-01-15T10:00:00Z",
  "models": [
    {
      "id": "model-test-001",
      "name": "test-model",
      "version": "1.0.0",
      "format": "onnx",
      "filename": "model.onnx",
      "sha256": "<compute with: sha256sum model.onnx>",
      "size_bytes": 12345678
    }
  ],
  "policies": []
}
```

Add your model file:
```bash
cp /path/to/your/model.onnx my-package/models/
```

Import:
```bash
temms import my-package/
```

## Set Up Conditions

Conditions drive policy decisions. Set some initial conditions:

```bash
# Weather conditions
temms condition set environmental.atmospheric.visibility_m 1000
temms condition set environmental.atmospheric.precipitation none

# Mission state
temms condition set operational.mission.phase patrol
temms condition set operational.mission.priority normal

# View all conditions
temms condition list
```

## Create a Policy

Create `my-policy.yaml`:

```yaml
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: simple-test-policy
  description: Test policy for demo

spec:
  slot: vision

  rules:
    - name: low-visibility
      priority: 50
      conditions:
        all:
          - metric: environmental.atmospheric.visibility_m
            operator: lte
            value: 100
      action:
        switch_to: yolov8-fog

  fallback_chain:
    - yolov8-daylight
    - mobilenet-tiny
```

Load policy (future feature, manual for now):
```bash
# TODO: Implement policy load command
# temms policy load my-policy.yaml
```

## Test Model Switching

### Manual Override (Operator Control)

```bash
# Check current slot status
temms slot status vision

# Manually activate a model
temms slot set vision yolov8-fog --reason "Testing fog model"

# View decision history
temms slot decisions --slot vision
```

### Simulate Condition Changes

```bash
# Simulate fog rolling in
temms condition set environmental.atmospheric.visibility_m 50

# Policy engine would detect this and switch models
# (when daemon is running)

# Clear operator override to let policies work
temms condition clear-overrides
```

## View System Status

```bash
# Overall status
temms status

# Detailed slot status
temms slot status vision

# All conditions
temms condition snapshot

# Recent decisions
temms slot decisions --limit 20
```

## Next Steps

### 1. Run the Daemon (When Implemented)

```bash
temms daemon start
```

This will:
- Collect conditions from sensors
- Evaluate policies periodically
- Automatically switch models
- Serve inference requests

### 2. Use Real Model Files

Replace example models with your actual ONNX/TFLite/TorchScript models.

### 3. Create Production Policies

Based on your specific use case:
- Weather-adaptive for outdoor robots
- Battery-adaptive for power-constrained devices
- Thermal-adaptive for edge devices
- Mission-adaptive for autonomous systems

### 4. Deploy to Edge Device

Use the deployment script:

```bash
sudo bash deploy/install.sh
```

This installs TEMMS as a systemd service.

## Troubleshooting

### Permission Errors

If using system paths (/etc/temms, /var/lib/temms), use sudo:
```bash
sudo temms init
```

Or use local paths for development:
```bash
temms init --config ./local.temms.yaml --data-dir ./local-data
```

### Package Import Fails

Check:
1. manifest.json is valid JSON
2. Model files exist in models/ directory
3. SHA256 hashes match (compute with `sha256sum`)
4. Use `--no-verify` to skip hash checking (dev only)

### Slot Not Found

Create slot first:
```bash
temms slot create <name> --description "..."
```

### Condition Not Found

Conditions must be set before policies can use them:
```bash
temms condition set <path> <value>
```

## Example Workflows

### Workflow 1: Weather-Adaptive Vision

```bash
# Setup
temms slot create vision --description "Weather-adaptive perception" --required
temms import weather-models-package/

# Sunny conditions
temms condition set environmental.atmospheric.visibility_m 5000
temms condition set environmental.celestial.ambient bright
# -> Would activate yolov8-daylight

# Fog rolls in
temms condition set environmental.atmospheric.visibility_m 80
# -> Would activate yolov8-fog

# Night time
temms condition set environmental.celestial.ambient dark
# -> Would activate yolov8-lowlight
```

### Workflow 2: Battery Management

```bash
# Setup
temms slot create vision --description "Battery-aware vision" --required
temms import efficiency-models-package/

# Full battery
temms condition set platform.power.battery_pct 95
# -> Use yolov8-full (high accuracy, high power)

# Low battery
temms condition set platform.power.battery_pct 15
# -> Switch to mobilenet-tiny (low power)

# Critical battery
temms condition set platform.power.battery_pct 5
# -> Switch to minimal-fallback (survival mode)
```

### Workflow 3: Multi-Slot Autonomous System

```bash
# Create slots
temms slot create vision --description "Primary vision" --required
temms slot create targeting --description "Target tracking" --required
temms slot create navigation --description "Path planning" --required

# Import models
temms import vision-package/
temms import targeting-package/
temms import navigation-package/

# Each slot can have independent policies
# and switch independently based on conditions
```

## Learning Resources

- [Architecture Overview](./ARCHITECTURE.md)
- [Policy Reference](./POLICY_REFERENCE.md) (TODO)
- [Condition System](./CONDITIONS.md) (TODO)
- [Example Policies](../examples/policies/)

## Getting Help

- GitHub Issues: https://github.com/yourusername/temms/issues
- Documentation: https://github.com/yourusername/temms/docs
