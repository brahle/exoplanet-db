from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.time import Time


def calculate_observability(
    ra_deg: float,
    dec_deg: float,
    time_utc: datetime,
    lat_deg: float,
    lon_deg: float,
    height_m: float = 0.0,
) -> dict[str, Any]:
    """
    Computes instant topocentric observability metrics for an astronomical target:
    - Altitude and Airmass (z < 60 deg, airmass < 2.0 -> altitude > 30 deg)
    - Solar elevation for astronomical darkness (h_sun < -18 deg)
    - Lunar separation (separation > 30 deg)
    """
    location = EarthLocation.from_geodetic(
        lon=lon_deg * u.deg, lat=lat_deg * u.deg, height=height_m * u.m
    )
    t = Time(time_utc)
    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")

    altaz_frame = AltAz(obstime=t, location=location)
    target_altaz = target.transform_to(altaz_frame)
    alt_deg = float(target_altaz.alt.deg)
    zenith_deg = 90.0 - alt_deg

    # Airmass approximation: sec(z) for z < 80 deg
    if alt_deg > 5.0:
        airmass = float(1.0 / math.cos(math.radians(zenith_deg)))
    else:
        airmass = 99.0

    # Solar altitude (astronomical twilight is -18 degrees)
    sun_alt_deg = float(get_sun(t).transform_to(altaz_frame).alt.deg)
    is_astronomical_night = sun_alt_deg < -18.0

    # Lunar separation (must be > 30 degrees)
    moon_coord = get_body("moon", t, location=location)
    moon_sep_deg = float(target.separation(moon_coord).deg)
    is_moon_safe = moon_sep_deg > 30.0

    # Altitude criteria: z < 60 deg <=> alt > 30 deg <=> airmass < 2.0
    is_target_up = alt_deg >= 30.0 and airmass <= 2.0

    is_observable = is_target_up and is_astronomical_night and is_moon_safe

    return {
        "target_altitude_deg": round(alt_deg, 2),
        "target_airmass": round(airmass, 2),
        "sun_altitude_deg": round(sun_alt_deg, 2),
        "moon_separation_deg": round(moon_sep_deg, 2),
        "is_target_up": is_target_up,
        "is_astronomical_night": is_astronomical_night,
        "is_moon_safe": is_moon_safe,
        "is_observable": is_observable,
    }


def compute_priority_score(
    disposition: str,
    timing_uncertainty_min: float,
    depth_ppm: float,
    duration_h: float,
    is_observable: bool,
    is_coherent: bool = True,
) -> float:
    """
    Ranks observation priority using a multi-factor score:
    Score = Credibility * Urgency * DepthFactor * DurationFactor
    Withholds (score = 0.0) targets that are unobservable or lack a coherent ephemeris.
    """
    if not is_observable or not is_coherent:
        return 0.0

    # 1. Credibility weight
    weights = {
        "confirmed_planet": 1.0,
        "validated_planet": 0.95,
        "planet_candidate": 0.85,
        "unclassified_transit_like_signal": 0.50,
        "false_positive": 0.0,
    }
    w_cred = weights.get(disposition, 0.5)
    if w_cred == 0.0:
        return 0.0

    # 2. Ephemeris Urgency: widening uncertainty window in danger of losing ephemeris
    # Logarithmic scaling, capped between 1.0 and 5.0
    w_urgency = min(5.0, 1.0 + math.log10(max(1.0, timing_uncertainty_min)))

    # 3. Transit Depth Factor: SNR scales with depth
    w_depth = math.sqrt(max(100.0, depth_ppm) / 1000.0)

    # 4. Duration Factor: reasonable observing window (1 to 5 hours)
    w_duration = min(2.0, max(0.5, duration_h / 2.5))

    return round(w_cred * w_urgency * w_depth * w_duration, 4)
