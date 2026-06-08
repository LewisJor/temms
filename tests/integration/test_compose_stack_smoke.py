"""
Docker Compose smoke checks for the normal local TEMMS stack.

These tests are intentionally shallow and stable. They verify that the daemon,
UI, MLflow, and Hub Lite surfaces are reachable after `docker compose up`,
without asserting volatile UI layout details.
"""

from __future__ import annotations

import json
import os
import time
from urllib import error, parse, request

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("TEMMS_COMPOSE_SMOKE") != "1",
    reason="compose smoke tests require a running docker compose stack",
)

TEMMS_URL = os.getenv("TEMMS_SMOKE_TEMMS_URL", "http://127.0.0.1:8080")
MLFLOW_URL = os.getenv("TEMMS_SMOKE_MLFLOW_URL", "http://127.0.0.1:5001")


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout_s: float = 5.0,
) -> tuple[int, str]:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(
        f"{base_url}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=timeout_s) as response:
            return response.status, response.read().decode("utf-8")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _wait_for_json(
    base_url: str,
    path: str,
    *,
    timeout_s: float = 120.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, text = _request(base_url, path)
            if status == 200:
                return json.loads(text)
            last_error = AssertionError(f"{path} returned HTTP {status}: {text[:200]}")
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = exc
        time.sleep(2)
    raise AssertionError(f"{base_url}{path} did not become ready: {last_error}")


def _wait_for_text(
    base_url: str,
    path: str,
    *,
    timeout_s: float = 120.0,
) -> str:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, text = _request(base_url, path)
            if status == 200:
                return text
            last_error = AssertionError(f"{path} returned HTTP {status}: {text[:200]}")
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = exc
        time.sleep(2)
    raise AssertionError(f"{base_url}{path} did not become ready: {last_error}")


def test_compose_temms_daemon_ui_hub_and_mlflow_are_ready():
    health = _wait_for_json(TEMMS_URL, "/v1/health")
    assert health["status"] == "ok"

    status = _wait_for_json(TEMMS_URL, "/v1/status")
    assert status["status"] in {"healthy", "degraded"}
    assert "vision" in status["slots"]

    slot = _wait_for_json(TEMMS_URL, "/v1/slots/vision/status")
    assert slot["name"] == "vision"
    assert slot["active_model"]

    ui_status, ui_html = _request(TEMMS_URL, "/ui/")
    assert ui_status == 200
    assert "TEMMS" in ui_html

    packages = _wait_for_json(TEMMS_URL, "/v1/hub/packages")
    assert "packages" in packages

    enroll_status, enroll_body = _request(
        TEMMS_URL,
        "/v1/hub/devices/enroll",
        method="POST",
        payload={
            "device_id": "ci-u22-smoke",
            "profile": "x86_64-cpu",
            "labels": {"ci": "true"},
            "inventory": {"runner": "ubuntu-22.04"},
        },
    )
    assert enroll_status == 200, enroll_body
    enrolled = json.loads(enroll_body)
    assert enrolled["device_id"] == "ci-u22-smoke"

    devices = _wait_for_json(TEMMS_URL, "/v1/hub/devices")
    assert any(device["device_id"] == "ci-u22-smoke" for device in devices["devices"])

    evidence = _wait_for_json(TEMMS_URL, "/v1/evidence")
    assert evidence["schema_version"] == "temms-evidence-bundle/v1"
    assert evidence["integrity"]["payload_sha256"]
    assert evidence["packages"]

    mlflow_health = _wait_for_text(MLFLOW_URL, "/health")
    assert "OK" in mlflow_health.upper()

    model_name = f"ci-smoke-{time.time_ns()}"
    create_status, create_body = _request(
        MLFLOW_URL,
        "/api/2.0/mlflow/registered-models/create",
        method="POST",
        payload={"name": model_name},
    )
    assert create_status == 200, create_body

    get_path = "/api/2.0/mlflow/registered-models/get?" + parse.urlencode(
        {"name": model_name}
    )
    get_status, get_body = _request(MLFLOW_URL, get_path)
    assert get_status == 200, get_body
    assert json.loads(get_body)["registered_model"]["name"] == model_name
