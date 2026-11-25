"""What the read-only tools do: report resolvers, geocode, search gazetteers,
look up boundaries.

The server makes no decisions. Every function here returns candidates with
the evidence a caller needs to choose - match score, feature type, a
positional uncertainty and where it came from, distance to a hint point -
and never picks one. Each candidate carries its ``resolver`` and ``method``
so it can be dropped straight into the output contract.
"""

from __future__ import annotations

import math
import time
from typing import Any, Awaitable, Callable

import anyio

from . import confirm, esri
from .arcgis import ArcGIS
from .config import Boundary, Config, Gazetteer, Locator, host_of
from .errors import ArcGISError, ConfigError, PolicyError, SignInRequired, SpatialiseError

CONCURRENCY = 4
INFO_TTL_S = 600
_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# Positional uncertainty (metres) by locator match type, where the type says how
# the point was placed. Area-like types use the size of the match's extent instead.
_ADDR_UNCERTAINTY_M = {
    "Subaddress": 15, "PointAddress": 15, "Parcel": 30, "StreetAddress": 50, "StreetAddressExt": 100,
    "StreetInt": 30, "StreetMidBlock": 150, "StreetBetween": 150, "DistanceMarker": 100,
    "LatLong": 5, "XY": 5, "YX": 5, "MGRS": 100, "USNG": 100, "POI": 100,
}
_KEEP_ATTRS = ("Type", "PlaceName", "City", "Subregion", "Region", "RegionAbbr", "Postal", "Country", "Status")
_OFFSET_LADDER_DEG = (0.0005, 0.002, 0.008, 0.03)
_NUMERIC_FIELD_TYPES = {"esriFieldTypeInteger", "esriFieldTypeSmallInteger", "esriFieldTypeDouble",
                        "esriFieldTypeSingle", "esriFieldTypeOID", "esriFieldTypeBigInteger"}


def clear_cache() -> None:
    _info_cache.clear()


async def gather(items: list[Any], fn: Callable[[Any], Awaitable[Any]]) -> list[Any]:
    """Run fn over items with bounded concurrency, keeping order."""
    out: list[Any] = [None] * len(items)
    limiter = anyio.CapacityLimiter(CONCURRENCY)

    async def one(i: int, item: Any) -> None:
        async with limiter:
            out[i] = await fn(item)

    try:
        async with anyio.create_task_group() as tg:
            for i, item in enumerate(items):
                tg.start_soon(one, i, item)
    except BaseExceptionGroup as group:
        # A task group wraps what its tasks raise. Callers want the real error:
        # the first one we raised on purpose, else whatever came first.
        leaves = _leaves(group)
        raise next((e for e in leaves if isinstance(e, SpatialiseError)), leaves[0]) from None
    return out


def _leaves(group: BaseExceptionGroup) -> list[BaseException]:
    out: list[BaseException] = []
    for e in group.exceptions:
        out += _leaves(e) if isinstance(e, BaseExceptionGroup) else [e]
    return out


def _check_point(p: Any, what: str) -> list[float]:
    if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)):
        raise SpatialiseError(f"{what} must be [longitude, latitude]")
    lon, lat = float(p[0]), float(p[1])
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise SpatialiseError(f"{what} {p} is outside longitude/latitude range - is it latitude first?")
    return [lon, lat]


def _check_bbox(b: Any) -> list[float]:
    if not (isinstance(b, (list, tuple)) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)):
        raise SpatialiseError("bbox must be [min_lon, min_lat, max_lon, max_lat]")
    if not (b[0] < b[2] and b[1] < b[3]):
        raise SpatialiseError("bbox must be [min_lon, min_lat, max_lon, max_lat] with min below max")
    return [float(v) for v in b]


def _check_texts(values: Any, what: str, max_items: int, max_len: int = 500) -> list[str]:
    if not isinstance(values, list) or not values:
        raise SpatialiseError(f"{what} must be a non-empty list")
    if len(values) > max_items:
        raise PolicyError(f"{len(values)} {what} in one call is over this server's limit of {max_items}. "
                          "Split the batch.")
    out = []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            raise SpatialiseError(f"every entry in {what} must be non-empty text")
        if len(v) > max_len:
            raise SpatialiseError(f"an entry in {what} is longer than {max_len} characters")
        out.append(v.strip())
    return out


