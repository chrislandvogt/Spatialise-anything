"""Server configuration: which resolvers exist, and the policy on each.

The plan's rule is that locator choice is explicit: the customer's own locator
first, a public geocoder last and off unless enabled. That order and that
switch live here, in one TOML file the operator owns, and nowhere else.

Secrets never go in the file. They come from the environment:

    SPATIALISE_CONFIG                path to the TOML file
    SPATIALISE_PUBLIC_URL            the URL clients reach this server on
    SPATIALISE_ARCGIS_CLIENT_ID      OAuth app registered in the portal
    SPATIALISE_ARCGIS_CLIENT_SECRET  optional (PKCE is always used)
    SPATIALISE_SECRET                signs confirmation tokens, encrypts the token store
    SPATIALISE_STORE_PATH            optional SQLite file for sign-ins (default: memory only)
    ARCGIS_TOKEN                     static mode only: a token or API key for local use
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .errors import ConfigError

AuthMode = Literal["oauth", "static", "none"]
ResourceAuth = Literal["user", "none"]
LocatorKind = Literal["customer", "org", "public"]

# Esri's public geocoding hosts. A locator on one of these is "public" whatever
# the config calls it, so the public-geocoder switch cannot be bypassed by a label.
PUBLIC_GEOCODER_HOSTS = ("geocode.arcgis.com", "geocode-api.arcgis.com", "geocodeqa.arcgis.com")
# ArcGIS Online stored geocodes: 40 credits per 1,000.
ARCGIS_ONLINE_STORED_CREDITS_PER_1000 = 40.0
_KIND_ORDER = {"customer": 0, "org": 1, "public": 2}
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


@dataclass(frozen=True)
class Portal:
    url: str = "https://www.arcgis.com"
    client_id: str = ""
    client_secret: str = ""
    trusted_hosts: tuple[str, ...] = ()
    allowed_org_ids: tuple[str, ...] = ()
    token_minutes: int = 60

    @property
    def rest(self) -> str:
        return self.url.rstrip("/") + "/sharing/rest"

    def trusts(self, url: str) -> bool:
        """True when the user's ArcGIS token may be sent to this URL's host.
        A token sent to a host outside this list would be a credential leak."""
        host = host_of(url)
        return bool(host) and any(fnmatch.fnmatch(host, pat) for pat in self.trusted_hosts)


@dataclass(frozen=True)
class Policy:
    allow_public_geocoder: bool = False
    allow_publish: bool = True
    # A geocode call with more queries than this must be confirmed with the
    # token the dry run returns, so a count is always reported before a batch.
    confirm_batches_over: int = 1
    max_batch: int = 200
    max_candidates: int = 10
    max_publish_features: int = 2000
    default_country: str = "AUS"


@dataclass(frozen=True)
class Locator:
    name: str
    url: str
    kind: LocatorKind = "customer"
    auth: ResourceAuth = "user"
    description: str = ""
    version: str = ""
    for_storage: bool = False
    credits_per_1000: float = 0.0

    @property
    def billed(self) -> bool:
        return self.credits_per_1000 > 0


@dataclass(frozen=True)
class Gazetteer:
    name: str
    url: str
    name_field: str
    alt_name_fields: tuple[str, ...] = ()
    type_field: str = ""
    state_field: str = ""
    id_field: str = ""
    extra_fields: tuple[str, ...] = ()
    where: str = ""
    auth: ResourceAuth = "user"
    description: str = ""
    version: str = ""
    # Positional uncertainty for point features, where the layer cannot say how
    # big the thing is. Keyed by upper-case feature type, with a default.
    default_uncertainty_m: float = 1000.0
    type_uncertainty_m: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Boundary:
    name: str
    url: str
    code_field: str = ""
    name_field: str = ""
    state_field: str = ""
    generalised_url: str = ""
    extra_fields: tuple[str, ...] = ()
    auth: ResourceAuth = "user"
    description: str = ""
    version: str = ""


@dataclass(frozen=True)
class Network:
    """A polyline layer whose features carry a name: roads, rivers, pipelines."""
    name: str
    url: str
    name_field: str
    alt_name_fields: tuple[str, ...] = ()
    where: str = ""
    auth: ResourceAuth = "user"
    description: str = ""
    version: str = ""
    # An anchor further than this from every matched line means the name or the anchor is wrong.
    max_snap_m: float = 10_000.0
    # Loose ends closer than this are treated as joined (a name often lapses through a town).
    gap_tolerance_m: float = 250.0


@dataclass(frozen=True)
class Config:
    portal: Portal = field(default_factory=Portal)
    policy: Policy = field(default_factory=Policy)
    locators: tuple[Locator, ...] = ()
    gazetteers: tuple[Gazetteer, ...] = ()
    boundaries: tuple[Boundary, ...] = ()
    networks: tuple[Network, ...] = ()
    auth_mode: AuthMode = "none"
    public_url: str = "http://127.0.0.1:8000"
    secret: str = ""
    store_path: str = ""
    static_token: str = ""
    allow_insecure_http: bool = False

    # ------------------------------------------------------------ lookups

    def locator(self, name: str | None) -> Locator:
        """The named locator, or the first one policy allows. Customer first,
        then org, then public - and public only when enabled."""
        if name:
            for loc in self.locators:
                if loc.name == name:
                    self.check_locator_allowed(loc)
                    return loc
            raise ConfigError(f"no locator named {name!r}. Call list_resolvers to see the names.")
        for loc in self.ordered_locators():
            if self.locator_block_reason(loc) is None:
                return loc
        if self.locators:
            raise ConfigError("every configured locator is blocked by policy. Call list_resolvers for the reasons.")
        raise ConfigError("no locator is configured on this server, so addresses cannot be geocoded.")

    def ordered_locators(self) -> list[Locator]:
        return sorted(self.locators, key=lambda loc: _KIND_ORDER[loc.kind])  # stable: file order within a kind

    def locator_block_reason(self, loc: Locator) -> str | None:
        if loc.kind == "public" and not self.policy.allow_public_geocoder:
            return "public geocoders are disabled by server policy (policy.allow_public_geocoder = false)"
        return None

    def check_locator_allowed(self, loc: Locator) -> None:
        from .errors import PolicyError
        reason = self.locator_block_reason(loc)
        if reason:
            raise PolicyError(f"locator {loc.name!r} may not be used: {reason}. Use another resolver from "
                              "list_resolvers, or leave the mention unresolved with the needs_network flag.")

    def gazetteer(self, name: str) -> Gazetteer:
        for g in self.gazetteers:
            if g.name == name:
                return g
        raise ConfigError(f"no gazetteer named {name!r}. Configured: {[g.name for g in self.gazetteers] or 'none'}.")

    def boundary(self, name: str) -> Boundary:
        for b in self.boundaries:
            if b.name.lower() == name.lower():
                return b
        raise ConfigError(f"no boundary set named {name!r}. Configured: {[b.name for b in self.boundaries] or 'none'}.")

    def network(self, name: str | None) -> Network:
        if not self.networks:
            raise ConfigError("no line network is configured on this server, so 'along X between A and B' cannot "
                              "be traced. Leave the mention unresolved with the needs_network flag.")
        if not name:
            if len(self.networks) == 1:
                return self.networks[0]
            raise ConfigError(f"say which network to trace: {[n.name for n in self.networks]}")
        for n in self.networks:
            if n.name.lower() == name.lower():
                return n
        raise ConfigError(f"no network named {name!r}. Configured: {[n.name for n in self.networks]}.")


# ---------------------------------------------------------------- loading

def _tuple(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(str(x) for x in v)


def _check_url(url: str, what: str, allow_insecure: bool) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname and (allow_insecure or parts.hostname in _LOCAL_HOSTS):
        return
    raise ConfigError(f"{what}: {url!r} must be an https URL")


def _known(table: dict, allowed: set[str], what: str) -> None:
    extra = set(table) - allowed
    if extra:
        raise ConfigError(f"{what}: unknown setting(s) {sorted(extra)}. Allowed: {sorted(allowed)}")


def from_dict(data: dict[str, Any], env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    _known(data, {"server", "portal", "policy", "locators", "gazetteers", "boundaries", "networks"}, "config")
    server = dict(data.get("server", {}))
    _known(server, {"auth_mode", "public_url", "store_path", "allow_insecure_http"}, "[server]")
    allow_insecure = bool(server.get("allow_insecure_http", False))

    p = dict(data.get("portal", {}))
    _known(p, {"url", "trusted_hosts", "allowed_org_ids", "token_minutes"}, "[portal]")
    portal_url = str(p.get("url", "https://www.arcgis.com")).rstrip("/")
    _check_url(portal_url, "[portal] url", allow_insecure)
    trusted = list(_tuple(p.get("trusted_hosts")))
    portal_host = host_of(portal_url)
    if portal_host not in trusted:
        trusted.insert(0, portal_host)
    if portal_host.endswith(".arcgis.com") and "*.arcgis.com" not in trusted:
        trusted.append("*.arcgis.com")
    for pat in trusted:
        if pat in ("*", "*.*") or pat.startswith("*.") and pat.count(".") < 2:
            raise ConfigError(f"[portal] trusted_hosts: {pat!r} is too broad - the user's token would go to any host")
    portal = Portal(
        url=portal_url,
        client_id=env.get("SPATIALISE_ARCGIS_CLIENT_ID", ""),
        client_secret=env.get("SPATIALISE_ARCGIS_CLIENT_SECRET", ""),
        trusted_hosts=tuple(trusted),
        allowed_org_ids=_tuple(p.get("allowed_org_ids")),
        token_minutes=int(p.get("token_minutes", 60)),
    )

    pol = dict(data.get("policy", {}))
    _known(pol, set(Policy.__dataclass_fields__), "[policy]")
    policy = Policy(**pol)
    if policy.max_batch < 1 or policy.confirm_batches_over < 0:
        raise ConfigError("[policy] max_batch must be >= 1 and confirm_batches_over >= 0")

    def resource_auth(t: dict, what: str, url: str) -> ResourceAuth:
        auth = t.get("auth", "user" if portal.trusts(url) else "none")
        if auth not in ("user", "none"):
            raise ConfigError(f"{what}: auth must be 'user' or 'none'")
        if auth == "user" and not portal.trusts(url):
            raise ConfigError(
                f"{what}: auth = 'user' would send the user's ArcGIS token to {host_of(url)}, which is not a "
                f"trusted host of the portal. Add it to [portal] trusted_hosts if it is federated with the "
                f"portal, or set auth = 'none' for a public service.")
        return auth

    names: set[str] = set()

    def unique(name: str, what: str) -> str:
        if not name or name in names:
            raise ConfigError(f"{what}: name {name!r} is missing or used twice")
        names.add(name)
        return name

    locators = []
    for t in data.get("locators", []):
        _known(t, set(Locator.__dataclass_fields__), "[[locators]]")
        what = f"[[locators]] {t.get('name', '?')}"
        url = str(t.get("url", "")).rstrip("/")
        _check_url(url, what, allow_insecure)
        kind = t.get("kind", "customer")
        if kind not in _KIND_ORDER:
            raise ConfigError(f"{what}: kind must be customer, org or public")
        public_host = host_of(url) in PUBLIC_GEOCODER_HOSTS
        if public_host and kind != "public":
            raise ConfigError(f"{what}: {host_of(url)} is Esri's public geocoder, so kind must be 'public'")
        for_storage = bool(t.get("for_storage", public_host))
        credits = float(t.get("credits_per_1000", ARCGIS_ONLINE_STORED_CREDITS_PER_1000 if public_host else 0.0))
        if public_host and not for_storage:
            raise ConfigError(f"{what}: results are always persisted, so the public geocoder must use "
                              "for_storage = true (stored geocodes)")
        locators.append(Locator(
            name=unique(str(t.get("name", "")), what), url=url, kind=kind, auth=resource_auth(t, what, url),
            description=str(t.get("description", "")), version=str(t.get("version", "")),
            for_storage=for_storage, credits_per_1000=credits))

    gazetteers = []
    for t in data.get("gazetteers", []):
        _known(t, set(Gazetteer.__dataclass_fields__), "[[gazetteers]]")
        what = f"[[gazetteers]] {t.get('name', '?')}"
        url = str(t.get("url", "")).rstrip("/")
        _check_url(url, what, allow_insecure)
        if not t.get("name_field"):
            raise ConfigError(f"{what}: name_field is required")
        gazetteers.append(Gazetteer(
            name=unique(str(t.get("name", "")), what), url=url, name_field=str(t["name_field"]),
            alt_name_fields=_tuple(t.get("alt_name_fields")), type_field=str(t.get("type_field", "")),
            state_field=str(t.get("state_field", "")), id_field=str(t.get("id_field", "")),
            extra_fields=_tuple(t.get("extra_fields")), where=str(t.get("where", "")),
            auth=resource_auth(t, what, url), description=str(t.get("description", "")),
            version=str(t.get("version", "")), default_uncertainty_m=float(t.get("default_uncertainty_m", 1000.0)),
            type_uncertainty_m={str(k).upper(): float(v) for k, v in dict(t.get("type_uncertainty_m", {})).items()}))

    boundaries = []
    for t in data.get("boundaries", []):
        _known(t, set(Boundary.__dataclass_fields__), "[[boundaries]]")
        what = f"[[boundaries]] {t.get('name', '?')}"
        url = str(t.get("url", "")).rstrip("/")
        _check_url(url, what, allow_insecure)
        gen = str(t.get("generalised_url", "")).rstrip("/")
        if gen:
            _check_url(gen, what + " generalised_url", allow_insecure)
            if host_of(gen) != host_of(url):
                raise ConfigError(f"{what}: generalised_url must be on the same host as url")
        if not (t.get("code_field") or t.get("name_field")):
            raise ConfigError(f"{what}: set code_field, name_field or both")
        boundaries.append(Boundary(
            name=unique(str(t.get("name", "")), what), url=url, code_field=str(t.get("code_field", "")),
            name_field=str(t.get("name_field", "")), state_field=str(t.get("state_field", "")),
            generalised_url=gen, extra_fields=_tuple(t.get("extra_fields")), auth=resource_auth(t, what, url),
            description=str(t.get("description", "")), version=str(t.get("version", ""))))

    networks = []
    for t in data.get("networks", []):
        _known(t, set(Network.__dataclass_fields__), "[[networks]]")
        what = f"[[networks]] {t.get('name', '?')}"
        url = str(t.get("url", "")).rstrip("/")
        _check_url(url, what, allow_insecure)
        if not t.get("name_field"):
            raise ConfigError(f"{what}: name_field is required")
        networks.append(Network(
            name=unique(str(t.get("name", "")), what), url=url, name_field=str(t["name_field"]),
            alt_name_fields=_tuple(t.get("alt_name_fields")), where=str(t.get("where", "")),
            auth=resource_auth(t, what, url), description=str(t.get("description", "")),
            version=str(t.get("version", "")), max_snap_m=float(t.get("max_snap_m", 10_000.0)),
            gap_tolerance_m=float(t.get("gap_tolerance_m", 250.0))))

    static_token = env.get("ARCGIS_TOKEN", "")
    auth_mode = server.get("auth_mode") or ("oauth" if portal.client_id else "static" if static_token else "none")
    if auth_mode not in ("oauth", "static", "none"):
        raise ConfigError("[server] auth_mode must be oauth, static or none")
    public_url = str(env.get("SPATIALISE_PUBLIC_URL") or server.get("public_url") or "http://127.0.0.1:8000").rstrip("/")
    _check_url(public_url, "public_url", allow_insecure)
    secret = env.get("SPATIALISE_SECRET", "")
    if auth_mode == "oauth":
        if not portal.client_id:
            raise ConfigError("auth_mode = oauth needs SPATIALISE_ARCGIS_CLIENT_ID (an OAuth app registered in the "
                              f"portal with redirect URI {public_url}/oauth/arcgis/callback)")
        if len(secret) < 32:
            raise ConfigError("auth_mode = oauth needs SPATIALISE_SECRET of at least 32 characters")
    if auth_mode == "static" and not static_token:
        raise ConfigError("auth_mode = static needs ARCGIS_TOKEN in the environment")
    return Config(portal=portal, policy=policy, locators=tuple(locators), gazetteers=tuple(gazetteers),
                  boundaries=tuple(boundaries), networks=tuple(networks), auth_mode=auth_mode, public_url=public_url, secret=secret,
                  store_path=str(env.get("SPATIALISE_STORE_PATH") or server.get("store_path") or ""),
                  static_token=static_token, allow_insecure_http=allow_insecure)


def load(path: str | Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    path = path or env.get("SPATIALISE_CONFIG")
    data: dict[str, Any] = {}
    if path:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file {p} not found")
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"config file {p} is not valid TOML: {e}") from e
    return from_dict(data, env)
