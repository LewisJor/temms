# Example TEMMS Package

This is an example package structure for TEMMS. Packages can come from TEMMS
Hub, an existing registry, build pipeline, package/evidence exchange, or air-gap
export workflow. TEMMS consumes the package locally and decides which candidate
model should run.

## Structure

```
package-example/
├── manifest.json          # Package metadata
├── models/                # Pre-validated model files (place actual .onnx files here)
│   ├── yolov8n-daylight.onnx
│   ├── yolov8n-lowlight.onnx
│   └── mobilenet-v2-tiny.onnx
└── policies/              # Policy YAML files
    └── weather-adaptive.yaml
```

## Usage

To import this package:

```bash
temms import ./examples/package-example/
```

Note: For this example to work, you would need to add actual model files to the `models/` directory and update the SHA256 hashes in `manifest.json`.
