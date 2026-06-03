"""
Tests for SlotManager (#11).

Tests:
- Slot CRUD (create, get, list)
- Slot state management
- Model activation + decision logging
- Operator overrides (set, clear, has_active, expiration)
- Decision audit log
"""

import json

import pytest
import time
from datetime import datetime, timedelta

from temms.slots.manager import SlotManager, SlotState, Slot, OperatorOverride


# ── Slot CRUD ────────────────────────────────────────────────────────


class TestSlotCRUD:
    """Test basic slot create/read operations."""

    def test_create_slot(self, slot_manager):
        slot = slot_manager.create_slot(
            name="vision",
            description="Vision processing",
            required=True,
            default_model="yolov8",
            candidates=["yolov8", "mobilenet"],
            metadata={"hw": "jetson"},
        )

        assert slot.name == "vision"
        assert slot.description == "Vision processing"
        assert slot.required is True
        assert slot.default_model == "yolov8"
        assert slot.state == SlotState.STOPPED
        assert slot.active_model_id is None
        assert slot.candidates == ["yolov8", "mobilenet"]
        assert slot.metadata == {"hw": "jetson"}

    def test_create_slot_defaults(self, slot_manager):
        slot = slot_manager.create_slot(name="nav", description="Nav")

        assert slot.required is False
        assert slot.default_model is None
        assert slot.candidates == []
        assert slot.metadata == {}

    def test_get_slot(self, slot_manager):
        slot_manager.create_slot(name="vision", description="Vision")

        slot = slot_manager.get_slot("vision")

        assert slot is not None
        assert slot.name == "vision"

    def test_get_slot_not_found(self, slot_manager):
        assert slot_manager.get_slot("nonexistent") is None

    def test_list_slots_empty(self, slot_manager):
        assert slot_manager.list_slots() == []

    def test_list_slots(self, slot_manager):
        slot_manager.create_slot(name="a", description="A")
        slot_manager.create_slot(name="b", description="B")

        slots = slot_manager.list_slots()

        assert len(slots) == 2
        names = {s.name for s in slots}
        assert names == {"a", "b"}

    def test_create_duplicate_raises(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V1")

        with pytest.raises(Exception):
            slot_manager.create_slot(name="vision", description="V2")


# ── Slot State ───────────────────────────────────────────────────────


class TestSlotState:
    """Test slot state management."""

    def test_update_slot_state(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        slot_manager.update_slot_state("vision", SlotState.LOADING)
        slot = slot_manager.get_slot("vision")
        assert slot.state == SlotState.LOADING

        slot_manager.update_slot_state("vision", SlotState.RUNNING)
        slot = slot_manager.get_slot("vision")
        assert slot.state == SlotState.RUNNING

    def test_update_slot_state_error(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        slot_manager.update_slot_state("vision", SlotState.ERROR)

        assert slot_manager.get_slot("vision").state == SlotState.ERROR


# ── Model Activation ─────────────────────────────────────────────────


class TestModelActivation:
    """Test model activation and decision logging."""

    def test_activate_model(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        slot_manager.activate_model(
            slot_name="vision",
            model_id="yolov8-v1",
            trigger_type="policy",
            trigger_detail="thermal-adaptive",
        )

        slot = slot_manager.get_slot("vision")
        assert slot.active_model_id == "yolov8-v1"
        assert slot.state == SlotState.RUNNING

    def test_activate_model_logs_decision(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        slot_manager.activate_model(
            slot_name="vision",
            model_id="model-a",
            trigger_type="startup",
            trigger_detail="default_model",
            conditions={"temp": 50},
            audit_metadata={
                "package_id": "pkg-a",
                "model_version": "1.0.0",
                "provenance": {"source": "mlflow"},
            },
        )

        decisions = slot_manager.get_decision_log("vision")
        assert len(decisions) == 1
        assert decisions[0]["to_model"] == "model-a"
        assert decisions[0]["trigger_type"] == "startup"
        audit_metadata = json.loads(decisions[0]["audit_metadata"])
        assert audit_metadata["package_id"] == "pkg-a"
        assert audit_metadata["model_version"] == "1.0.0"
        assert audit_metadata["provenance"]["source"] == "mlflow"

    def test_activate_model_from_previous(self, slot_manager):
        """Second activation logs from_model correctly."""
        slot_manager.create_slot(name="vision", description="V")

        slot_manager.activate_model(
            "vision", "model-a", "startup", "initial"
        )
        slot_manager.activate_model(
            "vision", "model-b", "policy", "thermal"
        )

        decisions = slot_manager.get_decision_log("vision")
        # Most recent first
        assert decisions[0]["from_model"] == "model-a"
        assert decisions[0]["to_model"] == "model-b"

    def test_activate_nonexistent_slot_raises(self, slot_manager):
        with pytest.raises(ValueError, match="Slot not found"):
            slot_manager.activate_model(
                "nonexistent", "model", "test", "test"
            )


# ── Operator Overrides ───────────────────────────────────────────────


class TestOperatorOverrides:
    """Test operator override tracking (#1)."""

    def test_set_override(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        slot_manager.set_operator_override(
            slot_name="vision",
            model_id="thermal-v2",
            reason="fog conditions",
            source="operator-1",
        )

        slot = slot_manager.get_slot("vision")
        assert slot.operator_override is not None
        assert slot.operator_override.model_id == "thermal-v2"
        assert slot.operator_override.reason == "fog conditions"
        assert slot.operator_override.source == "operator-1"

    def test_has_active_override(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        assert slot_manager.has_active_override("vision") is False

        slot_manager.set_operator_override("vision", "model", "reason")

        assert slot_manager.has_active_override("vision") is True

    def test_clear_override(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")
        slot_manager.set_operator_override("vision", "model", "reason")

        slot_manager.clear_operator_override("vision")

        assert slot_manager.has_active_override("vision") is False
        slot = slot_manager.get_slot("vision")
        assert slot.operator_override is None

    def test_override_with_duration(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        slot_manager.set_operator_override(
            "vision", "model", "reason", duration_s=3600
        )

        slot = slot_manager.get_slot("vision")
        assert slot.operator_override.expires_at is not None

    def test_expired_override_auto_cleared(self, slot_manager):
        """Override with past expiration is auto-cleared by has_active_override."""
        slot_manager.create_slot(name="vision", description="V")

        # Set override with 1-second duration
        slot_manager.set_operator_override(
            "vision", "model", "reason", duration_s=1
        )

        # Manually set expires_at to the past
        from datetime import timedelta
        past = datetime.now() - timedelta(seconds=10)
        slot_manager.execute_and_commit(
            "UPDATE slots SET override_expires_at = ? WHERE name = ?",
            (past, "vision"),
        )

        # has_active_override should detect expiration and clear it
        assert slot_manager.has_active_override("vision") is False

        # Override should be cleared in DB
        slot = slot_manager.get_slot("vision")
        assert slot.operator_override is None

    def test_permanent_override_never_expires(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        slot_manager.set_operator_override("vision", "model", "reason")

        slot = slot_manager.get_slot("vision")
        assert slot.operator_override.expires_at is None
        assert slot.operator_override.is_expired() is False

    def test_has_active_override_no_slot(self, slot_manager):
        assert slot_manager.has_active_override("nonexistent") is False

    def test_set_override_nonexistent_slot_raises(self, slot_manager):
        with pytest.raises(ValueError, match="Slot not found"):
            slot_manager.set_operator_override("nonexistent", "model", "reason")


# ── Decision Log ─────────────────────────────────────────────────────


class TestDecisionLog:
    """Test decision audit log."""

    def test_decision_log_empty(self, slot_manager):
        assert slot_manager.get_decision_log() == []

    def test_decision_log_records_switch(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")
        slot_manager.activate_model("vision", "m1", "startup", "init")

        log = slot_manager.get_decision_log()

        assert len(log) == 1
        assert log[0]["slot"] == "vision"
        assert log[0]["to_model"] == "m1"

    def test_decision_log_filter_by_slot(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")
        slot_manager.create_slot(name="nav", description="N")

        slot_manager.activate_model("vision", "v1", "startup", "init")
        slot_manager.activate_model("nav", "n1", "startup", "init")

        vision_log = slot_manager.get_decision_log("vision")
        assert len(vision_log) == 1
        assert vision_log[0]["slot"] == "vision"

    def test_decision_log_limit(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        for i in range(10):
            slot_manager.activate_model("vision", f"m{i}", "test", "test")

        log = slot_manager.get_decision_log("vision", limit=3)
        assert len(log) == 3

    def test_decision_log_ordered_desc(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        slot_manager.activate_model("vision", "first", "startup", "init")
        slot_manager.activate_model("vision", "second", "policy", "rule")

        log = slot_manager.get_decision_log("vision")

        # Most recent first
        assert log[0]["to_model"] == "second"
        assert log[1]["to_model"] == "first"

    def test_decision_log_conditions_snapshot(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")

        conditions = {"platform": {"compute": {"cpu_temp_c": 75}}}
        slot_manager.activate_model(
            "vision", "m1", "policy", "thermal", conditions=conditions
        )

        log = slot_manager.get_decision_log("vision")
        import json
        snapshot = json.loads(log[0]["conditions_snapshot"])
        assert snapshot["platform"]["compute"]["cpu_temp_c"] == 75


# ── Slot.to_dict ─────────────────────────────────────────────────────


class TestSlotToDict:
    """Test Slot serialization."""

    def test_to_dict_basic(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V", required=True)

        slot = slot_manager.get_slot("vision")
        d = slot.to_dict()

        assert d["name"] == "vision"
        assert d["required"] is True
        assert d["state"] == "stopped"
        assert d["operator_override"] is None

    def test_to_dict_with_override(self, slot_manager):
        slot_manager.create_slot(name="vision", description="V")
        slot_manager.set_operator_override(
            "vision", "model", "reason", source="api"
        )

        slot = slot_manager.get_slot("vision")
        d = slot.to_dict()

        assert d["operator_override"] is not None
        assert d["operator_override"]["model_id"] == "model"
        assert d["operator_override"]["source"] == "api"


# ── OperatorOverride dataclass ───────────────────────────────────────


class TestOperatorOverrideDataclass:
    """Test OperatorOverride helper methods."""

    def test_not_expired_no_expiry(self):
        override = OperatorOverride(
            model_id="m",
            reason="r",
            source="s",
            set_at=datetime.now(),
            expires_at=None,
        )
        assert override.is_expired() is False

    def test_not_expired_future(self):
        override = OperatorOverride(
            model_id="m",
            reason="r",
            source="s",
            set_at=datetime.now(),
            expires_at=datetime.now() + timedelta(hours=1),
        )
        assert override.is_expired() is False

    def test_expired_past(self):
        override = OperatorOverride(
            model_id="m",
            reason="r",
            source="s",
            set_at=datetime.now() - timedelta(hours=2),
            expires_at=datetime.now() - timedelta(hours=1),
        )
        assert override.is_expired() is True
