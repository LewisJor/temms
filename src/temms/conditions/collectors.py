"""
Condition collectors - gather data from various sources.

Collectors can be sync or async. The daemon runs sync collectors
in an executor to avoid blocking.
"""

from typing import Protocol, Dict, Any, Optional, runtime_checkable
from pathlib import Path
import logging
import asyncio

logger = logging.getLogger(__name__)


@runtime_checkable
class ConditionCollector(Protocol):
    """Interface for condition data sources."""

    def collect(self) -> Dict[str, Any]:
        """
        Collect current condition values.

        Returns:
            Dictionary mapping condition paths to values
        """
        ...

    @property
    def source_priority(self) -> int:
        """
        Source priority level.

        Returns:
            Priority value (higher = more authoritative)
        """
        ...

    @property
    def source_name(self) -> str:
        """Source identifier."""
        ...


@runtime_checkable
class AsyncConditionCollector(Protocol):
    """Async interface for condition collectors."""

    async def collect_async(self) -> Dict[str, Any]:
        """Collect conditions asynchronously."""
        ...

    @property
    def source_priority(self) -> int:
        """Source priority level."""
        ...

    @property
    def source_name(self) -> str:
        """Source identifier."""
        ...


class SystemMetricsCollector:
    """Collects system metrics (CPU, memory, etc.)."""

    def __init__(self):
        self.source_name = "system_sensors"

    @property
    def source_priority(self) -> int:
        return 100  # Onboard sensor priority

    def collect(self) -> Dict[str, Any]:
        """Collect system metrics."""
        metrics = {}

        # CPU temperature
        try:
            temp = self._read_cpu_temp()
            if temp is not None:
                metrics["platform.compute.cpu_temp_c"] = temp
        except Exception as e:
            logger.warning(f"Failed to read CPU temp: {e}")

        # Memory
        try:
            mem_info = self._read_memory()
            if mem_info:
                metrics["platform.compute.memory_available_mb"] = mem_info["available_mb"]
        except Exception as e:
            logger.warning(f"Failed to read memory: {e}")

        # Battery (if available)
        try:
            battery = self._read_battery()
            if battery:
                metrics["platform.power.battery_pct"] = battery["percent"]
                metrics["platform.power.power_source"] = battery["source"]
        except Exception as e:
            logger.debug(f"No battery info: {e}")

        return metrics

    def _read_cpu_temp(self) -> Optional[float]:
        """Read CPU temperature from thermal zone."""
        thermal_zones = Path("/sys/class/thermal").glob("thermal_zone*")
        for zone in thermal_zones:
            try:
                temp_file = zone / "temp"
                if temp_file.exists():
                    temp_millic = int(temp_file.read_text().strip())
                    return temp_millic / 1000.0
            except Exception:
                continue
        return None

    def _read_memory(self) -> Optional[Dict[str, int]]:
        """Read memory info from /proc/meminfo."""
        try:
            meminfo = Path("/proc/meminfo").read_text()
            mem_available = None

            for line in meminfo.splitlines():
                if line.startswith("MemAvailable:"):
                    mem_available = int(line.split()[1]) // 1024  # Convert KB to MB
                    break

            if mem_available:
                return {"available_mb": mem_available}
        except Exception:
            pass
        return None

    def _read_battery(self) -> Optional[Dict[str, Any]]:
        """Read battery info if available."""
        power_supply = Path("/sys/class/power_supply")
        if not power_supply.exists():
            return None

        for supply in power_supply.iterdir():
            try:
                capacity_file = supply / "capacity"
                status_file = supply / "status"

                if capacity_file.exists() and status_file.exists():
                    capacity = int(capacity_file.read_text().strip())
                    status = status_file.read_text().strip().lower()

                    source = "battery" if status == "discharging" else "tethered"

                    return {
                        "percent": capacity,
                        "source": source,
                    }
            except Exception:
                continue

        return None


class TimeBasedCollector:
    """Derives time-based conditions."""

    def __init__(self):
        self.source_name = "time_derived"

    @property
    def source_priority(self) -> int:
        return 90  # Derived data priority

    def collect(self) -> Dict[str, Any]:
        """Collect time-based conditions."""
        from datetime import datetime

        now = datetime.now()

        conditions = {}

        # Time of day
        hour = now.hour
        if 6 <= hour < 12:
            time_of_day = "morning"
        elif 12 <= hour < 18:
            time_of_day = "afternoon"
        elif 18 <= hour < 22:
            time_of_day = "evening"
        else:
            time_of_day = "night"

        conditions["operational.time_of_day"] = time_of_day

        # Lighting estimate (very basic - would be better with GPS + sun calc)
        if 7 <= hour < 19:
            ambient = "bright"
        elif 19 <= hour < 21 or 6 <= hour < 7:
            ambient = "low"
        else:
            ambient = "dark"

        conditions["environmental.celestial.ambient"] = ambient

        return conditions


