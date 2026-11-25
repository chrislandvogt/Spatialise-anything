"""The MCP server: six tools over ArcGIS REST, and the app that serves them.

    list_resolvers     what may be used here, and the policy on each
    geocode            address or place text -> scored candidates
    reverse_geocode    point -> nearest address or place
    search_gazetteer   place name -> candidates with feature type and geometry
    lookup_boundary    code or name -> polygon
    publish_features   contract features -> hosted feature layer

trace_segment, the seventh tool in the design, is the next phase.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .arcgis import Anonymous, ArcGIS, Identity, StaticIdentity
from .auth import CALLBACK_PATH, CONSENT_PATH, SCOPE, ArcGISOAuthProvider, SpatialiseAccessToken
from .config import Config
from .errors import SpatialiseError
from .publish import Publisher
from .resolvers import Resolvers
from .store import Store, open_store

log = logging.getLogger("spatialise_mcp")

INSTRUCTIONS = """\
Spatialise resolves places through this organisation's ArcGIS locators and feature services, and publishes \
results to hosted feature layers. Use it with the spatialise-anything skill.

- Call list_resolvers first. It says which locators, gazetteers and boundary sets exist here and what policy \
allows. Use only what it lists.
- Every tool returns candidates, never a decision. You choose, keep the candidates you rejected with a reason, \
and mark a mention ambiguous when the candidates cannot be separated.
- Nothing is geocoded or published silently. A batch geocode and every publish first return a dry run with a \
confirm_token; report the count, destination and cost to the user, then repeat the call with the token.
- Publish only when the user asks, and only to the target they name.
- Each candidate carries resolver, method and uncertainty_m: copy them into the feature unchanged.
"""

LonLat = Annotated[list[float], Field(min_length=2, max_length=2, description="[longitude, latitude] in WGS84/GDA2020")]
BBox = Annotated[list[float], Field(min_length=4, max_length=4, description="[min_lon, min_lat, max_lon, max_lat]")]
# Results go out as structured content plus the same JSON as compact text. The SDK's default text is
# indented, which puts every coordinate of a polygon on its own line and doubles what the model reads.
Result = Annotated[CallToolResult, dict[str, Any]]
ConfirmToken = Annotated[str | None, Field(
    description="The confirm_token from this tool's dry run. Omit on the first call; pass it on the second, with "
                "every other argument unchanged, once the user has agreed.")]


@dataclass
class App:
    cfg: Config
    mcp: MCPServer
    http: httpx.AsyncClient
    provider: ArcGISOAuthProvider | None
    store: Store

    def asgi(self, host: str = "127.0.0.1") -> Starlette:
        """The Starlette app for Streamable HTTP. Stateless JSON: each request
        stands alone, so the server scales out and survives restarts."""
        public_host = urlsplit(self.cfg.public_url).hostname or ""
        local = public_host in ("127.0.0.1", "localhost", "::1")
        if self.cfg.auth_mode == "static" and not (local and host in ("127.0.0.1", "localhost", "::1")):
            raise SpatialiseError("auth_mode = static shares one ArcGIS token with every caller, so it only runs "
                                  "on localhost. Use auth_mode = oauth for a remote server.")
        # DNS-rebinding checks protect local servers. A remote server is protected by OAuth instead, and must
        # accept whatever Origin its clients send.
        security = None if local else TransportSecuritySettings(enable_dns_rebinding_protection=False)
        return self.mcp.streamable_http_app(stateless_http=True, json_response=True, host=host,
                                            transport_security=security)

    async def aclose(self) -> None:
        await self.http.aclose()


def build(cfg: Config, *, http: httpx.AsyncClient | None = None, store: Store | None = None) -> App:
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False,
                                     headers={"User-Agent": f"spatialise-mcp/{__version__}"})
    store = store or open_store(cfg.store_path, cfg.secret)
    provider: ArcGISOAuthProvider | None = None
    auth: AuthSettings | None = None
    if cfg.auth_mode == "oauth":
        provider = ArcGISOAuthProvider(cfg, store, http)
        auth = AuthSettings(
            issuer_url=cfg.public_url,  # type: ignore[arg-type]
            resource_server_url=provider.resource_url,  # type: ignore[arg-type]
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                                  default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
        )
    mcp = MCPServer("spatialise", title="Spatialise Anything", version=__version__, instructions=INSTRUCTIONS,
                    auth_server_provider=provider, auth=auth, log_level="WARNING")

    def identity() -> Identity:
        if provider is not None:
            token = get_access_token()
            if isinstance(token, SpatialiseAccessToken):
                return provider.identity(token)
            return Anonymous()
        if cfg.auth_mode == "static":
            return StaticIdentity(cfg.static_token)
        return Anonymous()

    def arcgis() -> ArcGIS:
        return ArcGIS(cfg, http, identity())

    async def run(coro: Any) -> CallToolResult:
        try:
            out = await coro
        except SpatialiseError as e:
            raise ToolError(str(e)) from e
        text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=out)

    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                                open_world_hint=True)
    sends_text = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                 open_world_hint=True)

    @mcp.tool(title="List resolvers", annotations=read_only)
    async def list_resolvers(
        check: Annotated[bool, Field(description="Also contact each service and report whether it answers. "
                                                 "Slower; use when a tool has just failed.")] = False,
    ) -> Result:
        """Report which locators, gazetteers and boundary sets this server may use, the policy on each
        (allowed or blocked and why, billed or not, whether results are stored), who is signed in, and whether
        publishing is available. Call this first: it decides which resolver to pass to the other tools.
        Sends nothing about the user's documents anywhere."""
        return await run(Resolvers(cfg, arcgis()).list_resolvers(check))

    @mcp.tool(title="Geocode addresses", annotations=sends_text)
    async def geocode(
        queries: Annotated[list[str], Field(min_length=1, description="Addresses or place text, one per mention, "
                           "e.g. ['12 Smith St, Ballarat VIC 3350']. Include suburb and state when the document "
                           "gives them.")],
        resolver: Annotated[str | None, Field(description="Locator name from list_resolvers. Omit to use the "
                            "first one policy allows (the customer's own locator before any other).")] = None,
        max_candidates: Annotated[int, Field(ge=1, le=20, description="Candidates to return per query.")] = 5,
        country: Annotated[str | None, Field(description="ISO country code to search within, e.g. 'AUS'. Omit "
                           "for the server default; pass '' for no restriction.")] = None,
        near: Annotated[LonLat | None, Field(description="Prefer matches near this point (the document's "
                        "region or a place already resolved). Adds distance_km to each candidate.")] = None,
        bbox: Annotated[BBox | None, Field(description="Only return matches inside this box.")] = None,
        confirm_token: ConfirmToken = None,
    ) -> Result:
        """Turn address or place text into scored candidates with a locator (findAddressCandidates).

        Returns, per query, every candidate: label, match type (kind), point, score 0-1, uncertainty_m with its
        basis, and the resolver to cite. It does not choose: several close scores mean the mention is ambiguous.
        An unmatched query has no candidates and stays unresolved.

        A call with more queries than the server's confirm_batches_over returns status
        'confirmation_required' and sends nothing: it reports the count, where the text would go, whether
        results are stored, the estimated credits, and a confirm_token. Tell the user, then call again with the
        same arguments and the token."""
        return await run(Resolvers(cfg, arcgis()).geocode(queries, resolver, max_candidates, country, near, bbox,
                                                           confirm_token))

    @mcp.tool(title="Reverse geocode points", annotations=sends_text)
    async def reverse_geocode(
        points: Annotated[list[LonLat], Field(min_length=1, description="Points to describe, each "
                          "[longitude, latitude].")],
        resolver: Annotated[str | None, Field(description="Locator name from list_resolvers. Omit for the "
                            "first one policy allows.")] = None,
        confirm_token: ConfirmToken = None,
    ) -> Result:
        """Find the nearest address or place to each point (reverseGeocode) - for labelling photo locations and
        parsed coordinates. Returns a label, its match type, the matched point and distance_m from the input.
        Use it to label a feature, never to move it: the geometry stays the coordinate that was given.
        Batches need a confirm_token, exactly as geocode does."""
        return await run(Resolvers(cfg, arcgis()).reverse_geocode(points, resolver, confirm_token))

    @mcp.tool(title="Search gazetteer", annotations=read_only)
    async def search_gazetteer(
        names: Annotated[list[str], Field(min_length=1, max_length=50, description="Place names as written, "
                         "e.g. ['Lake Wendouree', 'Yarrowee River']. Leave out words that are not part of the "
                         "name ('the', 'near').")],
        gazetteer: Annotated[str | None, Field(description="Gazetteer name from list_resolvers. Omit to search "
                             "all of them.")] = None,
        feature_types: Annotated[list[str] | None, Field(description="Only these feature types, as the "
                                 "gazetteer spells them, e.g. ['LAKE', 'RESERVOIR']. Search without it first to "
                                 "see the spellings.")] = None,
        state: Annotated[str | None, Field(description="State or territory, as the gazetteer spells it "
                         "(e.g. 'VIC'). Ignored by gazetteers with no state field.")] = None,
        near: Annotated[LonLat | None, Field(description="Adds distance_km from this point to each candidate "
                        "and orders by it.")] = None,
        bbox: Annotated[BBox | None, Field(description="Only return features intersecting this box.")] = None,
        match: Annotated[Literal["exact", "prefix", "contains"], Field(description="exact (default) matches "
                         "the whole name, ignoring case. Use prefix or contains only when exact finds "
                         "nothing.")] = "exact",
        limit: Annotated[int, Field(ge=1, le=50, description="Candidates per name.")] = 10,
        geometry: Annotated[Literal["point", "simplified", "full"], Field(description="point (default): a "
                            "representative point, plus bbox for lines and areas. simplified or full: also the "
                            "feature's own geometry, when it has at most max_vertices.")] = "point",
        max_vertices: Annotated[int, Field(ge=20, le=5000, description="Vertex budget per returned "
                                "geometry.")] = 300,
    ) -> Result:
        """Look up place names in the organisation's gazetteer layers: lakes, rivers, roads, facilities,
        localities - whatever the layers hold. Returns every candidate with its feature type (kind), point,
        uncertainty_m and basis, and the resolver to cite.

        The same name is often several things (a locality, a station and a post office). Use the sentence to
        choose the feature type, and the document's other places to choose between same-named features. If
        they cannot be separated, the mention is ambiguous. Read-only; sends the names to the gazetteer's
        host."""
        return await run(Resolvers(cfg, arcgis()).search_gazetteer(names, gazetteer, feature_types, state, near,
                                                                    bbox, match, limit, geometry, max_vertices))

    @mcp.tool(title="Look up boundary", annotations=read_only)
    async def lookup_boundary(
        boundary: Annotated[str, Field(description="Boundary set name from list_resolvers, e.g. 'sa2' or "
                            "'lga'.")],
        codes: Annotated[list[str] | None, Field(max_length=50, description="Codes to look up, e.g. "
                         "['201011001']. Preferred over names: a code is unambiguous.")] = None,
        names: Annotated[list[str] | None, Field(max_length=50, description="Names to look up, matched whole "
                         "and case-insensitively, e.g. ['Ballarat'].")] = None,
        state: Annotated[str | None, Field(description="Restrict name matches to this state, as the boundary "
                         "set spells it.")] = None,
        geometry: Annotated[Literal["simplified", "full", "point", "none"], Field(description="simplified "
                            "(default): the polygon, generalised until it fits max_vertices. full: as served. "
                            "point: a point inside and the area only. none: attributes only.")] = "simplified",
        max_vertices: Annotated[int, Field(ge=20, le=20000, description="Vertex budget per polygon.")] = 300,
        limit: Annotated[int, Field(ge=1, le=20, description="Candidates per code or name.")] = 5,
    ) -> Result:
        """Turn a code or name into its polygon from a configured boundary set: LGA, SA2, postcode, or the
        organisation's own zones. Returns, per query, the matching areas with code, name, point, bbox,
        area_km2, the polygon, and uncertainty_m (how far the simplified boundary may sit from the true one).

        A name that matches more than one area returns them all - choose with the document's context or mark
        the mention ambiguous. Read-only."""
        return await run(Resolvers(cfg, arcgis()).lookup_boundary(boundary, codes, names, state, geometry,
                                                                   max_vertices, limit))

    @mcp.tool(title="Publish features", annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True))
    async def publish_features(
        features: Annotated[dict[str, Any], Field(description="The GeoJSON FeatureCollection the skill produced "
                            "(the contents of the .geojson file), unchanged.")],
        create: Annotated[str | None, Field(max_length=120, description="Title for a NEW hosted feature layer, "
                          "as the user named it. Give this or url, not both.")] = None,
        url: Annotated[str | None, Field(description="FeatureServer URL of a layer this tool created earlier, "
                       "to add these features to it. Give this or create, not both.")] = None,
        confirm_token: ConfirmToken = None,
    ) -> Result:
        """Write contract features to a hosted feature layer in the user's ArcGIS portal, as the signed-in
        user. Only when the user has asked to publish, and only to the target they named.

        The first call writes nothing: it validates the features against the output contract and returns the
        plan (target, portal, account, rows per layer) with a confirm_token. Show the user the plan; if they
        approve, call again with the same features and target plus the token.

        A published service has layers for points, lines and polygons and a table 'unplaced' for ambiguous
        and unresolved mentions, so nothing is dropped. New layers are private to the user. Returns the item
        and layer URLs and the rows written; a 'partial' status means some rows failed and says which."""
        return await run(Publisher(cfg, arcgis()).publish(features, create, url, confirm_token))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__, "auth_mode": cfg.auth_mode},
                            headers={"Cache-Control": "no-store"})

    if provider is not None:
        @mcp.custom_route(CONSENT_PATH, methods=["GET", "POST"])
        async def consent(request: Request) -> Response:
            assert provider is not None
            return await provider.handle_consent(request)

        @mcp.custom_route(CALLBACK_PATH, methods=["GET"])
        async def arcgis_callback(request: Request) -> Response:
            assert provider is not None
            return await provider.handle_callback(request)

    return App(cfg=cfg, mcp=mcp, http=http, provider=provider, store=store)
