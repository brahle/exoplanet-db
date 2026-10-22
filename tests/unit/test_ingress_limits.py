"""Edge limits in deploy/k8s/base/ingressroute.yaml: tighter per-IP budgets for the costly routes."""

from __future__ import annotations

from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parents[2] / "deploy" / "k8s" / "base" / "ingressroute.yaml"
HOSTS = (
    "Host(`exoplanets.brahle.com`) || Host(`exoplanet.brahle.com`) || "
    "Host(`exoplanets.marica.brahle.com`) || Host(`exoplanet.marica.brahle.com`) || "
    "Host(`exoplanet-api.marica.brahle.com`)"
)


def docs():
    return list(yaml.safe_load_all(MANIFEST.read_text()))


def middlewares():
    return {d["metadata"]["name"]: d["spec"] for d in docs() if d["kind"] == "Middleware"}


def routes():
    (route,) = [d for d in docs() if d["kind"] == "IngressRoute"]
    return route["spec"]["routes"]


def route_for(fragment):
    matches = [r for r in routes() if fragment in r["match"]]
    assert len(matches) == 1, (fragment, [r["match"] for r in matches])
    return matches[0]


def names(route):
    return [m["name"] for m in route.get("middlewares", [])]


def per_second(spec):
    rl = spec["rateLimit"]
    period = {"1s": 1, "1m": 60}[rl.get("period", "1s")]
    return rl["average"] / period


def test_every_route_serves_the_same_hosts_and_keeps_the_base_limit():
    assert len(routes()) == 4
    for route in routes():
        assert route["match"].startswith(f"({HOSTS})") or route["match"] == HOSTS
        assert "exoplanet-ratelimit" in names(route)
        assert route["services"] == [{"name": "exoplanet-db-api", "port": 8000}]


def test_queue_and_novelty_get_a_much_tighter_rate_limit_than_the_catalogue():
    mw = middlewares()
    route = route_for("PathPrefix(`/v1/observations`)")
    assert "PathPrefix(`/v1/novelty`)" in route["match"]
    assert "exoplanet-ratelimit-expensive" in names(route)
    assert per_second(mw["exoplanet-ratelimit-expensive"]) <= 1
    assert (
        per_second(mw["exoplanet-ratelimit-expensive"]) < per_second(mw["exoplanet-ratelimit"]) / 50
    )


def test_mcp_traffic_gets_its_own_rate_limit():
    mw = middlewares()
    route = route_for("PathPrefix(`/mcp`) || PathPrefix(`/sse`)")
    assert "exoplanet-ratelimit-mcp" in names(route)
    assert per_second(mw["exoplanet-ratelimit-mcp"]) < per_second(mw["exoplanet-ratelimit"])


def test_sse_streams_are_capped_per_client_ip_not_per_host():
    mw = middlewares()
    route = route_for("Method(`GET`)")
    assert "Path(`/mcp/sse`)" in route["match"] and "PathPrefix(`/sse`)" in route["match"]
    assert {"exoplanet-ratelimit-mcp", "exoplanet-sse-inflight"} <= set(names(route))
    inflight = mw["exoplanet-sse-inflight"]["inFlightReq"]
    assert 1 <= inflight["amount"] <= 10
    # Traefik's inFlightReq groups by Host when no criterion is set; the client IP must be explicit.
    assert "ipStrategy" in inflight["sourceCriterion"]
    assert inflight["sourceCriterion"]["ipStrategy"].get("depth", 0) == 0


def test_specific_routes_outrank_the_catch_all():
    by_priority = sorted(routes(), key=lambda r: r["priority"], reverse=True)
    assert "Method(`GET`)" in by_priority[0]["match"]
    assert by_priority[-1]["match"] == HOSTS
    assert by_priority[0]["priority"] > by_priority[1]["priority"] > by_priority[-1]["priority"]
    assert by_priority[1]["priority"] == by_priority[2]["priority"]
