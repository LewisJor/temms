"""
Condition collectors - gather data from various sources.

Collectors can be sync or async. The daemon runs sync collectors
in an executor to avoid blocking.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Per-sensor health is published under this prefix at the same priority as other
# runtime health, so policies (and the evidence chain) can see a blind sensor.
SENSOR_HEALTH_PREFIX = "runtime.sensors"


class SensorStatus:
    """Outcome of a single sensor read.

    The distinction that matters under DDIL: ``ABSENT`` means this platform
    genuinely has no such sensor (a desktop has no battery) — expected, and
    healthy. ``FAILED`` means the sensor exists but could not be read — the
    device has gone *blind* to an input its policies may depend on, which is
    actionable and must be visible.
    """

    OK = "ok"
    ABSENT = "absent"
    FAILED = "failed"


@dataclass(frozen=True)
class SensorRead:
    """A sensor read plus why it produced (or did not produce) a value."""

    status: str
    value: Any = None
    error: str | None = None

    @property
    def healthy(self) -> bool:
        """Absent hardware is not a fault; an unreadable sensor is."""
        return self.status != SensorStatus.FAILED

    @classmethod
    def ok(cls, value: Any) -> "SensorRead":
        return cls(SensorStatus.OK, value=value)

    @classmethod
    def absent(cls) -> "SensorRead":
        return cls(SensorStatus.ABSENT)

    @classmethod
    def failed(cls, error: BaseException | str) -> "SensorRead":
        return cls(SensorStatus.FAILED, error=str(error))


@runtime_checkable
class ConditionCollector(Protocol):
    """Interface for condition data sources."""

    def collect(self) -> dict[str, Any]:
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

    async def collect_async(self) -> dict[str, Any]:
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

    def collect(self) -> dict[str, Any]:
        """Collect system metrics, publishing per-sensor health alongside values.

        A failed sensor must not look like an absent one. Without per-sensor
        health, a thermal probe that starts failing in the field simply stops
        contributing its condition; the policy rule that depends on it silently
        stops matching, thermal-adaptive switching quietly ceases, and nothing
        records why. Each sensor therefore reports its own status so a blind
        input is visible to policy and to the evidence chain.
        """
        metrics: dict[str, Any] = {}

        readings = {
            "cpu_temp": self._safe_read("cpu_temp", self._read_cpu_temp),
            "memory": self._safe_read("memory", self._read_memory),
            "battery": self._safe_read("battery", self._read_battery),
        }

        cpu = readings["cpu_temp"]
        if cpu.status == SensorStatus.OK:
            metrics["platform.compute.cpu_temp_c"] = cpu.value

        memory = readings["memory"]
        if memory.status == SensorStatus.OK:
            metrics["platform.compute.memory_available_mb"] = memory.value["available_mb"]

        battery = readings["battery"]
        if battery.status == SensorStatus.OK:
            metrics["platform.power.battery_pct"] = battery.value["percent"]
            metrics["platform.power.power_source"] = battery.value["source"]

        for name, reading in readings.items():
            metrics[f"{SENSOR_HEALTH_PREFIX}.{name}.status"] = reading.status
            metrics[f"{SENSOR_HEALTH_PREFIX}.{name}.healthy"] = reading.healthy
            metrics[f"{SENSOR_HEALTH_PREFIX}.{name}.last_error"] = reading.error

        return metrics

    @staticmethod
    def _safe_read(name: str, read) -> SensorRead:
        """Run one sensor read, converting an unexpected raise into FAILED.

        The individual readers already classify their own outcome; this is the
        backstop that keeps one broken sensor from taking down the whole collect
        pass while still recording that it broke.
        """
        try:
            return read()
        except Exception as exc:  # a reader itself misbehaved
            logger.warning(f"Sensor {name} read failed: {exc}")
            return SensorRead.failed(exc)

    def _read_cpu_temp(self) -> SensorRead:
        """Read CPU temperature from a thermal zone.

        No thermal zones at all -> ABSENT. Zones present but every one of them
        unreadable -> FAILED: the hardware claims a sensor we can no longer read.
        """
        zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*"))
        if not zones:
            return SensorRead.absent()

        last_error: str | None = None
        found_temp_file = False
        for zone in zones:
            temp_file = zone / "temp"
            if not temp_file.exists():
                continue
            found_temp_file = True
            try:
                temp_millic = int(temp_file.read_text().strip())
            except (OSError, ValueError) as exc:
                last_error = f"{temp_file}: {exc}"
                continue
            return SensorRead.ok(temp_millic / 1000.0 if temp_millic > 1000 else float(temp_millic))

        if not found_temp_file:
            return SensorRead.absent()
        return SensorRead.failed(last_error or "no readable thermal zone")

    def _read_memory(self) -> SensorRead:
        """Read available memory from /proc/meminfo.

        No /proc/meminfo -> ABSENT (non-Linux). Present but unparseable ->
        FAILED; memory is a feasibility input, so silently losing it matters.
        """
        meminfo_path = Path("/proc/meminfo")
        if not meminfo_path.exists():
            return SensorRead.absent()
        try:
            meminfo = meminfo_path.read_text()
        except OSError as exc:
            return SensorRead.failed(exc)

        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                try:
                    return SensorRead.ok({"available_mb": int(line.split()[1]) // 1024})
                except (IndexError, ValueError) as exc:
                    return SensorRead.failed(f"malformed MemAvailable line: {exc}")
        return SensorRead.failed("MemAvailable not present in /proc/meminfo")

    def _read_battery(self) -> SensorRead:
        """Read battery state if this platform has one.

        No power_supply tree, or no supply exposing capacity+status -> ABSENT
        (a tethered device legitimately has no battery). A supply that exposes
        those files but cannot be read -> FAILED.
        """
        power_supply = Path("/sys/class/power_supply")
        if not power_supply.exists():
            return SensorRead.absent()

        last_error: str | None = None
        found_battery = False
        try:
            supplies = sorted(power_supply.iterdir())
        except OSError as exc:
            return SensorRead.failed(exc)

        for supply in supplies:
            capacity_file = supply / "capacity"
            status_file = supply / "status"
            if not (capacity_file.exists() and status_file.exists()):
                continue
            found_battery = True
            try:
                capacity = int(capacity_file.read_text().strip())
                status = status_file.read_text().strip().lower()
            except (OSError, ValueError) as exc:
                last_error = f"{supply.name}: {exc}"
                continue
            return SensorRead.ok(
                {
                    "percent": capacity,
                    "source": "battery" if status == "discharging" else "tethered",
                }
            )

        if not found_battery:
            return SensorRead.absent()
        return SensorRead.failed(last_error or "no readable battery supply")


class TimeBasedCollector:
    """Derives time-based conditions."""

    def __init__(self):
        self.source_name = "time_derived"

    @property
    def source_priority(self) -> int:
        return 90  # Derived data priority

    def collect(self) -> dict[str, Any]:
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

