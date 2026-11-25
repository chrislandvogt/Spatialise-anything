from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fake_arcgis import PORTAL, PUBLIC, SERVICES, WORLD, FakeArcGIS  # noqa: E402

from spatialise_mcp import config as config_mod  # noqa: E402
from spatialise_mcp import resolvers  # noqa: E402
from spatialise_mcp.server import App, build  # noqa: E402

SECRET = "test-secret-" + "x" * 32

BASE: dict[str, Any] = {
    "portal": {"url": PORTAL, "trusted_hosts": ["gis.portal.test"]},
    "locators": [
        {"name": "customer-addresses", "url": f"{SERVICES}/Addresses/GeocodeServer", "kind": "customer",
         "version": "2026-11"},
    ],
    "gazetteers": [
        {"name": "placenames", "url": f"{SERVICES}/Placenames/FeatureServer/0", "name_field": "PLACE_NAME",
         "type_field": "FEATURE_TYPE", "state_field": "State", "id_field": "PLACE_ID",
         "where": "PLACE_NAME_STATUS = 'REGISTERED'", "version": "test-1",
         "type_uncertainty_m": {"LAKE": 5000, "LOCALITY": 2000}},
        {"name": "water", "url": f"{SERVICES}/Water/FeatureServer/0", "name_field": "NAME", "type_field": "TYPE"},
    ],
    "boundaries": [
        {"name": "sa2", "url": f"{PUBLIC}/SA2/MapServer/0", "code_field": "sa2_code_2021",
         "name_field": "sa2_name_2021", "state_field": "state_name_2021", "version": "ASGS 2021"},
        {"name": "zones", "url": f"{SERVICES}/Zones/FeatureServer/0", "code_field": "ZONE_NO",
         "name_field": "ZONE_NAME"},
    ],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake() -> FakeArcGIS:
    resolvers.clear_cache()
    return FakeArcGIS()


@pytest.fixture
def make_app(fake: FakeArcGIS):
    apps: list[App] = []

    def _make(overrides: dict[str, Any] | None = None, *, mode: str = "static", env: dict[str, str] | None = None,
              token: str | None = None) -> App:
        data = copy.deepcopy(BASE)
        for k, v in (overrides or {}).items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
        e = {"SPATIALISE_SECRET": SECRET, **(env or {})}
        if mode == "static":
            e["ARCGIS_TOKEN"] = token or fake.issue_token()
        elif mode == "oauth":
            e.setdefault("SPATIALISE_ARCGIS_CLIENT_ID", "client-abc")
            e.setdefault("SPATIALISE_PUBLIC_URL", "http://127.0.0.1:8000")
        data.setdefault("server", {})["auth_mode"] = mode
        cfg = config_mod.from_dict(data, e)
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app))
        app = build(cfg, http=http)
        apps.append(app)
        return app

    return _make


async def call(app: App, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool through the MCP protocol (in-process) and return its structured result.
    Tool errors come back as {"_error": "<message>"}."""
    from mcp import Client
    async with Client(app.mcp) as client:
        result = await client.call_tool(tool, args or {})
    if result.is_error:
        return {"_error": " ".join(getattr(c, "text", "") for c in result.content)}
    return result.structured_content


# ---------------------------------------------------------------- OAuth over HTTP

import base64  # noqa: E402
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import secrets  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402

SERVER = "http://127.0.0.1:8000"
REDIRECT = "http://localhost:9999/cb"


class Web:
    """The server's HTTP app plus a browser-ish helper that walks the sign-in redirects."""

    def __init__(self, app: App, fake: FakeArcGIS, http: httpx.AsyncClient):
        self.app, self.fake, self.http = app, fake, http
        self.portal = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app))

    async def register(self) -> str:
        r = await self.http.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "test client", "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert r.status_code == 201, r.text
        return r.json()["client_id"]

    async def authorize(self, client_id: str, challenge: str, state: str = "st-1", **extra: str) -> httpx.Response:
        return await self.http.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256", "scope": "spatialise",
            "resource": SERVER + "/mcp", **extra})

    async def sign_in(self) -> dict[str, Any]:
        """Register, authorise, sign in at the fake portal, exchange the code. Returns the token response
        plus client_id."""
        client_id = await self.register()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        r = await self.authorize(client_id, challenge)
        assert r.status_code == 302, r.text
        back = await self.through_portal(r.headers["location"])
        q = parse_qs(urlsplit(back.headers["location"]).query)
        assert back.headers["location"].startswith(REDIRECT), back.headers["location"]
        assert q.get("state") == ["st-1"], q
        r = await self.http.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                                 "redirect_uri": REDIRECT, "client_id": client_id,
                                                 "code_verifier": verifier, "resource": SERVER + "/mcp"})
        assert r.status_code == 200, r.text
        return {**r.json(), "client_id": client_id, "code": q["code"][0], "verifier": verifier}

    async def consent(self, consent_url: str, decision: str = "approve") -> httpx.Response:
        """Open the server's consent page as the user's browser does (keeping its cookie) and press a button."""
        url = urlsplit(consent_url)
        page = await self.http.get(url.path + "?" + url.query)
        assert page.status_code == 200, page.text
        import re
        fields = dict(re.findall(r'name="(tx|nonce)" value="([^"]+)"', page.text))
        return await self.http.post("/oauth/consent", data={**fields, "decision": decision})

    async def through_portal(self, consent_url: str) -> httpx.Response:
        """Approve on the consent page, visit the portal's authorize page (the fake signs in at once), then
        the server's callback."""
        approved = await self.consent(consent_url)
        assert approved.status_code == 303, approved.text
        p = await self.portal.get(approved.headers["location"])
        assert p.status_code == 302, p.text
        cb = urlsplit(p.headers["location"])
        return await self.http.get(cb.path + "?" + cb.query)

    async def rpc(self, token: str | None, method: str, params: dict[str, Any] | None = None) -> httpx.Response:
        headers = {"Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await self.http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                                   "params": params or {}})

    async def tool(self, token: str, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        r = await self.rpc(token, "tools/call", {"name": name, "arguments": args or {}})
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        if result.get("isError"):
            return {"_error": " ".join(c.get("text", "") for c in result["content"])}
        return result["structuredContent"]


@pytest.fixture
def web(make_app, fake):
    @contextlib.asynccontextmanager
    async def _web(overrides: dict[str, Any] | None = None, env: dict[str, str] | None = None):
        app = make_app(overrides, mode="oauth", env=env)
        asgi = app.asgi()
        async with asgi.router.lifespan_context(asgi):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url=SERVER) as http:
                yield Web(app, fake, http)
    return _web
