#!/usr/bin/env python3
"""Append a compact TEMMS compose smoke HTML report to GitHub Actions."""

from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import time
from typing import Any
from urllib import error, request

DEFAULT_TEMMS_URL = "http://127.0.0.1:8080"
DEFAULT_MLFLOW_URL = "http://127.0.0.1:5001"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--temms-url",
        default=os.getenv("TEMMS_SMOKE_TEMMS_URL", DEFAULT_TEMMS_URL),
    )
    parser.add_argument(
        "--mlflow-url",
        default=os.getenv("TEMMS_SMOKE_MLFLOW_URL", DEFAULT_MLFLOW_URL),
    )
    args = parser.parse_args()

    report = build_report(args.temms_url, args.mlflow_url)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(report)
            handle.write("\n")
    else:
        print(report)
    return 0


def build_report(temms_url: str, mlflow_url: str) -> str:
    services = compose_services()
    images = compose_images()
    probes = {
        "TEMMS health": get_json(temms_url, "/v1/health"),
        "TEMMS status": get_json(temms_url, "/v1/status"),
        "Vision slot": get_json(temms_url, "/v1/slots/vision/status"),
        "Hub packages": get_json(temms_url, "/v1/hub/packages"),
        "Hub devices": get_json(temms_url, "/v1/hub/devices"),
        "Evidence": get_json(temms_url, "/v1/evidence"),
        "UI": get_text(temms_url, "/ui/"),
        "MLflow": get_text(mlflow_url, "/health"),
    }

    status = body(probes["TEMMS status"])
    slot = body(probes["Vision slot"])
    packages = body(probes["Hub packages"])
    devices = body(probes["Hub devices"])
    evidence = body(probes["Evidence"])

    ok_count = sum(1 for probe in probes.values() if probe["ok"])
    total_ms = sum(float(probe["elapsed_ms"]) for probe in probes.values())

    rows = [
        ("HTTP checks", f"{ok_count}/{len(probes)} OK"),
        ("Probe time", f"{total_ms:.1f} ms"),
        ("Daemon", status.get("status", "unknown") if isinstance(status, dict) else "unknown"),
        (
            "Active model",
            slot.get("active_model", "unknown") if isinstance(slot, dict) else "unknown",
        ),
        (
            "Hub packages",
            str(len(packages.get("packages", []))) if isinstance(packages, dict) else "unknown",
        ),
        (
            "Hub devices",
            str(len(devices.get("devices", []))) if isinstance(devices, dict) else "unknown",
        ),
        (
            "Evidence hash",
            ((evidence.get("integrity") or {}).get("payload_sha256") or "unknown")[:16]
            if isinstance(evidence, dict)
            else "unknown",
        ),
        ("Images", str(len(images))),
    ]

    return "\n".join(
        [
            "<h2>TEMMS Compose Smoke</h2>",
            "<table>",
            "<tr><th>Metric</th><th>Value</th></tr>",
            *[f"<tr><td>{esc(k)}</td><td><code>{esc(v)}</code></td></tr>" for k, v in rows],
            "</table>",
            "<details><summary>Services</summary>",
            "<table><tr><th>Service</th><th>State</th><th>Image</th></tr>",
            *[
                "<tr><td>{}</td><td><code>{}</code></td><td><code>{}</code></td></tr>".format(
                    esc(service.get("Service") or service.get("Name") or "unknown"),
                    esc(service.get("State") or service.get("Status") or "unknown"),
                    esc(service.get("Image") or "unknown"),
                )
                for service in services
            ],
            "</table></details>",
            "<details><summary>Images</summary>",
            "<ul>",
            *[f"<li><code>{esc(image)}</code></li>" for image in images],
            "</ul></details>",
        ]
    )


def compose_services() -> list[dict[str, Any]]:
    result = run(["docker", "compose", "ps", "--format", "json"])
    text = result["stdout"].strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        services = []
        for line in text.splitlines():
            try:
                services.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return services


def compose_images() -> list[str]:
    result = run(["docker", "compose", "config", "--images"])
    return sorted({line.strip() for line in result["stdout"].splitlines() if line.strip()})


def get_json(base_url: str, path: str) -> dict[str, Any]:
    probe = get_text(base_url, path)
    if probe["ok"]:
        try:
            probe["body"] = json.loads(str(probe["body"]))
        except json.JSONDecodeError as exc:
            probe["ok"] = False
            probe["body"] = {"error": str(exc)}
    return probe


def get_text(base_url: str, path: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        req = request.Request(f"{base_url}{path}", method="GET")
        with request.urlopen(req, timeout=5.0) as response:
            text = response.read().decode("utf-8", errors="replace")
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "elapsed_ms": elapsed_ms(started),
                "body": text,
            }
    except error.HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "elapsed_ms": elapsed_ms(started),
            "body": exc.read().decode("utf-8", errors="replace"),
        }
    except Exception as exc:  # pragma: no cover - CI diagnostic path
        return {"ok": False, "status": "error", "elapsed_ms": elapsed_ms(started), "body": str(exc)}


def run(command: list[str]) -> dict[str, str]:
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)
        return {"stdout": completed.stdout, "stderr": completed.stderr}
    except Exception as exc:  # pragma: no cover - CI diagnostic path
        return {"stdout": "", "stderr": str(exc)}


def body(probe: dict[str, Any]) -> Any:
    return probe.get("body") if probe.get("ok") else {}


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


if __name__ == "__main__":
    raise SystemExit(main())
