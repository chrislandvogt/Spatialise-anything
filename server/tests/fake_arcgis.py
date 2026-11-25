"""An in-process stand-in for an ArcGIS portal, its services and Esri's public
geocoder, faithful to the REST shapes the server relies on.

It answers on any hostname (requests are routed in by httpx.ASGITransport), and
records the host, path and form of every request so tests can assert where the
user's token did and did not go.

Shapes were checked against live services in November 2026: the World
Geocoding Service (candidate, extent and error objects; 499 for forStorage
without a token), ABS ASGS MapServer layers and Vicmap hosted feature layers
(layer info, query, error and empty-result objects).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

PORTAL = "https://portal.test"
SERVICES = "https://gis.portal.test/arcgis/rest/services"
PUBLIC = "https://public.example/arcgis/rest/services"
WORLD = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer"
ORG_ID = "ORG123"


def err(code: int, message: str, details: list[str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details or []}})


def _square(x: float, y: float, d: float, clockwise: bool = True) -> list[list[float]]:
    ring = [[x - d, y - d], [x - d, y + d], [x + d, y + d], [x + d, y - d], [x - d, y - d]]  # clockwise
    return ring if clockwise else ring[::-1]


@dataclass
class Layer:
    name: str
    geometry_type: str | None  # None for a table
    fields: list[dict[str, Any]]
    features: list[dict[str, Any]] = field(default_factory=list)
    secured: bool = True
    editable: bool = False
    pagination: bool = True
    next_oid: int = 1
    max_records: int = 2000

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "currentVersion": 11.5, "id": 0, "name": self.name,
            "type": "Table" if self.geometry_type is None else "Feature Layer",
            "objectIdField": self.fields[0]["name"], "fields": self.fields, "maxRecordCount": self.max_records,
            "supportedQueryFormats": "JSON, geoJSON, PBF",
            "capabilities": "Query,Create,Update,Delete,Editing" if self.editable else "Query",
            "advancedQueryCapabilities": {"supportsPagination": self.pagination},
        }
        if self.geometry_type:
            out["geometryType"] = self.geometry_type
        return out


def fld(name: str, typ: str = "String", length: int | None = 255) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "type": "esriFieldType" + typ, "alias": name}
    if typ == "String" and length:
        out["length"] = length
    return out


@dataclass
class Locator:
    records: list[dict[str, Any]]  # address, x, y, Addr_type, + attributes
    secured: bool = True
    single_line: str = "SingleLine"
    storage_needs_token: bool = False


def _norm(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


class FakeArcGIS:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.users = {"jane": {"password": "pw", "org": ORG_ID, "role": "org_publisher",
                               "privileges": ["portal:publisher:publishFeatures", "features:user:edit"]}}
        self.sign_in_as = "jane"      # who "types their password" at the fake authorize page
        self.deny_sign_in = False
        self.codes: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}       # access token -> {user, exp}
        self.refresh: dict[str, dict[str, Any]] = {}      # refresh token -> {user, client_id}
        self.token_ttl = 1800
        self.rotate_refresh = False
        self.fail_next: list[int] = []                    # HTTP statuses to return before behaving
        self.services: dict[str, dict[str, Any]] = {}     # name -> {"layers": {id: Layer}, "owner": user}
        self.locators: dict[str, Locator] = {}
        self._seed()
        self.app = Starlette(routes=[Route("/{path:path}", self._handle, methods=["GET", "POST"])])

    # ------------------------------------------------------------ data

    def _seed(self) -> None:
        self.locators["gis.portal.test/Addresses"] = Locator(records=[
            {"address": "189-193 Hargreaves Street, Bendigo, Victoria, 3550", "x": 144.2825028, "y": -36.7588188,
             "Addr_type": "PointAddress", "City": "Bendigo", "RegionAbbr": "VIC", "Postal": "3550",
             "PlaceName": "Bendigo Town Hall"},
            {"address": "Railway Place, Bendigo, Victoria, 3550", "x": 144.2829551, "y": -36.7655937,
             "Addr_type": "StreetName", "City": "Bendigo", "RegionAbbr": "VIC", "Postal": "3550",
             "PlaceName": "Bendigo railway station car park", "extent": 0.002},
            {"address": "Hutton Street, Kyneton, Victoria, 3444", "x": 144.4532558, "y": -37.2470751,
             "Addr_type": "POI", "City": "Kyneton", "RegionAbbr": "VIC", "Postal": "3444",
             "PlaceName": "Kyneton Town Hall"},
            {"address": "1 Main Street, Greendale, Victoria, 3341", "x": 144.30, "y": -37.55,
             "Addr_type": "PointAddress", "City": "Greendale", "RegionAbbr": "VIC"},
            {"address": "1 Main Street, Bridgewater, Victoria, 3516", "x": 143.94, "y": -36.60,
             "Addr_type": "PointAddress", "City": "Bridgewater", "RegionAbbr": "VIC"},
        ])
        self.locators["geocode.arcgis.com/World"] = Locator(secured=False, storage_needs_token=True, records=[
            {"address": "Bendigo, Victoria", "x": 144.2794, "y": -36.7570, "Addr_type": "Locality",
             "City": "Bendigo", "RegionAbbr": "VIC", "Country": "AUS", "extent": 0.08},
        ])
        names = Layer("Placenames", "esriGeometryPoint", [
            fld("OBJECTID", "OID"), fld("PLACE_ID", "Integer"), fld("PLACE_NAME"), fld("FEATURE_TYPE"),
            fld("State"), fld("PLACE_NAME_STATUS")])
        for i, (n, t, x, y, status) in enumerate([
            ("LAKE EPPALOCK", "LAKE", 144.5686861, -36.8696968, "REGISTERED"),
            ("LAKE EPPALOCK", "LOCALITY", 144.5415361, -36.8820968, "REGISTERED"),
            ("RICHMOND", "LOCALITY", 145.0028659, -37.8220368, "REGISTERED"),
            ("RICHMOND", "POST OFFICE", 145.0015359, -37.8189968, "REGISTERED"),
            ("RICHMOND", "RAIL STATION", 144.9896059, -37.8238268, "REGISTERED"),
            ("RICHMOND PARK", "PARK", 144.99, -37.83, "REGISTERED"),
            ("O'BRIENS CROSSING", "LOCALITY", 144.37, -37.49, "REGISTERED"),
            ("OLD NAME", "LOCALITY", 144.0, -37.0, "HISTORICAL"),
        ], start=1):
            names.features.append({"attributes": {"OBJECTID": i, "PLACE_ID": 15000 + i, "PLACE_NAME": n,
                                                  "FEATURE_TYPE": t, "State": "VIC", "PLACE_NAME_STATUS": status},
                                   "geometry": {"x": x, "y": y}})
        self.services["gis.portal.test/Placenames"] = {"layers": {0: names}, "owner": "admin"}

        water = Layer("Water", "esriGeometryPolygon", [fld("OBJECTID", "OID"), fld("NAME"), fld("TYPE")])
        water.features.append({"attributes": {"OBJECTID": 1, "NAME": "Lake Eppalock", "TYPE": "reservoir"},
                               "geometry": {"rings": [_square(144.55, -36.89, 0.03)]}})
        self.services["gis.portal.test/Water"] = {"layers": {0: water}, "owner": "admin"}

        # A public boundary service on a host the portal does not trust: no token may ever arrive here.
        sa2 = Layer("SA2", "esriGeometryPolygon", [
            fld("objectid", "OID"), fld("sa2_code_2021"), fld("sa2_name_2021"), fld("state_name_2021"),
            fld("area_albers_sqkm", "Double")], secured=False)
        dense = [[144.30 + 0.2 * (i / 600), -37.30 + 0.0005 * (i % 2)] for i in range(601)]
        dense_ring = [[144.30, -37.10]] + [[144.50, -37.10]] + dense[::-1] + [[144.30, -37.10]]
        for i, (code, name, state, rings) in enumerate([
            ("202021030", "Kyneton", "Victoria", [dense_ring]),
            ("202021027", "Castlemaine", "Victoria", [_square(144.22, -37.06, 0.05),             # exterior
                                                     _square(144.22, -37.06, 0.01, clockwise=False)]),  # hole
            ("206041122", "Richmond (Vic.)", "Victoria", [_square(145.0, -37.82, 0.01)]),
            ("124031464", "Richmond - Clarendon", "New South Wales", [_square(150.75, -33.6, 0.03)]),
            ("601031017", "Islands", "Tasmania", [_square(147.0, -43.0, 0.02), _square(147.2, -43.0, 0.02)]),
        ], start=1):
            sa2.features.append({"attributes": {"objectid": i, "sa2_code_2021": code, "sa2_name_2021": name,
                                                "state_name_2021": state, "area_albers_sqkm": 100.0},
                                 "geometry": {"rings": rings}})
        self.services["public.example/SA2"] = {"layers": {0: sa2}, "owner": "abs"}

        # A road network. MAIN HIGHWAY runs east along 37 S in 20 segments, changes to MAIN FREEWAY, has a 90 m
        # hole where the name lapses through a town, and an unrelated MAIN STREET far to the north.
        roads = Layer("Roads", "esriGeometryPolyline", [fld("OBJECTID", "OID"), fld("ROAD_NAME"), fld("CLASS")],
                      max_records=8)
        segs = []
        for i in range(20):
            x0, x1 = 144.0 + i * 0.02, 144.0 + (i + 1) * 0.02
            if i == 9:
                x1 -= 0.001                                           # the hole
            pts = [[x0 + (x1 - x0) * k / 4, -37.0 + (0.002 if k in (1, 3) else 0)] for k in range(5)]
            segs.append(("MAIN HIGHWAY" if i < 14 else "MAIN FREEWAY", pts if i % 3 else pts[::-1]))
        segs.append(("MAIN STREET", [[144.1, -36.5], [144.2, -36.5]]))
        segs.append(("MAIN HIGHWAY", [[144.9, -37.0], [145.0, -37.0]]))   # same name, far away, not joined
        segs.append(("OTHER ROAD", [[144.1, -37.0], [144.1, -36.9]]))
        for i, (n, pts) in enumerate(segs, start=1):
            roads.features.append({"attributes": {"OBJECTID": i, "ROAD_NAME": n, "CLASS": "1"},
                                   "geometry": {"paths": [pts]}})
        self.services["gis.portal.test/Roads"] = {"layers": {0: roads}, "owner": "admin"}

        zones = Layer("Zones", "esriGeometryPolygon", [fld("OBJECTID", "OID"), fld("ZONE_NO", "Integer"),
                                                       fld("ZONE_NAME")], pagination=False)
        zones.features.append({"attributes": {"OBJECTID": 1, "ZONE_NO": 7, "ZONE_NAME": "North depot zone"},
                               "geometry": {"rings": [_square(144.3, -36.7, 0.1)]}})
        self.services["gis.portal.test/Zones"] = {"layers": {0: zones}, "owner": "admin"}

    # ------------------------------------------------------------ plumbing

    def issue_token(self, user: str = "jane", ttl: int | None = None) -> str:
        tok = "agol-" + secrets.token_urlsafe(16)
        self.tokens[tok] = {"user": user, "exp": time.time() + (self.token_ttl if ttl is None else ttl)}
        return tok

    def _user(self, form: dict[str, str]) -> str | None | bool:
        """Username for a valid token, None for no token, False for a bad one."""
        tok = form.get("token")
        if not tok:
            return None
        info = self.tokens.get(tok)
        if not info or info["exp"] < time.time():
            return False
        return info["user"]

    async def _handle(self, request: Request) -> Response:
        form = dict(request.query_params)
        if request.method == "POST":
            form.update({k: str(v) for k, v in (await request.form()).items()})
        host = request.url.hostname or ""
        path = "/" + request.path_params["path"]
        self.calls.append({"host": host, "path": path, "method": request.method, "form": form,
                           "query": dict(request.query_params)})
        if self.fail_next:
            return Response("busy", status_code=self.fail_next.pop(0))
        if path.startswith("/sharing/rest/"):
            return self._portal(request, path[len("/sharing/rest"):], form)
        m = re.match(r"^/arcgis/rest/(admin/)?services/(.+?)/(GeocodeServer|FeatureServer|MapServer)(/.*)?$", path)
        if not m:
            return Response("<html>not found</html>", status_code=404, media_type="text/html")
        admin, name, kind, rest = bool(m.group(1)), m.group(2), m.group(3), (m.group(4) or "").strip("/")
        key = f"{host}/{name}"
        if kind == "GeocodeServer":
            return self._locator(key, rest, form)
        return self._feature_service(key, rest, form, admin)

    # ------------------------------------------------------------ portal

    def _portal(self, request: Request, path: str, form: dict[str, str]) -> Response:
        if path == "/oauth2/authorize":
            if form.get("response_type") != "code" or not form.get("client_id") or not form.get("redirect_uri"):
                return Response("bad authorize request", status_code=400)
            if self.deny_sign_in:
                q = urlencode({"error": "access_denied", "error_description": "user denied", "state": form["state"]})
                return RedirectResponse(f"{form['redirect_uri']}?{q}", status_code=302)
            code = "code-" + secrets.token_urlsafe(12)
            self.codes[code] = {"client_id": form["client_id"], "redirect_uri": form["redirect_uri"],
                                "challenge": form.get("code_challenge"), "user": self.sign_in_as}
            return RedirectResponse(f"{form['redirect_uri']}?{urlencode({'code': code, 'state': form['state']})}",
                                    status_code=302)
        if path == "/oauth2/token":
            return self._token(form)
        user = self._user(form)
        if user is False:
            return err(498, "Invalid token.")
        if user is None:
            return err(499, "Token Required")
        u = self.users[user]
        if path == "/portals/self":
            return JSONResponse({"id": u["org"], "name": "Test Org", "isPortal": False, "portalHostname": "portal.test",
                                 "user": {"username": user, "role": u["role"], "privileges": u["privileges"],
                                          "orgId": u["org"]}})
        m = re.match(r"^/portals/(\w+)/isServiceNameAvailable$", path)
        if m:
            taken = any(k.split("/", 1)[1].lower() == form.get("name", "").lower() for k in self.services)
            return JSONResponse({"available": not taken})
        m = re.match(r"^/content/users/(\w+)/createService$", path)
        if m:
            if m.group(1) != user:
                return err(403, "You do not have permissions to access this resource or perform this operation.")
            if "portal:publisher:publishFeatures" not in u["privileges"]:
                return err(403, "User does not have privileges to perform this operation.")
            params = json.loads(form["createParameters"])
            name = params["name"]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
                return err(400, "Invalid service name.")
            key = f"gis.portal.test/{name}"
            if key in self.services:
                return err(409, "Service name already exists.")
            item = secrets.token_hex(16)
            self.services[key] = {"layers": {}, "owner": user, "item": item, "title": form.get("title"),
                                  "description": params.get("serviceDescription")}
            url = f"{SERVICES}/{name}/FeatureServer"
            return JSONResponse({"encodedServiceURL": url, "itemId": item, "name": name, "serviceItemId": item,
                                 "serviceurl": url, "size": -1, "success": True, "type": "Feature Service",
                                 "isView": False})
        return err(400, "Unknown portal operation")

    def _token(self, form: dict[str, str]) -> Response:
        def bad(desc: str) -> JSONResponse:
            return JSONResponse({"error": {"code": 400, "error": "invalid_grant", "error_description": desc,
                                           "message": desc, "details": []}})
        if form.get("grant_type") == "authorization_code":
            rec = self.codes.pop(form.get("code", ""), None)
            if not rec:
                return bad("Invalid authorization code")
            if rec["client_id"] != form.get("client_id") or rec["redirect_uri"] != form.get("redirect_uri"):
                return bad("client_id or redirect_uri mismatch")
            if rec["challenge"]:
                digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != rec["challenge"]:
                    return bad("PKCE verification failed")
            refresh = "refresh-" + secrets.token_urlsafe(16)
            self.refresh[refresh] = {"user": rec["user"], "client_id": rec["client_id"]}
            return JSONResponse({"access_token": self.issue_token(rec["user"]), "expires_in": self.token_ttl,
                                 "username": rec["user"], "ssl": True, "refresh_token": refresh,
                                 "refresh_token_expires_in": 1209600})
        if form.get("grant_type") == "refresh_token":
            rec = self.refresh.get(form.get("refresh_token", ""))
            if not rec or rec["client_id"] != form.get("client_id"):
                return bad("Invalid refresh token")
            out: dict[str, Any] = {"access_token": self.issue_token(rec["user"]), "expires_in": self.token_ttl,
                                   "username": rec["user"], "ssl": True}
            if self.rotate_refresh:
                del self.refresh[form["refresh_token"]]
                new = "refresh-" + secrets.token_urlsafe(16)
                self.refresh[new] = rec
                out.update(refresh_token=new, refresh_token_expires_in=1209600)
            return JSONResponse(out)
        return bad("unsupported grant type")

    # ------------------------------------------------------------ locators

    def _locator(self, key: str, op: str, form: dict[str, str]) -> Response:
        loc = self.locators.get(key)
        if loc is None:
            return err(404, "Service not found")
        user = self._user(form)
        if user is False:
            return err(498, "Invalid Token")
        needs = loc.secured or (loc.storage_needs_token and form.get("forStorage") == "true")
        if needs and user is None:
            return JSONResponse({"error": {"code": 499, "extendedCode": -2147200230,
                                           "message": "Token required but not passed in the request.",
                                           "details": ["Token required."]}})
        if op == "":
            return JSONResponse({"currentVersion": 11.5, "serviceDescription": key,
                                 "capabilities": "Geocode,ReverseGeocode",
                                 "singleLineAddressField": {"name": loc.single_line, "type": "esriFieldTypeString",
                                                            "alias": "Single Line Input", "required": False,
                                                            "length": 200},
                                 "addressFields": [], "candidateFields": [], "spatialReference": {"wkid": 4326},
                                 "locatorProperties": {"MaxBatchSize": 1000, "SuggestedBatchSize": 150}})
        if op == "findAddressCandidates":
            text = form.get(loc.single_line)
            if text is None:
                return err(400, "Unable to complete operation.", ["Missing address input."])
            q = set(_norm(text))
            hits = []
            for r in loc.records:
                target = set(_norm(r["address"] + " " + r.get("PlaceName", "")))
                score = round(100 * len(q & target) / max(1, len(q)), 2)
                if score >= 70:
                    hits.append((score, r))
            hits.sort(key=lambda h: -h[0])
            cands = []
            for score, r in hits[: int(form.get("maxLocations", 5))]:
                d = r.get("extent", 0.001)
                attrs = {k: v for k, v in r.items() if k not in ("address", "x", "y", "extent")}
                attrs["Match_addr"] = r["address"]
                cands.append({"address": r["address"], "location": {"x": r["x"], "y": r["y"]}, "score": score,
                              "attributes": attrs, "extent": {"xmin": r["x"] - d, "ymin": r["y"] - d,
                                                              "xmax": r["x"] + d, "ymax": r["y"] + d}})
            return JSONResponse({"spatialReference": {"wkid": 4326, "latestWkid": 4326}, "candidates": cands})
        if op == "reverseGeocode":
            pt = json.loads(form["location"])
            best = min(loc.records, key=lambda r: (r["x"] - pt["x"]) ** 2 + (r["y"] - pt["y"]) ** 2)
            if (best["x"] - pt["x"]) ** 2 + (best["y"] - pt["y"]) ** 2 > 0.01 ** 2:
                return JSONResponse({"error": {"code": 400, "extendedCode": -2147467259,
                                               "message": "Cannot perform query. Invalid query parameters.",
                                               "details": ["Unable to find address for the specified location."]}})
            return JSONResponse({"address": {"Match_addr": best["address"], "LongLabel": best["address"],
                                             "Addr_type": best["Addr_type"], "City": best.get("City", "")},
                                 "location": {"x": best["x"], "y": best["y"],
                                              "spatialReference": {"wkid": 4326, "latestWkid": 4326}}})
        return err(400, "Invalid URL")

    # ------------------------------------------------------------ feature services

    def _feature_service(self, key: str, rest: str, form: dict[str, str], admin: bool) -> Response:
        svc = self.services.get(key)
        if svc is None:
            return err(404, "Service not found")
        layers: dict[int, Layer] = svc["layers"]
        user = self._user(form)
        secured = svc["owner"] != "abs"
        if user is False:
            return err(498, "Invalid Token")
        if secured and user is None:
            return err(499, "Token Required")
        if admin:
            if rest != "addToDefinition":
                return err(400, "Invalid URL")
            if svc["owner"] != user:
                return err(403, "You do not have permissions to access this resource or perform this operation.")
            definition = json.loads(form["addToDefinition"])
            made = []
            for d in definition.get("layers", []) + definition.get("tables", []):
                layers[d["id"]] = Layer(d["name"], d.get("geometryType"), d["fields"], editable=True)
                made.append({"name": d["name"], "id": d["id"]})
            return JSONResponse({"success": True, "layers": made})
        if rest == "":
            return JSONResponse({
                "currentVersion": 11.5, "serviceDescription": svc.get("description", ""),
                "layers": [{"id": i, "name": l.name, "geometryType": l.geometry_type} for i, l in layers.items()
                           if l.geometry_type],
                "tables": [{"id": i, "name": l.name} for i, l in layers.items() if not l.geometry_type]})
        m = re.match(r"^(\d+)(?:/(\w+))?$", rest)
        if not m or int(m.group(1)) not in layers:
            return err(400, "Invalid URL")
        layer, op = layers[int(m.group(1))], m.group(2)
        if op is None:
            return JSONResponse({**layer.info(), "id": int(m.group(1))})
        if op == "query":
            return self._query(layer, form)
        if op == "addFeatures":
            if not layer.editable or svc["owner"] != user:
                return err(403, "Requested operation is not supported by this service.")
            return self._add(layer, json.loads(form["features"]))
        return err(400, "Invalid URL")

    def _query(self, layer: Layer, form: dict[str, str]) -> Response:
        names = [f["name"] for f in layer.fields]
        db = sqlite3.connect(":memory:")
        db.execute(f"CREATE TABLE t ({', '.join(chr(34) + n + chr(34) for n in names)})")
        for feat in layer.features:
            db.execute(f"INSERT INTO t VALUES ({', '.join('?' * len(names))})",
                       [feat["attributes"].get(n) for n in names])
        try:
            order = [r[0] for r in db.execute(f"SELECT rowid FROM t WHERE {form.get('where') or '1=1'}")]
        except sqlite3.Error:
            return JSONResponse({"error": {"code": 400, "message": "Failed to execute query.", "details": []}})
        feats = [layer.features[i - 1] for i in order]
        if form.get("geometryType") == "esriGeometryEnvelope" and form.get("geometry"):
            x0, y0, x1, y1 = (float(v) for v in form["geometry"].split(","))

            def inside(g: dict[str, Any]) -> bool:
                pts = [[g["x"], g["y"]]] if "x" in g else [p for ring in g.get("rings", g.get("paths", [])) for p in ring]
                return any(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in pts)
            feats = [f for f in feats if inside(f["geometry"])]
        if (form.get("resultRecordCount") or form.get("resultOffset")) and not layer.pagination:
            return err(400, "Pagination is not supported.")
        feats = feats[int(form.get("resultOffset") or 0):]
        n = min(int(form.get("resultRecordCount") or layer.max_records), layer.max_records)
        exceeded, feats = len(feats) > n, feats[:n]
        want = None if form.get("outFields", "*") == "*" else form["outFields"].split(",")
        if want and any(w not in names for w in want):
            return JSONResponse({"error": {"code": 400, "message": "Failed to execute query.", "details": []}})
        offset = float(form.get("maxAllowableOffset") or 0)
        out = []
        for f in feats:
            row: dict[str, Any] = {"attributes": {k: v for k, v in f["attributes"].items() if not want or k in want}}
            if form.get("returnGeometry", "true") == "true" and "geometry" in f:
                row["geometry"] = _generalise(f["geometry"], offset)
            out.append(row)
        body: dict[str, Any] = {"objectIdFieldName": names[0], "fields": layer.fields, "features": out}
        if layer.geometry_type:
            body.update(geometryType=layer.geometry_type, spatialReference={"wkid": 4326, "latestWkid": 4326})
        if exceeded:
            body["exceededTransferLimit"] = True
        return JSONResponse(body)

    def _add(self, layer: Layer, feats: list[dict[str, Any]]) -> Response:
        defs = {f["name"]: f for f in layer.fields}
        shape = {"esriGeometryPoint": "x", "esriGeometryPolyline": "paths", "esriGeometryPolygon": "rings"}
        results, staged = [], []
        for feat in feats:
            problem = None
            for k, v in feat.get("attributes", {}).items():
                if k not in defs:
                    problem = f"Field {k} does not exist"
                elif isinstance(v, str) and defs[k].get("length") and len(v) > defs[k]["length"]:
                    problem = "String or binary data would be truncated."
                elif v is not None and defs[k]["type"] in ("esriFieldTypeInteger", "esriFieldTypeDouble") and \
                        not isinstance(v, (int, float)):
                    problem = f"Invalid value for field {k}"
            if layer.geometry_type and shape[layer.geometry_type] not in (feat.get("geometry") or {}):
                problem = "Invalid geometry for this layer."
            if not layer.geometry_type and feat.get("geometry"):
                problem = "Tables do not accept geometry."
            if problem:
                results.append({"objectId": -1, "success": False, "error": {"code": 1000, "description": problem}})
            else:
                staged.append(feat)
                results.append({"objectId": layer.next_oid + len(staged) - 1, "success": True})
        if any(not r["success"] for r in results):  # rollbackOnFailure
            return JSONResponse({"addResults": [r if not r["success"] else
                                                {"objectId": -1, "success": False,
                                                 "error": {"code": 1003, "description": "Operation rolled back."}}
                                                for r in results]})
        for feat in staged:
            feat = {"attributes": {"OBJECTID": layer.next_oid, **feat.get("attributes", {})},
                    **({"geometry": feat["geometry"]} if feat.get("geometry") else {})}
            layer.features.append(feat)
            layer.next_oid += 1
        return JSONResponse({"addResults": results})


def _generalise(geom: dict[str, Any], offset: float) -> dict[str, Any]:
    """A crude stand-in for maxAllowableOffset: drop vertices closer than the offset to the last one kept."""
    if not offset or "rings" not in geom:
        return geom
    rings = []
    for ring in geom["rings"]:
        kept = [ring[0]]
        for p in ring[1:-1]:
            if abs(p[0] - kept[-1][0]) >= offset or abs(p[1] - kept[-1][1]) >= offset:
                kept.append(p)
        kept.append(ring[-1])
        rings.append(kept if len(kept) >= 4 else ring)
    return {"rings": rings}
