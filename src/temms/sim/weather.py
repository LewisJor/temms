"""
Weather augmentation engine using OpenCV.

Applies fog, rain, snow, darkness, and sun flare effects to images.
No external dependency beyond numpy + opencv (both already needed for inference).
Each effect has an intensity parameter in [0.0, 1.0] that controls severity.

Why not Albumentations?  We considered it, but for a real-time sim loop we
only need 5 transforms and we don't want to add a dependency.  The transforms
here are ~10 lines each and run in <2ms per 640x480 frame on a 2020 MacBook.
"""

import numpy as np
from typing import Optional


def apply_fog(
    image: np.ndarray,
    intensity: float = 0.5,
    seed: Optional[int] = None,
) -> np.ndarray:
    """
    Simulate fog by blending with a white overlay and reducing contrast.

    intensity 0.0 = clear day, 1.0 = near-zero visibility.
    Corresponding TEMMS condition: environmental.atmospheric.visibility_m
      intensity 0.0 → ~10000m, 0.3 → ~500m, 0.6 → ~100m, 1.0 → ~10m
    """
    intensity = np.clip(intensity, 0.0, 1.0)
    if intensity < 0.01:
        return image

    h, w = image.shape[:2]
    rng = np.random.RandomState(seed)

    # Create fog layer — a near-white image with Gaussian noise for texture
    fog = np.ones_like(image, dtype=np.float32) * 255.0

    # Add slight spatial variation so it doesn't look like a flat overlay
    noise = rng.normal(0, 15, (h // 8, w // 8, 1)).astype(np.float32)
    # Resize noise to full image (bilinear gives smooth clouds)
    import cv2

    noise = cv2.resize(noise, (w, h), interpolation=cv2.INTER_LINEAR)
    if noise.ndim == 2:
        noise = noise[:, :, np.newaxis]
    fog = np.clip(fog + noise, 200, 255)

    # Blend original with fog
    alpha = intensity * 0.75  # cap at 75% opacity so image is never fully white
    result = cv2.addWeighted(
        image.astype(np.float32), 1.0 - alpha, fog, alpha, 0
    )

    # Reduce contrast (fog washes out dark areas)
    contrast_factor = 1.0 - intensity * 0.5
    mean = result.mean()
    result = mean + (result - mean) * contrast_factor

    return np.clip(result, 0, 255).astype(np.uint8)


def apply_rain(
    image: np.ndarray,
    intensity: float = 0.5,
    seed: Optional[int] = None,
) -> np.ndarray:
    """
    Simulate rain by drawing angled streaks and darkening the image.

    intensity 0.0 = dry, 1.0 = downpour.
    """
    import cv2

    intensity = np.clip(intensity, 0.0, 1.0)
    if intensity < 0.01:
        return image

    h, w = image.shape[:2]
    rng = np.random.RandomState(seed)
    result = image.copy()

    # Darken image (overcast sky)
    darkness = 1.0 - intensity * 0.3
    result = (result.astype(np.float32) * darkness).astype(np.uint8)

    # Draw rain streaks
    n_drops = int(200 * intensity + 50)
    for _ in range(n_drops):
        x = rng.randint(0, w)
        y = rng.randint(0, h)
        length = rng.randint(10, 30)
        angle_offset = rng.randint(-5, 5)

        x2 = x + angle_offset
        y2 = min(y + length, h - 1)

        alpha = rng.uniform(0.3, 0.7)
        color = int(200 * alpha)
        cv2.line(result, (x, y), (x2, y2), (color, color, color), 1, cv2.LINE_AA)

    # Slight blur to soften streaks
    if intensity > 0.5:
        result = cv2.GaussianBlur(result, (3, 3), 0)

    return result


def apply_snow(
    image: np.ndarray,
    intensity: float = 0.5,
    seed: Optional[int] = None,
) -> np.ndarray:
    """
    Simulate snow by adding white dots and brightening the scene.

    intensity 0.0 = clear, 1.0 = blizzard.
    """
    import cv2

    intensity = np.clip(intensity, 0.0, 1.0)
    if intensity < 0.01:
        return image

    h, w = image.shape[:2]
    rng = np.random.RandomState(seed)
    result = image.copy()

    # Brighten image slightly (snow reflects light)
    brightness = 1.0 + intensity * 0.15
    result = np.clip(result.astype(np.float32) * brightness, 0, 255).astype(np.uint8)

    # Draw snowflakes
    n_flakes = int(300 * intensity + 30)
    for _ in range(n_flakes):
        x = rng.randint(0, w)
        y = rng.randint(0, h)
        radius = rng.randint(1, max(2, int(3 * intensity)))
        cv2.circle(result, (x, y), radius, (255, 255, 255), -1, cv2.LINE_AA)

    # Soft blur to mimic motion
    if intensity > 0.4:
        result = cv2.GaussianBlur(result, (3, 3), 0)

    return result


def apply_darkness(
    image: np.ndarray,
    intensity: float = 0.5,
    seed: Optional[int] = None,
) -> np.ndarray:
    """
    Simulate nighttime / low ambient light.

    intensity 0.0 = full daylight, 1.0 = near-total darkness.
    Corresponding TEMMS condition: environmental.celestial.ambient
      intensity 0.0 → bright, 0.3 → normal, 0.6 → low, 1.0 → dark
    """
    intensity = np.clip(intensity, 0.0, 1.0)
    if intensity < 0.01:
        return image

    rng = np.random.RandomState(seed)
    result = image.astype(np.float32)

    # Reduce brightness
    factor = 1.0 - intensity * 0.85
    result *= factor

    # Shift color temperature toward blue (moonlight)
    if intensity > 0.3:
        blue_shift = intensity * 0.15
        result[:, :, 0] = np.clip(result[:, :, 0] * (1.0 + blue_shift), 0, 255)  # B
        result[:, :, 2] = np.clip(result[:, :, 2] * (1.0 - blue_shift * 0.5), 0, 255)  # R

    # Add sensor noise (cameras get noisy in low light)
    if intensity > 0.4:
        noise_level = intensity * 15
        noise = rng.normal(0, noise_level, result.shape).astype(np.float32)
        result += noise

    return np.clip(result, 0, 255).astype(np.uint8)


def apply_sun_flare(
    image: np.ndarray,
    intensity: float = 0.5,
    seed: Optional[int] = None,
) -> np.ndarray:
    """
    Simulate sun flare / glare from a bright light source.

    intensity 0.0 = none, 1.0 = blinding.
    """
    import cv2

    intensity = np.clip(intensity, 0.0, 1.0)
    if intensity < 0.01:
        return image

    h, w = image.shape[:2]
    rng = np.random.RandomState(seed)
    result = image.astype(np.float32)

    # Place flare source in upper portion of image
    cx = rng.randint(w // 4, 3 * w // 4)
    cy = rng.randint(0, h // 3)

    # Create radial gradient
    Y, X = np.ogrid[:h, :w]
    dist = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2).astype(np.float32)
    max_dist = np.sqrt(h**2 + w**2)

    # Flare intensity falls off with distance
    flare = np.exp(-dist / (max_dist * 0.2 * (1.0 + intensity)))
    flare = (flare * 255 * intensity * 0.8)[:, :, np.newaxis]

    result += flare
    return np.clip(result, 0, 255).astype(np.uint8)


# ----- Mapping from TEMMS conditions to weather effects -----

def conditions_to_effects(conditions: dict) -> dict:
    """
    Convert TEMMS condition values to weather effect intensities.

    This is the bridge between the TEMMS condition system and the
    visual simulation.  It maps semantic condition values (visibility
    in meters, precipitation type) to effect parameters.

    Returns:
        dict with keys: fog, rain, snow, darkness, sun_flare
              each a float in [0.0, 1.0]
    """
    effects = {
        "fog": 0.0,
        "rain": 0.0,
        "snow": 0.0,
        "darkness": 0.0,
        "sun_flare": 0.0,
    }

    # -- Visibility → fog --
    vis = conditions.get("environmental.atmospheric.visibility_m")
    if vis is not None:
        vis = float(vis)
        if vis >= 5000:
            effects["fog"] = 0.0
        elif vis >= 1000:
            effects["fog"] = 0.1
        elif vis >= 500:
            effects["fog"] = 0.3
        elif vis >= 100:
            effects["fog"] = 0.55
        elif vis >= 50:
            effects["fog"] = 0.7
        else:
            effects["fog"] = 0.9

    # -- Precipitation → rain / snow --
    precip = conditions.get("environmental.atmospheric.precipitation", "none")
    if precip in ("rain", "drizzle"):
        effects["rain"] = 0.4 if precip == "drizzle" else 0.7
    elif precip == "heavy_rain":
        effects["rain"] = 0.9
    elif precip in ("snow", "sleet"):
        effects["snow"] = 0.6
    elif precip in ("fog", "mist"):
        effects["fog"] = max(effects["fog"], 0.5)

    # -- Ambient light → darkness --
    ambient = conditions.get("environmental.celestial.ambient")
    if ambient == "dark":
        effects["darkness"] = 0.85
    elif ambient == "low":
        effects["darkness"] = 0.55
    elif ambient == "normal":
        effects["darkness"] = 0.15
    # "bright" = 0.0 (default)

    # -- Sun elevation → sun flare --
    sun_elev = conditions.get("environmental.celestial.sun_elevation_deg")
    if sun_elev is not None:
        sun_elev = float(sun_elev)
        if sun_elev < 0:
            # Below horizon → dark, no flare
            effects["darkness"] = max(effects["darkness"], 0.7)
        elif sun_elev < 15:
            # Low sun → potential flare
            effects["sun_flare"] = 0.5

    return effects


def apply_weather(
    image: np.ndarray,
    effects: dict,
    seed: Optional[int] = None,
) -> np.ndarray:
    """
    Apply all weather effects to an image in the correct order.

    Args:
        image: BGR uint8 image (OpenCV format)
        effects: dict from conditions_to_effects()
        seed: random seed for reproducibility

    Returns:
        Augmented image
    """
    result = image

    # Order matters:  darkness first, then atmospheric, then flare
    if effects.get("darkness", 0) > 0.01:
        result = apply_darkness(result, effects["darkness"], seed)

    if effects.get("fog", 0) > 0.01:
        result = apply_fog(result, effects["fog"], seed)

    if effects.get("rain", 0) > 0.01:
        result = apply_rain(result, effects["rain"], seed)

    if effects.get("snow", 0) > 0.01:
        result = apply_snow(result, effects["snow"], seed)

    if effects.get("sun_flare", 0) > 0.01:
        result = apply_sun_flare(result, effects["sun_flare"], seed)

    return result
