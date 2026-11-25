"""Sign-in is the user's own.

Claude and other MCP clients register dynamically and expect an OAuth 2.1
authorisation server. ArcGIS portals do not offer dynamic registration, so
this server stands between the two:

    client  --/authorize-->  this server  --/oauth2/authorize-->  ArcGIS portal
    client  <--- code -----  /oauth/arcgis/callback  <--- code ---  (user signs in)

Before the hand-off to the portal, this server shows its own consent page
naming the application that asked. That step is not decoration. Anyone can
register a client here, and a portal skips its own consent screen for an app
the user has approved before - so without it, a malicious client could send
a signed-in user through the flow and collect a session without them ever
seeing a prompt (the "confused deputy" problem). The page is bound to the
user's browser by a cookie the attacker cannot read or set, and cannot be
framed.

The client ends up with a token for *this server*. The user's ArcGIS token
and refresh token stay here, tied to that server token, and are used only to
call ArcGIS hosts the portal trusts. Every ArcGIS call therefore runs as the
signed-in user, so the portal's own sharing and permissions apply as normal.

Both legs use PKCE. The ArcGIS app's client secret is optional.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio
import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import Config
from .store import Store, digest

log = logging.getLogger("spatialise_mcp.auth")

SCOPE = "spatialise"
CALLBACK_PATH = "/oauth/arcgis/callback"
CONSENT_PATH = "/oauth/consent"
CONSENT_COOKIE = "spatialise_consent"
ACCESS_TTL_S = 3600
CODE_TTL_S = 300
PENDING_TTL_S = 600
DEFAULT_REFRESH_TTL_S = 14 * 24 * 3600
MAX_CLIENTS = 5000


class SpatialiseAccessToken(AccessToken):
    grant_id: str


class SpatialiseRefreshToken(RefreshToken):
    grant_id: str


class SpatialiseAuthorizationCode(AuthorizationCode):
    grant_id: str


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class ArcGISOAuthProvider:
    """OAuth authorisation server for MCP clients, backed by an ArcGIS portal sign-in."""

    def __init__(self, cfg: Config, store: Store, http: httpx.AsyncClient):
        self.cfg = cfg
        self.store = store
        self.http = http
        self.resource_url = cfg.public_url + "/mcp"
        self.callback_url = cfg.public_url + CALLBACK_PATH
        self._refresh_locks: dict[str, anyio.Lock] = {}

    # ------------------------------------------------------------ clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        from mcp.server.auth.provider import RegistrationError
        if self.store.count("client") >= MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "this server has reached its client limit")
        self.store.put("client", client_info.client_id, client_info.model_dump(mode="json", exclude_none=True))

    # ------------------------------------------------------------ leg 1: to the portal

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource_url.rstrip("/"):
            raise AuthorizeError("invalid_target", "this server only issues tokens for " + self.resource_url)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        self.store.put("pending", state, {
            "client_id": client.client_id,
            "client_name": (client.client_name or "")[:80],
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "client_state": params.state,
            "scopes": params.scopes or [SCOPE],
            "verifier": verifier,
        }, time.time() + PENDING_TTL_S)
        return f"{self.cfg.public_url}{CONSENT_PATH}?{urlencode({'tx': state})}"

    def _portal_authorize_url(self, state: str, verifier: str) -> str:
        query = {
            "client_id": self.cfg.portal.client_id,
            "response_type": "code",
            "redirect_uri": self.callback_url,
            "state": state,
            "expiration": str(self.cfg.portal.token_minutes),
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{self.cfg.portal.rest}/oauth2/authorize?{urlencode(query)}"

    # ------------------------------------------------------------ consent, on this server

    async def handle_consent(self, request: Request) -> Response:
        secure = self.cfg.public_url.startswith("https://")
        if request.method == "GET":
            tx = request.query_params.get("tx", "")
            pending = self.store.get("pending", tx) if tx else None
            if pending is None:
                return _page("Sign-in expired", "This sign-in link is no longer valid. Go back to Claude and "
                                                "connect Spatialise again.", 400)
            nonce = secrets.token_urlsafe(24)
            pending["nonce"] = digest(nonce)
            self.store.put("pending", tx, pending, time.time() + PENDING_TTL_S)
            resp = _consent_page(pending, tx, nonce, urlsplit(self.cfg.portal.url).hostname or "ArcGIS")
            resp.set_cookie(CONSENT_COOKIE, nonce, max_age=PENDING_TTL_S, path=CONSENT_PATH, httponly=True,
                            secure=secure, samesite="lax")
            return resp

        form = await request.form()
        tx, nonce = str(form.get("tx", "")), str(form.get("nonce", ""))
        pending = self.store.get("pending", tx) if tx else None
        cookie = request.cookies.get(CONSENT_COOKIE, "")
        # The form value must match the cookie this browser was given for this sign-in. Another site can
        # start a sign-in and learn its tx, but it can neither read that cookie nor plant it.
        if pending is None or not nonce or not hmac.compare_digest(cookie, nonce) or \
                not hmac.compare_digest(pending.get("nonce", ""), digest(nonce)):
            return _page("Sign-in could not continue", "This page was not opened from your own sign-in. Go back "
                                                       "to Claude and connect Spatialise again.", 400)
        if form.get("decision") != "approve":
            self.store.delete("pending", tx)
            resp: Response = RedirectResponse(
                construct_redirect_uri(pending["redirect_uri"], state=pending["client_state"], error="access_denied",
                                       error_description="the user did not approve the connection"),
                status_code=303, headers={"Cache-Control": "no-store"})
        else:
            pending["consented"] = True
            pending.pop("nonce", None)
            self.store.put("pending", tx, pending, time.time() + PENDING_TTL_S)
            resp = RedirectResponse(self._portal_authorize_url(tx, pending["verifier"]), status_code=303,
                                    headers={"Cache-Control": "no-store"})
        resp.delete_cookie(CONSENT_COOKIE, path=CONSENT_PATH)
        return resp

    # ------------------------------------------------------------ leg 2: back from the portal

    async def handle_callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        pending = self.store.pop("pending", state) if state else None
        if pending is None or not pending.get("consented"):
            return _page("Sign-in expired", "This sign-in link is no longer valid. Go back to Claude and connect "
                                            "Spatialise again.", 400)

        def back(**params: str | None) -> Response:
            return RedirectResponse(construct_redirect_uri(pending["redirect_uri"], state=pending["client_state"],
                                                           **params), status_code=302,
                                    headers={"Cache-Control": "no-store"})

        if request.query_params.get("error"):
            return back(error="access_denied", error_description="ArcGIS sign-in was cancelled or refused")
        code = request.query_params.get("code")
        if not code:
            return back(error="invalid_request", error_description="ArcGIS returned no authorisation code")
        try:
            grant = await self._portal_token({"grant_type": "authorization_code", "code": code,
                                              "redirect_uri": self.callback_url, "code_verifier": pending["verifier"]})
            who = await self._whoami(grant["access_token"])
        except _PortalError as e:
            log.warning("ArcGIS sign-in failed: %s", e)
            return back(error="server_error", error_description="ArcGIS sign-in could not be completed")
        allowed = self.cfg.portal.allowed_org_ids
        if allowed and who["org_id"] not in allowed:
            log.warning("sign-in refused: organisation not on the allowed list")
            return back(error="access_denied",
                        error_description="your ArcGIS organisation is not allowed to use this server")
        grant_id = secrets.token_urlsafe(18)
        grant.update(who)
        self.store.put("grant", grant_id, grant, grant["refresh_expires_at"])
        our_code = secrets.token_urlsafe(32)
        self.store.put("code", digest(our_code), {
            "client_id": pending["client_id"], "scopes": pending["scopes"],
            "code_challenge": pending["code_challenge"], "redirect_uri": pending["redirect_uri"],
            "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
            "grant_id": grant_id, "subject": who["username"], "expires_at": time.time() + CODE_TTL_S,
        }, time.time() + CODE_TTL_S)
        return back(code=our_code)

    # ------------------------------------------------------------ codes and tokens

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> SpatialiseAuthorizationCode | None:
        data = self.store.get("code", digest(authorization_code))
        if not data or data["client_id"] != client.client_id:
            return None
        return SpatialiseAuthorizationCode(
            code=authorization_code, scopes=data["scopes"], expires_at=data["expires_at"],
            client_id=data["client_id"], code_challenge=data["code_challenge"], redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"], resource=self.resource_url,
            subject=data["subject"], grant_id=data["grant_id"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: SpatialiseAuthorizationCode) -> OAuthToken:
        if self.store.pop("code", digest(authorization_code.code)) is None:
            raise TokenError("invalid_grant", "authorisation code already used")
        grant = self.store.get("grant", authorization_code.grant_id)
        if grant is None:
            raise TokenError("invalid_grant", "the ArcGIS sign-in behind this code has expired")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.grant_id,
                           authorization_code.subject, grant["refresh_expires_at"])

    def _issue(self, client_id: str, scopes: list[str], grant_id: str, subject: str | None,
               refresh_expires_at: float) -> OAuthToken:
        now = time.time()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        common = {"client_id": client_id, "scopes": scopes, "grant_id": grant_id, "subject": subject}
        self.store.put("access", digest(access), {**common, "expires_at": int(now + ACCESS_TTL_S)}, now + ACCESS_TTL_S)
        self.store.put("refresh", digest(refresh), {**common, "expires_at": int(refresh_expires_at)},
                       refresh_expires_at)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL_S, refresh_token=refresh, scope=" ".join(scopes))

    async def load_access_token(self, token: str) -> SpatialiseAccessToken | None:
        data = self.store.get("access", digest(token))
        if not data or data["expires_at"] < time.time():
            return None
        return SpatialiseAccessToken(token=token, client_id=data["client_id"], scopes=data["scopes"],
                                     expires_at=data["expires_at"], resource=self.resource_url,
                                     subject=data.get("subject"), grant_id=data["grant_id"])

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> SpatialiseRefreshToken | None:
        data = self.store.get("refresh", digest(refresh_token))
        if not data or data["client_id"] != client.client_id:
            return None
        return SpatialiseRefreshToken(token=refresh_token, client_id=data["client_id"], scopes=data["scopes"],
                                      expires_at=data["expires_at"], resource=self.resource_url,
                                      subject=data.get("subject"), grant_id=data["grant_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: SpatialiseRefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        if self.store.pop("refresh", digest(refresh_token.token)) is None:
            raise TokenError("invalid_grant", "refresh token already used")
        grant = self.store.get("grant", refresh_token.grant_id)
        if grant is None:
            raise TokenError("invalid_grant", "the ArcGIS sign-in has expired; sign in again")
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.grant_id,
                           refresh_token.subject, grant["refresh_expires_at"])

    async def revoke_token(self, token: SpatialiseAccessToken | SpatialiseRefreshToken) -> None:
        self.store.delete("access", digest(token.token))
        self.store.delete("refresh", digest(token.token))
        if isinstance(token, SpatialiseRefreshToken):
            self.store.delete("grant", token.grant_id)

    async def exchange_identity_assertion(self, client: Any, params: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "the JWT bearer grant is not supported by this server")

    # ------------------------------------------------------------ ArcGIS tokens for tools

    def identity(self, token: SpatialiseAccessToken) -> "GrantIdentity":
        return GrantIdentity(self, token.grant_id, token.subject)

    async def arcgis_token(self, grant_id: str, force_refresh: bool = False) -> str | None:
        grant = self.store.get("grant", grant_id)
        if grant is None:
            return None
        if not force_refresh and grant["access_expires_at"] - 60 > time.time():
            return grant["access_token"]
        lock = self._refresh_locks.setdefault(grant_id, anyio.Lock())
        async with lock:
            grant = self.store.get("grant", grant_id)  # another request may have refreshed it meanwhile
            if grant is None:
                return None
            if not force_refresh and grant["access_expires_at"] - 60 > time.time():
                return grant["access_token"]
            if not grant.get("refresh_token"):
                return None
            try:
                fresh = await self._portal_token({"grant_type": "refresh_token",
                                                  "refresh_token": grant["refresh_token"]})
            except _PortalError as e:
                log.warning("ArcGIS token refresh failed: %s", e)
                self.store.delete("grant", grant_id)  # the sign-in is dead; make the user sign in again
                return None
            grant["access_token"] = fresh["access_token"]
            grant["access_expires_at"] = fresh["access_expires_at"]
            if fresh.get("refresh_token"):  # portals may rotate the refresh token
                grant["refresh_token"] = fresh["refresh_token"]
                grant["refresh_expires_at"] = fresh["refresh_expires_at"]
            self.store.put("grant", grant_id, grant, grant["refresh_expires_at"])
            return grant["access_token"]

    # ------------------------------------------------------------ portal calls

    async def _portal_token(self, form: dict[str, str]) -> dict[str, Any]:
        body = {"client_id": self.cfg.portal.client_id, "f": "json", **form}
        if self.cfg.portal.client_secret:
            body["client_secret"] = self.cfg.portal.client_secret
        data = await self._portal_post(f"{self.cfg.portal.rest}/oauth2/token", body)
        if "access_token" not in data:
            raise _PortalError("no access token in the portal's response")
        now = time.time()
        out: dict[str, Any] = {"access_token": data["access_token"],
                               "access_expires_at": now + int(data.get("expires_in", 1800))}
        if data.get("refresh_token"):
            out["refresh_token"] = data["refresh_token"]
            out["refresh_expires_at"] = now + int(data.get("refresh_token_expires_in", DEFAULT_REFRESH_TTL_S))
        elif form["grant_type"] == "authorization_code":
            out["refresh_token"] = ""
            out["refresh_expires_at"] = out["access_expires_at"]
        return out

    async def _whoami(self, token: str) -> dict[str, str]:
        data = await self._portal_post(f"{self.cfg.portal.rest}/portals/self", {"f": "json", "token": token})
        user = data.get("user") or {}
        if not user.get("username"):
            raise _PortalError("the portal did not say who signed in")
        return {"username": str(user["username"]), "org_id": str(data.get("id") or user.get("orgId") or "")}

    async def _portal_post(self, url: str, body: dict[str, str]) -> dict[str, Any]:
        try:
            resp = await self.http.post(url, data=body)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise _PortalError(type(e).__name__) from e
        if not isinstance(data, dict) or "error" in data:
            err = data.get("error") if isinstance(data, dict) else None
            msg = err.get("error_description") or err.get("message") if isinstance(err, dict) else str(err)
            raise _PortalError(str(msg)[:200])
        return data


class _PortalError(Exception):
    pass


class GrantIdentity:
    """The signed-in user, for the ArcGIS client."""

    def __init__(self, provider: ArcGISOAuthProvider, grant_id: str, username: str | None):
        self._provider = provider
        self._grant_id = grant_id
        self.username = username

    async def token(self, force_refresh: bool = False) -> str | None:
        return await self._provider.arcgis_token(self._grant_id, force_refresh)


_PAGE_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"}
_PAGE_STYLE = ("font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1rem;color:#1d1d1b")


def _consent_page(pending: dict[str, Any], tx: str, nonce: str, portal_host: str) -> HTMLResponse:
    e = html.escape
    back_to = urlsplit(pending["redirect_uri"]).netloc or pending["redirect_uri"]
    name = pending.get("client_name") or "An application"
    body = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Spatialise</title>
<body style="{_PAGE_STYLE}">
<h1 style="font-size:1.35rem">Connect to Spatialise?</h1>
<p><strong>{e(name)}</strong> is asking to use Spatialise with your ArcGIS account.</p>
<p>If you continue, you will sign in at <strong>{e(portal_host)}</strong>, and then be sent back to
<strong>{e(back_to)}</strong>. That application will be able to geocode, search and look up places, and
publish hosted feature layers, <em>as you</em>, through this server. It is never given your ArcGIS password
or token.</p>
<p>Only continue if you started this yourself, from an application at <strong>{e(back_to)}</strong>.
The name above is whatever the application calls itself; the address is what counts.</p>
<form method="post" action="{CONSENT_PATH}" style="margin-top:1.5rem">
<input type="hidden" name="tx" value="{e(tx)}"><input type="hidden" name="nonce" value="{e(nonce)}">
<button name="decision" value="approve" style="font:inherit;padding:.6rem 1.2rem;margin-right:.6rem;border:0;
border-radius:6px;background:#1f5fbf;color:#fff;cursor:pointer">Continue to sign in</button>
<button name="decision" value="deny" style="font:inherit;padding:.6rem 1.2rem;border:1px solid #999;
border-radius:6px;background:#fff;color:#1d1d1b;cursor:pointer">Cancel</button>
</form></body></html>"""
    return HTMLResponse(body, headers=dict(_PAGE_HEADERS))


def _page(title: str, message: str, status: int) -> HTMLResponse:
    body = (f"<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>{html.escape(title)}</title>"
            f"<body style=\"{_PAGE_STYLE}\">"
            f"<h1 style=\"font-size:1.35rem\">{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>")
    return HTMLResponse(body, status_code=status, headers=dict(_PAGE_HEADERS))
