"""The whole stack the way a real connector uses it: the SDK's own MCP client and
OAuth implementation, over real HTTP, against the server on a real port.

The other tests drive the OAuth endpoints by hand. This one proves that a
standards-following client - discovery, dynamic registration, PKCE, resource
indicators, bearer calls, refresh - gets through without help.
"""

import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn

from conftest import BASE, SECRET
from spatialise_mcp import config as C
from spatialise_mcp.server import build

pytestmark = pytest.mark.anyio


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running(fake):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    data = {**BASE, "server": {"auth_mode": "oauth"}}
    cfg = C.from_dict(data, {"SPATIALISE_SECRET": SECRET, "SPATIALISE_ARCGIS_CLIENT_ID": "client-abc",
                             "SPATIALISE_PUBLIC_URL": base})
    app = build(cfg, http=httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app)))
    server = uvicorn.Server(uvicorn.Config(app.asgi(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    yield base, app
    server.should_exit = True
    thread.join(timeout=5)


async def test_sdk_client_signs_in_and_calls_tools(running, fake):
    import httpx2
    from mcp import Client
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    base, app = running
    redirect = "http://localhost:9999/cb"
    seen: dict[str, str] = {}

    class Storage:
        tokens = None
        client = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.client

        async def set_client_info(self, info):
            self.client = info

    async def open_browser(url: str) -> None:
        """What the user's browser does: consent page -> portal sign-in -> server callback -> client."""
        async with httpx.AsyncClient() as real, httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app)) as portal:
            r = await real.get(url)
            assert r.status_code == 302 and "/oauth/consent?tx=" in r.headers["location"]
            page = await real.get(r.headers["location"])          # the consent page; the browser keeps its cookie
            assert "sdk test" in page.text and "localhost:9999" in page.text
            fields = dict(re.findall(r'name="(tx|nonce)" value="([^"]+)"', page.text))
            ok = await real.post(base + "/oauth/consent", data={**fields, "decision": "approve"})
            assert ok.status_code == 303 and "portal.test/sharing/rest/oauth2/authorize" in ok.headers["location"]
            p = await portal.get(ok.headers["location"])
            back = await real.get(p.headers["location"])
            assert back.status_code == 302, back.text
            seen["final"] = back.headers["location"]

    async def read_redirect() -> AuthorizationCodeResult:
        q = parse_qs(urlsplit(seen["final"]).query)
        assert seen["final"].startswith(redirect)
        return AuthorizationCodeResult(code=q["code"][0], state=q.get("state", [None])[0])

    storage = Storage()
    oauth = OAuthClientProvider(
        server_url=base + "/mcp",
        client_metadata=OAuthClientMetadata(client_name="sdk test", redirect_uris=[redirect],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], token_endpoint_auth_method="none"),
        storage=storage, redirect_handler=open_browser, callback_handler=read_redirect)

    async with httpx2.AsyncClient(auth=oauth, timeout=20) as http:
        async with Client(streamable_http_client(base + "/mcp", http_client=http)) as client:
            assert client.server_info.name == "spatialise"
            assert "confirm_token" in (client.instructions or "")
            names = {t.name for t in (await client.list_tools()).tools}
            assert "publish_features" in names
            who = await client.call_tool("list_resolvers", {})
            assert who.structured_content["signed_in_as"] == "jane"
            geo = await client.call_tool("geocode", {"queries": ["Kyneton Town Hall, Hutton Street"]})
            assert geo.structured_content["results"][0]["status"] == "matched"
            err = await client.call_tool("geocode", {"queries": ["x"], "resolver": "nope"})
            assert err.is_error and "list_resolvers" in err.content[0].text

    assert storage.client is not None and storage.tokens.refresh_token
    assert not storage.tokens.access_token.startswith("agol-")      # the client never holds an ArcGIS token
    used = {c["form"]["token"] for c in fake.calls if c["path"].endswith("/findAddressCandidates")}
    assert all(fake.tokens[t]["user"] == "jane" for t in used)