class Resolvers:
    def __init__(self, cfg: Config, arcgis: ArcGIS):
        self.cfg = cfg
        self.arcgis = arcgis

    # ------------------------------------------------------------ service info

    async def info(self, url: str, auth: bool) -> dict[str, Any]:
        hit = _info_cache.get(url)
        if hit and hit[0] > time.time():
            return hit[1]
        data = await self.arcgis.call(url, {}, auth=auth)
        _info_cache[url] = (time.time() + INFO_TTL_S, data)
        return data

    def _resolver(self, name: str, version: str, info: dict[str, Any] | None = None) -> dict[str, str]:
        out = {"name": name}
        if version:
            out["version"] = version
        elif info and info.get("currentVersion"):
            out["version"] = f"ArcGIS REST {info['currentVersion']}"
        return out

    # ------------------------------------------------------------ list_resolvers

    async def list_resolvers(self, check: bool = False) -> dict[str, Any]:
        cfg = self.cfg
        default = None
        try:
            default = cfg.locator(None).name
        except SpatialiseError:
            pass
        locators = []
        for loc in cfg.ordered_locators():
            reason = cfg.locator_block_reason(loc)
            locators.append({
                "name": loc.name, "kind": loc.kind, "host": host_of(loc.url), "allowed": reason is None,
                **({"blocked_reason": reason} if reason else {}),
                "default": loc.name == default, "billed": loc.billed,
                **({"credits_per_1000": loc.credits_per_1000} if loc.billed else {}),
                "stored_results": loc.for_storage, "needs_sign_in": loc.auth == "user",
                **({"version": loc.version} if loc.version else {}),
                **({"description": loc.description} if loc.description else {}),
                "_url": loc.url, "_auth": loc.auth == "user",
            })
        gazetteers = [{
            "name": g.name, "host": host_of(g.url), "needs_sign_in": g.auth == "user",
            "filters": [f for f, on in (("feature_types", g.type_field), ("state", g.state_field)) if on],
            **({"version": g.version} if g.version else {}),
            **({"description": g.description} if g.description else {}),
            "_url": g.url, "_auth": g.auth == "user",
        } for g in cfg.gazetteers]
        boundaries = [{
            "name": b.name, "host": host_of(b.url), "needs_sign_in": b.auth == "user",
            "lookup_by": [k for k, on in (("code", b.code_field), ("name", b.name_field)) if on],
            **({"version": b.version} if b.version else {}),
            **({"description": b.description} if b.description else {}),
            "_url": b.url, "_auth": b.auth == "user",
        } for b in cfg.boundaries]
        entries = locators + gazetteers + boundaries
        if check:
            async def probe(e: dict[str, Any]) -> None:
                if e.get("allowed") is False:
                    e["status"] = "not checked (blocked by policy)"
                    return
                try:
                    await self.info(e["_url"], e["_auth"])
                    e["status"] = "ok"
                except SignInRequired:
                    e["status"] = "needs sign-in or permission"
                except SpatialiseError as err:
                    e["status"] = f"unavailable: {err}"
            await gather(entries, probe)
        for e in entries:
            e.pop("_url")
            e.pop("_auth")
        signed_in = self.arcgis.identity.username
        publish_ok = cfg.policy.allow_publish and cfg.auth_mode != "none"
        return {
            "signed_in_as": signed_in,
            "auth_mode": cfg.auth_mode,
            "portal": host_of(cfg.portal.url),
            "policy": {
                "resolver_order": "customer locators first, then the organisation's, then public geocoders",
                "public_geocoder_allowed": cfg.policy.allow_public_geocoder,
                "confirm_batches_over": cfg.policy.confirm_batches_over,
                "max_batch": cfg.policy.max_batch,
                "default_country": cfg.policy.default_country,
            },
            "locators": locators,
            "gazetteers": gazetteers,
            "boundaries": boundaries,
            "publish": {
                "enabled": publish_ok,
                **({} if publish_ok else {"reason": "publishing is disabled by server policy" if not
                                          cfg.policy.allow_publish else "publishing needs an ArcGIS sign-in"}),
                "max_features": cfg.policy.max_publish_features,
            },
            "not_available": [t for t, missing in (
                ("geocode / reverse_geocode", not any(e["allowed"] for e in locators)),
                ("search_gazetteer", not gazetteers), ("lookup_boundary", not boundaries)) if missing],
        }

    # ------------------------------------------------------------ confirmation

    def _confirm(self, action: str, count: int, args: dict[str, Any], token: str | None,
                 loc: Locator, unit: str) -> dict[str, Any] | None:
        """Return a dry-run report when confirmation is needed and absent; None to proceed."""
        if count <= self.cfg.policy.confirm_batches_over:
            return None
        subject = self.arcgis.identity.username
        if token:
            problem = confirm.verify(self.cfg.secret, token, action, subject, args)
            if problem:
                raise PolicyError(f"{problem}. Call {action} again without confirm_token to get a fresh count.")
            return None
        credits = round(count * loc.credits_per_1000 / 1000, 3)
        cost = (f"about {credits} ArcGIS credits (stored geocodes at {loc.credits_per_1000:g} per 1,000)"
                if loc.billed else "no credits (not a billed locator)")
        return {
            "status": "confirmation_required",
            "count": count,
            "resolver": {"name": loc.name, "kind": loc.kind},
            "sends_text_to": host_of(loc.url),
            "stored_results": loc.for_storage,
            "estimated_credits": credits,
            "confirm_token": confirm.issue(self.cfg.secret, action, subject, args),
            "message": (f"Nothing has been sent yet. This would send {count} {unit} to {host_of(loc.url)} "
                        f"({loc.kind} locator {loc.name!r}) and use {cost}. Tell the user that, and if they "
                        f"agree call {action} again with the same arguments plus this confirm_token."),
        }

    # ------------------------------------------------------------ geocode

    async def geocode(self, queries: list[str], resolver: str | None = None, max_candidates: int = 5,
                      country: str | None = None, near: list[float] | None = None,
                      bbox: list[float] | None = None, confirm_token: str | None = None) -> dict[str, Any]:
        cfg = self.cfg
        loc = cfg.locator(resolver)
        queries = _check_texts(queries, "queries", cfg.policy.max_batch)
        max_candidates = max(1, min(int(max_candidates), cfg.policy.max_candidates))
        near = _check_point(near, "near") if near is not None else None
        bbox = _check_bbox(bbox) if bbox is not None else None
        country = cfg.policy.default_country if country is None else country
        args = {"queries": queries, "resolver": loc.name, "max_candidates": max_candidates, "country": country,
                "near": near, "bbox": bbox}
        dry = self._confirm("geocode", len(queries), args, confirm_token, loc, "place strings")
        if dry:
            return dry

        auth = loc.auth == "user"
        info = await self.info(loc.url, auth)
        if "geocode" not in str(info.get("capabilities", "Geocode")).lower():
            raise ConfigError(f"locator {loc.name!r} does not offer geocoding")
        single = (info.get("singleLineAddressField") or {}).get("name")
        if not single:
            raise ConfigError(f"locator {loc.name!r} has no single-line address field, so free text cannot be "
                              "geocoded with it")
        res = self._resolver(loc.name, loc.version, info)
        base: dict[str, Any] = {"outFields": "*", "maxLocations": max_candidates, "outSR": 4326}
        if loc.for_storage:
            base["forStorage"] = True
        if country:
            base["sourceCountry"] = country
        if near:
            base["location"] = f"{near[0]},{near[1]}"
        if bbox:
            base["searchExtent"] = ",".join(str(v) for v in bbox)

        async def one(item: tuple[int, str]) -> dict[str, Any]:
            i, text = item
            try:
                data = await self.arcgis.call(loc.url + "/findAddressCandidates", {**base, single: text}, auth=auth)
            except SignInRequired:
                raise
            except SpatialiseError as e:
                return {"index": i, "query": text, "status": "error", "error": str(e), "candidates": []}
            cands = [c for c in (self._locator_candidate(c, res, near) for c in data.get("candidates", [])) if c]
            return {"index": i, "query": text, "status": "matched" if cands else "no_match", "candidates": cands}

        results = await gather(list(enumerate(queries)), one)
        errors = [r for r in results if r["status"] == "error"]
        if errors and len(errors) == len(results):
            raise SpatialiseError(f"every geocode failed. First error: {errors[0]['error']}")
        out: dict[str, Any] = {"status": "ok", "resolver": res, "geocodes_used": len(queries) - len(errors),
                               "stored_results": loc.for_storage, "results": results}
        if loc.billed:
            out["estimated_credits"] = round(out["geocodes_used"] * loc.credits_per_1000 / 1000, 3)
        if any(r["status"] == "no_match" for r in results):
            others = [x.name for x in cfg.ordered_locators() if x.name != loc.name and
                      cfg.locator_block_reason(x) is None]
            out["no_match_hint"] = ("Unmatched queries stay unresolved unless you try another resolver"
                                    + (f": {others}." if others else "; none other is allowed on this server."))
        return out

    def _locator_candidate(self, c: dict[str, Any], res: dict[str, str], near: list[float] | None) -> dict[str, Any] | None:
        loc = c.get("location") or {}
        try:
            x, y = float(loc["x"]), float(loc["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if math.isnan(x) or math.isnan(y):
            return None
        attrs = c.get("attributes") or {}
        kind = str(attrs.get("Addr_type") or "")
        extent = c.get("extent")
        if kind in _ADDR_UNCERTAINTY_M:
            unc, basis = float(_ADDR_UNCERTAINTY_M[kind]), f"typical for a {kind} match"
        else:
            half = esri.half_diagonal_m(extent)
            if half:
                unc, basis = max(100.0, half), "half the diagonal of the match extent"
            else:
                unc, basis = 1000.0, "default (the locator gave no match type or extent)"
        out: dict[str, Any] = {
            "label": str(c.get("address") or attrs.get("Match_addr") or attrs.get("LongLabel") or ""),
            "kind": kind or None,
            "point": [round(x, 7), round(y, 7)],
            "score": round(float(c.get("score") or 0) / 100, 3),
            "uncertainty_m": round(unc),
            "uncertainty_basis": basis,
            "method": "locator",
            "resolver": res,
        }
        extra = {k: attrs[k] for k in _KEEP_ATTRS if attrs.get(k) not in (None, "")}
        if extra:
            out["attributes"] = extra
        if near:
            out["distance_km"] = round(esri.haversine_m(near[0], near[1], x, y) / 1000, 1)
        return out

    # ------------------------------------------------------------ reverse_geocode

    async def reverse_geocode(self, points: list[list[float]], resolver: str | None = None,
                              confirm_token: str | None = None) -> dict[str, Any]:
        cfg = self.cfg
        loc = cfg.locator(resolver)
        if not isinstance(points, list) or not points:
            raise SpatialiseError("points must be a non-empty list of [longitude, latitude]")
        if len(points) > cfg.policy.max_batch:
            raise PolicyError(f"{len(points)} points in one call is over this server's limit of "
                              f"{cfg.policy.max_batch}. Split the batch.")
        pts = [_check_point(p, "each point") for p in points]
        args = {"points": pts, "resolver": loc.name}
        dry = self._confirm("reverse_geocode", len(pts), args, confirm_token, loc, "coordinates")
        if dry:
            return dry
        auth = loc.auth == "user"
        info = await self.info(loc.url, auth)
        if "reversegeocode" not in str(info.get("capabilities", "")).lower():
            raise ConfigError(f"locator {loc.name!r} does not offer reverse geocoding")
        res = self._resolver(loc.name, loc.version, info)

        async def one(item: tuple[int, list[float]]) -> dict[str, Any]:
            i, (lon, lat) = item
            params = {"location": {"x": lon, "y": lat, "spatialReference": esri.WGS84}, "outSR": 4326}
            if loc.for_storage:
                params["forStorage"] = True
            try:
                data = await self.arcgis.call(loc.url + "/reverseGeocode", params, auth=auth)
            except SignInRequired:
                raise
            except ArcGISError as e:
                if e.code == 400:  # "Unable to find address for the specified location"
                    return {"index": i, "point": [lon, lat], "status": "no_match"}
                return {"index": i, "point": [lon, lat], "status": "error", "error": str(e)}
            except SpatialiseError as e:
                return {"index": i, "point": [lon, lat], "status": "error", "error": str(e)}
            addr = data.get("address") or {}
            label = addr.get("LongLabel") or addr.get("Match_addr") or addr.get("ShortLabel")
            if not label:
                return {"index": i, "point": [lon, lat], "status": "no_match"}
            out: dict[str, Any] = {"index": i, "point": [lon, lat], "status": "matched", "label": str(label),
                                   "kind": addr.get("Addr_type") or None, "method": "locator", "resolver": res}
            at = data.get("location") or {}
            if "x" in at and "y" in at:
                out["matched_point"] = [round(float(at["x"]), 7), round(float(at["y"]), 7)]
                out["distance_m"] = round(esri.haversine_m(lon, lat, float(at["x"]), float(at["y"])))
            extra = {k: addr[k] for k in _KEEP_ATTRS if addr.get(k) not in (None, "")}
            if extra:
                out["attributes"] = extra
            return out

        results = await gather(list(enumerate(pts)), one)
        errors = [r for r in results if r["status"] == "error"]
        if errors and len(errors) == len(results):
            raise SpatialiseError(f"every reverse geocode failed. First error: {errors[0]['error']}")
        return {"status": "ok", "resolver": res, "geocodes_used": len(pts) - len(errors),
                "stored_results": loc.for_storage, "results": results}

    # ------------------------------------------------------------ layers

    async def _layer(self, url: str, auth: bool, what: str, fields: list[str]) -> tuple[dict[str, Any], dict[str, str]]:
        """Layer info, plus the configured field names resolved to the layer's
        own spelling. A wrong field name is an operator error, reported clearly."""
        info = await self.info(url, auth)
        have = {f["name"].lower(): f for f in info.get("fields") or []}
        if not have:
            raise ConfigError(f"{what}: {url} is not a queryable layer - point the URL at a layer "
                              "(…/FeatureServer/0), not a service")
        resolved = {}
        for f in fields:
            if f.lower() not in have:
                raise ConfigError(f"{what}: field {f!r} is not in the layer. Its fields are "
                                  f"{sorted(x['name'] for x in have.values())[:40]}")
            resolved[f] = have[f.lower()]["name"]
        return info, resolved

    @staticmethod
    def _is_numeric(info: dict[str, Any], field: str) -> bool:
        for f in info.get("fields") or []:
            if f["name"] == field:
                return f.get("type") in _NUMERIC_FIELD_TYPES
        return False

    async def _query(self, url: str, auth: bool, info: dict[str, Any], where: str, fields: list[str],
                     limit: int, geometry: bool, offset_deg: float | None, bbox: list[float] | None = None) -> tuple[list[dict], bool]:
        params: dict[str, Any] = {"where": where, "outFields": ",".join(dict.fromkeys(fields)),
                                  "returnGeometry": geometry, "outSR": 4326}
        if geometry:
            params["geometryPrecision"] = 6
            if offset_deg:
                params["maxAllowableOffset"] = offset_deg
        if bbox:
            params.update(geometry=",".join(str(v) for v in bbox), geometryType="esriGeometryEnvelope", inSR=4326,
                          spatialRel="esriSpatialRelIntersects")
        paged = bool((info.get("advancedQueryCapabilities") or {}).get("supportsPagination"))
        if paged:
            params["resultRecordCount"] = limit + 1
        data = await self.arcgis.call(url + "/query", params, auth=auth)
        feats = data.get("features") or []
        more = bool(data.get("exceededTransferLimit")) or len(feats) > limit
        return feats[:limit], more

    # ------------------------------------------------------------ search_gazetteer

    async def search_gazetteer(self, names: list[str], gazetteer: str | None = None,
                               feature_types: list[str] | None = None, state: str | None = None,
                               near: list[float] | None = None, bbox: list[float] | None = None,
                               match: str = "exact", limit: int = 10, geometry: str = "point",
                               max_vertices: int = 300) -> dict[str, Any]:
        cfg = self.cfg
        gazetteers = [cfg.gazetteer(gazetteer)] if gazetteer else list(cfg.gazetteers)
        if not gazetteers:
            raise ConfigError("no gazetteer is configured on this server. Place names that the offline gazetteer "
                              "does not hold stay unresolved with the needs_network flag.")
        names = _check_texts(names, "names", 50, 200)
        if match not in ("exact", "prefix", "contains"):
            raise SpatialiseError("match must be exact, prefix or contains")
        if geometry not in ("point", "simplified", "full"):
            raise SpatialiseError("geometry must be point, simplified or full")
        limit = max(1, min(int(limit), 50))
        max_vertices = max(20, min(int(max_vertices), 5000))
        near = _check_point(near, "near") if near is not None else None
        bbox = _check_bbox(bbox) if bbox is not None else None
        types = _check_texts(feature_types, "feature_types", 30, 100) if feature_types else None

        async def one(item: tuple[str, Gazetteer]) -> tuple[list[dict[str, Any]], bool]:
            name, g = item
            return await self._search_one(g, name, types, state, near, bbox, match, limit, geometry, max_vertices)

        pairs = [(n, g) for n in names for g in gazetteers]
        found = await gather(pairs, one)
        results = []
        for n in names:
            cands: list[dict[str, Any]] = []
            more = False
            for (pn, _), (cs, m) in zip(pairs, found):
                if pn == n:
                    cands += cs
                    more = more or m
            cands.sort(key=lambda c: (not c["exact"], c.get("distance_km", 0.0)))
            entry: dict[str, Any] = {"name": n, "count": len(cands[:limit]), "candidates": cands[:limit]}
            if more or len(cands) > limit:
                entry["truncated"] = True
                entry["hint"] = "more matches exist: narrow with feature_types, state, near or bbox"
            results.append(entry)
        return {"status": "ok", "gazetteers": [g.name for g in gazetteers], "match": match, "results": results}

    async def _search_one(self, g: Gazetteer, name: str, types: list[str] | None, state: str | None,
                          near: list[float] | None, bbox: list[float] | None, match: str, limit: int,
                          geometry: str, max_vertices: int) -> tuple[list[dict[str, Any]], bool]:
        auth = g.auth == "user"
        what = f"gazetteer {g.name!r}"
        wanted = [g.name_field, *g.alt_name_fields, *g.extra_fields]
        wanted += [f for f in (g.type_field, g.state_field, g.id_field) if f]
        info, f = await self._layer(g.url, auth, what, wanted)
        upper = name.upper()
        if match == "exact":
            tests = [f"UPPER({f[x]}) = {esri.sql_text(upper)}" for x in (g.name_field, *g.alt_name_fields)]
        else:
            pat = esri.like_text(upper, prefix=match == "contains", suffix=True)
            tests = [f"UPPER({f[x]}) LIKE {pat}" for x in (g.name_field, *g.alt_name_fields)]
        where = "(" + " OR ".join(tests) + ")"
        if g.where:
            where += f" AND ({g.where})"
        if types:
            if not g.type_field:
                raise SpatialiseError(f"{what} has no feature type field, so feature_types cannot filter it")
            where += f" AND UPPER({f[g.type_field]}) IN ({', '.join(esri.sql_text(t.upper()) for t in types)})"
        if state and g.state_field:
            where += f" AND UPPER({f[g.state_field]}) = {esri.sql_text(state.upper())}"
        is_point = info.get("geometryType") == "esriGeometryPoint"
        offset = None if is_point or geometry == "full" else _OFFSET_LADDER_DEG[1]
        oid = info.get("objectIdField") or "OBJECTID"
        fields = list(f.values()) + ([oid] if oid.lower() in {x["name"].lower() for x in info["fields"]} else [])
        feats, more = await self._query(g.url, auth, info, where, fields, limit, True, offset, bbox)
        res = self._resolver(g.name, g.version, info)
        out = []
        for feat in feats:
            a = feat.get("attributes") or {}
            geom = esri.to_geojson(feat.get("geometry"))
            if geom is None:
                continue
            label = str(a.get(f[g.name_field]) or "")
            kind = str(a.get(f[g.type_field]) or "") if g.type_field else ""
            point = esri.representative_point(geom)
            cand: dict[str, Any] = {
                "id": f"{g.name}:{a.get(f[g.id_field]) if g.id_field else a.get(oid)}",
                "label": label,
                "kind": kind or None,
                "exact": label.upper() == upper or any(str(a.get(f[x]) or "").upper() == upper
                                                        for x in g.alt_name_fields),
                "point": [round(point[0], 7), round(point[1], 7)],
                "method": "gazetteer",
                "resolver": res,
            }
            if g.state_field and a.get(f[g.state_field]):
                cand["state"] = a[f[g.state_field]]
            if geom["type"] == "Point":
                cand["uncertainty_m"] = round(g.type_uncertainty_m.get(kind.upper(), g.default_uncertainty_m))
                cand["uncertainty_basis"] = ("configured default" + (f" for {kind}" if kind.upper() in
                                             g.type_uncertainty_m else "") + ": a point gazetteer cannot say how "
                                             "big the place is (flag: default_uncertainty)")
            else:
                box = esri.bbox(geom)
                cand["bbox"] = [round(v, 6) for v in box]
                cand["geometry_type"] = geom["type"]
                if geometry == "point":
                    cand["uncertainty_m"] = round(max(50.0, esri.half_diagonal_m(box) or 0))
                    cand["uncertainty_basis"] = "half the diagonal of the feature's extent (point returned for a " \
                                                "line or area; ask for geometry='simplified' to get its shape)"
                else:
                    n = esri.vertex_count(geom)
                    if n > max_vertices:
                        cand["geometry_omitted"] = (f"{n} vertices is over max_vertices={max_vertices}; raise "
                                                    "max_vertices or use the point")
                        cand["uncertainty_m"] = round(max(50.0, esri.half_diagonal_m(box) or 0))
                        cand["uncertainty_basis"] = "half the diagonal of the feature's extent"
                    else:
                        cand["geometry"] = esri.round_geometry(geom)
                        tol = round((offset or 0) * 111_000)
                        cand["uncertainty_m"] = tol
                        cand["uncertainty_basis"] = (f"simplified to about {tol} m" if tol else
                                                     "full geometry as served")
            extra = {x: a[f[x]] for x in g.extra_fields if a.get(f[x]) not in (None, "")}
            if extra:
                cand["attributes"] = extra
            if near:
                cand["distance_km"] = round(esri.haversine_m(near[0], near[1], point[0], point[1]) / 1000, 1)
            out.append(cand)
        return out, more

    # ------------------------------------------------------------ lookup_boundary

    async def lookup_boundary(self, boundary: str, codes: list[str] | None = None, names: list[str] | None = None,
                              state: str | None = None, geometry: str = "simplified", max_vertices: int = 300,
                              limit: int = 5) -> dict[str, Any]:
        b: Boundary = self.cfg.boundary(boundary)
        if not codes and not names:
            raise SpatialiseError("give codes, names or both")
        if geometry not in ("simplified", "full", "point", "none"):
            raise SpatialiseError("geometry must be simplified, full, point or none")
        codes = _check_texts(codes, "codes", 50, 60) if codes else []
        names = _check_texts(names, "names", 50, 200) if names else []
        if codes and not b.code_field:
            raise SpatialiseError(f"boundary set {b.name!r} is looked up by name only")
        if names and not b.name_field:
            raise SpatialiseError(f"boundary set {b.name!r} is looked up by code only")
        limit = max(1, min(int(limit), 20))
        max_vertices = max(20, min(int(max_vertices), 20000))
        auth = b.auth == "user"
        what = f"boundary set {b.name!r}"
        wanted = [x for x in (b.code_field, b.name_field, b.state_field, *b.extra_fields) if x]
        url = b.generalised_url if (b.generalised_url and geometry in ("simplified", "point")) else b.url
        info, f = await self._layer(url, auth, what, wanted)

        clauses = []
        if codes:
            if self._is_numeric(info, f[b.code_field]):
                for c in codes:
                    if not c.lstrip("-").isdigit():
                        raise SpatialiseError(f"{what}: code {c!r} is not a number, and its code field is numeric")
                clauses.append(f"{f[b.code_field]} IN ({', '.join(codes)})")
            else:
                clauses.append(f"{f[b.code_field]} IN ({', '.join(esri.sql_text(c) for c in codes)})")
        if names:
            in_list = ", ".join(esri.sql_text(n.upper()) for n in names)
            clause = f"UPPER({f[b.name_field]}) IN ({in_list})"
            if state and b.state_field:
                clause = f"({clause} AND UPPER({f[b.state_field]}) = {esri.sql_text(state.upper())})"
            clauses.append(clause)
        where = " OR ".join(clauses)
        want_geom = geometry != "none"
        cap = (len(codes) + len(names)) * limit
        ladder: tuple[float | None, ...] = (None,) if geometry == "full" else _OFFSET_LADDER_DEG
        if geometry == "point":
            ladder = (_OFFSET_LADDER_DEG[1],)
        feats: list[dict] = []
        more = False
        offset: float | None = None
        geoms: list[dict[str, Any] | None] = []
        for offset in ladder if want_geom else (None,):
            feats, more = await self._query(url, auth, info, where, list(f.values()), cap, want_geom, offset)
            geoms = [esri.to_geojson(x.get("geometry")) for x in feats]
            if geometry != "simplified" or all(esri.vertex_count(g) <= max_vertices for g in geoms):
                break
        res = self._resolver(b.name, b.version, info)
        tol_m = round((offset or 0) * 111_000)

        cands = []
        for feat, geom in zip(feats, geoms or [None] * len(feats)):
            a = feat.get("attributes") or {}
            code = str(a.get(f[b.code_field])) if b.code_field and a.get(f[b.code_field]) is not None else None
            name = str(a.get(f[b.name_field])) if b.name_field and a.get(f[b.name_field]) is not None else None
            cand: dict[str, Any] = {"id": f"{b.name}:{code or name}", "label": name or code, "kind": b.name,
                                    "method": "boundary_join", "resolver": res}
            if code:
                cand["code"] = code
            if name:
                cand["name"] = name
            if b.state_field and a.get(f[b.state_field]):
                cand["state"] = a[f[b.state_field]]
            if geom is not None:
                point = esri.representative_point(geom)
                cand["point"] = [round(point[0], 6), round(point[1], 6)]
                cand["bbox"] = [round(v, 6) for v in esri.bbox(geom)]
                area = esri.area_km2(geom)
                if area is not None:
                    cand["area_km2"] = round(area, 2)
                    cand["radius_m"] = round(math.sqrt(area / math.pi) * 1000)
                n = esri.vertex_count(geom)
                if geometry == "point":
                    cand["uncertainty_m"] = cand.get("radius_m") or round(esri.half_diagonal_m(cand["bbox"]) or 0)
                    cand["uncertainty_basis"] = "radius of a circle of the same area (point returned for an area)"
                elif geometry == "full" and n > max_vertices:
                    cand["geometry_omitted"] = (f"{n} vertices is over max_vertices={max_vertices}; use "
                                                "geometry='simplified' or raise max_vertices")
                else:
                    cand["geometry"] = esri.round_geometry(geom)
                    cand["vertices"] = n
                    cand["uncertainty_m"] = tol_m
                    cand["uncertainty_basis"] = (f"boundary simplified to about {tol_m} m" if tol_m else
                                                 "boundary as served")
                    if geometry == "simplified" and n > max_vertices:
                        cand["over_vertex_budget"] = True
            elif want_geom:
                cand["geometry_omitted"] = "the layer returned no geometry for this feature"
            extra = {x: a[f[x]] for x in b.extra_fields if a.get(f[x]) not in (None, "")}
            if extra:
                cand["attributes"] = extra
            cands.append(cand)

        results = []
        for c in codes:
            hits = [x for x in cands if x.get("code") == c]
            results.append({"query": c, "by": "code", "count": len(hits[:limit]), "candidates": hits[:limit]})
        for n_ in names:
            hits = [x for x in cands if (x.get("name") or "").upper() == n_.upper()]
            results.append({"query": n_, "by": "name", "count": len(hits[:limit]), "candidates": hits[:limit]})
        out: dict[str, Any] = {"status": "ok", "boundary": b.name, "resolver": res, "results": results}
        if more:
            out["truncated"] = True
        if any(r["count"] == 0 for r in results):
            out["no_match_hint"] = ("A query with no candidates stays unresolved - check the code or spelling, or "
                                    "that this boundary set is the right one (list_resolvers).")
        return out
