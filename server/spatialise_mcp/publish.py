"""publish_features: contract features into a hosted feature layer.

Publish only on explicit approval, and only to a layer the user names. The
first call validates and reports a plan; nothing is written until the plan's
confirmation token comes back.

A feature layer holds one geometry type, and the contract has three plus
"none", so a published service has three layers and a table:

    0 points   1 lines   2 polygons   3 unplaced (table)

The table matters: ambiguous and unresolved mentions have no geometry, and
"nothing is dropped" has to survive publishing. A reviewer opening the service
sees every mention, placed or not.

New items are private to the signed-in user. Sharing is theirs to change in
the portal; this tool never shares anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from . import confirm, esri
from ._contract import contract, validate_features
from .arcgis import ArcGIS
from .config import Config, host_of
from .errors import ArcGISError, PolicyError, SignInRequired, SpatialiseError

MARKER = f"spatialise-contract {contract.CONTRACT_VERSION}"
CANDIDATES_LEN = 16000
CHUNK_FEATURES = 250
CHUNK_BYTES = 900_000

# name, esri type, length (strings), alias
FIELDS: list[tuple[str, str, int | None, str]] = [
    ("label", "esriFieldTypeString", 500, "Label"),
    ("mention", "esriFieldTypeString", 2000, "Mention"),
    ("status", "esriFieldTypeString", 20, "Status"),
    ("confidence", "esriFieldTypeDouble", None, "Confidence"),
    ("uncertainty_m", "esriFieldTypeDouble", None, "Uncertainty (m)"),
    ("tier", "esriFieldTypeString", 20, "Tier"),
    ("method", "esriFieldTypeString", 50, "Method"),
    ("resolver_name", "esriFieldTypeString", 255, "Resolver"),
    ("resolver_version", "esriFieldTypeString", 100, "Resolver version"),
    ("source_doc", "esriFieldTypeString", 500, "Source document"),
    ("source_page", "esriFieldTypeInteger", None, "Source page"),
    ("source_sheet", "esriFieldTypeString", 255, "Source sheet"),
    ("source_row", "esriFieldTypeInteger", None, "Source row"),
    ("source_column", "esriFieldTypeString", 255, "Source column"),
    ("char_start", "esriFieldTypeInteger", None, "Character start"),
    ("char_end", "esriFieldTypeInteger", None, "Character end"),
    ("source_crs", "esriFieldTypeString", 100, "Source CRS"),
    ("flags", "esriFieldTypeString", 500, "Flags"),
    ("candidate_count", "esriFieldTypeInteger", None, "Candidates considered"),
    ("candidates_json", "esriFieldTypeString", CANDIDATES_LEN, "Candidates (JSON)"),
    ("run_id", "esriFieldTypeString", 40, "Publish run"),
    ("published_at", "esriFieldTypeDate", None, "Published"),
]
REQUIRED_FIELDS = ("mention", "status", "confidence", "tier", "method", "candidates_json", "run_id")
LAYERS = (("points", "esriGeometryPoint"), ("lines", "esriGeometryPolyline"), ("polygons", "esriGeometryPolygon"))
TABLE = "unplaced"
_FAMILY = {"Point": "points", "MultiPoint": "points", "LineString": "lines", "MultiLineString": "lines",
           "Polygon": "polygons", "MultiPolygon": "polygons"}
_GEOM_OF = dict(LAYERS)
_STR_LEN = {name: length for name, t, length, _ in FIELDS if length}


def service_name(title: str) -> str:
    """ArcGIS service names allow letters, digits and underscores only."""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", title.strip()).strip("_")
    if not name or not name[0].isalpha():
        name = "Spatialise_" + name
    return name[:90].rstrip("_")


def _clip(name: str, value: Any) -> Any:
    if isinstance(value, str) and name in _STR_LEN and len(value) > _STR_LEN[name]:
        return value[: _STR_LEN[name] - 1] + "…"
    return value


def _candidates_json(cands: list[dict[str, Any]]) -> tuple[str, bool]:
    """Candidates as JSON that fits the field. Geometry other than points is
    dropped first, then trailing candidates; the count field keeps the truth."""
    def dump(cs: list[dict[str, Any]]) -> str:
        return json.dumps(cs, ensure_ascii=False, separators=(",", ":"))
    text = dump(cands)
    if len(text) <= CANDIDATES_LEN:
        return text, False
    slim = []
    for c in cands:
        c = dict(c)
        if isinstance(c.get("geometry"), dict) and c["geometry"].get("type") != "Point":
            c.pop("geometry")
        slim.append(c)
    while slim and len(dump(slim)) > CANDIDATES_LEN:
        slim.pop()
    return dump(slim), True


def to_rows(features: list[dict[str, Any]], run_id: str, now_ms: int) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Split contract features into Esri rows per target layer."""
    rows: dict[str, list[dict[str, Any]]] = {"points": [], "lines": [], "polygons": [], TABLE: []}
    trimmed = 0
    for f in features:
        p = f["properties"]
        src = p.get("source") or {}
        res = p.get("resolver") or {}
        cands = p.get("candidates") or []
        cj, cut = _candidates_json(cands)
        trimmed += cut
        attrs = {
            "label": p.get("label"), "mention": p.get("mention"), "status": p.get("status"),
            "confidence": p.get("confidence"), "uncertainty_m": p.get("uncertainty_m"), "tier": p.get("tier"),
            "method": p.get("method"), "resolver_name": res.get("name"), "resolver_version": res.get("version"),
            "source_doc": src.get("doc"), "source_page": src.get("page"), "source_sheet": src.get("sheet"),
            "source_row": src.get("row"), "source_column": src.get("column"), "char_start": src.get("char_start"),
            "char_end": src.get("char_end"), "source_crs": p.get("source_crs"),
            "flags": ",".join(p.get("flags") or []) or None, "candidate_count": len(cands),
            "candidates_json": cj, "run_id": run_id, "published_at": now_ms,
        }
        attrs = {k: _clip(k, v) for k, v in attrs.items()}
        geom = f.get("geometry")
        if not geom:
            rows[TABLE].append({"attributes": attrs})
        elif geom["type"] == "MultiPoint":  # a point layer holds single points: one row per point
            for pt in geom["coordinates"]:
                rows["points"].append({"geometry": esri.to_esri({"type": "Point", "coordinates": pt}),
                                       "attributes": dict(attrs)})
        else:
            rows[_FAMILY[geom["type"]]].append({"geometry": esri.to_esri(geom), "attributes": attrs})
    return rows, trimmed


