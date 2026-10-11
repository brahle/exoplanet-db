"""Token-efficient ("compact") renderings of MCP results.

Lists become one table ({"cols": [...], "rows": [[...]]}) so keys are written once, ids are
shortened to a 12-character prefix (every tool accepts it back), codes replace long enum
values (decoded by a "legend" that lists only the codes present), floats are rounded to the
precision that matters, and nulls stay as null so columns line up.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

ID_PREFIX = 12

DISPOSITION_CODES = {
    "confirmed_planet": "CP",
    "validated_planet": "VP",
    "planet_candidate": "PC",
    "unclassified_transit_like_signal": "TLS",
    "known_signal": "KS",
    "false_positive": "FP",
}
STATUS_CODES = {"supported": "S", "contradicted": "C", "unresolved": "U"}
SOURCE_CODES = {
    "nasa_pscomppars": "ps",
    "nasa_toi": "toi",
    "exofop_ctoi": "ctoi",
    "eumemic/exoplanets": "eum",
    "eumemic/exoplanets:vetting": "eumv",
    "git:brahle/exoplanet-candidates": "xc",
}
MATCH_CODES = {
    "period_match": "P",
    "harmonic_alias": "H",
    "same_star_period_unknown": "S?",
}


class Legend:
    """Collects the codes actually used so the legend stays as short as the result."""

    def __init__(self) -> None:
        self.used: dict[str, dict[str, str]] = {}

    def code(self, group: str, table: dict[str, str], value: str | None) -> str | None:
        if value is None:
            return None
        code = table.get(value, value)
        if code != value:
            self.used.setdefault(group, {})[code] = value
        return code

    def disp(self, value: str | None) -> str | None:
        return self.code("disp", DISPOSITION_CODES, value)

    def status(self, value: str | None) -> str | None:
        return self.code("status", STATUS_CODES, value)

    def sources(self, values: list[str] | None) -> str:
        return "+".join(self.code("src", SOURCE_CODES, v) or "" for v in values or [])

    def match(self, value: str | None) -> str | None:
        return self.code("match", MATCH_CODES, value)

    def render(self) -> dict[str, dict[str, str]]:
        return {k: dict(sorted(v.items())) for k, v in sorted(self.used.items())}


def sid(value: str | None) -> str | None:
    return value[:ID_PREFIX] if value else value


def rnd(value: float | None, digits: int) -> float | int | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    r = round(float(value), digits)
    return int(r) if digits <= 0 else r


def sig(value: float | None, figures: int = 2) -> float | None:
    """Round to significant figures (uncertainties)."""
    if value is None or value == 0 or (isinstance(value, float) and math.isnan(value)):
        return value
    return float(f"{value:.{figures}g}")


def minute(value: datetime | str | None) -> str | None:
    """ISO UTC to the minute: 2026-10-11T21:03Z."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.strftime("%Y-%m-%dT%H:%MZ")


def table(cols: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"cols": cols, "rows": rows}


SIGNAL_COLS = [
    "id",
    "name",
    "host",
    "tic",
    "ra",
    "dec",
    "P_d",
    "P_err",
    "T0_bjd",
    "T0_err",
    "dur_h",
    "depth_ppm",
    "disp",
    "score",
    "loc",
    "tim",
    "src",
]


def signal_row(s: dict[str, Any], legend: Legend) -> list[Any]:
    return [
        sid(s["id"]),
        s.get("display_name"),
        s.get("target_name"),
        s.get("tic_id"),
        rnd(s.get("ra_deg"), 5),
        rnd(s.get("dec_deg"), 5),
        s.get("period_d"),
        sig(s.get("period_err_d")),
        rnd(s.get("epoch_bjd"), 5),
        sig(s.get("epoch_err_d")),
        rnd(s.get("duration_h"), 2),
        rnd(s.get("depth_ppm"), 0),
        legend.disp(s.get("disposition")),
        s.get("planet_score"),
        legend.status(s.get("localisation")),
        legend.status(s.get("timing")),
        legend.sources(s.get("sources")),
    ]


