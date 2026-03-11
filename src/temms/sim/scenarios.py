"""
Pre-built simulation scenarios.

A scenario is a timeline of condition changes that drives the weather
augmentation engine and the TEMMS policy engine simultaneously.
Each step specifies conditions and a duration to hold them.

Usage:
    scenario = SCENARIOS["fog_rollout"]
    for step in scenario.steps:
        # apply step.conditions to sim + TEMMS
        # wait step.duration_s
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional


@dataclass
class ScenarioStep:
    """One step in a scenario timeline."""
    name: str
    conditions: Dict[str, Any]
    duration_s: float = 5.0
    description: str = ""


@dataclass
class Scenario:
    """A named sequence of condition changes."""
    name: str
    description: str
    steps: List[ScenarioStep] = field(default_factory=list)
    loop: bool = False  # Whether to repeat after last step


# ----- Built-in scenarios -----

FOG_ROLLOUT = Scenario(
    name="fog_rollout",
    description="Clear day → fog rolls in → near-zero visibility → clears up",
    steps=[
        ScenarioStep(
            name="clear_day",
            description="Beautiful clear day, full visibility",
            conditions={
                "environmental.atmospheric.visibility_m": 10000,
                "environmental.atmospheric.precipitation": "none",
                "environmental.celestial.ambient": "bright",
            },
            duration_s=8,
        ),
        ScenarioStep(
            name="light_haze",
            description="Haze starting to form",
            conditions={
                "environmental.atmospheric.visibility_m": 800,
                "environmental.atmospheric.precipitation": "none",
            },
            duration_s=5,
        ),
        ScenarioStep(
            name="moderate_fog",
            description="Fog rolling in — visibility dropping",
            conditions={
                "environmental.atmospheric.visibility_m": 200,
                "environmental.atmospheric.precipitation": "fog",
            },
            duration_s=6,
        ),
        ScenarioStep(
            name="heavy_fog",
            description="Dense fog — should trigger policy switch",
            conditions={
                "environmental.atmospheric.visibility_m": 80,
                "environmental.atmospheric.precipitation": "fog",
            },
            duration_s=8,
        ),
        ScenarioStep(
            name="critical_fog",
            description="Near-zero visibility — mobilenet fallback territory",
            conditions={
                "environmental.atmospheric.visibility_m": 15,
                "environmental.atmospheric.precipitation": "fog",
                "operational.mission.priority": "critical",
            },
            duration_s=6,
        ),
        ScenarioStep(
            name="fog_clearing",
            description="Fog beginning to lift",
            conditions={
                "environmental.atmospheric.visibility_m": 300,
                "environmental.atmospheric.precipitation": "mist",
            },
            duration_s=5,
        ),
        ScenarioStep(
            name="clear_again",
            description="Visibility restored — should switch back to daylight",
            conditions={
                "environmental.atmospheric.visibility_m": 5000,
                "environmental.atmospheric.precipitation": "none",
            },
            duration_s=8,
        ),
    ],
)

DAY_NIGHT_CYCLE = Scenario(
    name="day_night_cycle",
    description="Transition from daylight through sunset to full night and back",
    steps=[
        ScenarioStep(
            name="midday",
            description="Bright midday sun",
            conditions={
                "environmental.celestial.ambient": "bright",
                "environmental.celestial.sun_elevation_deg": 60,
                "environmental.atmospheric.visibility_m": 10000,
            },
            duration_s=6,
        ),
        ScenarioStep(
            name="late_afternoon",
            description="Sun getting low, warm light",
            conditions={
                "environmental.celestial.ambient": "normal",
                "environmental.celestial.sun_elevation_deg": 20,
            },
            duration_s=5,
        ),
        ScenarioStep(
            name="golden_hour",
            description="Low sun, potential glare",
            conditions={
                "environmental.celestial.ambient": "normal",
                "environmental.celestial.sun_elevation_deg": 8,
            },
            duration_s=5,
        ),
        ScenarioStep(
            name="twilight",
            description="Sun below horizon, fading light",
            conditions={
                "environmental.celestial.ambient": "low",
                "environmental.celestial.sun_elevation_deg": -2,
            },
            duration_s=6,
        ),
        ScenarioStep(
            name="full_night",
            description="Complete darkness — lowlight model should activate",
            conditions={
                "environmental.celestial.ambient": "dark",
                "environmental.celestial.sun_elevation_deg": -15,
            },
            duration_s=8,
        ),
        ScenarioStep(
            name="dawn",
            description="First light appearing",
            conditions={
                "environmental.celestial.ambient": "low",
                "environmental.celestial.sun_elevation_deg": 3,
            },
            duration_s=5,
        ),
        ScenarioStep(
            name="sunrise",
            description="Sun back up — daylight model should return",
            conditions={
                "environmental.celestial.ambient": "bright",
                "environmental.celestial.sun_elevation_deg": 30,
            },
            duration_s=6,
        ),
    ],
)

RAINSTORM = Scenario(
    name="rainstorm",
    description="Clear → light rain → heavy downpour → clearing",
    steps=[
        ScenarioStep(
            name="clear",
            conditions={
                "environmental.atmospheric.visibility_m": 10000,
                "environmental.atmospheric.precipitation": "none",
                "environmental.celestial.ambient": "normal",
            },
            duration_s=6,
        ),
        ScenarioStep(
            name="overcast",
            conditions={
                "environmental.atmospheric.visibility_m": 3000,
                "environmental.atmospheric.precipitation": "none",
                "environmental.celestial.ambient": "normal",
            },
            duration_s=4,
        ),
        ScenarioStep(
            name="drizzle",
            conditions={
                "environmental.atmospheric.visibility_m": 1500,
                "environmental.atmospheric.precipitation": "drizzle",
            },
            duration_s=5,
        ),
        ScenarioStep(
            name="moderate_rain",
            conditions={
                "environmental.atmospheric.visibility_m": 500,
                "environmental.atmospheric.precipitation": "rain",
            },
            duration_s=6,
        ),
        ScenarioStep(
            name="downpour",
            conditions={
                "environmental.atmospheric.visibility_m": 100,
                "environmental.atmospheric.precipitation": "heavy_rain",
            },
            duration_s=8,
        ),
        ScenarioStep(
            name="easing",
            conditions={
                "environmental.atmospheric.visibility_m": 800,
                "environmental.atmospheric.precipitation": "drizzle",
            },
            duration_s=5,
        ),
        ScenarioStep(
            name="clear_after",
            conditions={
                "environmental.atmospheric.visibility_m": 8000,
                "environmental.atmospheric.precipitation": "none",
            },
            duration_s=6,
        ),
    ],
)

COMBINED_STRESS = Scenario(
    name="combined_stress",
    description="Multi-factor stress: fog + night + battery drain, then recovery",
    steps=[
        ScenarioStep(
            name="nominal",
            description="All systems nominal",
            conditions={
                "environmental.atmospheric.visibility_m": 10000,
                "environmental.atmospheric.precipitation": "none",
                "environmental.celestial.ambient": "bright",
                "platform.power.battery_pct": 90,
                "platform.compute.cpu_temp_c": 45,
            },
            duration_s=6,
        ),
        ScenarioStep(
            name="fog_at_dusk",
            description="Fog + approaching darkness",
            conditions={
                "environmental.atmospheric.visibility_m": 200,
                "environmental.atmospheric.precipitation": "fog",
                "environmental.celestial.ambient": "low",
            },
            duration_s=8,
        ),
        ScenarioStep(
            name="thermal_stress",
            description="CPU heating up under load",
            conditions={
                "platform.compute.cpu_temp_c": 78,
            },
            duration_s=6,
        ),
        ScenarioStep(
            name="battery_drain",
            description="Battery getting low",
            conditions={
                "platform.power.battery_pct": 15,
                "platform.power.power_source": "battery",
            },
            duration_s=6,
        ),
        ScenarioStep(
            name="recovery",
            description="Plugged in, fog clears, temperature drops",
            conditions={
                "environmental.atmospheric.visibility_m": 5000,
                "environmental.atmospheric.precipitation": "none",
                "environmental.celestial.ambient": "bright",
                "platform.power.battery_pct": 50,
                "platform.power.power_source": "tethered",
                "platform.compute.cpu_temp_c": 50,
            },
            duration_s=8,
        ),
    ],
)


# Registry of all built-in scenarios
SCENARIOS: Dict[str, Scenario] = {
    s.name: s
    for s in [FOG_ROLLOUT, DAY_NIGHT_CYCLE, RAINSTORM, COMBINED_STRESS]
}
