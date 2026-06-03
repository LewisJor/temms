import pytest

from temms.daemon.deployment_state import DeploymentStateStore, DeploymentState
from temms.daemon.pending_ops import PendingOperationsStore


def test_deployment_state_store_roundtrip(tmp_path):
    store = DeploymentStateStore(tmp_path / "deployment_state.json")
    store.set_state(DeploymentState.READY, "test")
    assert store.get_state() == DeploymentState.READY


def test_deployment_state_write_failure_preserves_previous_state(tmp_path, monkeypatch):
    store = DeploymentStateStore(tmp_path / "deployment_state.json")
    store.set_state(DeploymentState.READY, "ready")
    previous_payload = store.path.read_text(encoding="utf-8")
    original_replace = type(store.path).replace

    def fail_replace(path, target):
        if target == store.path:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(type(store.path), "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.set_state(DeploymentState.FAILED, "failed")

    assert store.path.read_text(encoding="utf-8") == previous_payload
    assert store.get_state() == DeploymentState.READY
    assert not list(tmp_path.glob(".deployment_state.json-*"))


def test_pending_ops_enqueue_and_clear(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue("deploy", {"slot": "vision"})
    entries = store.read_all()
    assert len(entries) == 1
    store.clear()
    assert store.read_all() == []


def test_pending_ops_write_failure_preserves_previous_queue(tmp_path, monkeypatch):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue("deploy", {"slot": "vision"})
    previous_payload = store.path.read_text(encoding="utf-8")
    original_replace = type(store.path).replace

    def fail_replace(path, target):
        if target == store.path:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(type(store.path), "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.enqueue("rollback", {"slot": "vision"})

    assert store.path.read_text(encoding="utf-8") == previous_payload
    assert [entry["operation"] for entry in store.read_all()] == ["deploy"]
    assert not list(tmp_path.glob(".pending_operations.json-*"))
