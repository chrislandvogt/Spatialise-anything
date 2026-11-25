"""A thin async client for ArcGIS REST.

Three things matter here more than the calls themselves:

* The user's token goes only to hosts the portal trusts. Every request names
  whether it wants the token; the client refuses to attach it anywhere else.
* The token travels in the POST body, never in a URL, so it cannot end up in
  an access log or a Referer header.
* ArcGIS reports most failures as HTTP 200 with an ``error`` object. Those are
  turned into exceptions with a message the model can act on.

Nothing about a request's parameters is logged: place strings from a
customer's documents pass through here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import anyio
import httpx

from .config import Config, host_of
from .errors import ArcGISError, SignInRequired, SpatialiseError

log = logging.getLogger("spatialise_mcp.arcgis")

DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_S = 0.5
_RETRY_STATUS = {429, 502, 503, 504}
_INVALID_TOKEN = {498}
_TOKEN_REQUIRED = {499}


class Identity(Protocol):
    """Who is calling, and their ArcGIS token."""

    username: str | None

    async def token(self, force_refresh: bool = False) -> str | None: ...


class Anonymous:
    username: str | None = None

    async def token(self, force_refresh: bool = False) -> str | None:
        return None


class StaticIdentity:
    """A token or API key from the environment, for local single-user use."""

    def __init__(self, token: str, username: str | None = None):
        self._token = token
        self.username = username

    async def token(self, force_refresh: bool = False) -> str | None:
        return self._token


class ArcGIS:
    def __init__(self, cfg: Config, http: httpx.AsyncClient, identity: Identity,
                 retries: int | None = None, backoff_s: float | None = None):
        self.cfg = cfg
        self.http = http
        self.identity = identity
        self.retries = DEFAULT_RETRIES if retries is None else retries
        self.backoff_s = DEFAULT_BACKOFF_S if backoff_s is None else backoff_s

    async def call(self, url: str, params: dict[str, Any] | None = None, *, auth: bool) -> dict[str, Any]:
        """POST an operation with f=json and return the decoded body.

        ``auth`` says whether this resource is meant to receive the user's token.
        """
        host = host_of(url)
        form = {k: _form_value(v) for k, v in (params or {}).items() if v is not None}
        form["f"] = "json"
        refreshed = False
        while True:
            token = None
            if auth:
                if not self.cfg.portal.trusts(url):
                    # A config or URL-handling bug, not a user error: fail closed.
                    raise SpatialiseError(f"refusing to send the ArcGIS token to untrusted host {host}")
                token = await self.identity.token(force_refresh=refreshed)
                if not token and self.cfg.auth_mode != "none":
                    raise SignInRequired(
                        f"{host} needs your ArcGIS sign-in and this session has none. Reconnect the "
                        "Spatialise connector to sign in, then try again.")
            body = dict(form)
            if token:
                body["token"] = token
            data = await self._post(url, body, host)
            err = data.get("error") if isinstance(data, dict) else None
            if not err:
                return data
            code = err.get("code")
            if code in _INVALID_TOKEN and auth and not refreshed:
                refreshed = True  # one retry with a freshly refreshed token
                continue
            raise _map_error(err, host, auth, self.cfg.auth_mode)

    async def _post(self, url: str, body: dict[str, str], host: str) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                await anyio.sleep(self.backoff_s * 2 ** (attempt - 1))
            try:
                resp = await self.http.post(url, data=body)
            except httpx.TimeoutException as e:
                last = SpatialiseError(f"{host} did not answer in time")
                last.__cause__ = e
                continue
            except httpx.TransportError as e:
                last = SpatialiseError(f"could not reach {host} ({type(e).__name__})")
                last.__cause__ = e
                continue
            if resp.status_code in _RETRY_STATUS:
                last = SpatialiseError(f"{host} is busy or unavailable (HTTP {resp.status_code})")
                continue
            if resp.status_code in (401, 403):
                raise SignInRequired(f"{host} refused the request (HTTP {resp.status_code}). Your account may "
                                     "not have access to this service.")
            if resp.status_code >= 400:
                raise SpatialiseError(f"{host} returned HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as e:
                raise SpatialiseError(f"{host} did not return JSON - is the URL an ArcGIS REST endpoint?") from e
            if not isinstance(data, dict):
                raise SpatialiseError(f"{host} returned an unexpected response")
            return data
        assert last is not None
        log.warning("giving up on %s after %d attempts", host, self.retries + 1)
        raise last


def _form_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def _map_error(err: dict[str, Any], host: str, auth: bool, auth_mode: str) -> SpatialiseError:
    code = err.get("code")
    message = str(err.get("message") or err.get("error_description") or "unknown error")
    details = [str(d) for d in (err.get("details") or [])]
    if code in _INVALID_TOKEN:
        return SignInRequired(f"your ArcGIS sign-in for {host} has expired. Reconnect the Spatialise connector "
                              "to sign in again.")
    if code in _TOKEN_REQUIRED:
        if auth and auth_mode == "none":
            return SignInRequired(f"{host} needs an ArcGIS sign-in, and this server runs without one. "
                                  "Ask the operator to enable sign-in (auth_mode = oauth).")
        if not auth:
            return SignInRequired(f"{host} needs a token but is configured as a public service (auth = 'none'). "
                                  "Ask the operator to set auth = 'user' for it.")
        return SignInRequired(f"{host} needs your ArcGIS sign-in. Reconnect the Spatialise connector.")
    if code == 403:
        return SignInRequired(f"your ArcGIS account does not have permission for this operation on {host}: "
                              f"{message}")
    return ArcGISError(code, message, details, host)
