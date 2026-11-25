"""Sign-in is the user's own: the OAuth proxy in front of the ArcGIS portal."""

import base64
import hashlib
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import REDIRECT, SECRET, SERVER
from spatialise_mcp.errors import SpatialiseError
from spatialise_mcp.store import MemoryStore, SqliteStore, digest

pytestmark = pytest.mark.anyio


def challenge_for(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def grant_of(w):
    """The single ArcGIS grant in the store (tests sign in once)."""
    store = w.app.store
    ((_, gid),) = [k for k in store._data if k[0] == "grant"]
    return gid, store.get("grant", gid)


# ---------------------------------------------------------------- discovery and the door

async def test_metadata_and_unauthenticated_challenge(web):
    async with web() as w:
        meta = (await w.http.get("/.well-known/oauth-authorization-server")).json()
        assert meta["issuer"] == SERVER and meta["registration_endpoint"] == SERVER + "/register"
        assert meta["code_challenge_methods_supported"] == ["S256"] and meta["scopes_supported"] == ["spatialise"]
        res = (await w.http.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert res["resource"] == SERVER + "/mcp" and res["authorization_servers"] == [SERVER]
        r = await w.rpc(None, "tools/list")
        assert r.status_code == 401
        assert "resource_metadata=" in r.headers["www-authenticate"]
        r = await w.rpc("not-a-real-token", "tools/list")
        assert r.status_code == 401
        assert (await w.http.get("/healthz")).json()["status"] == "ok"   # health needs no token


# ---------------------------------------------------------------- the two-legged flow

async def test_authorize_shows_consent_then_sends_the_user_to_the_portal_with_pkce(web, fake):
    async with web() as w:
        client_id = await w.register()
        r = await w.authorize(client_id, challenge_for("v" * 50))
        assert r.status_code == 302 and r.headers["location"].startswith(SERVER + "/oauth/consent?tx=")
        assert not [c for c in fake.calls if "oauth2" in c["path"]]      # the portal has not been touched yet
        page = await w.http.get(urlsplit(r.headers["location"]).path + "?" + urlsplit(r.headers["location"]).query)
        assert "test client" in page.text and "localhost:9999" in page.text and "portal.test" in page.text
        assert page.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in page.headers["content-security-policy"]
        cookie = page.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "path=/oauth/consent" in cookie

        approved = await w.consent(r.headers["location"])
        assert approved.status_code == 303
        url = urlsplit(approved.headers["location"])
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert f"{url.scheme}://{url.netloc}{url.path}" == "https://portal.test/sharing/rest/oauth2/authorize"
        assert q["client_id"] == "client-abc" and q["response_type"] == "code"
        assert q["redirect_uri"] == SERVER + "/oauth/arcgis/callback"
        assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
        assert q["state"] != "st-1"                     # the portal sees our state, never the client's
        assert q["code_challenge"] != challenge_for("v" * 50)   # and our PKCE pair, not the client's


async def test_consent_cannot_be_given_by_another_site(web, fake):
    """The confused-deputy attack: a hostile client starts a sign-in, then tries to push the victim's
    browser past the consent page. It knows the tx. It does not have the victim's cookie."""
    import re
    async with web() as w:
        client_id = await w.register()

        async def start():
            """A fresh sign-in, as the attacker would begin one. Each attempt gets its own, so each
            refusal below is for the reason it claims."""
            r = await w.authorize(client_id, challenge_for("v" * 50))
            loc = urlsplit(r.headers["location"])
            w.http.cookies.clear()
            return loc, parse_qs(loc.query)["tx"][0]

        # 1. Straight to the callback, skipping consent.
        _, tx = await start()
        skipped = await w.http.get("/oauth/arcgis/callback", params={"code": "x", "state": tx})
        assert skipped.status_code == 400

        # 2. A forged approval with no page ever shown to this browser.
        _, tx = await start()
        forged = await w.http.post("/oauth/consent", data={"tx": tx, "nonce": "guess", "decision": "approve"})
        assert forged.status_code == 400 and "not opened from your own sign-in" in forged.text

        # 3. The attacker loads the page themselves to learn a nonce, then replays it from the victim's
        #    browser, which holds no matching cookie.
        loc, tx = await start()
        page = await w.http.get(loc.path + "?" + loc.query)
        nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
        w.http.cookies.clear()
        replay = await w.http.post("/oauth/consent", data={"tx": tx, "nonce": nonce, "decision": "approve"})
        assert replay.status_code == 400 and "not opened from your own sign-in" in replay.text

        # 4. A cookie planted in the victim's browser with a value the attacker chose.
        loc, tx = await start()
        await w.http.get(loc.path + "?" + loc.query)
        w.http.cookies.clear()
        w.http.cookies.set("spatialise_consent", "planted")
        planted = await w.http.post("/oauth/consent", data={"tx": tx, "nonce": "planted", "decision": "approve"})
        assert planted.status_code == 400 and "not opened from your own sign-in" in planted.text

        # 5. Control: the same steps with the real cookie do go through.
        loc, tx = await start()
        page = await w.http.get(loc.path + "?" + loc.query)
        nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
        ok = await w.http.post("/oauth/consent", data={"tx": tx, "nonce": nonce, "decision": "approve"})
        assert ok.status_code == 303 and "portal.test" in ok.headers["location"]

        assert not [c for c in fake.calls if "oauth2" in c["path"]] and w.app.store.count("grant") == 0


async def test_consent_page_escapes_what_the_client_calls_itself(web):
    async with web() as w:
        r = await w.http.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "<script>alert(1)</script>Claude",
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"]})
        r = await w.authorize(r.json()["client_id"], challenge_for("v" * 50))
        page = await w.http.get(urlsplit(r.headers["location"]).path + "?" + urlsplit(r.headers["location"]).query)
        assert "<script>" not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt;Claude" in page.text
        assert "the address is what counts" in page.text


async def test_cancel_on_the_consent_page_returns_to_the_client(web, fake):
    async with web() as w:
        client_id = await w.register()
        r = await w.authorize(client_id, challenge_for("v" * 50))
        denied = await w.consent(r.headers["location"], decision="deny")
        q = parse_qs(urlsplit(denied.headers["location"]).query)
        assert denied.headers["location"].startswith(REDIRECT) and q["error"] == ["access_denied"]
        assert q["state"] == ["st-1"] and not [c for c in fake.calls if "oauth2" in c["path"]]
        tx = parse_qs(urlsplit(r.headers["location"]).query)["tx"][0]
        assert (await w.http.get("/oauth/consent", params={"tx": tx})).status_code == 400   # it is spent


async def test_sign_in_issues_server_tokens_not_arcgis_tokens(web, fake):
    async with web() as w:
        t = await w.sign_in()
        assert t["token_type"] == "Bearer" and t["expires_in"] == 3600 and t["scope"] == "spatialise"
        assert not t["access_token"].startswith("agol-") and not t["refresh_token"].startswith("refresh-")
        gid, grant = grant_of(w)
        assert grant["username"] == "jane" and grant["org_id"] == "ORG123"
        assert grant["access_token"].startswith("agol-") and grant["refresh_token"].startswith("refresh-")
        # the server keeps only hashes of its own tokens
        assert w.app.store.get("access", t["access_token"]) is None
        assert w.app.store.get("access", digest(t["access_token"]))["grant_id"] == gid
        out = await w.tool(t["access_token"], "list_resolvers")
        assert out["signed_in_as"] == "jane"


async def test_tool_calls_run_as_the_signed_in_user(web, fake):
    async with web() as w:
        t = await w.sign_in()
        _, grant = grant_of(w)
        await w.tool(t["access_token"], "geocode", {"queries": ["Kyneton Town Hall"]})
        sent = [c["form"].get("token") for c in fake.calls if c["path"].endswith("/findAddressCandidates")]
        assert sent == [grant["access_token"]]


async def test_pkce_code_reuse_and_wrong_client_are_refused(web):
    async with web() as w:
        client_id = await w.register()
        verifier = "a" * 60
        r = await w.authorize(client_id, challenge_for(verifier))
        back = await w.through_portal(r.headers["location"])
        code = parse_qs(urlsplit(back.headers["location"]).query)["code"][0]
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client_id}
        bad = await w.http.post("/token", data={**form, "code_verifier": "b" * 60})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
        other = await w.register()
        stolen = await w.http.post("/token", data={**form, "client_id": other, "code_verifier": verifier})
        assert stolen.status_code == 400
        good = await w.http.post("/token", data={**form, "code_verifier": verifier})
        assert good.status_code == 200
        again = await w.http.post("/token", data={**form, "code_verifier": verifier})
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


async def test_redirect_uri_must_be_the_registered_one(web):
    async with web() as w:
        client_id = await w.register()
        r = await w.http.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": "https://evil.example/cb",
            "code_challenge": challenge_for("v" * 50), "code_challenge_method": "S256", "state": "s"})
        assert r.status_code == 400 and "evil.example" not in r.headers.get("location", "")


