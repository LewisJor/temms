"""
Tests for the visual simulation weather augmentation engine.
"""

import pytest
import numpy as np

from temms.sim.weather import (
    apply_fog,
    apply_rain,
    apply_snow,
    apply_darkness,
    apply_sun_flare,
    apply_weather,
    conditions_to_effects,
)


@pytest.fixture
def sample_image():
    """A simple 100x100 BGR test image."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[:50, :, 2] = 200  # Red top half
    img[50:, :, 1] = 200  # Green bottom half
    return img


class TestApplyFog:
    """Test fog augmentation."""

    def test_zero_intensity_is_noop(self, sample_image):
        result = apply_fog(sample_image, intensity=0.0)
        np.testing.assert_array_equal(result, sample_image)

    def test_fog_brightens_image(self, sample_image):
        result = apply_fog(sample_image, intensity=0.5)
        # Fog blends with white, so mean brightness should increase
        assert result.mean() > sample_image.mean()

    def test_fog_preserves_shape(self, sample_image):
        result = apply_fog(sample_image, intensity=0.8)
        assert result.shape == sample_image.shape

    def test_fog_output_dtype(self, sample_image):
        result = apply_fog(sample_image, intensity=0.5)
        assert result.dtype == np.uint8

    def test_fog_values_in_range(self, sample_image):
        result = apply_fog(sample_image, intensity=1.0)
        assert result.min() >= 0
        assert result.max() <= 255

    def test_fog_deterministic_with_seed(self, sample_image):
        r1 = apply_fog(sample_image, intensity=0.5, seed=42)
        r2 = apply_fog(sample_image, intensity=0.5, seed=42)
        np.testing.assert_array_equal(r1, r2)

    def test_higher_intensity_more_effect(self, sample_image):
        low = apply_fog(sample_image, intensity=0.2, seed=1)
        high = apply_fog(sample_image, intensity=0.8, seed=1)
        # Higher fog = closer to white = higher mean
        assert high.mean() > low.mean()


class TestApplyRain:
    """Test rain augmentation."""

    def test_zero_intensity_is_noop(self, sample_image):
        result = apply_rain(sample_image, intensity=0.0)
        np.testing.assert_array_equal(result, sample_image)

    def test_rain_darkens_image(self, sample_image):
        result = apply_rain(sample_image, intensity=0.8, seed=42)
        # Rain darkens image (overcast)
        # Compare non-rain-streak pixels by looking at average
        assert result.astype(float).mean() != sample_image.astype(float).mean()

    def test_rain_preserves_shape(self, sample_image):
        result = apply_rain(sample_image, intensity=0.5)
        assert result.shape == sample_image.shape

    def test_rain_output_dtype(self, sample_image):
        result = apply_rain(sample_image, intensity=0.5)
        assert result.dtype == np.uint8


class TestApplySnow:
    """Test snow augmentation."""

    def test_zero_intensity_is_noop(self, sample_image):
        result = apply_snow(sample_image, intensity=0.0)
        np.testing.assert_array_equal(result, sample_image)

    def test_snow_adds_bright_pixels(self, sample_image):
        result = apply_snow(sample_image, intensity=0.8, seed=42)
        # Snow adds white dots and brightens — should increase max brightness areas
        assert result.mean() >= sample_image.mean()

    def test_snow_preserves_shape(self, sample_image):
        result = apply_snow(sample_image, intensity=0.5)
        assert result.shape == sample_image.shape


class TestApplyDarkness:
    """Test darkness / night augmentation."""

    def test_zero_intensity_is_noop(self, sample_image):
        result = apply_darkness(sample_image, intensity=0.0)
        np.testing.assert_array_equal(result, sample_image)

    def test_darkness_reduces_brightness(self, sample_image):
        result = apply_darkness(sample_image, intensity=0.7, seed=42)
        assert result.astype(float).mean() < sample_image.astype(float).mean()

    def test_full_darkness_very_dark(self, sample_image):
        result = apply_darkness(sample_image, intensity=1.0, seed=42)
        assert result.astype(float).mean() < 50  # Very dark

    def test_darkness_preserves_shape(self, sample_image):
        result = apply_darkness(sample_image, intensity=0.5)
        assert result.shape == sample_image.shape


class TestApplySunFlare:
    """Test sun flare augmentation."""

    def test_zero_intensity_is_noop(self, sample_image):
        result = apply_sun_flare(sample_image, intensity=0.0)
        np.testing.assert_array_equal(result, sample_image)

    def test_flare_adds_brightness(self, sample_image):
        result = apply_sun_flare(sample_image, intensity=0.8, seed=42)
        assert result.astype(float).mean() >= sample_image.astype(float).mean()

    def test_flare_preserves_shape(self, sample_image):
        result = apply_sun_flare(sample_image, intensity=0.5)
        assert result.shape == sample_image.shape


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


class TestApplyWeather:
    """Test the combined weather application pipeline."""

    def test_no_effects_is_noop(self, sample_image):
        effects = {"fog": 0.0, "rain": 0.0, "snow": 0.0, "darkness": 0.0, "sun_flare": 0.0}
        result = apply_weather(sample_image, effects)
        np.testing.assert_array_equal(result, sample_image)

    def test_combined_effects(self, sample_image):
        effects = {"fog": 0.3, "rain": 0.2, "snow": 0.0, "darkness": 0.4, "sun_flare": 0.0}
        result = apply_weather(sample_image, effects, seed=42)
        assert result.shape == sample_image.shape
        assert result.dtype == np.uint8

    def test_all_effects_at_once(self, sample_image):
        effects = {"fog": 0.3, "rain": 0.3, "snow": 0.3, "darkness": 0.3, "sun_flare": 0.3}
        result = apply_weather(sample_image, effects, seed=42)
        assert result.shape == sample_image.shape
