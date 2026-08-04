"""
Tests for condition collectors with filesystem mocking (#10).

Tests:
- SystemMetricsCollector (CPU temp, memory, battery)
- TimeBasedCollector (time of day, lighting)
- MockWeatherCollector (set + collect)
- ScenarioCollector (YAML timeline)
- GPUMetricsCollector (nvidia-smi, tegrastats stubs)
- collect_all_async (concurrent collection, error handling)
"""

import pytest
import asyncio
from unittest.mock import patch, Mock, MagicMock
from pathlib import Path

from temms.conditions.collectors import (
    SensorRead,
    SensorStatus,
    SystemMetricsCollector,
    TimeBasedCollector,
    MockWeatherCollector,
    ScenarioCollector,
    GPUMetricsCollector,
    collect_all_async,
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


# ── MockWeatherCollector ─────────────────────────────────────────────


class TestMockWeatherCollector:
    """Test mock weather collector for testing scenarios."""

    def test_defaults(self):
        c = MockWeatherCollector()
        metrics = c.collect()

        assert metrics["environmental.atmospheric.visibility_m"] == 1000
        assert metrics["environmental.atmospheric.precipitation"] == "none"
        assert metrics["environmental.atmospheric.wind_speed_ms"] == 5

    def test_set_conditions(self):
        c = MockWeatherCollector()
        c.set_conditions(visibility_m=50, precipitation="fog")

        metrics = c.collect()

        assert metrics["environmental.atmospheric.visibility_m"] == 50
        assert metrics["environmental.atmospheric.precipitation"] == "fog"
        # Wind unchanged
        assert metrics["environmental.atmospheric.wind_speed_ms"] == 5

    def test_partial_update(self):
        c = MockWeatherCollector()
        c.set_conditions(wind_speed_ms=25.0)

        metrics = c.collect()

        # Only wind changed
        assert metrics["environmental.atmospheric.wind_speed_ms"] == 25.0
        assert metrics["environmental.atmospheric.visibility_m"] == 1000

    def test_source_priority(self):
        c = MockWeatherCollector()
        assert c.source_priority == 50


# ── ScenarioCollector ────────────────────────────────────────────────


class TestScenarioCollector:
    """Test scenario-based collector."""

    def test_no_scenario_returns_empty(self):
        c = ScenarioCollector()
        assert c.collect() == {}

    def test_load_and_collect(self, tmp_path):
        scenario = tmp_path / "test.yaml"
        scenario.write_text(
            """
steps:
  - time: 0
    conditions:
      visibility_m: 1000
  - time: 60
    conditions:
      visibility_m: 50
"""
        )

        c = ScenarioCollector()
        c.load_scenario(scenario)

        # At t=0 the first step should apply
        metrics = c.collect()
        assert metrics["visibility_m"] == 1000

    def test_reset(self, tmp_path):
        scenario = tmp_path / "test.yaml"
        scenario.write_text("steps:\n  - time: 0\n    conditions:\n      val: 1\n")

        c = ScenarioCollector()
        c.load_scenario(scenario)
        c.collect()  # sets start_time
        c.reset()

        assert c._start_time is None
        assert c._current_step_index == 0


# ── GPUMetricsCollector ──────────────────────────────────────────────


class TestGPUMetricsCollector:
    """Test GPU metrics collection."""

    def test_source_priority(self):
        c = GPUMetricsCollector()
        assert c.source_priority == 100

    @patch.object(GPUMetricsCollector, "_read_tegrastats", return_value=None)
    @patch.object(GPUMetricsCollector, "_check_nvidia", return_value=False)
    def test_no_gpu_returns_empty(self, mock_nvidia, mock_tegra):
        c = GPUMetricsCollector()
        assert c.collect() == {}

    @patch.object(GPUMetricsCollector, "_read_tegrastats", return_value=None)
    @patch.object(GPUMetricsCollector, "_check_nvidia", return_value=True)
    @patch.object(
        GPUMetricsCollector,
        "_read_nvidia_smi",
        return_value={
            "platform.compute.gpu_temp_c": 65.0,
            "platform.compute.gpu_utilization_pct": 80.0,
            "platform.compute.gpu_memory_used_mb": 1024.0,
            "platform.compute.gpu_memory_total_mb": 4096.0,
        },
    )
    def test_nvidia_smi_metrics(self, mock_smi, mock_nvidia, mock_tegra):
        c = GPUMetricsCollector()
        metrics = c.collect()

        assert metrics["platform.compute.gpu_temp_c"] == 65.0
        assert metrics["platform.compute.gpu_utilization_pct"] == 80.0

    @patch.object(
        GPUMetricsCollector,
        "_read_tegrastats",
        return_value={"platform.compute.gpu_temp_c": 55.0},
    )
    def test_tegrastats_takes_priority(self, mock_tegra):
        """Tegrastats is checked first; if it returns data, nvidia-smi is skipped."""
        c = GPUMetricsCollector()
        metrics = c.collect()

        assert metrics["platform.compute.gpu_temp_c"] == 55.0


# ── collect_all_async ────────────────────────────────────────────────


class _FakeCollector:
    """Minimal sync collector for testing."""

    def __init__(self, name, priority, data):
        self.source_name = name
        self._priority = priority
        self._data = data

    @property
    def source_priority(self):
        return self._priority

    def collect(self):
        return self._data


class _FakeAsyncCollector:
    """Minimal async collector for testing."""

    def __init__(self, name, priority, data):
        self.source_name = name
        self._priority = priority
        self._data = data

    @property
    def source_priority(self):
        return self._priority

    async def collect_async(self):
        return self._data


class _FailingCollector:
    """Collector that raises."""

    def __init__(self):
        self.source_name = "failing"

    @property
    def source_priority(self):
        return 50

    def collect(self):
        raise RuntimeError("sensor failure")


@pytest.mark.asyncio
class TestCollectAllAsync:
    """Test concurrent collector execution."""

    async def test_empty_collectors(self):
        result = await collect_all_async([])
        assert result == {}

    async def test_single_sync_collector(self):
        c = _FakeCollector("test", 100, {"a": 1, "b": 2})

        result = await collect_all_async([c])

        assert result == {"a": 1, "b": 2}

    async def test_multiple_collectors_merge(self):
        c1 = _FakeCollector("c1", 100, {"a": 1})
        c2 = _FakeCollector("c2", 50, {"b": 2})

        result = await collect_all_async([c1, c2])

        assert result == {"a": 1, "b": 2}

    async def test_failing_collector_doesnt_crash(self):
        good = _FakeCollector("good", 100, {"a": 1})
        bad = _FailingCollector()

        result = await collect_all_async([good, bad])

        # Good collector's data is preserved
        assert result == {"a": 1}

    async def test_async_collector(self):
        c = _FakeAsyncCollector("async", 100, {"x": 42})

        result = await collect_all_async([c])

        assert result == {"x": 42}

    async def test_mixed_sync_and_async(self):
        sync = _FakeCollector("sync", 100, {"a": 1})
        async_c = _FakeAsyncCollector("async", 100, {"b": 2})

        result = await collect_all_async([sync, async_c])

        assert result == {"a": 1, "b": 2}
