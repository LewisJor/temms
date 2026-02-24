"""
Tests for ConditionStore (#11).

Tests:
- set / get / get_all / get_snapshot
- Priority enforcement (higher-wins)
- Confidence tracking
- exists() method
- clear_operator_overrides
- get_stale_conditions
- Corrupt JSON handling
- History table recording
"""

import pytest
import json
import time
from datetime import datetime

from temms.conditions.store import ConditionStore, ConditionValue


# ── Basic set / get ──────────────────────────────────────────────────


class TestConditionStoreBasic:
    """Test basic get/set operations."""

    def test_set_and_get(self, condition_store):
        condition_store.set("temp", 72.5, "sensor", 100)

        cond = condition_store.get("temp")

        assert cond is not None
        assert cond.value == 72.5
        assert cond.source == "sensor"
        assert cond.priority == 100
        assert cond.confidence == 1.0

    def test_get_missing_returns_none(self, condition_store):
        assert condition_store.get("nonexistent") is None

    def test_set_overwrites_same_priority(self, condition_store):
        condition_store.set("temp", 50, "sensor", 100)
        condition_store.set("temp", 80, "sensor", 100)

        assert condition_store.get("temp").value == 80

    def test_set_returns_condition_value(self, condition_store):
        result = condition_store.set("temp", 50, "sensor", 100)

        assert isinstance(result, ConditionValue)
        assert result.value == 50

    def test_set_with_custom_confidence(self, condition_store):
        condition_store.set("temp", 50, "sensor", 100, confidence=0.7)

        assert condition_store.get("temp").confidence == 0.7

    def test_set_various_value_types(self, condition_store):
        """Test that different JSON-serializable types work."""
        condition_store.set("int_val", 42, "test", 100)
        condition_store.set("float_val", 3.14, "test", 100)
        condition_store.set("str_val", "hello", "test", 100)
        condition_store.set("bool_val", True, "test", 100)
        condition_store.set("list_val", [1, 2, 3], "test", 100)
        condition_store.set("dict_val", {"key": "val"}, "test", 100)
        condition_store.set("null_val", None, "test", 100)

        assert condition_store.get("int_val").value == 42
        assert condition_store.get("float_val").value == 3.14
        assert condition_store.get("str_val").value == "hello"
        assert condition_store.get("bool_val").value is True
        assert condition_store.get("list_val").value == [1, 2, 3]
        assert condition_store.get("dict_val").value == {"key": "val"}
        assert condition_store.get("null_val").value is None


# ── Priority enforcement ─────────────────────────────────────────────


class TestPriorityEnforcement:
    """Test that higher priority wins."""

    def test_higher_priority_overwrites(self, condition_store):
        condition_store.set("temp", 50, "sensor", 100)
        condition_store.set("temp", 80, "operator", 1000)

        cond = condition_store.get("temp")
        assert cond.value == 80
        assert cond.priority == 1000

    def test_lower_priority_rejected(self, condition_store):
        condition_store.set("temp", 80, "operator", 1000)
        result = condition_store.set("temp", 50, "sensor", 100)

        # Returns existing value, not the rejected one
        assert result.value == 80
        assert result.priority == 1000

        # Store still has the high-priority value
        assert condition_store.get("temp").value == 80

    def test_equal_priority_overwrites(self, condition_store):
        condition_store.set("temp", 50, "sensor", 100)
        condition_store.set("temp", 80, "sensor", 100)

        assert condition_store.get("temp").value == 80


# ── get_all ──────────────────────────────────────────────────────────


class TestGetAll:
    """Test get_all with prefix filtering."""

    def test_get_all_empty(self, condition_store):
        assert condition_store.get_all() == {}

    def test_get_all_returns_all(self, condition_store):
        condition_store.set("a.b", 1, "test", 100)
        condition_store.set("c.d", 2, "test", 100)

        all_conds = condition_store.get_all()

        assert len(all_conds) == 2
        assert "a.b" in all_conds
        assert "c.d" in all_conds

    def test_get_all_prefix_filter(self, condition_store):
        condition_store.set("weather.temp", 20, "test", 100)
        condition_store.set("weather.wind", 10, "test", 100)
        condition_store.set("system.cpu", 50, "test", 100)

        weather = condition_store.get_all(prefix="weather")

        assert len(weather) == 2
        assert "weather.temp" in weather
        assert "system.cpu" not in weather

    def test_get_all_prefix_no_match(self, condition_store):
        condition_store.set("system.cpu", 50, "test", 100)

        result = condition_store.get_all(prefix="weather")

        assert result == {}


