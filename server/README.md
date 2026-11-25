# Spatialise MCP server

The network half of [Spatialise Anything](../README.md). The skill decides;
this server resolves. It wraps ArcGIS REST - locators, feature services and
hosted feature layers - behind six tools, and it makes no decisions: every
tool returns candidates and metadata, and the skill picks.

| Tool | Does | ArcGIS REST operation |
|---|---|---|
| `list_resolvers` | Which locators, gazetteers and boundary sets may be used here, and the policy on each | (server config; optional service-info probes) |
| `geocode` | Address or place text to scored candidates | `findAddressCandidates` |
| `reverse_geocode` | Point to nearest address or place | `reverseGeocode` |
| `search_gazetteer` | Place name to candidates with feature type and geometry | feature layer `query` |
| `lookup_boundary` | Code or name to a polygon: LGA, SA2, postcode, your own zones | feature layer `query` |
| `publish_features` | Contract features to a new or existing hosted feature layer | `createService`, `addToDefinition`, `addFeatures` |

`trace_segment` - "along the Calder between Kyneton and Castlemaine" as a
line - is the next phase.

> **Status.** Built and tested against an in-process ArcGIS stand-in (108
> tests) and, read-only, against live public services (ABS boundaries, the
> VICNAMES register, the World Geocoding Service). **Not yet run against a
> real ArcGIS organisation**: sign-in, a customer locator and publishing all
> need an OAuth app and an account. [Verifying against your organisation](#verifying-against-your-organisation)
> is a ten-minute job with the script provided.

## Design rules

- **Locator choice is explicit.** The operator's config lists every resolver.
  Customer locators are used first, then the organisation's, then public
  geocoders - and public geocoders are refused unless `allow_public_geocoder`
  is on. Esri's public geocoder cannot be relabelled as anything else.
- **Nothing is sent silently.** A batch geocode and every publish first
  return a dry run: the count, the destination host, whether results are
  stored, the estimated credits, and a confirmation token. The real call needs
  that token back, and the token is bound to the exact arguments, so the
  report cannot be skipped.
- **Sign-in is the user's own.** Every ArcGIS call runs as the signed-in user,
  so the portal's sharing and permissions apply. No keys ship with the plugin.
- **Stored geocodes are counted.** Results are persisted, so the World
  Geocoding Service is always called with `forStorage=true` and billed as
  stored (40 credits per 1,000 on ArcGIS Online).
- **Nothing is dropped.** A published service has layers for points, lines
  and polygons, and a table `unplaced` for every ambiguous and unresolved
  mention.

## Try it with no ArcGIS organisation

The example config points at public Australian services, so the read-only
tools work straight away.

```bash
pip install -e "server[dev]"
spatialise-mcp check --config server/config.example.toml     # contacts each resolver
spatialise-mcp serve --config server/config.example.toml     # http://127.0.0.1:8000/mcp
```

Connect Claude Code to it:

```bash
claude mcp add --transport http spatialise http://127.0.0.1:8000/mcp
```

Then ask Claude for the SA2 called Kyneton, or what "Lake Eppalock" is in the
VICNAMES register.

## Sign-in: registering the OAuth app

MCP clients register themselves dynamically; ArcGIS portals do not offer
that. So this server is the OAuth authorisation server its clients see, and it
sends each user to your portal to sign in:

```
Claude --/authorize--> this server: consent page --> ArcGIS portal (user signs in)
Claude <---- code ---- /oauth/arcgis/callback <---- code ----
```

The user first sees a consent page on this server naming the application
that asked and the address they will be returned to, then signs in at the
portal. Claude ends up with a token for this server only. The user's ArcGIS
token and refresh token stay on the server.

1. In the portal, create OAuth 2.0 credentials (ArcGIS Online: *Content > New
   item > Developer credentials > OAuth 2.0 credentials*; Enterprise: *Content
   > New item > Application*).
2. Add the redirect URL `https://<your server>/oauth/arcgis/callback`.
3. Give the server the client ID. A client secret is optional: PKCE is always
   used.

```bash
export SPATIALISE_PUBLIC_URL=https://spatialise.example.gov.au
export SPATIALISE_ARCGIS_CLIENT_ID=<client id>
export SPATIALISE_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export SPATIALISE_STORE_PATH=/data/state.db        # optional; without it sign-ins are lost on restart
spatialise-mcp serve --config my-config.toml --host 0.0.0.0
```

| Variable | Purpose |
|---|---|
| `SPATIALISE_CONFIG` | Path to the TOML config |
| `SPATIALISE_PUBLIC_URL` | The HTTPS URL clients reach the server on |
| `SPATIALISE_ARCGIS_CLIENT_ID` | OAuth app in the portal; setting it turns sign-in on |
| `SPATIALISE_ARCGIS_CLIENT_SECRET` | Optional |
| `SPATIALISE_SECRET` | At least 32 characters. Signs confirmation tokens, encrypts the store |
| `SPATIALISE_STORE_PATH` | SQLite file for sign-in state, encrypted at rest |
| `ARCGIS_TOKEN` | Local use only: a token or API key instead of sign-in |

## Deploying

A container that needs an HTTPS URL in front of it and, ideally, a small
volume. Any container host will do.

```bash
docker build -t spatialise-mcp server/
docker run -p 8000:8000 \
  -v $PWD/my-config.toml:/etc/spatialise/config.toml:ro -v spatialise-data:/data \
  -e SPATIALISE_PUBLIC_URL=https://spatialise.example.gov.au \
  -e SPATIALISE_ARCGIS_CLIENT_ID=... -e SPATIALISE_SECRET=... \
  -e SPATIALISE_STORE_PATH=/data/state.db \
  spatialise-mcp
```

- Terminate TLS at your proxy or platform; `/healthz` is the health check.
- Run **one instance**. Sign-in state is a local SQLite file and token-refresh
  locks are in-process. Scaling out needs a shared store, which is not built.
- The server must be able to reach your portal and every configured service.
  For an Enterprise behind a firewall, host the server inside the network and
  publish only its HTTPS endpoint, or run it locally (below).

### Connecting Claude

**claude.ai and Cowork** - *Customize > Connectors > Add custom connector*,
with the URL `https://<your server>/mcp`, and "Register automatically" as the
OAuth client option. On Team and Enterprise plans an Owner adds it once in
*Organization settings > Connectors*; members then connect with their own
ArcGIS account.

**Claude Code** - `claude mcp add --transport http spatialise https://<your server>/mcp`,
then `/mcp` to sign in.

**Bundled with the plugin** - once you have a URL, add `.mcp.json` at the
plugin root so the connector is offered with the plugin:

```json
{
  "mcpServers": {
    "spatialise": { "type": "http", "url": "https://spatialise.example.gov.au/mcp" }
  }
}
```

It is deliberately not in this repository: there is no shared public
deployment, and a placeholder URL would break installs. On claude.ai and
Cowork the bundled connector still has to be connected by each user.

### Local use (an Enterprise you can only reach from your machine)

```bash
export ARCGIS_TOKEN=<your token>
spatialise-mcp stdio --config my-config.toml
```

This works in Claude Code and desktop Cowork only. `auth_mode = static`
shares one token with every caller, so the HTTP server refuses to run it on
anything but localhost.

## Configuration

One TOML file; [`config.example.toml`](config.example.toml) is commented
throughout. In outline:

```toml
[portal]
url = "https://gis.agency.gov.au/portal"
trusted_hosts = ["gis.agency.gov.au", "maps.agency.gov.au"]   # hosts that may receive the user's token
allowed_org_ids = ["AbCdEfGh12345678"]                         # optional: who may sign in

[policy]
allow_public_geocoder = false
confirm_batches_over = 1      # 0 = confirm every geocode

[[locators]]
name = "agency-addresses"
url = "https://gis.agency.gov.au/server/rest/services/Locators/Addresses/GeocodeServer"
kind = "customer"             # customer | org | public
version = "G-NAF 2026-11"     # goes into every feature's resolver.version

[[gazetteers]]                # any layer with one feature per named place
name = "hydro"
url = "https://gis.agency.gov.au/server/rest/services/Base/Hydro/FeatureServer/1"
name_field = "NAME"
type_field = "FEATURE_TYPE"

[[boundaries]]
name = "depot-zones"
url = "https://gis.agency.gov.au/server/rest/services/Ops/Zones/FeatureServer/0"
code_field = "ZONE_NO"
name_field = "ZONE_NAME"
```

The config is checked at start-up and fails with a reason: an unknown
setting, a non-HTTPS URL, a resolver that would send the user's token to a
host the portal does not trust, a public geocoder labelled as something else.

**Customer-locator-only** is the default posture: leave
`allow_public_geocoder = false` and list only your own locators. With an
Enterprise that proxies the World Geocoding Service through the portal, give
that URL `kind = "public"` yourself - the server can only recognise Esri's own
hostnames.

**Point gazetteers and size.** A point cannot say how big a lake is. Set
`default_uncertainty_m` and `type_uncertainty_m` per gazetteer; candidates
from them are flagged `default_uncertainty`. Where you have polygons, use
them: a polygon layer reports the feature's own extent.

## Security model

| Concern | What the server does |
|---|---|
| Token leaving the organisation | The user's ArcGIS token is attached only to hosts in `trusted_hosts`. The client refuses any other host even if asked. Public services are configured `auth = "none"` and never see a token (tested). |
| Tokens in logs | Every ArcGIS call is a POST with the token in the body, never the URL. Request parameters - place strings from documents - are not logged. |
| Stolen server state | The server's own tokens are stored only as SHA-256. ArcGIS tokens in the SQLite store are AES-256-GCM encrypted under a key derived from `SPATIALISE_SECRET`, each row bound to its key. |
| Token for another service | Tokens are issued for this server's `/mcp` only (RFC 8707) and checked on every request. |
| Sign-in hijack | PKCE on both legs, single-use state and codes, redirect URIs checked against the registered client. |
| A hostile client riding a user's existing ArcGIS session | Clients self-register, and a portal skips its own consent for an app already approved - so this server shows its own consent page before every hand-off. Approval is tied to the user's browser by a cookie another site cannot read or set, and the page cannot be framed (tested against forged and replayed approvals). |
| Wrong tenant | Optional `allowed_org_ids`: other organisations' members are refused at sign-in. |
| Unreported spend or disclosure | Confirmation tokens: HMAC over the action, the caller and a digest of the arguments, 15-minute life. |
| Injection | Field names come only from the operator's config. Caller text reaches SQL only as an escaped literal; numeric codes are validated as numbers. |
| Writing where it should not | Publish targets must be on a trusted host and carry this tool's schema. New items are private to the user; the server never changes sharing. |
| Shared-token mode exposed | `auth_mode = static` refuses to serve on a non-loopback address. |

Not built: rate limiting (put it at your proxy), audit logging of tool calls,
and multi-instance state.

## Verifying against your organisation

The tests cannot stand in for your portal. This does, in one command, as you:

```bash
export ARCGIS_TOKEN=<a token for your account>
python server/scripts/live_smoke.py --config my-config.toml \
    --address "1 Treasury Place, East Melbourne VIC" --address "200 Spring St, Melbourne VIC" \
    --publish "Spatialise smoke test"
```

It lists your resolvers and whether each answers, geocodes the addresses
through your locator (asking first, with the credit estimate), and - only
with `--publish`, and only after showing you the plan - creates one private
hosted feature layer with a point per address, a line, a polygon and an
unplaced row. Delete the layer afterwards. That run, plus one sign-in through
Claude, is the phase 3 exit test.

Things most likely to differ on a real portal, in order: the layer definition
accepted by `addToDefinition`; the single-line field name of a custom locator
(read from the locator, so it should adapt); a hosting server on a host
missing from `trusted_hosts` (reported plainly, with the fix).

## Development

```bash
pip install -e "server[dev]"
python -m pytest server/tests                       # 108 tests, no network
SPATIALISE_LIVE=1 python -m pytest server/tests/test_live.py   # read-only, real public services
```

`tests/fake_arcgis.py` is an in-process portal, locator and feature service
whose response shapes were checked against live services. The output contract
is validated with the skill's own code, vendored into
`spatialise_mcp/_contract/` by `tools/sync_contract.py`; a test fails if the
copies drift.

## Limitations

- No `trace_segment` yet, so "along the X between A and B" stays unresolved.
- Geometry passes through the conversation. Boundaries default to 300
  vertices; full-resolution polygons are available but costly.
- Published layers are EPSG:4326, with each feature's `source_crs` recorded.
  GDA2020 and WGS84 are treated as equal, as the contract does.
- A gazetteer should have one feature per place. A layer that splits a river
  into hundreds of segments returns hundreds of candidates.