async def test_token_for_another_resource_is_not_issued(web):
    async with web() as w:
        client_id = await w.register()
        r = await w.authorize(client_id, challenge_for("v" * 50), resource="https://other.example/mcp")
        assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT)
        assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["invalid_target"]


async def test_refused_or_broken_portal_sign_in_goes_back_to_the_client_as_an_error(web, fake):
    async with web() as w:
        client_id = await w.register()
        fake.deny_sign_in = True
        r = await w.authorize(client_id, challenge_for("v" * 50))
        back = await w.through_portal(r.headers["location"])
        q = parse_qs(urlsplit(back.headers["location"]).query)
        assert back.headers["location"].startswith(REDIRECT) and q["error"] == ["access_denied"] and q["state"] == ["st-1"]
        assert w.app.store.count("grant") == 0

        r = await w.http.get("/oauth/arcgis/callback", params={"code": "x", "state": "never-issued"})
        assert r.status_code == 400 and "no longer valid" in r.text

        fake.deny_sign_in = False
        r = await w.authorize(client_id, challenge_for("v" * 50))
        approved = await w.consent(r.headers["location"])
        state = parse_qs(urlsplit(approved.headers["location"]).query)["state"][0]
        forged = await w.http.get("/oauth/arcgis/callback", params={"code": "not-a-portal-code", "state": state})
        assert parse_qs(urlsplit(forged.headers["location"]).query)["error"] == ["server_error"]
        replay = await w.http.get("/oauth/arcgis/callback", params={"code": "x", "state": state})
        assert replay.status_code == 400                # a state works once


