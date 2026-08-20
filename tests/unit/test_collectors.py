"""
Tests for condition collectors with filesystem mocking (#10).

Tests:
- SystemMetricsCollector (CPU temp, memory, battery)
- TimeBasedCollector (time of day, lighting)
"""

from unittest.mock import patch

from temms.conditions.collectors import (
    SensorRead,
    SensorStatus,
    SystemMetricsCollector,
    TimeBasedCollector,
)

# ── SystemMetricsCollector ───────────────────────────────────────────


class TestSystemMetricsCollector:
    """Test system metrics collection with mocked filesystem."""

    def test_source_name(self):
        c = SystemMetricsCollector()
        assert c.source_name == "system_sensors"

    def test_source_priority(self):
        c = SystemMetricsCollector()
        assert c.source_priority == 100

    @patch.object(SystemMetricsCollector, "_read_cpu_temp")
    @patch.object(SystemMetricsCollector, "_read_memory")
    @patch.object(SystemMetricsCollector, "_read_battery")
    def test_collect_cpu_and_memory(self, mock_bat, mock_mem, mock_cpu):
        mock_cpu.return_value = SensorRead.ok(62.5)
        mock_mem.return_value = SensorRead.ok({"available_mb": 2048})
        mock_bat.return_value = SensorRead.absent()
        c = SystemMetricsCollector()
        metrics = c.collect()

        assert metrics["platform.compute.cpu_temp_c"] == 62.5
        assert metrics["platform.compute.memory_available_mb"] == 2048
        assert "platform.power.battery_pct" not in metrics

    @patch.object(SystemMetricsCollector, "_read_cpu_temp")
    @patch.object(SystemMetricsCollector, "_read_memory")
    @patch.object(SystemMetricsCollector, "_read_battery")
    def test_collect_battery(self, mock_bat, mock_mem, mock_cpu):
        mock_cpu.return_value = SensorRead.absent()
        mock_mem.return_value = SensorRead.absent()
        mock_bat.return_value = SensorRead.ok({"percent": 75, "source": "battery"})
        c = SystemMetricsCollector()
        metrics = c.collect()

        assert metrics["platform.power.battery_pct"] == 75
        assert metrics["platform.power.power_source"] == "battery"

    @patch.object(SystemMetricsCollector, "_read_cpu_temp", side_effect=Exception("fail"))
    @patch.object(SystemMetricsCollector, "_read_memory")
    @patch.object(SystemMetricsCollector, "_read_battery")
    def test_collect_handles_exceptions(self, mock_bat, mock_mem, mock_cpu):
        mock_mem.return_value = SensorRead.absent()
        mock_bat.return_value = SensorRead.absent()
        c = SystemMetricsCollector()
        metrics = c.collect()

        # Should not raise, no value published...
        assert "platform.compute.cpu_temp_c" not in metrics
        # ...but the failure must be VISIBLE, not silent.
        assert metrics["runtime.sensors.cpu_temp.status"] == SensorStatus.FAILED
        assert metrics["runtime.sensors.cpu_temp.healthy"] is False
        assert "fail" in metrics["runtime.sensors.cpu_temp.last_error"]

    def test_read_cpu_temp_no_thermal_zone(self, tmp_path):
        """No thermal zones at all is ABSENT (not a fault)."""
        c = SystemMetricsCollector()
        with patch("temms.conditions.collectors.Path") as MockPath:
            MockPath.return_value.glob.return_value = []
            result = c._read_cpu_temp()
        assert result.status == SensorStatus.ABSENT
        assert result.healthy is True

    def test_read_memory_no_meminfo(self):
        """No /proc/meminfo is ABSENT (non-Linux), not a failure."""
        c = SystemMetricsCollector()
        with patch("temms.conditions.collectors.Path") as MockPath:
            MockPath.return_value.exists.return_value = False
            result = c._read_memory()
        assert result.status == SensorStatus.ABSENT

    def test_unreadable_meminfo_is_failed_not_absent(self):
        """The distinction that matters: present-but-broken is a fault."""
        c = SystemMetricsCollector()
        with patch("temms.conditions.collectors.Path") as MockPath:
            MockPath.return_value.exists.return_value = True
            MockPath.return_value.read_text.side_effect = OSError("EIO")
            result = c._read_memory()
        assert result.status == SensorStatus.FAILED
        assert result.healthy is False
        assert "EIO" in result.error

    def test_meminfo_without_memavailable_is_failed(self):
        c = SystemMetricsCollector()
        with patch("temms.conditions.collectors.Path") as MockPath:
            MockPath.return_value.exists.return_value = True
            MockPath.return_value.read_text.return_value = "MemTotal: 100 kB\n"
            result = c._read_memory()
        assert result.status == SensorStatus.FAILED

    def test_every_sensor_publishes_health(self):
        """Health is emitted for every sensor on every pass, present or not."""
        c = SystemMetricsCollector()
        metrics = c.collect()
        for sensor in ("cpu_temp", "memory", "battery"):
            assert f"runtime.sensors.{sensor}.status" in metrics
            assert f"runtime.sensors.{sensor}.healthy" in metrics
            assert f"runtime.sensors.{sensor}.last_error" in metrics


# ── TimeBasedCollector ───────────────────────────────────────────────


class TestTimeBasedCollector:
    """Test time-based condition derivation."""

    def test_source_name(self):
        c = TimeBasedCollector()
        assert c.source_name == "time_derived"

    def test_source_priority(self):
        c = TimeBasedCollector()
        assert c.source_priority == 90

    def test_collect_returns_time_of_day(self):
        c = TimeBasedCollector()
        conditions = c.collect()

        assert "operational.time_of_day" in conditions
        assert conditions["operational.time_of_day"] in (
            "morning",
            "afternoon",
            "evening",
            "night",
        )

    def test_collect_returns_ambient_light(self):
        c = TimeBasedCollector()
        conditions = c.collect()

        assert "environmental.celestial.ambient" in conditions
        assert conditions["environmental.celestial.ambient"] in ("bright", "low", "dark")

    def test_all_values_are_valid_strings(self):
        """Verify all returned values are recognized categories."""
        c = TimeBasedCollector()
        conditions = c.collect()

        valid_times = {"morning", "afternoon", "evening", "night"}
        valid_light = {"bright", "low", "dark"}

        assert conditions["operational.time_of_day"] in valid_times
        assert conditions["environmental.celestial.ambient"] in valid_light