class MockWeatherCollector:
    """Mock weather collector for testing (replace with real sensor)."""

    def __init__(self):
        self.source_name = "weather_mock"
        self._visibility = 1000
        self._precipitation = "none"
        self._wind_speed = 5

    @property
    def source_priority(self) -> int:
        return 50  # External data priority

    def collect(self) -> Dict[str, Any]:
        """Mock weather data."""
        return {
            "environmental.atmospheric.visibility_m": self._visibility,
            "environmental.atmospheric.precipitation": self._precipitation,
            "environmental.atmospheric.wind_speed_ms": self._wind_speed,
        }

    def set_conditions(
        self,
        visibility_m: Optional[int] = None,
        precipitation: Optional[str] = None,
        wind_speed_ms: Optional[float] = None,
    ) -> None:
        """Set mock conditions for testing."""
        if visibility_m is not None:
            self._visibility = visibility_m
        if precipitation is not None:
            self._precipitation = precipitation
        if wind_speed_ms is not None:
            self._wind_speed = wind_speed_ms


class ScenarioCollector:
    """
    Scenario-based collector for testing.

    Reads conditions from a YAML file that defines time-based scenarios.
    """

    def __init__(self, scenario_file: Optional[Path] = None):
        self.source_name = "scenario"
        self.scenario_file = scenario_file
        self._scenario_data: Optional[Dict] = None
        self._start_time: Optional[float] = None
        self._current_step_index = 0

    @property
    def source_priority(self) -> int:
        return 50  # Same as external data

    def load_scenario(self, scenario_file: Path) -> None:
        """Load scenario from YAML file."""
        import yaml
        with open(scenario_file) as f:
            self._scenario_data = yaml.safe_load(f)
        self._start_time = None
        self._current_step_index = 0
        logger.info(f"Loaded scenario: {scenario_file}")

    def collect(self) -> Dict[str, Any]:
        """Collect conditions based on scenario timeline."""
        import time

        if self._scenario_data is None:
            return {}

        if self._start_time is None:
            self._start_time = time.time()

        elapsed = time.time() - self._start_time
        steps = self._scenario_data.get("steps", [])

        # Find current step based on elapsed time
        current_conditions = {}
        for step in steps:
            step_time = step.get("time", 0)
            if elapsed >= step_time:
                conditions = step.get("conditions", {})
                current_conditions.update(conditions)

        return current_conditions

    def reset(self) -> None:
        """Reset scenario to beginning."""
        self._start_time = None
        self._current_step_index = 0


class GPUMetricsCollector:
    """Collect GPU metrics (for NVIDIA Jetson and desktop GPUs)."""

    def __init__(self):
        self.source_name = "gpu_sensors"
        self._has_nvidia = None

    @property
    def source_priority(self) -> int:
        return 100  # Onboard sensor priority

    def _check_nvidia(self) -> bool:
        """Check if nvidia-smi is available."""
        if self._has_nvidia is None:
            import shutil
            self._has_nvidia = shutil.which("nvidia-smi") is not None
        return self._has_nvidia

    def collect(self) -> Dict[str, Any]:
        """Collect GPU metrics."""
        metrics = {}

        # Try tegrastats for Jetson
        tegra_metrics = self._read_tegrastats()
        if tegra_metrics:
            metrics.update(tegra_metrics)
            return metrics

        # Try nvidia-smi for desktop
        if self._check_nvidia():
            nvidia_metrics = self._read_nvidia_smi()
            if nvidia_metrics:
                metrics.update(nvidia_metrics)

        return metrics

    def _read_tegrastats(self) -> Optional[Dict[str, Any]]:
        """Read Jetson tegrastats."""
        # Tegrastats output would need to be parsed
        # This is a placeholder for actual implementation
        return None

    def _read_nvidia_smi(self) -> Optional[Dict[str, Any]]:
        """Read nvidia-smi output."""
        import subprocess

        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0:
                return None

            parts = result.stdout.strip().split(",")
            if len(parts) >= 4:
                return {
                    "platform.compute.gpu_temp_c": float(parts[0].strip()),
                    "platform.compute.gpu_utilization_pct": float(parts[1].strip()),
                    "platform.compute.gpu_memory_used_mb": float(parts[2].strip()),
                    "platform.compute.gpu_memory_total_mb": float(parts[3].strip()),
                }

        except Exception as e:
            logger.debug(f"nvidia-smi failed: {e}")

        return None


async def collect_all_async(
    collectors: list,
    executor=None,
) -> Dict[str, Any]:
    """
    Collect from all collectors concurrently.

    Handles both sync and async collectors.
    """
    loop = asyncio.get_event_loop()
    all_conditions = {}

    async def collect_one(collector):
        if isinstance(collector, AsyncConditionCollector):
            return await collector.collect_async()
        else:
            return await loop.run_in_executor(executor, collector.collect)

    # Collect concurrently
    tasks = [collect_one(c) for c in collectors]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for collector, result in zip(collectors, results):
        if isinstance(result, Exception):
            logger.error(f"Collector {collector.source_name} failed: {result}")
        elif isinstance(result, dict):
            all_conditions.update(result)

    return all_conditions
