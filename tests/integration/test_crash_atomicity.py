"""Crash-atomicity smoke test (#29).

A fast gate that the SIGKILL harness still works end to end. The full run lives
in `make crash-soak`; this keeps a few cycles in CI so the harness cannot rot
silently, and asserts the property that makes it meaningful: it detects torn
writes rather than passing vacuously.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = REPO_ROOT / "scripts" / "crash_soak.py"


def _run(tmp_path: Path, *, unsafe: bool, iterations: int = 4) -> tuple[int, dict]:
    report_path = tmp_path / "report.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    if unsafe:
        env["TEMMS_CRASH_SOAK_UNSAFE_WRITES"] = "1"

    proc = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            "--iterations",
            str(iterations),
            "--min-run",
            "0.15",
            "--max-run",
            "0.5",
            "--root",
            str(tmp_path / "state"),
            "--report",
            str(report_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    return proc.returncode, report


@pytest.mark.slow
def test_state_survives_sigkill_mid_write(tmp_path):
    """Atomic writes must leave no torn state after an abrupt kill."""
    returncode, report = _run(tmp_path, unsafe=False)

    assert report, "harness produced no report"
    assert report["totals"]["failed_cycles"] == 0, report["failures"]
    # A kill that lands after the worker already died proves nothing.
    assert report["totals"]["kills_on_live_worker"] == report["totals"]["cycles"]
    assert returncode == 0


@pytest.mark.slow
def test_harness_detects_torn_writes(tmp_path):
    """The detector must actually fire when atomicity is broken.

    Without this, a green crash-atomicity run would be indistinguishable from a
    harness that checks nothing.
    """
    returncode, report = _run(tmp_path, unsafe=True, iterations=6)

    assert report, "harness produced no report"
    assert returncode != 0, "harness passed despite deliberately torn writes"
    assert report["totals"]["failed_cycles"] > 0

    corrupt_checks = {
        check["check"]
        for failure in report["failures"]
        for check in failure["checks"]
        if not check["passed"]
    }
    assert corrupt_checks & {"deployment_state_parses", "intent_queue_parses"}, (
        f"expected a parse failure, got {corrupt_checks}"
    )
