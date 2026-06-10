"""
Unit tests for MLflow registry readiness mapping.
"""

from temms.mlflow_bridge import MLflowBridge


def test_mlflow_readiness_records_sim_test_provenance():
    """MLflow readiness metadata should retain operator-facing provenance."""
    readiness = MLflowBridge._readiness_from_run(
        params={
            "sim_passed": "true",
            "sim_source": "temms-sim",
            "sim_scenario": "fog-regression",
            "tests_passed": "true",
            "test_source": "pytest",
            "test_suite": "unit-readiness",
        },
        metrics={},
        tags={
            "sim_run_id": "sim-42",
            "test_run_id": "ci-99",
        },
    )

    assert readiness["sim_passed"] is True
    assert readiness["sim_evidence"] == {
        "passed": True,
        "source": "temms-sim",
        "detail": "fog-regression",
        "run_id": "sim-42",
        "recorded_at": None,
        "protected_by_signature": False,
    }
    assert readiness["tests_passed"] is True
    assert readiness["test_evidence"] == {
        "passed": True,
        "source": "pytest",
        "detail": "unit-readiness",
        "run_id": "ci-99",
        "recorded_at": None,
        "protected_by_signature": False,
    }
