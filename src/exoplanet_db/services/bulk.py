"""Batch reads for agents: many candidates or many identifiers in one round trip.

Returns plain JSON-ready dicts so the REST batch endpoints and the MCP tools share one shape.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from exoplanet_db.models import Report, Signal, Target

MAX_BATCH = 500
MIN_ID_PREFIX = 8

_HEX_ID = re.compile(r"^[0-9a-f-]+$")
_TIC = re.compile(r"^(?:tic)?[\s_-]*(\d{1,10})$", re.I)
_GAIA = re.compile(r"^gaia(?:[\s_-]*dr3)?[\s_-]*(\d{6,20})$", re.I)
_BARE_GAIA = re.compile(r"^\d{11,20}$")
_TOI_HOST = re.compile(r"^toi[\s_-]*(\d+)$", re.I)


def _norm(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value.lower())


def _norm_sql(column):
    return func.replace(func.replace(func.replace(func.lower(column), " ", ""), "-", ""), "_", "")


def _positive(value: float | None) -> float | None:
    return value if (value or 0) > 0 else None


def signal_record(s: Signal, with_target: bool = True) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": s.id,
        "display_name": s.display_name,
        "period_d": _positive(s.primary_period_d),
        "period_err_d": s.primary_period_err_d,
        "epoch_bjd": s.primary_epoch_bjd if (s.primary_epoch_bjd or 0) > 2400000 else None,
        "epoch_err_d": s.primary_epoch_err_d,
        "duration_h": s.primary_duration_h,
        "depth_ppm": s.primary_depth_ppm,
        "disposition": s.current_disposition,
        "planet_score": s.planet_score,
        "localisation": s.source_localisation_status,
        "timing": s.timing_consistency_status,
        "sources": sorted({r.source_name for r in s.reports}),
        "source_keys": sorted({r.source_key for r in s.reports}),
        "limits": s.scientific_limits or [],
    }
    if with_target and s.target is not None:
        t = s.target
        record.update(
            {
                "target_id": t.id,
                "target_name": t.primary_name,
                "tic_id": t.tic_id,
                "ra_deg": t.ra_deg,
                "dec_deg": t.dec_deg,
            }
        )
    return record


def target_record(t: Target) -> dict[str, Any]:
    signals = sorted(
        t.signals, key=lambda s: (_positive(s.primary_period_d) or 1e9, s.display_name)
    )
    return {
        "id": t.id,
        "primary_name": t.primary_name,
        "tic_id": t.tic_id,
        "gaia_dr3_id": t.gaia_dr3_id,
        "ra_deg": t.ra_deg,
        "dec_deg": t.dec_deg,
        "v_mag": t.v_mag,
        "tess_mag": t.tess_mag,
        "teff_k": t.teff_k,
        "radius_rsun": t.radius_rsun,
        "mass_msun": t.mass_msun,
        "aliases": t.aliases or [],
        "signals": [signal_record(s, with_target=False) for s in signals],
    }


async def load_targets(session: AsyncSession, target_ids: list[str]) -> list[dict[str, Any]]:
    """target_record for each id, in the given order."""
    if not target_ids:
        return []
    found = {
        t.id: target_record(t)
        for t in (
            await session.execute(
                select(Target)
                .where(Target.id.in_(target_ids))
                .options(selectinload(Target.signals).selectinload(Signal.reports))
            )
        ).scalars()
    }
    return [found[i] for i in target_ids if i in found]


async def get_signals(session: AsyncSession, ids: list[str]) -> dict[str, Any]:
    """Signals by full id or unique id prefix (>= 8 chars), in request order.

    Unknown ids land in not_found; a prefix shared by several signals lands in ambiguous
    with every full id it could mean.
    """
    wanted = list(dict.fromkeys(i.strip().lower() for i in ids if i and i.strip()))[:MAX_BATCH]
    full = [i for i in wanted if len(i) == 36]
    prefixes = [i for i in wanted if MIN_ID_PREFIX <= len(i) < 36 and _HEX_ID.match(i)]
    conds = []
    if full:
        conds.append(Signal.id.in_(full))
    by_len: dict[int, list[str]] = {}
    for p in prefixes:
        by_len.setdefault(len(p), []).append(p)
    for n, group in by_len.items():
        conds.append(func.substr(Signal.id, 1, n).in_(group))
    found: list[Signal] = []
    if conds:
        found = list(
            (
                await session.execute(
                    select(Signal)
                    .where(or_(*conds))
                    .options(selectinload(Signal.target), selectinload(Signal.reports))
                )
            )
            .scalars()
            .all()
        )
    items, not_found, ambiguous = [], [], {}
    for key in wanted:
        hits = [s for s in found if s.id == key] or [s for s in found if s.id.startswith(key)]
        if len(key) < MIN_ID_PREFIX or not hits:
            not_found.append(key)
        elif len(hits) > 1:
            ambiguous[key] = sorted(s.id for s in hits)
        else:
            items.append(signal_record(hits[0]))
    return {"items": items, "not_found": not_found, "ambiguous": ambiguous}


async def resolve_targets(session: AsyncSession, identifiers: list[str]) -> dict[str, Any]:
    """Resolve many identifiers exactly (not substring search) to their host stars.

    Understands TIC ids ("TIC 123", "123"), Gaia DR3 ids, star names, signal names
    ("WASP-12 b"), any source identifier ("TOI-1234.01", CTOI and eumemic keys) and TOI hosts
    ("TOI-1234"). Names are matched ignoring case, spaces, hyphens and underscores.
    """
    queries = list(dict.fromkeys(i.strip() for i in identifiers if i and i.strip()))[:MAX_BATCH]
    tic, gaia, toi, names = {}, {}, {}, {}
    for q in queries:
        if m := _GAIA.match(q):
            gaia[q] = m.group(1)
        elif _BARE_GAIA.match(q):
            gaia[q] = q
        elif m := _TIC.match(q):
            tic[q] = m.group(1)
        elif m := _TOI_HOST.match(q):
            toi[q] = m.group(1)
        names[q] = _norm(q)  # every identifier is also tried as a name

    # identifier -> (matched_by, target ids, signal ids)
    hits: dict[str, tuple[str, set[str], set[str]]] = {}

    def add(q: str, how: str, target_id: str, signal_id: str | None = None) -> None:
        current = hits.get(q)
        if current is not None and current[0] != how:
            return  # first (strongest) kind of match wins
        _, tids, sids = current or (how, set(), set())
        tids.add(target_id)
        if signal_id:
            sids.add(signal_id)
        hits[q] = (how, tids, sids)

    if tic:
        rows = await session.execute(
            select(Target.tic_id, Target.id).where(Target.tic_id.in_(set(tic.values())))
        )
        by_tic: dict[str, list[str]] = {}
        for tic_id, tid in rows.all():
            by_tic.setdefault(tic_id, []).append(tid)
        for q, digits in tic.items():
            for tid in by_tic.get(digits, []):
                add(q, "tic", tid)
    if gaia:
        values = set(gaia.values())
        rows = await session.execute(
            select(Target.gaia_dr3_id, Target.id).where(
                Target.gaia_dr3_id.in_(values | {f"Gaia DR3 {v}" for v in values})
            )
        )
        for gaia_id, tid in rows.all():
            digits = re.sub(r"\D", "", gaia_id or "")
            for q, want in gaia.items():
                if want == digits:
                    add(q, "gaia_dr3", tid)
    if toi:
        conds = [Report.source_key.like(f"TOI-{n}.%") for n in set(toi.values())]
        rows = await session.execute(
            select(Report.source_key, Report.target_id).where(
                Report.source_name == "nasa_toi", or_(*conds)
            )
        )
        for key, tid in rows.all():
            for q, n in toi.items():
                if key.startswith(f"TOI-{n}."):
                    add(q, "toi_host", tid)

    unresolved = {q: n for q, n in names.items() if q not in hits}
    if unresolved:
        values = set(unresolved.values())
        for name, tid in (
            await session.execute(
                select(Target.primary_name, Target.id).where(
                    _norm_sql(Target.primary_name).in_(values)
                )
            )
        ).all():
            for q, n in unresolved.items():
                if _norm(name) == n:
                    add(q, "name", tid)
        for name, tid, sid in (
            await session.execute(
                select(Signal.display_name, Signal.target_id, Signal.id).where(
                    _norm_sql(Signal.display_name).in_(values)
                )
            )
        ).all():
            for q, n in unresolved.items():
                if _norm(name) == n:
                    add(q, "signal_name", tid, sid)
        for key, tid, sid in (
            await session.execute(
                select(Report.source_key, Report.target_id, Report.signal_id).where(
                    _norm_sql(Report.source_key).in_(values)
                )
            )
        ).all():
            for q, n in unresolved.items():
                if _norm(key) == n:
                    add(q, "source_key", tid, sid)

    target_ids = sorted({tid for _, tids, _ in hits.values() for tid in tids})
    targets = await load_targets(session, target_ids)

    resolved = []
    for q in queries:
        if q not in hits:
            continue
        how, tids, sids = hits[q]
        resolved.append(
            {
                "query": q,
                "matched_by": how,
                "target_ids": sorted(tids),
                "signal_ids": sorted(sids),
            }
        )
    return {
        "resolved": resolved,
        "not_found": [q for q in queries if q not in hits],
        "targets": sorted(targets, key=lambda t: t["primary_name"]),
    }
