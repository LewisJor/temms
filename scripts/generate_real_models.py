#!/usr/bin/env python3
"""
Generate valid ONNX models for TEMMS simulation and testing.

Creates small but structurally valid ONNX models that ONNX Runtime can load
and run inference on. These are NOT trained models — they produce random
outputs — but they prove the full pipeline works.

Models generated:
  - yolov8-daylight: Conv-ReLU-Pool-FC network (input: 1x3x224x224, output: 1x80)
  - yolov8-lowlight: Same architecture, different weights
  - mobilenet-tiny:  Lighter architecture (smaller conv, same I/O shape)

Usage:
  python scripts/generate_real_models.py [--output-dir examples/package-example]
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np


def _ensure_onnx():
    """Ensure the onnx package is available."""
    try:
        import onnx  # noqa: F401
        from onnx import helper, TensorProto, numpy_helper  # noqa: F401
        return True
    except ImportError:
        print("ERROR: 'onnx' package not installed. Install with: pip install onnx")
        sys.exit(1)


def _make_conv_relu_pool_fc(
    model_name: str,
    input_channels: int = 3,
    input_h: int = 224,
    input_w: int = 224,
    conv_filters: int = 16,
    kernel_size: int = 3,
    num_classes: int = 80,
    seed: int = 42,
) -> bytes:
    """
    Build a minimal Conv -> ReLU -> GlobalAvgPool -> FC ONNX model.

    Returns serialized ONNX model bytes.
    """
    from onnx import helper, TensorProto, numpy_helper

    rng = np.random.RandomState(seed)

    # --- Conv weights: (out_channels, in_channels, kH, kW) ---
    conv_w = rng.randn(conv_filters, input_channels, kernel_size, kernel_size).astype(np.float32) * 0.1
    conv_b = np.zeros(conv_filters, dtype=np.float32)

    # --- FC weights: (num_classes, conv_filters) ---
    fc_w = rng.randn(num_classes, conv_filters).astype(np.float32) * 0.1
    fc_b = np.zeros(num_classes, dtype=np.float32)

    # Initializers
    conv_w_init = numpy_helper.from_array(conv_w, name="conv_w")
    conv_b_init = numpy_helper.from_array(conv_b, name="conv_b")
    fc_w_init = numpy_helper.from_array(fc_w, name="fc_w")
    fc_b_init = numpy_helper.from_array(fc_b, name="fc_b")

    # Nodes
    conv_node = helper.make_node(
        "Conv",
        inputs=["input", "conv_w", "conv_b"],
        outputs=["conv_out"],
        kernel_shape=[kernel_size, kernel_size],
        pads=[kernel_size // 2, kernel_size // 2, kernel_size // 2, kernel_size // 2],
    )

    relu_node = helper.make_node(
        "Relu",
        inputs=["conv_out"],
        outputs=["relu_out"],
    )

    # GlobalAveragePool: (N, C, H, W) -> (N, C, 1, 1)
    gap_node = helper.make_node(
        "GlobalAveragePool",
        inputs=["relu_out"],
        outputs=["gap_out"],
    )

    # Flatten: (N, C, 1, 1) -> (N, C)
    flatten_node = helper.make_node(
        "Flatten",
        inputs=["gap_out"],
        outputs=["flat_out"],
        axis=1,
    )

    # Gemm (FC layer): (N, C) x (num_classes, C)^T + bias
    fc_node = helper.make_node(
        "Gemm",
        inputs=["flat_out", "fc_w", "fc_b"],
        outputs=["output"],
        transB=1,
    )

    # Graph I/O
    input_tensor = helper.make_tensor_value_info(
        "input", TensorProto.FLOAT, [1, input_channels, input_h, input_w]
    )
    output_tensor = helper.make_tensor_value_info(
        "output", TensorProto.FLOAT, [1, num_classes]
    )

    graph = helper.make_graph(
        nodes=[conv_node, relu_node, gap_node, flatten_node, fc_node],
        name=model_name,
        inputs=[input_tensor],
        outputs=[output_tensor],
        initializer=[conv_w_init, conv_b_init, fc_w_init, fc_b_init],
    )

    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8

    # Validate
    from onnx import checker
    checker.check_model(model)

    return model.SerializeToString()


def _compute_sha256(data: bytes) -> str:
    """Compute SHA256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def generate_models(output_dir: Path) -> dict:
    """
    Generate all three ONNX models and write them to output_dir/models/.

    Returns a dict mapping filename -> {sha256, size_bytes, input_shape}.
    """
    models_dir = output_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    specs = [
        {
            "filename": "yolov8n-daylight.onnx",
            "model_name": "yolov8-daylight",
            "conv_filters": 16,
            "kernel_size": 3,
            "seed": 42,
            "input_shape": [1, 3, 224, 224],
        },
        {
            "filename": "yolov8n-lowlight.onnx",
            "model_name": "yolov8-lowlight",
            "conv_filters": 16,
            "kernel_size": 3,
            "seed": 123,  # Different seed = different weights
            "input_shape": [1, 3, 224, 224],
        },
        {
            "filename": "mobilenet-v2-tiny.onnx",
            "model_name": "mobilenet-tiny",
            "conv_filters": 8,   # Lighter model
            "kernel_size": 3,
            "seed": 789,
            "input_shape": [1, 3, 224, 224],
        },
    ]

    results = {}

    for spec in specs:
        print(f"  Generating {spec['filename']}...")
        model_bytes = _make_conv_relu_pool_fc(
            model_name=spec["model_name"],
            input_channels=spec["input_shape"][1],
            input_h=spec["input_shape"][2],
            input_w=spec["input_shape"][3],
            conv_filters=spec["conv_filters"],
            kernel_size=spec["kernel_size"],
            num_classes=80,
            seed=spec["seed"],
        )

        out_path = models_dir / spec["filename"]
        out_path.write_bytes(model_bytes)

        sha256 = _compute_sha256(model_bytes)
        size_bytes = len(model_bytes)

        results[spec["filename"]] = {
            "sha256": sha256,
            "size_bytes": size_bytes,
            "input_shape": spec["input_shape"],
            "model_name": spec["model_name"],
        }

        print(f"    -> {out_path} ({size_bytes} bytes, sha256={sha256[:16]}...)")

    return results