def _fields_json() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID",
                                  "sqlType": "sqlTypeOther", "nullable": False, "editable": False, "domain": None,
                                  "defaultValue": None}]
    for name, typ, length, alias in FIELDS:
        fld: dict[str, Any] = {"name": name, "type": typ, "alias": alias, "sqlType": "sqlTypeOther",
                               "nullable": True, "editable": True, "domain": None, "defaultValue": None}
        if length:
            fld["length"] = length
        out.append(fld)
    return out


def _definition(extent: dict[str, Any]) -> dict[str, Any]:
    common = {"displayField": "label", "description": MARKER, "hasAttachments": False,
              "htmlPopupType": "esriServerHTMLPopupTypeAsHTMLText", "objectIdField": "OBJECTID", "globalIdField": "",
              "typeIdField": "", "fields": _fields_json(), "indexes": [], "types": [], "templates": [],
              "supportedQueryFormats": "JSON", "hasStaticData": False, "maxRecordCount": 2000,
              "capabilities": "Query,Create,Update,Delete,Editing", "allowGeometryUpdates": True,
              "hasM": False, "hasZ": False}
    layers = [{**common, "id": i, "name": name, "type": "Feature Layer", "geometryType": geom, "extent": extent}
              for i, (name, geom) in enumerate(LAYERS)]
    table = {**common, "id": len(LAYERS), "name": TABLE, "type": "Table"}
    return {"layers": layers, "tables": [table]}


