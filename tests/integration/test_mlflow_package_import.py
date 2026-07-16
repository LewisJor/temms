"""Real (unmocked) MLflow -> TEMMS package import coverage.

Every other MLflow test in the suite installs a fake ``mlflow`` module, so the
actual registry download + package build path had no coverage. This test drives
it against a genuine MLflow tracking server configured the same way the Docker
demo is (proxied artifact serving via ``--serve-artifacts`` so a client that
does not share the artifact filesystem can still fetch model files).

It self-skips unless the ``mlflow`` extra is installed (``uv sync --extra
mlflow``), so the default suite stays fast and dependency-light.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("mlflow", reason="requires the mlflow extra")

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ONNX = REPO_ROOT / "examples" / "package-example" / "models" / "yolov8n-daylight.onnx"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, timeout: float = 90.0) -> None:
    from urllib import request

    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with request.urlopen(f"{url}/health", timeout=5) as resp:  # noqa: S310
                if resp.status == 200:
                    return
        except Exception as err:  # pragma: no cover - transient during startup
            last_err = err
        time.sleep(1.0)
    raise RuntimeError(f"MLflow server did not become healthy at {url}: {last_err}")


@pytest.fixture(scope="module")
def mlflow_server(tmp_path_factory) -> str:
    if not EXAMPLE_ONNX.exists():
        pytest.skip(f"example model not found: {EXAMPLE_ONNX}")

    root = tmp_path_factory.mktemp("mlflow")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            "mlflow",
            "server",
            "--backend-store-uri",
            f"sqlite:///{root / 'mlflow.db'}",
            "--serve-artifacts",
            "--artifacts-destination",
            str(root / "artifacts"),
            # Test-only: the client connects over 127.0.0.1 on an ephemeral port.
            "--allowed-hosts",
            "*",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_health(url)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        pytest.skip("could not start a local MLflow server")
    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def _register_model(tracking_uri: str, name: str) -> str:
    import mlflow
    from mlflow.tracking import MlflowClient

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("temms-import-test")
    client = MlflowClient()
    with mlflow.start_run() as run:
        run_id = run.info.run_id
        mlflow.log_param("input_schema", json.dumps({"shape": [1, 3, 640, 640]}))
        mlflow.log_param("output_schema", json.dumps({"shape": [1, 84, 8400]}))
        mlflow.log_param(
            "runtime_constraints",
            json.dumps({"runtimes": ["onnxruntime"], "device_profiles": ["x86_64-cpu"]}),
        )
        mlflow.log_param("model_format", "onnx")
        mlflow.log_artifact(str(EXAMPLE_ONNX), artifact_path="model")

    client.create_registered_model(name)
    version = client.create_model_version(
        name=name, source=f"runs:/{run_id}/model", run_id=run_id
    )
    return version.version


def test_build_package_from_real_mlflow_registry(mlflow_server, tmp_path):
    from temms.core.package_builder import build_package_from_mlflow

    name = "temms-import-detector"
    version = _register_model(mlflow_server, name)

    package_dir = build_package_from_mlflow(
        model_uri=f"models:/{name}/{version}",
        slot="vision",
        policy_path=None,
        output_dir=tmp_path,
        tracking_uri=mlflow_server,
        device_profile="x86_64-cpu",
        require_schema=True,
        require_runtime_constraints=True,
        archive=False,
    )

    manifest = json.loads((Path(package_dir) / "manifest.json").read_text())
    assert manifest["package_id"] == f"mlflow-{name}-{version}"
    models = manifest["models"]
    assert len(models) == 1
    model = models[0]
    assert model["filename"] == EXAMPLE_ONNX.name
    assert model["format"] == "onnx"
    # The packaged artifact must be a byte-for-byte copy of the registry model.
    import hashlib

    packaged = Path(package_dir) / "models" / model["filename"]
    assert packaged.exists()
    assert model["sha256"] == hashlib.sha256(EXAMPLE_ONNX.read_bytes()).hexdigest()
