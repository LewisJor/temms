# Example TEMMS Package

This is an example package structure for TEMMS. In production, packages would be created by the TEMMS Hub from MLflow models.

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

## Readiness Evidence

Hub displays `Signed`, `Sim`, `Test`, and `Val` from manifest evidence. Import
records hash validation automatically. Add these fields when your build pipeline
has produced the evidence:

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

Set `TEMMS_TRUSTED_SIGNATURE_KEYS='{"builder-key":"base64-ed25519-public-key"}'`
or `TEMMS_TRUSTED_SIGNATURE_KEYS_FILE` before import so Hub can verify the
manifest signature locally. Signature verification requires Python
`cryptography`; if it is unavailable, Hub leaves `Signed` unverified.

Use `temms package keygen --key-id builder-key --private-key ./builder.key
--trusted-keys ./trusted-keys.json` and `temms package sign ./examples/package-example
--key-id builder-key --private-key ./builder.key` to create a locally verifiable
package signature.

Hub requires all four evidence checks before deploy by default. For local smoke
tests, set `TEMMS_HUB_REQUIRED_EVIDENCE=val` to permit hash-validated-only
deployment.