def _extent(features: list[dict[str, Any]]) -> dict[str, Any]:
    boxes = [esri.bbox(f["geometry"]) for f in features if f.get("geometry")]
    if boxes:
        box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        pad = max(0.01, (box[2] - box[0]) * 0.1, (box[3] - box[1]) * 0.1)
        box = [max(-180, box[0] - pad), max(-90, box[1] - pad), min(180, box[2] + pad), min(90, box[3] + pad)]
    else:
        box = [-180, -90, 180, 90]
    return {"xmin": box[0], "ymin": box[1], "xmax": box[2], "ymax": box[3], "spatialReference": dict(esri.WGS84)}


def _chunks(rows: list[dict[str, Any]]):
    batch: list[dict[str, Any]] = []
    size = 0
    for r in rows:
        n = len(json.dumps(r, separators=(",", ":")))
        if batch and (len(batch) >= CHUNK_FEATURES or size + n > CHUNK_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(r)
        size += n
    if batch:
        yield batch


class Publisher:
    def __init__(self, cfg: Config, arcgis: ArcGIS):
        self.cfg = cfg
        self.arcgis = arcgis

    async def publish(self, collection: dict[str, Any], create: str | None = None, url: str | None = None,
                      confirm_token: str | None = None) -> dict[str, Any]:
        cfg = self.cfg
        if not cfg.policy.allow_publish:
            raise PolicyError("publishing is disabled on this server (policy.allow_publish = false). "
                              "Give the user the GeoJSON file instead.")
        if cfg.auth_mode == "none":
            raise SignInRequired("publishing needs an ArcGIS sign-in, and this server runs without one.")
        if bool(create) == bool(url):
            raise SpatialiseError("name exactly one target: create='<new layer name>' for a new hosted feature "
                                  "layer, or url='<FeatureServer URL>' for one this tool created earlier. "
                                  "Ask the user which - never choose a target for them.")
        if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
            raise SpatialiseError("features must be the GeoJSON FeatureCollection the skill produced")
        features = collection.get("features") or []
        if not features:
            raise SpatialiseError("the FeatureCollection has no features: there is nothing to publish")
        if len(features) > cfg.policy.max_publish_features:
            raise PolicyError(f"{len(features)} features is over this server's limit of "
                              f"{cfg.policy.max_publish_features} per publish")
        threshold = (collection.get("spatialise") or {}).get("threshold", contract.DEFAULT_THRESHOLD)
        errors, _ = validate_features.validate(collection, threshold)
        if errors:
            shown = "; ".join(errors[:8]) + (f"; and {len(errors) - 8} more" if len(errors) > 8 else "")
            raise SpatialiseError(f"the features break the output contract, so nothing was published: {shown}. "
                                  "Fix them (assemble.py validates as it builds) and try again.")

        user = await self._user(need_publish=bool(create))
        counts = {"points": 0, "lines": 0, "polygons": 0, TABLE: 0}
        for f in features:
            g = f.get("geometry")
            if not g:
                counts[TABLE] += 1
            elif g["type"] == "MultiPoint":
                counts["points"] += len(g["coordinates"])
            else:
                counts[_FAMILY[g["type"]]] += 1

        if create:
            name = service_name(create)
            await self._check_name_free(name, user["org_id"])
            target = {"action": "create", "title": create.strip(), "service_name": name}
            layer_ids = {n: i for i, (n, _) in enumerate(LAYERS)} | {TABLE: len(LAYERS)}
            service_url = None
        else:
            service_url, layer_ids = await self._inspect(str(url))
            missing = [k for k, n in counts.items() if n and k not in layer_ids]
            if missing:
                raise SpatialiseError(f"{service_url} has no {missing} layer for these features. Publish to a "
                                      "new layer with create= instead.")
            target = {"action": "append", "service_url": service_url}

        feature_digest = hashlib.sha256(json.dumps(features, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        args = {"target": target, "features": feature_digest}
        if not confirm_token:
            return {
                "status": "confirmation_required",
                "plan": {**target, "portal": host_of(cfg.portal.url), "as_user": user["username"],
                         "sharing": "private to the owner" if create else "unchanged",
                         "writes": {k: v for k, v in counts.items() if v},
                         "statuses": collection.get("spatialise", {}).get("counts")},
                "confirm_token": confirm.issue(cfg.secret, "publish_features", user["username"], args),
                "message": ("Nothing has been written yet. Show the user this plan - the target, the portal, the "
                            "account and the row counts - and only if they approve, call publish_features again "
                            "with the same features and target plus this confirm_token."),
            }
        problem = confirm.verify(cfg.secret, confirm_token, "publish_features", user["username"], args)
        if problem:
            raise PolicyError(f"{problem}. Call publish_features again without confirm_token for a fresh plan.")

        run_id = uuid.uuid4().hex
        rows, trimmed = to_rows(features, run_id, int(time.time() * 1000))
        item_id = None
        if create:
            service_url, item_id = await self._create(target["title"], target["service_name"], user["username"],
                                                      _extent(features))
        assert service_url is not None
        layers = []
        failed_total = 0
        for key, layer_id in sorted(layer_ids.items(), key=lambda kv: kv[1]):
            if not rows.get(key):
                continue
            added, failures = await self._add(f"{service_url}/{layer_id}", rows[key])
            failed_total += len(rows[key]) - added
            entry: dict[str, Any] = {"name": key, "url": f"{service_url}/{layer_id}", "added": added}
            if failures:
                entry["failed"] = len(rows[key]) - added
                entry["errors"] = failures[:5]
            layers.append(entry)
        out: dict[str, Any] = {
            "status": "published" if not failed_total else "partial",
            "service_url": service_url, "run_id": run_id, "layers": layers,
            "written": sum(x["added"] for x in layers), "failed": failed_total,
            "sharing": "private to the owner" if create else "unchanged",
        }
        if item_id:
            out["item_url"] = f"{cfg.portal.url}/home/item.html?id={item_id}"
        if trimmed:
            out["candidates_trimmed"] = (f"{trimmed} feature(s) had more candidate detail than the field holds; "
                                         "candidate_count keeps the true number")
        if failed_total:
            out["message"] = ("Some rows were not written. Tell the user exactly which layers are short; the rows "
                              f"that were written carry run_id {run_id}.")
        return out

    # ------------------------------------------------------------ portal

    async def _user(self, need_publish: bool) -> dict[str, str]:
        data = await self.arcgis.call(f"{self.cfg.portal.rest}/portals/self", {}, auth=True)
        user = data.get("user") or {}
        if not user.get("username"):
            raise SignInRequired("publishing needs an ArcGIS sign-in. Reconnect the Spatialise connector.")
        privileges = user.get("privileges")
        if need_publish and isinstance(privileges, list) and privileges and \
                "portal:publisher:publishFeatures" not in privileges and user.get("role") != "org_admin":
            raise PolicyError(f"your ArcGIS account ({user['username']}) is not allowed to publish hosted feature "
                              "layers. Ask your ArcGIS administrator, or give the user the GeoJSON file instead.")
        return {"username": str(user["username"]), "org_id": str(data.get("id") or user.get("orgId") or "")}

    async def _check_name_free(self, name: str, org_id: str) -> None:
        if not org_id:
            return
        data = await self.arcgis.call(f"{self.cfg.portal.rest}/portals/{org_id}/isServiceNameAvailable",
                                      {"name": name, "type": "Feature Service"}, auth=True)
        if data.get("available") is False:
            raise SpatialiseError(f"a feature service named {name!r} already exists in this organisation. Ask the "
                                  "user for another name, or for the URL of the existing layer to add to.")

    async def _inspect(self, url: str) -> tuple[str, dict[str, int]]:
        url = re.sub(r"/\d+/?$", "", url.strip().rstrip("/"))
        if not re.search(r"/FeatureServer$", url, re.I):
            raise SpatialiseError("url must be a FeatureServer URL, e.g. https://…/rest/services/<name>/FeatureServer")
        if not self.cfg.portal.trusts(url):
            raise PolicyError(f"{host_of(url)} is not a host of this server's ArcGIS portal, so it cannot be "
                              "published to from here.")
        info = await self.arcgis.call(url, {}, auth=True)
        ids: dict[str, int] = {}
        by_geom = {g: n for n, g in LAYERS}
        for lyr in (info.get("layers") or []) + (info.get("tables") or []):
            detail = await self.arcgis.call(f"{url}/{lyr['id']}", {}, auth=True)
            have = {f["name"].lower() for f in detail.get("fields") or []}
            if not all(r in have for r in REQUIRED_FIELDS):
                continue
            if detail.get("type") == "Table":
                ids[TABLE] = lyr["id"]
            elif detail.get("geometryType") in by_geom:
                ids[by_geom[detail["geometryType"]]] = lyr["id"]
        if not ids:
            raise SpatialiseError(f"{url} does not have the Spatialise schema (fields {list(REQUIRED_FIELDS)}), so "
                                  "it was not created by this tool. Publish to a new layer with create= instead.")
        return url, ids

    async def _create(self, title: str, name: str, username: str, extent: dict[str, Any]) -> tuple[str, str]:
        params = {
            "createParameters": {
                "name": name, "serviceDescription": MARKER, "hasStaticData": False, "maxRecordCount": 2000,
                "supportedQueryFormats": "JSON", "capabilities": "Query,Create,Update,Delete,Editing",
                "description": "", "copyrightText": "", "spatialReference": dict(esri.WGS84),
                "initialExtent": extent, "allowGeometryUpdates": True, "units": "esriDecimalDegrees",
                "xssPreventionInfo": {"xssPreventionEnabled": True, "xssPreventionRule": "InputOnly",
                                      "xssInputRule": "rejectInvalid"},
            },
            "outputType": "featureService",
            "title": title,
            "snippet": "Places extracted by Spatialise Anything, with confidence and provenance on every feature.",
            "tags": "spatialise-anything",
        }
        made = await self.arcgis.call(f"{self.cfg.portal.rest}/content/users/{username}/createService", params,
                                      auth=True)
        service_url = str(made.get("serviceurl") or made.get("encodedServiceURL") or "").rstrip("/")
        item_id = str(made.get("itemId") or made.get("serviceItemId") or "")
        if not made.get("success", True) or not service_url:
            raise SpatialiseError("the portal did not create the feature service")
        if not self.cfg.portal.trusts(service_url):
            raise PolicyError(f"the portal created the service on {host_of(service_url)}, which is not in this "
                              "server's trusted hosts, so features cannot be written to it. The empty service "
                              f"exists ({service_url}); ask the operator to add the host to [portal] trusted_hosts.")
        admin = re.sub(r"/rest/services/", "/rest/admin/services/", service_url, count=1, flags=re.I)
        try:
            await self.arcgis.call(f"{admin}/addToDefinition", {"addToDefinition": _definition(extent)}, auth=True)
        except SpatialiseError as e:
            raise SpatialiseError(f"the feature service was created ({service_url}) but its layers could not be "
                                  f"added: {e}. Tell the user the empty service exists so they can delete it.") from e
        return service_url, item_id

    async def _add(self, layer_url: str, rows: list[dict[str, Any]]) -> tuple[int, list[str]]:
        added, failures = 0, []
        for batch in _chunks(rows):
            try:
                data = await self.arcgis.call(layer_url + "/addFeatures", {"features": batch,
                                                                             "rollbackOnFailure": True}, auth=True)
            except SignInRequired:
                raise
            except (ArcGISError, SpatialiseError) as e:
                failures.append(str(e))
                continue
            results = data.get("addResults") or []
            ok = sum(1 for r in results if r.get("success"))
            added += ok
            for r in results:
                if not r.get("success"):
                    err = r.get("error") or {}
                    failures.append(f"{err.get('code')}: {err.get('description') or err.get('message')}")
            if len(results) < len(batch):
                failures.append(f"the layer answered for {len(results)} of {len(batch)} rows")
        return added, failures
