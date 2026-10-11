from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.ingest.common import signal_for_existing_report, upsert_fit, upsert_report
from exoplanet_db.models import (
    EphemerisFit,
    PublishedSnapshot,
    Report,
    Signal,
    Target,
)
from exoplanet_db.services.reconciliation import match_signal_to_existing

logger = logging.getLogger(__name__)

EUMEMIC_DATA_ATTRIBUTION = (
    "Results, tables, and figures from eumemic/exoplanets licensed under CC BY 4.0 "
    "(Creative Commons Attribution 4.0 International). Source: https://github.com/eumemic/exoplanets"
)


class EumemicIngest:
    """
    Community adapter for eumemic/exoplanets catalog.
    Ingests candidate tables, ephemerides, predictions, and vetting tiers (A, B, N, L1, C).
    Enforces CC BY 4.0 attribution and normalises BJD_TDB to BTJD (BJD - 2457000).
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self._touched: set[str] = set()
        self._best: dict[str, str] = {}

    async def _settle_disposition(self, signal: Signal, disposition: str) -> None:
        """eumemic sets the disposition only of signals no NASA or ExoFOP table reports.

        It outranks our own unvetted dossiers (their investigation is the reason to ingest
        discarded signals at all). Within one run the most favourable eumemic verdict wins, so a
        signal counted in one release and dropped in another stays a candidate.
        """
        best = self._best.get(signal.id)
        if best is not None and DISPOSITION_ORDER[best] <= DISPOSITION_ORDER[disposition]:
            return
        self._best[signal.id] = disposition
        stronger = (
            await self.session.execute(
                select(Report.id)
                .where(
                    Report.signal_id == signal.id,
                    Report.source_name.in_(("nasa_pscomppars", "nasa_toi", "exofop_ctoi")),
                )
                .limit(1)
            )
        ).first()
        if stronger is None:
            signal.current_disposition = disposition

    def parse_candidates_csv(self, csv_content: str) -> list[dict[str, Any]]:
        """Parses eumemic candidates.csv into typed dictionaries."""
        reader = csv.DictReader(io.StringIO(csv_content.strip()))
        rows = []
        for r in reader:
            # Handle possible key naming variations across candidates.csv and candidates_allsky.csv
            tic = (r.get("tic") or r.get("TIC") or r.get("tic_id") or r.get("tid") or "").strip()
            period_str = r.get("period") or r.get("P_d") or r.get("pl_orbper") or r.get("period_d")
            t0_str = (
                r.get("t0")
                or r.get("T0_BJD")
                or r.get("t0_bjd")
                or r.get("pl_tranmid")
                or r.get("epoch")
            )
            tier = (r.get("tier") or r.get("Tier") or "").strip()
            status = r.get("status") or r.get("disposition") or "planet_candidate"
            depth_str = r.get("depth") or r.get("depth_ppm") or r.get("pl_trandep")
            dur_str = (
                r.get("duration") or r.get("T14_h") or r.get("duration_h") or r.get("pl_trandur")
            )
            ra_str = r.get("ra") or r.get("ra_deg")
            dec_str = r.get("dec") or r.get("dec_deg")
            p_err_val = r.get("P_err") or r.get("period_err")
            t0_err_val = r.get("t0_err") or r.get("T0_err")
            label = (r.get("label") or r.get("candidate_id") or "").strip()

            if not tic or not period_str or not t0_str:
                continue

            try:
                period = float(period_str)
                t0_raw = float(t0_str)
                # Normalise to BTJD and BJD_TDB
                if t0_raw > 2400000:
                    bjd_tdb = t0_raw
                    btjd = t0_raw - 2457000.0
                else:
                    btjd = t0_raw
                    bjd_tdb = t0_raw + 2457000.0

                rows.append(
                    {
                        "tic": tic,
                        # Stable across releases; mirrors eumemic's candidates/TIC<id>_P<period> naming
                        "source_key": f"TIC{tic}_P{period:.3f}",
                        "candidate_id": label or f"TIC {tic} ({period:.3f} d)",
                        "period_d": period,
                        "period_err_d": float(p_err_val) if p_err_val else None,
                        "t0_btjd": btjd,
                        "t0_bjd_tdb": bjd_tdb,
                        "t0_err_d": float(t0_err_val) if t0_err_val else None,
                        "depth_ppm": float(depth_str) if depth_str else None,
                        "duration_h": float(dur_str) if dur_str else None,
                        "tier": tier.upper(),
                        "status": status,
                        "ra_deg": float(ra_str) if ra_str else None,
                        "dec_deg": float(dec_str) if dec_str else None,
                        "raw": dict(r),
                    }
                )
            except (ValueError, TypeError):
                continue
        return rows

    async def ingest_candidates(
        self,
        records: list[dict[str, Any]],
        commit_sha: str = "HEAD",
        source_file: str = "results_public/candidates.csv",
        record_snapshot: bool = True,
    ) -> int:
        """Ingests candidates parsed from eumemic/exoplanets (one file's worth of records)."""
        now = datetime.now(UTC)
        count = 0

        for r in records:
            tic = str(r["tic"])
            target_name = f"TIC {tic}"

            target_stmt = select(Target).where(
                (Target.tic_id == tic) | (Target.primary_name == target_name)
            )
            res = await self.session.execute(target_stmt)
            target = res.scalars().first()

            if not target:
                target = Target(
                    primary_name=target_name,
                    tic_id=tic,
                    # eumemic publishes no coordinates; enrich_tic_targets fills them from TIC 8.2
                    ra_deg=r["ra_deg"] or 0.0,
                    dec_deg=r["dec_deg"] or 0.0,
                    teff_k=_num(r["raw"].get("Teff")),
                    radius_rsun=_num(r["raw"].get("Rstar")),
                    tess_mag=_num(r["raw"].get("Tmag")),
                    aliases=[f"TIC {tic}"],
                )
                self.session.add(target)
                await self.session.flush()

            # Signal reconciliation
            signals_stmt = select(Signal).where(Signal.target_id == target.id)
            existing_signals = list((await self.session.execute(signals_stmt)).scalars().all())

            matched_signal = await signal_for_existing_report(
                self.session, "eumemic/exoplanets", r["source_key"]
            ) or match_signal_to_existing(
                existing_signals=existing_signals,
                cand_period=r["period_d"],
                cand_epoch=r["t0_bjd_tdb"],
                period_err=r["period_err_d"],
                epoch_err=r["t0_err_d"],
                duration_h=r["duration_h"],
            )

            tier_disposition, verdict = tier_verdict(r["tier"], r["raw"].get("c_reason"))
            if r["tier"] in COUNTED_TIERS:
                limits = [
                    f"eumemic/exoplanets tier {r['tier']} candidate ({source_file})",
                    "TESS-only vetting; ground-based follow-up needed to confirm or reject.",
                ]
            else:
                limits = [f"eumemic/exoplanets: {verdict} ({source_file})"]

            if matched_signal:
                signal = matched_signal
                previous = signal.scientific_limits or []
                if signal.id not in self._touched:
                    # Drop the previous run's eumemic verdicts, so a re-tiered signal shows the
                    # current one only
                    previous = [x for x in previous if not x.startswith("eumemic/exoplanets")]
                signal.scientific_limits = list(dict.fromkeys(previous + limits))
                await self._settle_disposition(signal, tier_disposition)
            else:
                signal = Signal(
                    target_id=target.id,
                    display_name=r["candidate_id"],
                    primary_period_d=r["period_d"],
                    primary_period_err_d=r["period_err_d"],
                    primary_epoch_bjd=r["t0_bjd_tdb"],
                    primary_duration_h=r["duration_h"],
                    primary_depth_ppm=r["depth_ppm"],
                    current_disposition=tier_disposition,
                    source_localisation_status="unresolved",
                    timing_consistency_status="unresolved",
                    scientific_limits=limits,
                )
                self.session.add(signal)
                await self.session.flush()
                self._best[signal.id] = tier_disposition
            self._touched.add(signal.id)

            # Report
            report = Report(
                target_id=target.id,
                signal_id=signal.id,
                source_name="eumemic/exoplanets",
                source_key=r["source_key"],
                retrieval_utc=now,
                commit_sha=commit_sha,
                author_or_team="eumemic",
                bibcode_or_url=f"https://github.com/eumemic/exoplanets/blob/{commit_sha}/{source_file}",
                reported_disposition=tier_disposition,
                period_d=r["period_d"],
                period_err_d=r["period_err_d"],
                epoch_bjd=r["t0_bjd_tdb"],
                epoch_err_d=r["t0_err_d"],
                duration_h=r["duration_h"],
                depth_ppm=r["depth_ppm"],
                # The tier comes from tiers_earlier_candidates.csv for earlier releases
                raw_payload={**r["raw"], "tier": r["tier"]},
                license="CC BY 4.0",
            )
            report = await upsert_report(self.session, report)

            # EphemerisFit
            ephem_fit = EphemerisFit(
                report_id=report.id,
                signal_id=signal.id,
                period_d=r["period_d"],
                period_err_d=r["period_err_d"],
                epoch_bjd_tdb=r["t0_bjd_tdb"],
                epoch_err_d=r["t0_err_d"],
                duration_h=r["duration_h"],
                depth_ppm=r["depth_ppm"],
                time_scale="TDB",
                fit_model=f"eumemic_tier_{r['tier']}",
                is_coherent=True,
            )
            ephem_fit = await upsert_fit(self.session, ephem_fit)
            count += 1

        if record_snapshot:
            self.session.add(
                PublishedSnapshot(
                    source_name="eumemic/exoplanets",
                    snapshot_utc=now,
                    commit_sha=commit_sha,
                    record_count=count,
                    status="validated",
                    validation_metrics={
                        "source_file": source_file,
                        "ingested_candidates": count,
                        "license": "CC BY 4.0",
                    },
                    published_at=now,
                )
            )
        await self.session.commit()
        return count


# Tiers A, B, N (crowded field) and L1 (low SNR) are eumemic's counted planet candidates.
COUNTED_TIERS = {"A", "B", "N", "L1"}
PUBLISHED_TIERS = COUNTED_TIERS  # kept for callers of the old name
# Everything else eumemic documents but does not count: (disposition, what it means)
DISCARDED_TIERS = {
    "C": ("unclassified_transit_like_signal", "tier C, listed but not counted"),
    "L": (
        "unclassified_transit_like_signal",
        "tier L, passes the false-alarm classifier but not the binary, grazing, literature "
        "and TRICERATOPS checks; not counted",
    ),
    "LIKELY EB": ("false_positive", "likely eclipsing binary; not counted"),
    "LIKELY NEARBY FP": (
        "false_positive",
        "likely false positive from a nearby star (centroid offset or a neighbour that can "
        "produce the dip); not counted",
    ),
    "LIKELY FALSE ALARM": ("false_positive", "likely false alarm (systematics); not counted"),
    "REPORTED": (
        "unclassified_transit_like_signal",
        "already reported elsewhere (literature or Zenodo); not counted",
    ),
    "UNTIERED": (
        "unclassified_transit_like_signal",
        "listed in an earlier release but not tiered (passes everything except the check for a "
        "TIC star within 15\u2033 that could produce the dip); not counted",
    ),
}
DISPOSITION_ORDER = {
    "planet_candidate": 0,
    "unclassified_transit_like_signal": 1,
    "false_positive": 2,
}


def tier_verdict(tier: str, reason: str | None = None) -> tuple[str, str]:
    """(disposition, human-readable verdict) for an eumemic tier."""
    if tier in COUNTED_TIERS:
        return "planet_candidate", f"tier {tier} candidate"
    disposition, verdict = DISCARDED_TIERS.get(
        tier, ("unclassified_transit_like_signal", f"tier {tier}; not counted")
    )
    reason = (reason or "").strip()
    return disposition, f"{verdict} ({reason})" if reason else verdict


EUMEMIC_REPO = "eumemic/exoplanets"
ALLSKY_FILE = "results_public/candidates_allsky.csv"
EARLIER_TIERS_FILE = "results_public/tiers_earlier_candidates.csv"


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _earlier_tier(tiers: list[dict[str, str]], tic: str, period: float) -> str | None:
    for t in tiers:
        p = _num(t.get("P_d"))
        if t.get("TIC", "").strip() == tic and p and abs(p - period) / period < 2e-4:
            return (t.get("tier") or "").strip().upper()
    return None


async def fetch_eumemic_candidates(sha: str) -> list[tuple[str, list[dict[str, Any]]]]:
    """The published candidate list at commit `sha`, as (source_file, records) pairs.

    Every documented row, counted or not: discarded signals carry eumemic's verdict.
    - candidates_allsky.csv: the full-sky release with its tier for every row (A, B, N, L1
      counted; C, L, likely EB / nearby FP / false alarm, reported elsewhere not).
    - candidates.csv and candidates_<date>.csv: earlier releases, re-tiered in
      tiers_earlier_candidates.csv; rows it does not list are "untiered".
    """
    import httpx

    parser = EumemicIngest(session=None)  # type: ignore[arg-type]
    raw_base = f"https://raw.githubusercontent.com/{EUMEMIC_REPO}/{sha}/"
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        listing = await client.get(
            f"https://api.github.com/repos/{EUMEMIC_REPO}/contents/results_public",
            params={"ref": sha},
        )
        listing.raise_for_status()
        names = [e["name"] for e in listing.json() if e.get("type") == "file"]
        earlier_files = sorted(
            f"results_public/{n}"
            for n in names
            if n.startswith("candidates") and n.endswith(".csv") and n != "candidates_allsky.csv"
        )

        async def get(path: str) -> str:
            resp = await client.get(raw_base + path)
            resp.raise_for_status()
            return resp.text

        out: list[tuple[str, list[dict[str, Any]]]] = []
        allsky_all = parser.parse_candidates_csv(await get(ALLSKY_FILE))
        if not allsky_all:
            raise ValueError(f"{ALLSKY_FILE} at {sha} parsed to 0 rows")
        for r in allsky_all:
            r["tier"] = r["tier"] or "UNTIERED"
        out.append((ALLSKY_FILE, allsky_all))

        tiers = list(csv.DictReader(io.StringIO((await get(EARLIER_TIERS_FILE)).strip())))
        for path in earlier_files:
            kept = []
            for r in parser.parse_candidates_csv(await get(path)):
                r["tier"] = _earlier_tier(tiers, r["tic"], r["period_d"]) or "UNTIERED"
                kept.append(r)
            out.append((path, kept))
    return out


async def fetch_and_ingest_eumemic(session: AsyncSession) -> int:
    """Ingests the published eumemic/exoplanets candidates, pinned to the current commit."""
    from exoplanet_db.ingest.common import github_head_sha

    sha = await github_head_sha(EUMEMIC_REPO)
    ingest = EumemicIngest(session)
    total = 0
    seen: set[str] = set()
    per_file: dict[str, int] = {}
    for source_file, records in await fetch_eumemic_candidates(sha):
        fresh = [r for r in records if r["source_key"] not in seen]
        seen.update(r["source_key"] for r in fresh)
        n = await ingest.ingest_candidates(
            fresh, commit_sha=sha, source_file=source_file, record_snapshot=False
        )
        per_file[source_file] = n
        total += n
    now = datetime.now(UTC)
    session.add(
        PublishedSnapshot(
            source_name="eumemic/exoplanets",
            snapshot_utc=now,
            commit_sha=sha,
            record_count=total,
            status="validated",
            validation_metrics={"files": per_file, "license": "CC BY 4.0"},
            published_at=now,
        )
    )
    await session.commit()
    logger.info("eumemic/exoplanets @ %s: %d documented signals", sha[:8], total)
    return total


VETTING_SOURCE = "eumemic/exoplanets:vetting"
VETTING_PREFIX = "results_public/vetted_signals"


def parse_vetting_rows(csv_text: str, stem: str) -> list[dict[str, Any]]:
    """Rows of a vetted_signals*.csv: every signal eumemic's search put through LEO-Vetter."""
    rows = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        tic = (r.get("tic") or "").strip()
        period, t0 = _num(r.get("period")), _num(r.get("t0"))
        if not tic or not period or period <= 0 or t0 is None:
            continue
        fails = [f.strip() for f in (r.get("fails") or "").split(";") if f.strip()]
        rows.append(
            {
                "tic": tic,
                "period_d": period,
                "epoch_bjd": t0 + 2457000.0 if t0 < 2400000 else t0,  # BTJD -> BJD_TDB
                "duration_h": _num(r.get("duration_h")),
                "depth_ppm": _num(r.get("depth_ppm")),
                "fails": fails,
                "source_key": f"{stem}:TIC{tic}_P{period:.4f}",
                "raw": {
                    "file": stem,
                    "fails": fails,
                    "n_fail": len(fails),
                    **{
                        k: r.get(k)
                        for k in (
                            "rank",
                            "MES",
                            "snr",
                            "rp",
                            "method",
                            "known_status",
                            "known_detail",
                        )
                    },
                },
            }
        )
    return rows


async def attach_vetting_evidence(session: AsyncSession) -> dict[str, int]:
    """Attach eumemic's per-signal vetting results to signals already in the catalogue.

    The vetted_signals tables list ~18k automated detections, most of them noise, so they never
    create signals: a row only becomes a report on an existing signal of the same star and
    period, recording that eumemic examined it and which LEO-Vetter tests it failed. Runs after
    de-duplication so evidence lands on the merged signals.
    """
    import gzip

    import httpx

    from exoplanet_db.ingest.common import github_head_sha

    sha = await github_head_sha(EUMEMIC_REPO)
    raw_base = f"https://raw.githubusercontent.com/{EUMEMIC_REPO}/{sha}/"
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        listing = await client.get(
            f"https://api.github.com/repos/{EUMEMIC_REPO}/contents/results_public",
            params={"ref": sha},
        )
        listing.raise_for_status()
        paths = sorted(
            f"results_public/{e['name']}"
            for e in listing.json()
            if e.get("type") == "file"
            and f"results_public/{e['name']}".startswith(VETTING_PREFIX)
            and e["name"].endswith(".csv.gz")
        )
        rows: list[dict[str, Any]] = []
        for path in paths:
            resp = await client.get(raw_base + path)
            resp.raise_for_status()
            stem = path.rsplit("/", 1)[1].removesuffix(".csv.gz")
            rows.extend(parse_vetting_rows(gzip.decompress(resp.content).decode(), stem))
    if not rows:
        raise ValueError(f"no vetted_signals rows at {sha}")
    return await attach_vetting_rows(session, rows, sha, [p.rsplit("/", 1)[1] for p in paths])


async def attach_vetting_rows(
    session: AsyncSession, rows: list[dict[str, Any]], sha: str, files: list[str]
) -> dict[str, int]:
    tics = sorted({r["tic"] for r in rows})
    targets: dict[str, str] = {}
    for i in range(0, len(tics), 1000):
        for tic_id, tid in (
            await session.execute(
                select(Target.tic_id, Target.id).where(Target.tic_id.in_(tics[i : i + 1000]))
            )
        ).all():
            targets.setdefault(tic_id, tid)
    signals_by_target: dict[str, list[Signal]] = {}
    target_ids = sorted(set(targets.values()))
    for i in range(0, len(target_ids), 1000):
        for sig in (
            await session.execute(
                select(Signal).where(Signal.target_id.in_(target_ids[i : i + 1000]))
            )
        ).scalars():
            signals_by_target.setdefault(sig.target_id, []).append(sig)

    now = datetime.now(UTC)
    attached = failed = 0
    for r in rows:
        tid = targets.get(r["tic"])
        if tid is None:
            continue
        signal = match_signal_to_existing(
            existing_signals=signals_by_target.get(tid, []),
            cand_period=r["period_d"],
            cand_epoch=r["epoch_bjd"],
            duration_h=r["duration_h"],
        )
        if signal is None:
            continue
        await upsert_report(
            session,
            Report(
                target_id=tid,
                signal_id=signal.id,
                source_name=VETTING_SOURCE,
                source_key=r["source_key"],
                retrieval_utc=now,
                commit_sha=sha,
                author_or_team="eumemic",
                bibcode_or_url=f"https://github.com/{EUMEMIC_REPO}/blob/{sha}/results_public/{r['raw']['file']}.csv.gz",
                reported_disposition="unclassified_transit_like_signal",
                period_d=r["period_d"],
                epoch_bjd=r["epoch_bjd"],
                duration_h=r["duration_h"],
                depth_ppm=r["depth_ppm"],
                raw_payload=r["raw"],
                license="CC BY 4.0",
            ),
        )
        if signal.current_disposition not in ("confirmed_planet", "validated_planet"):
            verdict = (
                "failed " + "; ".join(r["fails"])
                if r["fails"]
                else "passed, but not promoted to a candidate"
            )
            line = f"eumemic/exoplanets automated vetting ({r['raw']['file']}): {verdict}"
            if line not in (signal.scientific_limits or []):
                signal.scientific_limits = [*(signal.scientific_limits or []), line]
        attached += 1
        failed += bool(r["fails"])
    session.add(
        PublishedSnapshot(
            source_name=VETTING_SOURCE,
            snapshot_utc=now,
            commit_sha=sha,
            record_count=attached,
            status="validated",
            validation_metrics={
                "files": files,
                "rows": len(rows),
                "attached": attached,
                "attached_with_failed_tests": failed,
                "license": "CC BY 4.0",
            },
            published_at=now,
        )
    )
    await session.commit()
    return {"rows": len(rows), "attached": attached, "with_failed_tests": failed}
