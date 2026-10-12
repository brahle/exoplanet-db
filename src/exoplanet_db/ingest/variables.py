"""Loads eclipsing-binary and variable-star catalogues from VizieR into variable_stars.

TESS EBs (4.6k), Gaia DR3 eclipsing binaries (2.2M) and every VSX entry with a period
(~4.8M). Only the columns contamination checks need are fetched, in declination bands so
no single TAP response is large. The catalogues change rarely (VSX monthly, the others are
fixed releases), so a catalogue loaded within REFRESH_DAYS is skipped unless forced.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from exoplanet_db.models import PublishedSnapshot, VariableStar

logger = logging.getLogger(__name__)

TAP_URL = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
REFRESH_DAYS = 30
BAND_DEG = 5.0
COLUMNS = (
    "catalogue",
    "source_id",
    "name",
    "ra_deg",
    "dec_deg",
    "period_d",
    "var_type",
    "mag",
    "mag_band",
    "epoch_bjd",
)


def _f(value: str | None) -> float | None:
    try:
        v = float(value) if value not in (None, "") else None
    except ValueError:
        return None
    return v if v is not None and v == v else None  # drop NaN


def _s(value: str | None, size: int) -> str | None:
    v = (value or "").strip()
    return v[:size] or None


def _tess_eb(r: dict[str, str]) -> tuple | None:
    period, bjd0 = _f(r.get("Per")), _f(r.get("BJD0"))
    if not period or period <= 0:
        return None
    return (
        "tess_eb",
        r["TIC"].strip(),
        f"TIC {r['TIC'].strip()}",
        _f(r["RAJ2000"]),
        _f(r["DEJ2000"]),
        period,
        "EB",
        _f(r.get("Tmag")),
        "T",
        bjd0 + 2457000.0 if bjd0 is not None else None,
    )


def _gaia_eb(r: dict[str, str]) -> tuple | None:
    freq = _f(r.get("Freq"))
    if not freq or freq <= 0:
        return None
    return (
        "gaia_dr3_eb",
        r["Source"].strip(),
        f"Gaia DR3 {r['Source'].strip()}",
        _f(r["RA_ICRS"]),
        _f(r["DE_ICRS"]),
        1.0 / freq,  # orbital frequency of the two-Gaussian model
        "EB",
        _f(r.get("magModRef")),
        "G",
        None,  # TimeRef is on Gaia's own time scale; not converted
    )


def _vsx(r: dict[str, str]) -> tuple | None:
    period = _f(r.get("Period"))
    if not period or period <= 0:
        return None
    epoch = _f(r.get("Epoch"))
    return (
        "vsx",
        r["OID"].strip(),
        _s(r.get("Name"), 128),
        _f(r["RAJ2000"]),
        _f(r["DEJ2000"]),
        period,
        _s(r.get("Type"), 32),
        _f(r.get("max")),
        _s(r.get("n_max"), 8),
        epoch if epoch and epoch > 2400000 else None,
    )


# catalogue -> (VizieR table, columns, declination column, row parser, banded)
SPECS: dict[str, tuple[str, str, str, Any, bool]] = {
    "tess_eb": (
        "J/ApJS/258/16/tess-ebs",
        "TIC, RAJ2000, DEJ2000, Tmag, BJD0, Per",
        "DEJ2000",
        _tess_eb,
        False,
    ),
    "gaia_dr3_eb": (
        "I/358/veb",
        "Source, RA_ICRS, DE_ICRS, Freq, magModRef",
        "DE_ICRS",
        _gaia_eb,
        True,
    ),
    "vsx": (
        "B/vsx/vsx",
        'OID, Name, RAJ2000, DEJ2000, Type, Period, "max", n_max, Epoch',
        "DEJ2000",
        _vsx,
        True,
    ),
}


def _bands(banded: bool) -> list[tuple[float, float] | None]:
    if not banded:
        return [None]
    edges = [-90.0 + i * BAND_DEG for i in range(int(180 / BAND_DEG) + 1)]
    return list(zip(edges[:-1], edges[1:], strict=True))


CHUNK_ROWS = 50_000


async def _fetch(client: httpx.AsyncClient, query: str):
    """Stream one TAP CSV response as dict rows (a dense band can hold a million rows)."""
    async with client.stream(
        "POST",
        TAP_URL,
        data={
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "FORMAT": "csv",
            "MAXREC": "3000000",
            "QUERY": query,
        },
    ) as resp:
        resp.raise_for_status()
        header: list[str] | None = None
        async for line in resp.aiter_lines():
            if not line:
                continue
            values = next(csv.reader([line]))
            if header is None:
                header = values
                continue
            yield dict(zip(header, values, strict=False))


async def _insert(session: AsyncSession, rows: list[tuple]) -> None:
    if not rows:
        return
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        raw = await conn.get_raw_connection()
        await raw.driver_connection.copy_records_to_table(
            "variable_stars", records=rows, columns=list(COLUMNS)
        )
    else:
        await session.execute(insert(VariableStar), [dict(zip(COLUMNS, r)) for r in rows])


async def _load_band(session: AsyncSession, client: httpx.AsyncClient, query: str, parse) -> int:
    """Insert one band in chunks; a failed attempt rolls back to a savepoint and retries."""
    for attempt in range(3):
        savepoint = await session.begin_nested()
        count, chunk = 0, []
        try:
            async for raw in _fetch(client, query):
                row = parse(raw)
                if row is None or row[3] is None or row[4] is None:
                    continue
                chunk.append(row)
                if len(chunk) >= CHUNK_ROWS:
                    await _insert(session, chunk)
                    count, chunk = count + len(chunk), []
            await _insert(session, chunk)
            await savepoint.commit()
            return count + len(chunk)
        except httpx.HTTPError:
            await savepoint.rollback()
            if attempt == 2:
                raise
            logger.warning("VizieR query failed, retrying: %s", query[:120])
    return 0


async def load_catalogue(
    session: AsyncSession, catalogue: str, client: httpx.AsyncClient | None = None
) -> int:
    """Replace one catalogue's rows in a single transaction (readers see old or new, never half)."""
    table, cols, dec_col, parse, banded = SPECS[catalogue]
    own = client is None
    client = client or httpx.AsyncClient(timeout=600.0)
    total = 0
    try:
        await session.execute(delete(VariableStar).where(VariableStar.catalogue == catalogue))
        for band in _bands(banded):
            where = f" WHERE {dec_col} >= {band[0]} AND {dec_col} < {band[1]}" if band else ""
            if band and band[1] >= 90.0:
                where = f" WHERE {dec_col} >= {band[0]}"
            total += await _load_band(
                session, client, f'SELECT {cols} FROM "{table}"{where}', parse
            )
        if total == 0:
            raise ValueError(f"{catalogue}: VizieR returned no rows")
        now = datetime.now(UTC)
        session.add(
            PublishedSnapshot(
                source_name=f"variables:{catalogue}",
                snapshot_utc=now,
                record_count=total,
                status="validated",
                validation_metrics={"vizier_table": table},
                published_at=now,
            )
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        if own:
            await client.aclose()
    return total


async def refresh_catalogues(session: AsyncSession, force: bool = False) -> dict[str, Any]:
    """Load each catalogue that is missing or older than REFRESH_DAYS (all of them if force)."""
    out: dict[str, Any] = {}
    cutoff = datetime.now(UTC) - timedelta(days=REFRESH_DAYS)
    async with httpx.AsyncClient(timeout=600.0) as client:
        for catalogue in SPECS:
            last = (
                await session.execute(
                    select(func.max(PublishedSnapshot.snapshot_utc)).where(
                        PublishedSnapshot.source_name == f"variables:{catalogue}"
                    )
                )
            ).scalar()
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            present = (
                await session.execute(
                    select(VariableStar.id).where(VariableStar.catalogue == catalogue).limit(1)
                )
            ).first()
            if not force and present and last and last > cutoff:
                out[catalogue] = f"fresh ({last.date()})"
                continue
            out[catalogue] = await load_catalogue(session, catalogue, client)
    return out