# ── get_snapshot ─────────────────────────────────────────────────────


class TestGetSnapshot:
    """Test nested snapshot generation."""

    def test_empty_snapshot(self, condition_store):
        assert condition_store.get_snapshot() == {}

    def test_flat_snapshot(self, condition_store):
        condition_store.set("temp", 50, "test", 100)

        snapshot = condition_store.get_snapshot()

        assert snapshot == {"temp": 50}

    def test_nested_snapshot(self, condition_store):
        condition_store.set("platform.compute.cpu_temp_c", 62, "test", 100)
        condition_store.set("platform.compute.memory_mb", 2048, "test", 100)
        condition_store.set("weather.visibility_m", 500, "test", 100)

        snapshot = condition_store.get_snapshot()

        assert snapshot["platform"]["compute"]["cpu_temp_c"] == 62
        assert snapshot["platform"]["compute"]["memory_mb"] == 2048
        assert snapshot["weather"]["visibility_m"] == 500


# ── exists ───────────────────────────────────────────────────────────


class TestExists:
    """Test exists() method."""

    def test_exists_true(self, condition_store):
        condition_store.set("temp", 50, "test", 100)

        assert condition_store.exists("temp") is True

    def test_exists_false(self, condition_store):
        assert condition_store.exists("nonexistent") is False


# ── clear_operator_overrides ─────────────────────────────────────────


class TestClearOperatorOverrides:
    """Test clearing high-priority conditions."""

    def test_clears_operator_priority(self, condition_store):
        condition_store.set("a", 1, "operator", 1000)
        condition_store.set("b", 2, "sensor", 100)

        count = condition_store.clear_operator_overrides()

        assert count == 1
        assert condition_store.get("a") is None
        assert condition_store.get("b") is not None

    def test_clears_multiple(self, condition_store):
        condition_store.set("a", 1, "op", 1000)
        condition_store.set("b", 2, "op", 2000)
        condition_store.set("c", 3, "sensor", 50)

        count = condition_store.clear_operator_overrides()

        assert count == 2
        assert condition_store.get("a") is None
        assert condition_store.get("b") is None
        assert condition_store.get("c") is not None

    def test_clears_nothing_when_no_overrides(self, condition_store):
        condition_store.set("a", 1, "sensor", 100)

        count = condition_store.clear_operator_overrides()

        assert count == 0


# ── Corrupt JSON handling ────────────────────────────────────────────


class TestCorruptDataHandling:
    """Test that corrupt JSON values are handled gracefully."""

    def test_corrupt_value_returns_none(self, condition_store):
        """Directly insert invalid JSON to test _row_to_condition."""
        condition_store.execute_and_commit(
            """
            INSERT INTO conditions (path, value, source, priority, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("corrupt", "not-valid-json{{{", "test", 100, 1.0, datetime.now()),
        )

        cond = condition_store.get("corrupt")

        assert cond is None

    def test_corrupt_value_skipped_in_get_all(self, condition_store):
        condition_store.set("good", 42, "test", 100)

        # Insert corrupt entry
        condition_store.execute_and_commit(
            """
            INSERT INTO conditions (path, value, source, priority, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("bad", "{invalid", "test", 100, 1.0, datetime.now()),
        )

        all_conds = condition_store.get_all()

        # Only good condition returned
        assert len(all_conds) == 1
        assert "good" in all_conds


# ── History recording ────────────────────────────────────────────────


class TestConditionHistory:
    """Test that condition changes are recorded in history."""

    def test_set_records_history(self, condition_store):
        condition_store.set("temp", 50, "sensor", 100)
        condition_store.set("temp", 80, "sensor", 100)

        rows = condition_store.fetchall(
            "SELECT * FROM condition_history WHERE path = ?", ("temp",)
        )

        assert len(rows) == 2
        assert json.loads(rows[0]["value"]) == 50
        assert json.loads(rows[1]["value"]) == 80


# ── ConditionValue dataclass ─────────────────────────────────────────


class TestConditionValue:
    """Test ConditionValue dataclass."""

    def test_to_dict(self):
        cv = ConditionValue(
            path="test",
            value=42,
            source="sensor",
            priority=100,
            confidence=0.9,
            updated_at=datetime(2024, 1, 15, 10, 30),
        )

        d = cv.to_dict()

        assert d["path"] == "test"
        assert d["value"] == 42
        assert d["source"] == "sensor"
        assert d["priority"] == 100
        assert d["confidence"] == 0.9
        assert d["updated_at"] == "2024-01-15T10:30:00"
