"""CI gate for the soak/chaos harness (issue #13).

Runs a very short soak so the harness itself — and the reliability invariants it
asserts (zero silent inference errors, bounded swap frequency under a flapping
sensor, clean restart recovery) — stay green in CI. The long-duration run is a
separate operator/nightly activity.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "soak.py"


def _load_soak():
    spec = importlib.util.spec_from_file_location("temms_soak", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve the module's globals.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_soak_short_run_passes(monkeypatch):
    monkeypatch.setenv("TEMMS_INFERENCE_SIMULATE_RUNTIME", "1")
    soak = _load_soak()

    report = await soak.run_soak(duration_s=2.0, seed=7)

    assert report["schema_version"] == "temms-reliability-report/v1"
    assert report["totals"]["inferences"] > 0
    assert report["totals"]["inference_errors"] == 0
    assert report["recovery"]["failed"] == 0
    assert report["passed"] is True
    # Every declared invariant must hold.
    assert all(inv["passed"] for inv in report["invariants"])
