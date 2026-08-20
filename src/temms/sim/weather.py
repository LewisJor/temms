"""
Weather augmentation engine using OpenCV.

Applies fog, rain, snow, darkness, and sun flare effects to images.
No external dependency beyond numpy + opencv (both already needed for inference).
Each effect has an intensity parameter in [0.0, 1.0] that controls severity.

Why not Albumentations?  We considered it, but for a real-time sim loop we
only need 5 transforms and we don't want to add a dependency.  The transforms
here are ~10 lines each and run in <2ms per 640x480 frame on a 2020 MacBook.
"""



# ----- Mapping from TEMMS conditions to weather effects -----

def conditions_to_effects(conditions: dict) -> dict:  # noqa: C901  (tracked in #54)
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


