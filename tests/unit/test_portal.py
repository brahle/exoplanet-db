from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from exoplanet_db.api.app import app


@pytest.mark.asyncio
async def test_human_portal_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Test root HTML portal
        res = await client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers.get("content-type", "")
        assert "Exoplanet DB" in res.text
        assert 'href="#catalog"' in res.text
        assert 'href="#queue"' in res.text
        assert 'id="source-filter"' in res.text
        assert "Creative Commons Attribution 4.0 International" in res.text

        # 2. Test static CSS
        res_css = await client.get("/static/portal.css")
        assert res_css.status_code == 200
        assert "text/css" in res_css.headers.get("content-type", "")
        assert "--accent-cyan" in res_css.text
        assert ".source-badge" in res_css.text

        # 3. Test static JS
        res_js = await client.get("/static/portal.js")
        assert res_js.status_code == 200
        assert "application/javascript" in res_js.headers.get(
            "content-type", ""
        ) or "text/javascript" in res_js.headers.get("content-type", "")
        assert "handleRoute" in res_js.text
        assert "openDossierModal" in res_js.text