async def test_only_allowed_organisations_may_sign_in(web, fake):
    async with web({"portal": {"allowed_org_ids": ["SOMEONE_ELSE"]}}) as w:
        client_id = await w.register()
        r = await w.authorize(client_id, challenge_for("v" * 50))
        back = await w.through_portal(r.headers["location"])
        q = parse_qs(urlsplit(back.headers["location"]).query)
        assert q["error"] == ["access_denied"] and "organisation" in q["error_description"][0]
        assert w.app.store.count("grant") == 0
    async with web({"portal": {"allowed_org_ids": ["ORG123"]}}) as w:
        assert (await w.sign_in())["access_token"]


# ---------------------------------------------------------------- lifetimes

async def test_refresh_rotates_and_old_tokens_die(web):
    async with web() as w:
        t = await w.sign_in()
        form = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": t["client_id"]}
        r = await w.http.post("/token", data=form)
        assert r.status_code == 200
        new = r.json()
        assert new["access_token"] != t["access_token"] and new["refresh_token"] != t["refresh_token"]
        assert (await w.tool(new["access_token"], "list_resolvers"))["signed_in_as"] == "jane"
        assert (await w.http.post("/token", data=form)).status_code == 400      # the old refresh token is spent


async def test_expired_server_token_is_refused(web):
    async with web() as w:
        t = await w.sign_in()
        rec = w.app.store.get("access", digest(t["access_token"]))
        rec["expires_at"] = int(time.time()) - 5
        w.app.store.put("access", digest(t["access_token"]), rec)
        assert (await w.rpc(t["access_token"], "tools/list")).status_code == 401


async def test_arcgis_token_is_refreshed_before_it_expires(web, fake):
    async with web() as w:
        t = await w.sign_in()
        gid, grant = grant_of(w)
        old = grant["access_token"]
        grant["access_expires_at"] = time.time() + 5        # inside the 60 s safety margin
        w.app.store.put("grant", gid, grant, grant["refresh_expires_at"])
        out = await w.tool(t["access_token"], "geocode", {"queries": ["Kyneton Town Hall"]})
        assert out["status"] == "ok"
        _, after = grant_of(w)
        assert after["access_token"] != old
        assert [c for c in fake.calls if c["path"].endswith("/findAddressCandidates")][0]["form"]["token"] == after["access_token"]


async def test_arcgis_token_revoked_mid_session_is_refreshed_once_and_retried(web, fake):
    async with web() as w:
        t = await w.sign_in()
        _, grant = grant_of(w)
        del fake.tokens[grant["access_token"]]              # the portal forgot it; our copy still looks valid
        out = await w.tool(t["access_token"], "geocode", {"queries": ["Kyneton Town Hall"]})
        assert out["status"] == "ok"
        attempts = [c for c in fake.calls if c["path"].endswith("/findAddressCandidates") or c["path"].endswith("GeocodeServer")]
        assert len({c["form"]["token"] for c in attempts}) == 2


async def test_rotating_refresh_tokens_are_kept(web, fake):
    fake.rotate_refresh = True
    async with web() as w:
        t = await w.sign_in()
        gid, grant = grant_of(w)
        first_refresh = grant["refresh_token"]
        for _ in range(2):
            _, grant = grant_of(w)
            grant["access_expires_at"] = 0
            w.app.store.put("grant", gid, grant, grant["refresh_expires_at"])
            assert (await w.tool(t["access_token"], "geocode", {"queries": ["Kyneton Town Hall"]}))["status"] == "ok"
        assert grant_of(w)[1]["refresh_token"] != first_refresh