def update_manifest(output_dir: Path, model_info: dict) -> None:
    """
    Update manifest.json with correct SHA256 hashes and sizes.
    """
    manifest_path = output_dir / "manifest.json"

    if not manifest_path.exists():
        print(f"  No manifest.json found at {manifest_path}, creating new one.")

    manifest = {
        "schema_version": "v1",
        "package_id": "pkg-vision-models-20240115",
        "name": "vision-models",
        "version": "1.0.0",
        "description": "YOLOv8 vision models for various conditions (sim-generated)",
        "created_at": "2024-01-15T10:00:00Z",
        "created_by": "temms-sim-generator",
        "models": [
            {
                "id": "model-yolov8-daylight-001",
                "name": "yolov8-daylight",
                "version": "1.0.0",
                "format": "onnx",
                "filename": "yolov8n-daylight.onnx",
                "sha256": model_info["yolov8n-daylight.onnx"]["sha256"],
                "size_bytes": model_info["yolov8n-daylight.onnx"]["size_bytes"],
                "metadata": {
                    "input_shape": [1, 3, 224, 224],
                    "classes": 80,
                    "description": "YOLOv8n optimized for daylight conditions"
                }
            },
            {
                "id": "model-yolov8-lowlight-001",
                "name": "yolov8-lowlight",
                "version": "1.0.0",
                "format": "onnx",
                "filename": "yolov8n-lowlight.onnx",
                "sha256": model_info["yolov8n-lowlight.onnx"]["sha256"],
                "size_bytes": model_info["yolov8n-lowlight.onnx"]["size_bytes"],
                "metadata": {
                    "input_shape": [1, 3, 224, 224],
                    "classes": 80,
                    "description": "YOLOv8n optimized for low-light conditions"
                }
            },
            {
                "id": "model-mobilenet-tiny-001",
                "name": "mobilenet-tiny",
                "version": "1.0.0",
                "format": "onnx",
                "filename": "mobilenet-v2-tiny.onnx",
                "sha256": model_info["mobilenet-v2-tiny.onnx"]["sha256"],
                "size_bytes": model_info["mobilenet-v2-tiny.onnx"]["size_bytes"],
                "metadata": {
                    "input_shape": [1, 3, 224, 224],
                    "classes": 80,
                    "description": "MobileNetV2 for low-power fallback"
                }
            }
        ],
        "policies": [
            {
                "name": "weather-adaptive",
                "filename": "weather-adaptive.yaml",
                "slot": "vision"
            }
        ],
        "source_registry": "mlflow://production",
        "mlflow_run_id": "abc123def456",
        "tags": {
            "environment": "simulation",
            "platform": "docker-sim",
            "use_case": "autonomous-patrol"
        }
    }

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    print(f"  Updated manifest: {manifest_path}")


def verify_models(output_dir: Path) -> bool:
    """
    Verify generated models load correctly with ONNX Runtime.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        print("  WARNING: onnxruntime not installed, skipping verification")
        return True

    models_dir = output_dir / "models"
    success = True

    for onnx_file in sorted(models_dir.glob("*.onnx")):
        try:
            session = ort.InferenceSession(str(onnx_file))
            inp = session.get_inputs()[0]
            out = session.get_outputs()[0]

            # Run dummy inference
            dummy_input = np.random.randn(*inp.shape).astype(np.float32)
            results = session.run(None, {inp.name: dummy_input})

            print(f"  OK  {onnx_file.name}: input={inp.name}{inp.shape} -> output={out.name}{out.shape}")
            print(f"      Output sample: {results[0][0][:5]}...")
        except Exception as e:
            print(f"  FAIL {onnx_file.name}: {e}")
            success = False

    return success


def main():
    parser = argparse.ArgumentParser(description="Generate ONNX models for TEMMS sim")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent.parent / "examples" / "package-example",
        help="Output directory (default: examples/package-example)",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip ONNX Runtime verification",
    )
    args = parser.parse_args()

    _ensure_onnx()

    print(f"\n=== TEMMS Model Generator ===")
    print(f"Output: {args.output_dir}\n")

    print("1. Generating ONNX models...")
    model_info = generate_models(args.output_dir)

    print("\n2. Updating manifest.json...")
    update_manifest(args.output_dir, model_info)

    if not args.skip_verify:
        print("\n3. Verifying models with ONNX Runtime...")
        if verify_models(args.output_dir):
            print("\nAll models verified successfully!")
        else:
            print("\nSome models failed verification!")
            sys.exit(1)
    else:
        print("\n3. Skipping verification (--skip-verify)")

    print(f"\nDone! Models written to {args.output_dir}/models/")


if __name__ == "__main__":
    main()
