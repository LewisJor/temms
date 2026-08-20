"""Tests for the condition-to-effects mapping the headless sim drives."""

from temms.sim.weather import conditions_to_effects


class TestConditionsToEffects:
    """Test the TEMMS conditions → visual effects mapping."""

    def test_clear_conditions(self):
        effects = conditions_to_effects({
            "environmental.atmospheric.visibility_m": 10000,
            "environmental.atmospheric.precipitation": "none",
            "environmental.celestial.ambient": "bright",
        })
        assert effects["fog"] == 0.0
        assert effects["rain"] == 0.0
        assert effects["snow"] == 0.0
        assert effects["darkness"] == 0.0

    def test_low_visibility_triggers_fog(self):
        effects = conditions_to_effects({
            "environmental.atmospheric.visibility_m": 80,
        })
        assert effects["fog"] >= 0.5

    def test_rain_precipitation(self):
        effects = conditions_to_effects({
            "environmental.atmospheric.precipitation": "rain",
        })
        assert effects["rain"] > 0.0

    def test_snow_precipitation(self):
        effects = conditions_to_effects({
            "environmental.atmospheric.precipitation": "snow",
        })
        assert effects["snow"] > 0.0

    def test_dark_ambient(self):
        effects = conditions_to_effects({
            "environmental.celestial.ambient": "dark",
        })
        assert effects["darkness"] >= 0.8

    def test_low_sun_triggers_flare(self):
        effects = conditions_to_effects({
            "environmental.celestial.sun_elevation_deg": 8,
        })
        assert effects["sun_flare"] > 0.0

    def test_negative_sun_triggers_darkness(self):
        effects = conditions_to_effects({
            "environmental.celestial.sun_elevation_deg": -10,
        })
        assert effects["darkness"] >= 0.7


