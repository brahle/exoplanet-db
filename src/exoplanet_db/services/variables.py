"""Catalogued variable stars and eclipsing binaries near a position, at a signal's period.

A transit-like dip at the period (or a harmonic) of a catalogued eclipsing binary within a
couple of TESS pixels (21 arcsec each) is most likely that binary's light leaking into the
aperture. These catalogues are reference data: they never create signals.
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.models import Signal, Target, VariableStar
from exoplanet_db.services.reconciliation import angular_distance_arcsec

CATALOGUES = {
    "tess_eb": "TESS eclipsing binaries (Prša et al. 2022, J/ApJS/258/16)",
    "gaia_dr3_eb": "Gaia DR3 eclipsing binaries (I/358/veb)",
    "vsx": "AAVSO International Variable Star Index (B/vsx), periodic entries",
}
HARMONIC_RATIOS = (0.25, 1 / 3, 0.5, 2.0, 3.0, 4.0)
SAME_STAR_ARCSEC = 2.5

# Stored per signal and scored: tighter than the interactive novelty check, because a real
# contaminating binary and the transit search measure the same physical period, and a loose
# tolerance in a crowded field matches some unrelated variable by chance.
SIGNAL_RADIUS_ARCSEC = 120.0
SIGNAL_TOLERANCE_PERCENT = 0.3
SIGNAL_RATIOS = (0.5, 2.0)


def is_eclipsing(catalogue: str, var_type: str | None) -> bool:
    if catalogue in ("tess_eb", "gaia_dr3_eb"):
        return True
    t = (var_type or "").strip().upper()
    return (t.startswith("E") and not t.startswith("EP")) or t.startswith("ELL")


def is_known_transit(var_type: str | None) -> bool:
    """VSX type EP: an exoplanet transit, i.e. a previously reported transit, not a binary."""
    return (var_type or "").strip().upper().startswith("EP")


def cone_condition(ra_col, dec_col, ra_deg: float, dec_deg: float, radius_arcsec: float):
    """Index-friendly bounding box around a cone; correct across RA 0/360 and near the poles."""
    r_deg = radius_arcsec / 3600.0
    dec_lo, dec_hi = max(-90.0, dec_deg - r_deg), min(90.0, dec_deg + r_deg)
    cond = (dec_col >= dec_lo) & (dec_col <= dec_hi)
    if dec_hi < 89.9 and dec_lo > -89.9:
        cos_edge = min(math.cos(math.radians(dec_lo)), math.cos(math.radians(dec_hi)))
        ra_half = min(180.0, r_deg / max(cos_edge, 1e-6))
        ra_lo, ra_hi = ra_deg - ra_half, ra_deg + ra_half
        if ra_half < 180.0:
            if ra_lo < 0:
                ra_cond = (ra_col >= ra_lo + 360.0) | (ra_col <= ra_hi)
            elif ra_hi >= 360.0:
                ra_cond = (ra_col >= ra_lo) | (ra_col <= ra_hi - 360.0)
            else:
                ra_cond = (ra_col >= ra_lo) & (ra_col <= ra_hi)
            cond = cond & ra_cond
    return cond


async def cone(
    session: AsyncSession, ra_deg: float, dec_deg: float, radius_arcsec: float
) -> list[tuple[VariableStar, float]]:
    rows = (
        await session.execute(
            select(VariableStar).where(
                cone_condition(
                    VariableStar.ra_deg, VariableStar.dec_deg, ra_deg, dec_deg, radius_arcsec
                )
            )
        )
    ).scalars()
    hits = []
    for v in rows:
        sep = angular_distance_arcsec(ra_deg, dec_deg, v.ra_deg, v.dec_deg)
        if sep <= radius_arcsec:
            hits.append((v, sep))
    hits.sort(key=lambda h: h[1])
    return hits


def match(
    hits: list[tuple[VariableStar, float]],
    period_d: float,
    period_tolerance_percent: float,
    harmonic_tolerance_percent: float,
    ratios: tuple[float, ...] = HARMONIC_RATIOS,
) -> list[dict[str, Any]]:
    """Variables in the cone at the period or a harmonic; eclipsing binaries on the same star
    are listed whatever their period (a catalogue period can itself be an alias)."""
    out = []
    for v, sep in hits:
        ratio = v.period_d / period_d if period_d > 0 else None
        reason = None
        if ratio is not None and abs(ratio - 1.0) * 100.0 <= period_tolerance_percent:
            reason = "variable_period_match"
        elif ratio is not None and any(
            abs(ratio - h) / h * 100.0 <= harmonic_tolerance_percent for h in ratios
        ):
            reason = "variable_harmonic"
        elif sep <= SAME_STAR_ARCSEC and is_eclipsing(v.catalogue, v.var_type):
            reason = "same_star_eclipsing_binary"
        if reason is None:
            continue
        out.append(
            {
                "catalogue": v.catalogue,
                "source_id": v.source_id,
                "name": v.name,
                "var_type": v.var_type,
                "eclipsing": is_eclipsing(v.catalogue, v.var_type),
                "known_transit": is_known_transit(v.var_type),
                "separation_arcsec": round(sep, 2),
                "catalog_period_d": v.period_d,
                "period_ratio": round(ratio, 4) if ratio is not None else None,
                "mag": v.mag,
                "mag_band": v.mag_band,
                "match_reason": reason,
            }
        )
    out.sort(
        key=lambda m: (m["match_reason"] == "same_star_eclipsing_binary", m["separation_arcsec"])
    )
    return out


async def catalogue_status(session: AsyncSession) -> list[dict[str, Any]]:
    """checked_sources entries for the variable catalogues (count, last load)."""
    from sqlalchemy import func

    from exoplanet_db.models import PublishedSnapshot

    counts = dict(
        (
            await session.execute(
                select(VariableStar.catalogue, func.count(VariableStar.id)).group_by(
                    VariableStar.catalogue
                )
            )
        ).all()
    )
    loaded = dict(
        (
            await session.execute(
                select(PublishedSnapshot.source_name, func.max(PublishedSnapshot.snapshot_utc))
                .where(PublishedSnapshot.source_name.like("variables:%"))
                .group_by(PublishedSnapshot.source_name)
            )
        ).all()
    )
    return [
        {
            "source_name": f"variables:{cat}",
            "description": desc,
            "variable_stars": counts.get(cat, 0),
            "last_ingested_utc": loaded.get(f"variables:{cat}"),
        }
        for cat, desc in CATALOGUES.items()
        if counts.get(cat)
    ]


async def crossmatch_signals(session: AsyncSession) -> dict[str, int]:
    """Store, on every periodic signal, the catalogued variables near its host that share its
    period (or twice / half of it). Planet scoring reads Signal.variable_matches."""
    if not (await session.execute(select(VariableStar.id).limit(1))).first():
        return {"signals": 0, "with_matches": 0, "skipped": "no variable catalogues loaded"}
    rows = (
        await session.execute(
            select(Signal.id, Signal.primary_period_d, Target.ra_deg, Target.dec_deg).join(
                Target, Target.id == Signal.target_id
            )
        )
    ).all()
    by_target: dict[tuple[float, float], list[tuple[str, float | None]]] = {}
    for sid, period, ra, dec in rows:
        if ra is None or dec is None:
            continue
        by_target.setdefault((ra, dec), []).append((sid, period))
    updates, with_matches = [], 0
    for (ra, dec), signals in by_target.items():
        hits = await cone(session, ra, dec, SIGNAL_RADIUS_ARCSEC)
        for sid, period in signals:
            matches = (
                match(
                    hits,
                    period,
                    SIGNAL_TOLERANCE_PERCENT,
                    SIGNAL_TOLERANCE_PERCENT,
                    SIGNAL_RATIOS,
                )
                if (period or 0) > 0
                else []
            )
            matches = [m for m in matches if m["match_reason"] != "same_star_eclipsing_binary"]
            with_matches += bool(matches)
            updates.append({"_id": sid, "variable_matches": matches[:10]})
    from sqlalchemy import bindparam

    stmt = (
        update(Signal.__table__)
        .where(Signal.__table__.c.id == bindparam("_id"))
        .values(variable_matches=bindparam("variable_matches"))
    )
    for i in range(0, len(updates), 2000):
        await session.execute(stmt, updates[i : i + 2000])
    await session.commit()
    return {"signals": len(updates), "with_matches": with_matches}
