from __future__ import annotations

import math
from typing import Any
from urllib.parse import quote

R_SUN_IN_R_EARTH = 109.076
R_SUN_IN_AU = 0.00465047
# Kepler's third law in solar units: a[AU]^3 = M[Msun] * P[yr]^2
DAYS_PER_YEAR = 365.25

NASA_OVERVIEW_URL = "https://exoplanetarchive.ipac.caltech.edu/overview/{host}"
EXOFOP_TARGET_URL = "https://exofop.ipac.caltech.edu/tess/target.php?id={tic}"


def _positive(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or v <= 0:
        return None
    return v


def semi_major_axis_au(period_d: float | None, star_mass_msun: float | None) -> float | None:
    """Circular-orbit semi-major axis from Kepler's third law (planet mass neglected)."""
    p = _positive(period_d)
    m = _positive(star_mass_msun)
    if p is None or m is None:
        return None
    return (m * (p / DAYS_PER_YEAR) ** 2) ** (1.0 / 3.0)


def radius_from_depth_earth(
    depth_ppm: float | None, star_radius_rsun: float | None
) -> float | None:
    """Rp = R* * sqrt(depth). Assumes an undiluted, non-grazing, uniform-disc transit."""
    d = _positive(depth_ppm)
    r = _positive(star_radius_rsun)
    if d is None or r is None or d >= 1_000_000:
        return None
    return r * math.sqrt(d / 1_000_000.0) * R_SUN_IN_R_EARTH


def planet_radius_earth(
    raw_payload: dict[str, Any] | None,
    depth_ppm: float | None,
    star_radius_rsun: float | None,
) -> tuple[float | None, str | None]:
    """Catalogue radius when published, otherwise a transit-depth estimate. Returns (value, basis)."""
    catalogue = _positive((raw_payload or {}).get("pl_rade"))
    if catalogue is not None:
        return catalogue, "catalogue"
    derived = radius_from_depth_earth(depth_ppm, star_radius_rsun)
    if derived is not None:
        return derived, "transit_depth"
    return None, None


def source_url(
    source_name: str,
    source_key: str | None,
    bibcode_or_url: str | None,
    raw_payload: dict[str, Any] | None,
    tic_id: str | None,
) -> str | None:
    """Best public landing page for a report. Stored URLs win; NASA rows get derived links."""
    if bibcode_or_url and bibcode_or_url.startswith(("http://", "https://")):
        return bibcode_or_url
    if bibcode_or_url:
        return f"https://ui.adsabs.harvard.edu/abs/{quote(bibcode_or_url, safe='')}"
    raw = raw_payload or {}
    if source_name == "nasa_pscomppars":
        host = raw.get("hostname")
        if host:
            return NASA_OVERVIEW_URL.format(host=quote(str(host), safe=""))
    if source_name in ("nasa_toi", "exofop_ctoi"):
        tic = tic_id or (str(raw.get("tid")) if raw.get("tid") else None)
        if tic:
            return EXOFOP_TARGET_URL.format(tic=quote(str(tic), safe=""))
    return None


def star_radius_from_payloads(payloads: list[dict[str, Any]]) -> float | None:
    for raw in payloads:
        r = _positive((raw or {}).get("st_rad"))
        if r is not None:
            return r
    return None


def star_mass_from_payloads(payloads: list[dict[str, Any]]) -> float | None:
    for raw in payloads:
        m = _positive((raw or {}).get("st_mass"))
        if m is not None:
            return m
    return None


def star_teff_from_payloads(payloads: list[dict[str, Any]]) -> float | None:
    for raw in payloads:
        t = _positive((raw or {}).get("st_teff"))
        if t is not None:
            return t
    return None