def signals(records: list[dict[str, Any]], legend: Legend) -> dict[str, Any]:
    return table(SIGNAL_COLS, [signal_row(s, legend) for s in records])


def targets_and_signals(targets: list[dict[str, Any]], legend: Legend) -> dict[str, Any]:
    """Two tables: hosts, and their signals keyed by host id."""
    host_rows, signal_rows = [], []
    for t in targets:
        host_rows.append(
            [
                sid(t["id"]),
                t["primary_name"],
                t.get("tic_id"),
                t.get("gaia_dr3_id"),
                rnd(t.get("ra_deg"), 5),
                rnd(t.get("dec_deg"), 5),
                rnd(t.get("tess_mag"), 2),
                rnd(t.get("teff_k"), 0),
                rnd(t.get("radius_rsun"), 3),
                rnd(t.get("mass_msun"), 3),
            ]
        )
        for s in t.get("signals", []):
            row = signal_row(s, legend)
            # host columns (name, tic, ra, dec) live on the host row
            signal_rows.append([sid(t["id"]), row[0], row[1], *row[6:]])
    return {
        "targets": table(
            ["id", "name", "tic", "gaia_dr3", "ra", "dec", "tmag", "teff_k", "r_sun", "m_sun"],
            host_rows,
        ),
        "signals": table(["target", "id", "name", *SIGNAL_COLS[6:]], signal_rows),
    }


def near_matches(matches: list[dict[str, Any]], legend: Legend) -> list[list[Any]]:
    return [
        [
            sid(m["candidate_id"]),
            m["display_name"],
            m.get("tic_id"),
            m["angular_separation_arcsec"],
            m.get("catalog_period_d"),
            m.get("period_ratio"),
            legend.match(m["match_reason"]),
            legend.disp(m.get("disposition")),
            m.get("planet_score"),
            legend.sources(m.get("sources")),
        ]
        for m in matches
    ]


NEAR_MATCH_COLS = [
    "id",
    "name",
    "tic",
    "sep_arcsec",
    "P_d",
    "ratio",
    "match",
    "disp",
    "score",
    "src",
]


def checked_sources(sources: list[dict[str, Any]]) -> dict[str, Any]:
    return table(
        ["source", "signals", "last_ingested", "commit"],
        [
            [
                s["source_name"],
                s.get("signals"),
                (s.get("last_ingested_utc") or "")[:10] or None,
                (s.get("commit_sha") or "")[:12] or None,
            ]
            for s in sources
        ],
    )


QUEUE_COLS = [
    "id",
    "name",
    "host",
    "mid",
    "win_start",
    "win_end",
    "dur_h",
    "depth_ppm",
    "sigma_min",
    "alt_in",
    "alt_mid",
    "alt_eg",
    "sun",
    "moon_sep",
    "prio",
    "score",
    "disp",
    "src",
]


def queue(items: list[dict[str, Any]], legend: Legend) -> dict[str, Any]:
    return table(
        QUEUE_COLS,
        [
            [
                sid(it["signal_id"]),
                it["signal_name"],
                it["target_name"],
                minute(it["transit_mid_utc"]),
                minute(it.get("window_start_utc")),
                minute(it.get("window_end_utc")),
                rnd(it.get("duration_h"), 2),
                rnd(it.get("depth_ppm"), 0),
                rnd(it.get("timing_uncertainty_min"), 1),
                rnd(it.get("altitude_ingress_deg"), 0),
                rnd(it.get("target_altitude_deg"), 0),
                rnd(it.get("altitude_egress_deg"), 0),
                rnd(it.get("sun_altitude_deg"), 0),
                rnd(it.get("moon_separation_deg"), 0),
                rnd(it.get("priority_score"), 2),
                it.get("planet_score"),
                legend.disp(it.get("current_disposition")),
                legend.sources(it.get("source_names")),
            ]
            for it in items
        ],
    )
