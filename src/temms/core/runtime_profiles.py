"""
Runtime and device capability helpers for edge compatibility checks.
"""

from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MVP_DEVICE_PROFILES = {
    "x86_64-cpu": {
        "description": "Generic x86_64 VM or mini PC using CPU runtimes",
        "arch": "x86_64",
        "runtime_defaults": {
            "onnx_providers": ["CPUExecutionProvider"],
        },
    },
    "arm64-jetson": {
        "description": "NVIDIA Jetson-class ARM64 system",
        "arch": "arm64",
        "runtime_defaults": {
            "onnx_providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        },
    },
    "rpi5-tflite": {
        "description": "Raspberry Pi 5 optimized for TensorFlow Lite",
        "arch": "arm64",
        "runtime_defaults": {
            "onnx_providers": ["CPUExecutionProvider"],
            "tflite_num_threads": 4,
        },
    },
    "orin-tensorrt": {
        "description": "NVIDIA Jetson Orin optimized for TensorRT engines",
        "arch": "arm64",
        "runtime_defaults": {
            "onnx_providers": [
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            ],
        },
    },
}

DEFAULT_RUNTIME_TARGETS = {
    "temms-x86_64-cpu": {
        "runtime_target_id": "temms-x86_64-cpu",
        "name": "TEMMS x86_64 CPU",
        "description": "Default Linux amd64 CPU runtime for VM and laptop edge simulations",
        "image": "temms/agent:inference-amd64",
        "registry": "docker.io",
        "os": "linux",
        "arch": "amd64",
        "device_profiles": ["x86_64-cpu"],
        "runtimes": {
            "onnxruntime": {
                "available": True,
                "providers": ["CPUExecutionProvider"],
            }
        },
        "accelerators": {},
        "runtime_constraints": {
            "device_profiles": ["x86_64-cpu"],
            "runtimes": ["onnxruntime"],
            "providers": ["CPUExecutionProvider"],
        },
        "source": "default",
        "default": True,
    },
    "temms-arm64-jetson": {
        "runtime_target_id": "temms-arm64-jetson",
        "name": "TEMMS ARM64 Jetson",
        "description": "Default Linux arm64 Jetson-class runtime with CUDA-capable ONNX providers",
        "image": "temms/agent:inference-arm64-jetson",
        "registry": "docker.io",
        "os": "linux",
        "arch": "arm64",
        "device_profiles": ["arm64-jetson"],
        "runtimes": {
            "onnxruntime": {
                "available": True,
                "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            }
        },
        "accelerators": {"nvidia": {"available": True}},
        "runtime_constraints": {
            "device_profiles": ["arm64-jetson"],
            "runtimes": ["onnxruntime"],
            "preferred_providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            "accelerators": ["nvidia"],
        },
        "source": "default",
        "default": True,
    },
    "temms-rpi5-tflite": {
        "runtime_target_id": "temms-rpi5-tflite",
        "name": "TEMMS Raspberry Pi 5 TFLite",
        "description": "Default Linux arm64 runtime for Raspberry Pi 5 TensorFlow Lite sims",
        "image": "temms/agent:inference-arm64-rpi5",
        "registry": "docker.io",
        "os": "linux",
        "arch": "arm64",
        "device_profiles": ["rpi5-tflite"],
        "runtimes": {
            "tflite_runtime": {
                "available": True,
                "options": {"num_threads": 4},
            }
        },
        "accelerators": {},
        "runtime_constraints": {
            "device_profiles": ["rpi5-tflite"],
            "runtimes": ["tflite_runtime"],
        },
        "source": "default",
        "default": True,
    },
    "temms-orin-tensorrt": {
        "runtime_target_id": "temms-orin-tensorrt",
        "name": "TEMMS Orin TensorRT",
        "description": "Default Linux arm64 Orin runtime for TensorRT/CUDA optimized models",
        "image": "temms/agent:inference-arm64-orin",
        "registry": "docker.io",
        "os": "linux",
        "arch": "arm64",
        "device_profiles": ["orin-tensorrt"],
        "runtimes": {
            "onnxruntime": {
                "available": True,
                "providers": [
                    "TensorrtExecutionProvider",
                    "CUDAExecutionProvider",
                    "CPUExecutionProvider",
                ],
            },
            "tensorrt": {"available": True},
        },
        "accelerators": {"nvidia": {"available": True}},
        "runtime_constraints": {
            "device_profiles": ["orin-tensorrt"],
            "runtimes": ["onnxruntime", "tensorrt"],
            "preferred_providers": [
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
            ],
            "accelerators": ["nvidia"],
        },
        "source": "default",
        "default": True,
    },
}

DEVICE_PROFILE_ALIASES = {
    "x86-64-cpu": "x86_64-cpu",
    "amd64-cpu": "x86_64-cpu",
    "x64-cpu": "x86_64-cpu",
    "aarch64-jetson": "arm64-jetson",
    "jetson": "arm64-jetson",
    "jetson-arm64": "arm64-jetson",
    "jetson-orin": "orin-tensorrt",
    "orin": "orin-tensorrt",
    "nvidia-orin": "orin-tensorrt",
    "rpi5": "rpi5-tflite",
    "raspberry-pi-5": "rpi5-tflite",
    "raspberrypi5": "rpi5-tflite",
}


@dataclass
class RuntimeCapabilities:
    """Detected runtime and hardware capabilities for this edge device."""

    os: str
    machine: str
    python: str
    device_profile: str
    arch: str | None = None
    board_model: str | None = None
    runtimes: dict[str, Any] = field(default_factory=dict)
    accelerators: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "os": self.os,
            "machine": self.machine,
            "arch": self.arch or _normalize_arch(self.machine),
            "python": self.python,
            "device_profile": self.device_profile,
            "board_model": self.board_model,
            "runtimes": self.runtimes,
            "accelerators": self.accelerators,
        }


def detect_runtime_capabilities() -> RuntimeCapabilities:
    """Detect optional inference runtimes, providers, and accelerators."""
    tflite_status = _tflite_status()
    runtimes = {
        "onnx": _runtime_status("onnxruntime"),
        "onnxruntime": _runtime_status("onnxruntime"),
        "tensorflow": _runtime_status("tensorflow"),
        "tflite": tflite_status,
        "tflite_runtime": _runtime_status("tflite_runtime"),
        "torch": _runtime_status("torch"),
        "torchscript": _runtime_status("torch"),
        "tensorrt": _runtime_status("tensorrt"),
    }
    accelerators = _detect_accelerators()
    machine = platform.machine() or "unknown"
    board_model = _detect_board_model()

    return RuntimeCapabilities(
        os=platform.platform(),
        machine=machine,
        arch=_normalize_arch(machine),
        python=sys.version.split()[0],
        device_profile=_infer_device_profile(machine, runtimes, accelerators, board_model),
        board_model=board_model,
        runtimes=runtimes,
        accelerators=accelerators,
    )


def normalize_device_profile(profile: str | None) -> str | None:
    """Return the canonical MVP device profile for a user or detected value."""
    if profile is None:
        return None
    normalized = profile.strip().lower().replace("_", "-")
    return DEVICE_PROFILE_ALIASES.get(normalized, normalized)


def known_device_profiles() -> dict[str, dict[str, Any]]:
    """Return the canonical MVP device profile registry."""
    return dict(MVP_DEVICE_PROFILES)


def default_runtime_targets() -> dict[str, dict[str, Any]]:
    """Return built-in container runtime targets for Hub Lite."""
    return {target_id: dict(target) for target_id, target in DEFAULT_RUNTIME_TARGETS.items()}


def runtime_defaults_for_profile(
    profile: str | None,
    capabilities: RuntimeCapabilities | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return runtime defaults for a device profile, filtered by local capability."""
    normalized = normalize_device_profile(profile)
    metadata = MVP_DEVICE_PROFILES.get(normalized or "", {})
    defaults = dict(metadata.get("runtime_defaults", {}))

    if not defaults and normalized and normalized.endswith("-nvidia"):
        defaults["onnx_providers"] = ["CUDAExecutionProvider", "CPUExecutionProvider"]

    if capabilities is None:
        capabilities = detect_runtime_capabilities()
    if isinstance(capabilities, RuntimeCapabilities):
        capabilities = capabilities.to_dict()
    elif not isinstance(capabilities, dict):
        capabilities = {
            "device_profile": getattr(capabilities, "device_profile", None),
            "runtimes": getattr(capabilities, "runtimes", {}),
            "accelerators": getattr(capabilities, "accelerators", {}),
        }

    providers = defaults.get("onnx_providers")
    if providers:
        onnx_status = capabilities.get("runtimes", {}).get("onnxruntime", {})
        available = onnx_status.get("providers", [])
        if available or "providers" in onnx_status:
            defaults["onnx_providers"] = [
                provider for provider in providers if provider in available
            ]
        elif onnx_status.get("available") is False:
            defaults.pop("onnx_providers", None)

    return defaults


def runtime_constraints_satisfied(
    constraints: dict[str, Any] | None,
    capabilities: RuntimeCapabilities | dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Return whether runtime constraints are satisfied and why not."""
    if not constraints:
        return True, []

    if capabilities is None:
        capabilities = detect_runtime_capabilities()
    if isinstance(capabilities, RuntimeCapabilities):
        capabilities = capabilities.to_dict()

    reasons: list[str] = []
    runtimes = capabilities.get("runtimes", {})
    accelerators = capabilities.get("accelerators", {})
    device_profile = normalize_device_profile(capabilities.get("device_profile"))

    required_profiles = [
        profile
        for profile in (
            normalize_device_profile(profile) for profile in constraints.get("device_profiles", [])
        )
        if profile
    ]
    if required_profiles and device_profile not in required_profiles:
        reasons.append(f"device profile {device_profile} is not in {sorted(required_profiles)}")

    required_runtimes = constraints.get("runtimes") or []
    missing_runtimes = [
        runtime for runtime in required_runtimes if not _runtime_available(runtimes, runtime)
    ]
    if missing_runtimes:
        reasons.append(f"missing runtimes: {', '.join(sorted(missing_runtimes))}")

    required_providers = constraints.get("providers") or []
    available_providers = set(runtimes.get("onnxruntime", {}).get("providers", []))
    if required_providers:
        missing_providers = sorted(set(required_providers) - available_providers)
        if missing_providers:
            reasons.append(f"missing ONNX providers: {', '.join(missing_providers)}")

    preferred_providers = (
        constraints.get("provider_order") or constraints.get("preferred_providers") or []
    )
    if preferred_providers and not any(
        provider in available_providers for provider in preferred_providers
    ):
        reasons.append(
            "none of the preferred ONNX providers are available: "
            + ", ".join(str(provider) for provider in preferred_providers)
        )

    required_accelerators = constraints.get("accelerators") or []
    missing_accelerators = [
        accel
        for accel in required_accelerators
        if not accelerators.get(accel, {}).get("available", False)
    ]
    if missing_accelerators:
        reasons.append(f"missing accelerators: {', '.join(sorted(missing_accelerators))}")

    if constraints.get("requires_gpu") and not any(
        accelerator.get("available", False) for accelerator in accelerators.values()
    ):
        reasons.append("GPU accelerator is required but none was detected")

    return not reasons, reasons


def package_runtime_constraints(
    manifest: dict[str, Any],
    model_id: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Extract runtime constraints from a package manifest."""
    compatibility = (
        manifest.get("compatibility") if isinstance(manifest.get("compatibility"), dict) else {}
    )
    package_constraints = dict(manifest.get("runtime_constraints", {}) or {})
    package_constraints.update(compatibility.get("runtime_constraints") or {})
    extracted: list[tuple[str, dict[str, Any]]] = []

    for model in manifest.get("models", []):
        if model_id and model.get("id") != model_id:
            continue
        constraints = dict(package_constraints)
        constraints.update(model.get("runtime_constraints", {}))
        if constraints:
            extracted.append((model.get("id", "unknown"), constraints))

    return extracted


def _runtime_status(module_name: str) -> dict[str, Any]:
    status: dict[str, Any] = {"available": importlib.util.find_spec(module_name) is not None}
    if not status["available"]:
        return status

    if module_name == "onnxruntime":
        try:
            import onnxruntime as ort

            status["providers"] = ort.get_available_providers()
        except Exception:
            status["providers"] = []

    return status


def _tflite_status() -> dict[str, Any]:
    """Return TensorFlow Lite availability through either standalone or TensorFlow runtime."""
    standalone = _runtime_status("tflite_runtime")
    if standalone.get("available"):
        standalone["module"] = "tflite_runtime"
        return standalone

    tensorflow = _runtime_status("tensorflow")
    return {
        "available": bool(tensorflow.get("available")),
        "module": "tensorflow" if tensorflow.get("available") else None,
    }


def _runtime_available(runtimes: dict[str, Any], name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    aliases = {
        "onnx": "onnxruntime",
        "ort": "onnxruntime",
        "tflite": "tflite_runtime",
        "torchscript": "torch",
        "trt": "tensorrt",
    }
    key = aliases.get(normalized, normalized)
    return bool(runtimes.get(key, {}).get("available", False))


def _normalize_arch(machine: str | None) -> str:
    """Return a canonical architecture label for diagnostics and compatibility."""
    arch = (machine or "unknown").lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }
    return aliases.get(arch, arch)


def _detect_accelerators() -> dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    tegrastats = shutil.which("tegrastats")
    accelerators = {
        "nvidia": {
            "available": bool(nvidia_smi or Path("/dev/nvidia0").exists()),
            "tool": nvidia_smi,
            "device_file": Path("/dev/nvidia0").exists(),
        },
        "jetson": {
            "available": bool(tegrastats),
            "tool": tegrastats,
        },
    }

    if nvidia_smi:
        try:
            output = subprocess.run(
                [nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if output.stdout.strip():
                accelerators["nvidia"]["devices"] = [
                    line.strip() for line in output.stdout.splitlines() if line.strip()
                ]
        except Exception:
            pass

    return accelerators


def _detect_board_model() -> str | None:
    """Best-effort SBC/edge board model detection."""
    model_path = Path("/proc/device-tree/model")
    try:
        if model_path.exists():
            return model_path.read_text(encoding="utf-8", errors="ignore").strip("\x00\n ")
    except Exception:
        pass
    return None


def _infer_device_profile(
    machine: str,
    runtimes: dict[str, Any],
    accelerators: dict[str, Any],
    board_model: str | None = None,
) -> str:
    arch = machine.lower()
    board = (board_model or "").lower()

    if "raspberry pi 5" in board:
        return "rpi5-tflite"

    if accelerators.get("jetson", {}).get("available") and runtimes.get("tensorrt", {}).get(
        "available"
    ):
        return "orin-tensorrt"
    if accelerators.get("jetson", {}).get("available") or "nvidia jetson" in board:
        return "arm64-jetson"
    if accelerators.get("nvidia", {}).get("available"):
        return f"{arch}-nvidia"
    if arch in {"aarch64", "arm64"}:
        return "arm64-cpu"
    if arch in {"x86_64", "amd64"}:
        return "x86_64-cpu"
    return f"{arch}-cpu"
