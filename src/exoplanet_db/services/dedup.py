"""Post-ingest de-duplication: one signal per physical transit signal on each star.

Sources are ingested independently, so the same signal can arrive several times (a TOI,
the CTOI that became it, a eumemic re-detection). Two signals on one star are merged when

1. an explicit cross-identifier links them (ExoFOP "Promoted to TOI"), or
2. their ephemerides agree (check_signal_match "identical_signal") or their periods agree
   to SAME_PERIOD_REL_TOL,

unless the merge would join two different entries of the same catalogue: that catalogue
already asserts they are distinct (TOI-123.01 vs .02, NASA b vs c).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.models import EphemerisFit, Report, Signal, SignalHarmonic
from exoplanet_db.services.reconciliation import SAME_PERIOD_REL_TOL, check_signal_match

logger = logging.getLogger(__name__)

# Lower is more authoritative: it names the merged signal and sets its disposition.
SOURCE_RANK = {"nasa_pscomppars": 0, "nasa_toi": 1, "exofop_ctoi": 2, "eumemic/exoplanets": 3}


# Attached to existing signals as evidence; never names, creates or separates signals
EVIDENCE_ONLY_SOURCES = {"eumemic/exoplanets:vetting"}


def source_rank(source_name: str) -> int:
    return SOURCE_RANK.get(source_name, 4)


@dataclass
class _Sig:
    id: str
    target_id: str
    name: str
    period: float
    period_err: float | None
    epoch: float
    epoch_err: float | None
    duration: float | None
    disposition: str
    keys: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    @property
    def best_rank(self) -> int:
        return min((source_rank(s) for s in self.keys), default=9)


def _compatible(a: _Sig, b: _Sig) -> bool:
    """False when one catalogue lists both under different identifiers.

    Evidence-only sources (eumemic's vetting tables) assert nothing about identity.
    """
    for source in a.keys.keys() & b.keys.keys() - EVIDENCE_ONLY_SOURCES:
        if a.keys[source] != b.keys[source]:
            return False
    return True


def _same_signal(a: _Sig, b: _Sig) -> bool:
    if a.period <= 0 or b.period <= 0:
        return False
    if abs(a.period - b.period) / max(a.period, b.period) < SAME_PERIOD_REL_TOL:
        return True
    if a.epoch <= 2400000 or b.epoch <= 2400000:
        return False
    res = check_signal_match(
        period1_d=a.period,
        period1_err=a.period_err,
        epoch1_bjd=a.epoch,
        epoch1_err=a.epoch_err,
        period2_d=b.period,
        period2_err=b.period_err,
        epoch2_bjd=b.epoch,
        epoch2_err=b.epoch_err,
        duration1_h=a.duration,
        duration2_h=b.duration,
    )
    return res.get("match_type") == "identical_signal"


async def _load(
    session: AsyncSession,
) -> tuple[dict[str, _Sig], list[tuple[str, str, str, str]]]:
    sigs: dict[str, _Sig] = {}
    rows = await session.execute(
        select(
            Signal.id,
            Signal.target_id,
            Signal.display_name,
            Signal.primary_period_d,
            Signal.primary_period_err_d,
            Signal.primary_epoch_bjd,
            Signal.primary_epoch_err_d,
            Signal.primary_duration_h,
            Signal.current_disposition,
        )
    )
    for sid, tid, name, p, pe, e, ee, d, disp in rows:
        sigs[sid] = _Sig(sid, tid, name, p or 0.0, pe, e or 0.0, ee, d, disp)

    promoted: list[tuple[str, str, str, str]] = []  # (signal, report, CTOI key, "TOI-xxxx.yy")
    reports = await session.execute(
        select(
            Report.id, Report.signal_id, Report.source_name, Report.source_key, Report.raw_payload
        )
    )
    for rid, sid, source, key, raw in reports:
        if sid not in sigs:
            continue
        sigs[sid].keys[source].add(key)
        if source == "exofop_ctoi" and isinstance(raw, dict):
            toi = str(raw.get("Promoted to TOI") or "").strip()
            if toi:
                promoted.append((sid, rid, key, f"TOI-{toi}"))
    return sigs, promoted


async def _absorb(session: AsyncSession, keep: _Sig, drop: _Sig) -> None:
    """Move everything from `drop` onto `keep`, then delete `drop`."""
    await session.execute(
        update(Report).where(Report.signal_id == drop.id).values(signal_id=keep.id)
    )
    await session.execute(
        update(EphemerisFit).where(EphemerisFit.signal_id == drop.id).values(signal_id=keep.id)
    )
    await session.execute(
        update(SignalHarmonic).where(SignalHarmonic.signal_id == drop.id).values(signal_id=keep.id)
    )
    await session.execute(
        update(SignalHarmonic)
        .where(SignalHarmonic.harmonic_signal_id == drop.id)
        .values(harmonic_signal_id=keep.id)
    )
    dropped = await session.get(Signal, drop.id)
    kept = await session.get(Signal, keep.id)
    if dropped is not None and kept is not None:
        kept.scientific_limits = list(
            dict.fromkeys((kept.scientific_limits or []) + (dropped.scientific_limits or []))
        )
    if dropped is not None:
        session.expunge(dropped)
    await session.execute(delete(Signal).where(Signal.id == drop.id))
    for source, keys in drop.keys.items():
        keep.keys[source] |= keys


async def _settle(session: AsyncSession, sig: _Sig) -> None:
    """Name and disposition of a merged signal follow its most authoritative source."""
    signal = await session.get(Signal, sig.id)
    if signal is None:
        return
    reports = list(
        (await session.execute(select(Report).where(Report.signal_id == sig.id))).scalars()
    )
    if not reports:
        return
    best = min(reports, key=lambda r: (source_rank(r.source_name), r.source_key))
    if best.source_name in ("nasa_pscomppars", "nasa_toi"):
        signal.display_name = best.source_key
    if any(r.source_name == "nasa_pscomppars" for r in reports):
        signal.current_disposition = "confirmed_planet"
    elif best.source_name == "nasa_toi" and best.reported_disposition:
        signal.current_disposition = best.reported_disposition
    fits = {
        f.report_id: f
        for f in (
            await session.execute(select(EphemerisFit).where(EphemerisFit.signal_id == sig.id))
        ).scalars()
    }
    if best.id in fits:
        signal.preferred_ephemeris_fit_id = fits[best.id].id


async def dedupe_signals(session: AsyncSession) -> dict[str, Any]:
    sigs, promoted = await _load(session)
    toi_signal = {key: sid for sid, s in sigs.items() for key in s.keys.get("nasa_toi", set())}
    merged_ids: dict[str, str] = {}  # absorbed -> survivor
    touched: set[str] = set()

    def live(sid: str) -> str:
        while sid in merged_ids:
            sid = merged_ids[sid]
        return sid

    async def merge(a_id: str, b_id: str) -> bool:
        a, b = sigs[live(a_id)], sigs[live(b_id)]
        if a.id == b.id or a.target_id != b.target_id or not _compatible(a, b):
            return False
        keep, drop = sorted((a, b), key=lambda s: (s.best_rank, s.name))
        await _absorb(session, keep, drop)
        merged_ids[drop.id] = keep.id
        touched.add(keep.id)
        return True

    explicit = refused = 0
    for ctoi_sid, ctoi_rid, ctoi_key, toi_key in promoted:
        toi_sid = toi_signal.get(toi_key)
        if toi_sid is None or live(toi_sid) == live(ctoi_sid):
            continue
        src, dst = sigs[live(ctoi_sid)], sigs[live(toi_sid)]
        if src.target_id == dst.target_id and await merge(dst.id, src.id):
            explicit += 1
            continue
        # The CTOI's signal belongs elsewhere (another host record for the star, or it also
        # carries a different TOI): the explicit identifier wins for this one report.
        await session.execute(update(Report).where(Report.id == ctoi_rid).values(signal_id=dst.id))
        await session.execute(
            update(EphemerisFit).where(EphemerisFit.report_id == ctoi_rid).values(signal_id=dst.id)
        )
        src.keys["exofop_ctoi"].discard(ctoi_key)
        dst.keys["exofop_ctoi"].add(ctoi_key)
        explicit += 1

    by_target: dict[str, list[str]] = defaultdict(list)
    for sid, s in sigs.items():
        if sid not in merged_ids and s.period > 0:
            by_target[s.target_id].append(sid)
    implicit = 0
    for ids in by_target.values():
        if len(ids) < 2:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = sigs[live(ids[i])], sigs[live(ids[j])]
                if a.id != b.id and _same_signal(a, b):
                    if await merge(a.id, b.id):
                        implicit += 1
                    else:
                        refused += 1

    for sid in touched:
        if sid not in merged_ids:
            await _settle(session, sigs[sid])
    await session.commit()
    result = {
        "merged_by_identifier": explicit,
        "merged_by_ephemeris": implicit,
        "kept_apart_same_catalogue": refused,
    }
    logger.info("Signal de-duplication: %s", result)
    return result
