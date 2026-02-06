"""
Pytest configuration and shared fixtures for TEMMS tests.
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

from temms.core.cache import ModelCache, ModelFormat
from temms.core.storage import ModelStorage
from temms.slots.manager import SlotManager
from temms.conditions.store import ConditionStore
from temms.policy.engine import PolicyEngine


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test data."""
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir)


@pytest.fixture
def db_path(temp_dir):
    """Create temporary database path."""
    return temp_dir / "temms.db"


@pytest.fixture
def model_dir(temp_dir):
    """Create temporary model directory."""
    path = temp_dir / "models"
    path.mkdir()
    return path


@pytest.fixture
def model_cache(db_path):
    """Create ModelCache instance."""
    return ModelCache(db_path)


@pytest.fixture
def model_storage(model_dir):
    """Create ModelStorage instance."""
    return ModelStorage(model_dir)


@pytest.fixture
def slot_manager(db_path):
    """Create SlotManager instance."""
    return SlotManager(db_path)


@pytest.fixture
def condition_store(db_path):
    """Create ConditionStore instance."""
    return ConditionStore(db_path)


@pytest.fixture
def policy_engine(condition_store):
    """Create PolicyEngine instance."""
    return PolicyEngine(condition_store)


@pytest.fixture
def sample_model_file(temp_dir):
    """Create a sample model file for testing."""
    model_file = temp_dir / "test_model.onnx"
    # Create a dummy file with some content
    model_file.write_bytes(b"ONNX_MODEL_PLACEHOLDER_DATA" * 100)
    return model_file


@pytest.fixture
def sample_cached_model(model_cache, model_storage, sample_model_file):
    """Create a sample cached model for testing."""
    model_id = "test-model-v1"

    # Store the model file
    dest_path, sha256, size = model_storage.store_model(
        sample_model_file, model_id, verify=True
    )

    # Add to cache
    return model_cache.add_cached_model(
        model_id=model_id,
        name="test-model",
        version="1.0.0",
        format=ModelFormat.ONNX,
        path=dest_path,
        sha256=sha256,
        size_bytes=size,
        package_id="test-package",
        metadata={"input_shape": [1, 3, 224, 224]},
    )


@pytest.fixture
def sample_slot(slot_manager):
    """Create a sample slot for testing."""
    return slot_manager.create_slot(
        name="vision",
        description="Vision processing slot",
        required=True,
        default_model="test-model",
        candidates=["test-model", "fallback-model"],
        metadata={"fallback_chain": ["test-model", "fallback-model"]},
    )


@pytest.fixture
def sample_policy_yaml(temp_dir):
    """Create a sample policy YAML file."""
    policy_content = """
apiVersion: temms/v1
kind: SlotPolicy
metadata:
  name: thermal-adaptive
  description: Switch to lighter model when device overheats
spec:
  slot: vision
  rules:
    - name: thermal-throttle
      priority: 50
      conditions:
        all:
          - metric: platform.compute.cpu_temp_c
            operator: gte
            value: 75
      action:
        switch_to: test-model-tiny
  fallback_chain:
    - test-model
    - test-model-tiny
"""
    policy_file = temp_dir / "thermal-adaptive.yaml"
    policy_file.write_text(policy_content)
    return policy_file


@pytest.fixture
def mock_onnx_session():
    """Mock ONNX inference session for testing without actual model."""
    class MockInput:
        name = "input"

    class MockSession:
        def get_inputs(self):
            return [MockInput()]

        def run(self, output_names, input_dict):
            import numpy as np
            # Return dummy output matching typical detection model
            return [np.array([[0.1, 0.2, 0.3, 0.4, 0.9]])]

    return MockSession()
