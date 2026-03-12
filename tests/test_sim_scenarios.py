"""
Tests for sim scenarios.
"""

import pytest

from temms.sim.scenarios import (
    SCENARIOS,
    FOG_ROLLOUT,
    DAY_NIGHT_CYCLE,
    RAINSTORM,
    COMBINED_STRESS,
    Scenario,
    ScenarioStep,
)


class TestScenarioRegistry:
    """Test the scenario registry."""

    def test_all_scenarios_registered(self):
        assert "fog_rollout" in SCENARIOS
        assert "day_night_cycle" in SCENARIOS
        assert "rainstorm" in SCENARIOS
        assert "combined_stress" in SCENARIOS

    def test_scenario_count(self):
        assert len(SCENARIOS) == 4


class TestFogRollout:
    """Test the fog rollout scenario."""

    def test_has_steps(self):
        assert len(FOG_ROLLOUT.steps) > 0

    def test_starts_clear(self):
        first = FOG_ROLLOUT.steps[0]
        assert first.conditions["environmental.atmospheric.visibility_m"] >= 5000

    def test_ends_clear(self):
        last = FOG_ROLLOUT.steps[-1]
        assert last.conditions["environmental.atmospheric.visibility_m"] >= 1000

    def test_has_fog_step(self):
        fog_steps = [
            s for s in FOG_ROLLOUT.steps
            if s.conditions.get("environmental.atmospheric.visibility_m", 10000) < 100
        ]
        assert len(fog_steps) > 0

    def test_all_steps_have_duration(self):
        for step in FOG_ROLLOUT.steps:
            assert step.duration_s > 0


class TestDayNightCycle:
    """Test the day/night cycle scenario."""

    def test_has_night_step(self):
        night_steps = [
            s for s in DAY_NIGHT_CYCLE.steps
            if s.conditions.get("environmental.celestial.ambient") == "dark"
        ]
        assert len(night_steps) > 0

    def test_has_day_step(self):
        day_steps = [
            s for s in DAY_NIGHT_CYCLE.steps
            if s.conditions.get("environmental.celestial.ambient") == "bright"
        ]
        assert len(day_steps) > 0


class TestScenarioStep:
    """Test ScenarioStep dataclass."""

    def test_defaults(self):
        step = ScenarioStep(name="test", conditions={"a": 1})
        assert step.duration_s == 5.0
        assert step.description == ""

    def test_custom_duration(self):
        step = ScenarioStep(name="test", conditions={}, duration_s=10.0)
        assert step.duration_s == 10.0


class TestAllScenariosValid:
    """Validate all scenarios have required structure."""

    @pytest.mark.parametrize("name", list(SCENARIOS.keys()))
    def test_scenario_has_name(self, name):
        scenario = SCENARIOS[name]
        assert scenario.name == name

    @pytest.mark.parametrize("name", list(SCENARIOS.keys()))
    def test_scenario_has_description(self, name):
        scenario = SCENARIOS[name]
        assert len(scenario.description) > 0

    @pytest.mark.parametrize("name", list(SCENARIOS.keys()))
    def test_scenario_has_steps(self, name):
        scenario = SCENARIOS[name]
        assert len(scenario.steps) >= 2

    @pytest.mark.parametrize("name", list(SCENARIOS.keys()))
    def test_all_steps_have_conditions(self, name):
        scenario = SCENARIOS[name]
        for step in scenario.steps:
            assert len(step.conditions) > 0
