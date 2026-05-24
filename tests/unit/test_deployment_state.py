from temms.daemon.deployment_state import DeploymentStateStore, DeploymentState
from temms.daemon.pending_ops import PendingOperationsStore


def test_deployment_state_store_roundtrip(tmp_path):
    store = DeploymentStateStore(tmp_path / "deployment_state.json")
    store.set_state(DeploymentState.READY, "test")
    assert store.get_state() == DeploymentState.READY


def test_pending_ops_enqueue_and_clear(tmp_path):
    store = PendingOperationsStore(tmp_path / "pending_operations.json")
    store.enqueue("deploy", {"slot": "vision"})
    entries = store.read_all()
    assert len(entries) == 1
    store.clear()
    assert store.read_all() == []