async def test_dead_arcgis_sign_in_tells_the_user_to_reconnect(web, fake):
    async with web() as w:
        t = await w.sign_in()
        gid, grant = grant_of(w)
        fake.refresh.clear()                                # the portal revoked the refresh token
        grant["access_expires_at"] = 0
        w.app.store.put("grant", gid, grant, grant["refresh_expires_at"])
        out = await w.tool(t["access_token"], "geocode", {"queries": ["Kyneton Town Hall"]})
        assert "Reconnect the Spatialise connector" in out["_error"]
        assert w.app.store.count("grant") == 0
        public = await w.tool(t["access_token"], "lookup_boundary", {"boundary": "sa2", "codes": ["202021030"]})
        assert public["status"] == "ok"                     # public layers need no ArcGIS token


async def test_revoke(web):
    async with web() as w:
        t = await w.sign_in()
        r = await w.http.post("/revoke", data={"token": t["access_token"], "client_id": t["client_id"],
                                               "client_secret": ""})
        assert r.status_code == 200, r.text
        assert (await w.rpc(t["access_token"], "tools/list")).status_code == 401
        # revoking the refresh token ends the ArcGIS sign-in too
        t2 = await w.sign_in()
        token = await w.app.provider.load_refresh_token(await w.app.provider.get_client(t2["client_id"]),
                                                        t2["refresh_token"])
        await w.app.provider.revoke_token(token)
        assert w.app.store.get("grant", token.grant_id) is None


async def test_two_users_never_share_a_token(web, fake):
    fake.users["raj"] = {"password": "pw", "org": "ORG123", "role": "org_user", "privileges": ["features:user:edit"]}
    async with web() as w:
        jane = await w.sign_in()
        fake.sign_in_as = "raj"
        raj = await w.sign_in()
        assert (await w.tool(jane["access_token"], "list_resolvers"))["signed_in_as"] == "jane"
        assert (await w.tool(raj["access_token"], "list_resolvers"))["signed_in_as"] == "raj"
        fake.calls.clear()
        await w.tool(raj["access_token"], "geocode", {"queries": ["Kyneton Town Hall"]})
        used = {c["form"]["token"] for c in fake.calls if "token" in c["form"]}
        assert {fake.tokens[tok]["user"] for tok in used} == {"raj"}


# ---------------------------------------------------------------- the store

def test_sqlite_store_encrypts_and_binds_rows(tmp_path):
    path = str(tmp_path / "s.db")
    s = SqliteStore(path, SECRET)
    s.put("grant", "g1", {"access_token": "agol-SECRET-VALUE", "n": 1}, time.time() + 60)
    assert s.get("grant", "g1") == {"access_token": "agol-SECRET-VALUE", "n": 1}
    assert b"agol-SECRET-VALUE" not in open(path, "rb").read()
    assert b"agol-SECRET-VALUE" not in (open(path + "-wal", "rb").read() if (tmp_path / "s.db-wal").exists() else b"")
    assert SqliteStore(path, "another-secret-" + "y" * 32).get("grant", "g1") is None   # wrong key
    db = sqlite3.connect(path)
    db.execute("UPDATE kv SET key = 'g2' WHERE key = 'g1'")                             # move the row
    db.commit()
    assert SqliteStore(path, SECRET).get("grant", "g2") is None
    with pytest.raises(ValueError):
        SqliteStore(path, "short")


@pytest.mark.parametrize("make", [lambda p: MemoryStore(), lambda p: SqliteStore(str(p / "t.db"), SECRET)])
def test_store_expiry_pop_and_count(tmp_path, make):
    s = make(tmp_path)
    s.put("code", "a", {"v": 1}, time.time() + 60)
    s.put("code", "b", {"v": 2}, time.time() - 1)
    assert s.get("code", "b") is None and s.count("code") == 1
    assert s.pop("code", "a") == {"v": 1} and s.pop("code", "a") is None and s.count("code") == 0


async def test_sign_ins_survive_a_restart_with_a_store_file(web, fake, tmp_path):
    env = {"SPATIALISE_STORE_PATH": str(tmp_path / "state.db")}
    async with web(env=env) as w:
        t = await w.sign_in()
    async with web(env=env) as w2:                          # a new process, same file
        assert (await w2.tool(t["access_token"], "list_resolvers"))["signed_in_as"] == "jane"
        r = await w2.http.post("/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                               "client_id": t["client_id"]})
        assert r.status_code == 200


# ---------------------------------------------------------------- deployment guards

def test_shared_token_mode_only_runs_on_localhost(make_app):
    app = make_app(mode="static", env={"SPATIALISE_PUBLIC_URL": "https://mcp.example.org"})
    with pytest.raises(SpatialiseError, match="only runs\\s+on localhost"):
        app.asgi(host="0.0.0.0")
    local = make_app(mode="static")
    with pytest.raises(SpatialiseError):
        local.asgi(host="0.0.0.0")
    assert local.asgi(host="127.0.0.1") is not None
