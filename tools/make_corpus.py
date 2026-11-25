#!/usr/bin/env python3
"""Generate the evaluation corpus: synthetic inputs and their expected output.

Every expected position comes from an independent reference, never from the
skill's own scripts, so the corpus can catch the scripts being wrong:

  * coordinate conversions  - pyproj (PROJ) and the mgrs package
  * localities, postcodes, LGAs - raw ABS ASGS shapefiles read with pyshp,
                              centroids and areas from shapely
  * geodesic constructions  - pyproj.Geod
  * a handful of landmarks  - OpenStreetMap positions, written in below
                              (c) OpenStreetMap contributors, ODbL

Items whose expected answer involves judgement or approximate reference
geometry are marked "review": "needs_hand_check" in the manifest. A human
signs them off by changing that to "checked" with initials and date.

Build-time dependencies: pyproj, mgrs, pyshp, shapely, Pillow.

    python tools/make_corpus.py --abs /path/to/abs/shapefiles
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

import mgrs as mgrslib
import shapefile
from PIL import Image
from pyproj import CRS, Geod, Transformer
from pyproj.transformer import TransformerGroup
from shapely.geometry import mapping, shape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "spatialise-anything" / "scripts"))
import contract as C  # noqa: E402  (structure only - no positions come from the skill)

CORPUS = ROOT / "corpus"
GEOD = Geod(ellps="GRS80")
ABS_RESOLVER = {"name": "reference:ABS ASGS Ed3", "version": "SAL2021/POA2021/LGA2025"}
OSM_RESOLVER = {"name": "reference:OpenStreetMap", "version": "2026-11"}
PROJ_RESOLVER = {"name": "reference:PROJ", "version": "pyproj"}


# ---------------------------------------------------------------- references

class ABS:
    def __init__(self, src: Path):
        self.src = src
        self._cache = {}

    def _layer(self, stem, code_f, name_f):
        if stem not in self._cache:
            r = shapefile.Reader(str(self.src / stem))
            idx = {}
            for sr in r.iterShapeRecords():
                rec = sr.record.as_dict()
                idx[(str(rec[code_f]), str(rec[name_f]))] = sr.shape
            self._cache[stem] = idx
        return self._cache[stem]

    def get(self, kind: str, name: str):
        stem, code_f, name_f = {
            "locality": ("SAL_2021_AUST_GDA2020", "SAL_CODE21", "SAL_NAME21"),
            "postcode": ("POA_2021_AUST_GDA2020", "POA_CODE21", "POA_NAME21"),
            "lga": ("LGA_2025_AUST_GDA2020", "LGA_CODE25", "LGA_NAME25"),
        }[kind]
        layer = self._layer(stem, code_f, name_f)
        hits = [(k, s) for k, s in layer.items() if k[1] == name or k[0] == name]
        if len(hits) != 1:
            raise KeyError(f"{kind} {name!r}: {len(hits)} matches")
        (code, nm), shp = hits[0]
        geom = shape(shp.__geo_interface__)
        pt = geom.centroid if geom.contains(geom.centroid) else geom.representative_point()
        # Equal-area radius from the geodesic area.
        area_m2 = abs(GEOD.geometry_area_perimeter(geom)[0])
        return {"code": code, "name": nm, "geom": geom, "point": (pt.x, pt.y),
                "radius_m": round(math.sqrt(area_m2 / math.pi)), "area_km2": area_m2 / 1e6}


def _best(src, dst, x, y):
    """Most accurate PROJ operation from src to dst whose area covers the
    point. Ballpark (null) operations are refused: they silently skip the
    datum shift, which is exactly what this corpus tests."""
    ops = []
    for tr in TransformerGroup(src, dst, always_xy=True).transformers:
        a = tr.area_of_use
        if "ballpark" in tr.description.lower() or tr.accuracy is None or tr.accuracy < 0:
            continue
        if a and not (a.west <= x <= a.east and a.south <= y <= a.north):
            continue
        ops.append(tr)
    if not ops:
        raise RuntimeError(f"no real transformation {src}->{dst} at {x},{y}")
    return min(ops, key=lambda t: t.accuracy).transform(x, y)


def proj(src_epsg: int, x: float, y: float) -> tuple[float, float]:
    """Any EPSG CRS -> GDA2020 lon/lat, via the source's geographic CRS and
    real datum transformations only (AGD goes via GDA94)."""
    crs = CRS.from_epsg(src_epsg)
    geog = crs.geodetic_crs if crs.is_projected else crs
    lon, lat = Transformer.from_crs(crs, geog, always_xy=True).transform(x, y) if crs.is_projected else (x, y)
    g = geog.to_epsg()
    if g == 7844:
        return lon, lat
    if g in (4202, 4203):
        lon, lat = _best(g, 4283, lon, lat)
        g = 4283
    return _best(g, 7844, lon, lat)


def mgrs_centre(s: str) -> tuple[float, float]:
    """Centre of an MGRS cell via the mgrs package: append a 5 to each digit
    group so the package's lower-left corner lands mid-cell."""
    body = s.replace(" ", "")
    zone_band_sq = body[:5]
    digits = body[5:]
    k = len(digits) // 2
    e, n = digits[:k], digits[k:]
    e2, n2 = (e + "5" + "0" * 5)[:5], (n + "5" + "0" * 5)[:5]
    lat, lon = mgrslib.MGRS().toLatLon(zone_band_sq + e2 + n2)
    return lon, lat


def pt(lon, lat):
    return C.point(lon, lat)


def poly_geojson(geom, tol=0.002):
    g = mapping(geom.simplify(tol, preserve_topology=True))
    def rnd(o):
        if isinstance(o, (list, tuple)) and o and isinstance(o[0], (int, float)):
            return [round(o[0], 5), round(o[1], 5)]
        return [rnd(x) for x in o]
    return {"type": g["type"], "coordinates": rnd(g["coordinates"])}


# ---------------------------------------------------------------- helpers

class Item:
    def __init__(self, item_id: str, tier: str, description: str, review: str = "generated_from_reference",
                 frame: dict | None = None):
        self.id = item_id
        self.dir = CORPUS / "items" / item_id
        self.tier = tier
        self.description = description
        self.review = review
        self.frame = frame or {}
        self.inputs = []
        self.features = []
        self.network = []
        self.texts = {}
        self.notes = []

    def text(self, name: str, body: str):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_text(body, encoding="utf-8")
        self.inputs.append(name)
        self.texts[name] = body

    def span(self, doc: str, mention: str, nth: int = 1) -> dict:
        body = self.texts[doc]
        i = -1
        for _ in range(nth):
            i = body.find(mention, i + 1)
            if i < 0:
                raise ValueError(f"{mention!r} not in {doc}")
        return C.make_source(doc, char_start=i, char_end=i + len(mention))

    def add(self, *, network=False, **kw):
        f = C.make_feature(**kw)
        self.features.append(f)
        if network:
            self.network.append(kw["mention"])
        return f

    def write(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        fc = C.make_collection(self.features, inputs=self.inputs, threshold=C.DEFAULT_THRESHOLD, frame=self.frame)
        fc["spatialise"]["generated"] = "corpus"
        (self.dir / "expected.geojson").write_text(json.dumps(fc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"id": self.id, "tier": self.tier, "description": self.description, "inputs": self.inputs,
                "frame": self.frame, "expected_features": len(self.features),
                "network_mentions": self.network, "review": self.review, "notes": self.notes}


def named(item, abs_, doc, mention, kind, abs_name, label, nth=1, rejects=(), conf=0.9, polygon=False,
          tier="named", method=None):
    ref = abs_.get(kind, abs_name)
    geom = poly_geojson(ref["geom"]) if polygon else pt(*ref["point"])
    return item.add(geometry=geom, label=label, mention=mention, source=item.span(doc, mention, nth), tier=tier,
                    method=method or ("boundary_join" if polygon else "gazetteer"), resolver=ABS_RESOLVER,
                    confidence=conf, uncertainty_m=0 if polygon else ref["radius_m"], status="resolved",
                    candidates=[{"label": lb, "rejected": why} for lb, why in rejects], source_crs="EPSG:7844")


# ---------------------------------------------------------------- items

def build(abs_: ABS) -> list[dict]:
    if (CORPUS / "items").exists():
        shutil.rmtree(CORPUS / "items")
    items = []

    # ---------------------------------------------------------- tier 0: mixed formats and traps
    it = Item("t0-coords-mixed", "explicit",
              "Every supported coordinate format, mixed with phone numbers, part numbers, money, dates, "
              "ratios and measurements that must not be read as coordinates.", frame={"country": "AU", "state": "VIC"})
    mg_ll = (144.5430, -36.9046)  # near Lake Eppalock
    mg = mgrslib.MGRS().toMGRS(mg_ll[1], mg_ll[0], MGRSPrecision=4)
    mg_txt = f"{mg[:5]} {mg[5:9]} {mg[9:13]}"
    doc = "field-report.txt"
    it.text(doc, f"""FIELD INSPECTION REPORT - CALDER CATCHMENT (synthetic test document)
Report ref FR-2026-0412, rev 1.3.2. Inspector contact: 03 5422 1234 / 0412 345 678.
ABN 12 345 678 901. Purchase order PO-144-37-8136, total $1,234.56 incl. GST.

Site 1 - pump station. Position -37.24712, 144.45291 (GDA2020), recorded 14:30 on 12/03/2026.
Site 2 - bore. Located 37°03'50.4"S 144°13'01.2"E.
Site 3 - storage tank at 36° 45.42' S, 144° 16.77' E.
Site 4 - culvert. MGA2020 zone 55: E 273456 N 5918234.
Site 5 - gauge board, MGRS {mg_txt}.
Site 6 - survey mark supplied as WKT: POINT(144.2794 -36.7570)
Site 7 - lat: -36.8521 lon: 144.5317
Site 8 - from the 2015 plan (GDA94), MGA zone 55 E 260150 N 5893420.

Measurements: 12.345, 67.891 mm. Flow ratio 1.25, 3.50. Pump model 2.250/4.125.
Next inspection due 2026-09-14. See pages 3.5, 4.1 of the manual.
""")
    R = {"name": "parse_coords"}

    def ex(mention, lonlat, unc, crs, flags=()):
        it.add(geometry=pt(*lonlat), label=None, mention=mention, source=it.span(doc, mention), tier="explicit",
               method="coord_parse", resolver=R, confidence=0.95, uncertainty_m=unc, status="resolved",
               source_crs=crs, flags=flags)

    ex("-37.24712, 144.45291", (144.45291, -37.24712), 1, "EPSG:7844")
    ex("37°03'50.4\"S 144°13'01.2\"E", (144 + 13 / 60 + 1.2 / 3600, -(37 + 3 / 60 + 50.4 / 3600)), 3.5,
       "EPSG:4326 (assumed)", ["datum_assumed"])
    ex("36° 45.42' S, 144° 16.77' E", (144 + 16.77 / 60, -(36 + 45.42 / 60)), 11.3, "EPSG:4326 (assumed)",
       ["datum_assumed"])
    ex("E 273456 N 5918234", proj(7855, 273456, 5918234), 1, "EPSG:7855")
    ex(mg_txt, mgrs_centre(mg_txt), 7.1, "EPSG:4326 (MGRS)")
    ex("POINT(144.2794 -36.7570)", (144.2794, -36.7570), 7.6, "EPSG:4326 (assumed)", ["datum_assumed"])
    ex("lat: -36.8521 lon: 144.5317", (144.5317, -36.8521), 7.6, "EPSG:4326 (assumed)", ["datum_assumed"])
    ex("E 260150 N 5893420", proj(28355, 260150, 5893420), 1, "EPSG:28355")
    items.append(it.write())

    # ---------------------------------------------------------- tier 0: datums
    it = Item("t0-datums", "explicit",
              "Legacy and mixed datums: AGD66 grid (not converted, flagged), GDA94 (transformed), WGS84, and none.",
              frame={"country": "AU", "state": "VIC"})
    doc = "legacy-notes.txt"
    it.text(doc, """LEGACY SITE NOTES (synthetic test document)
Old bore, from a 1985 survey plan: AMG66 zone 55, E 273300 N 5918050.
Same bore re-surveyed in GDA94: -36.85640, 144.45720
Weather station (WGS84): -37.06400, 144.21700
Trig point, no datum given: -36.75700, 144.27900
""")
    agd = proj(20255, 273300, 5918050)
    it.notes.append("AGD66 truth via PROJ: EPSG:20255 -> AGD66 -> GDA94 ('AGD66 to GDA94 (4)', 1 m) -> GDA2020. "
                    "The unconverted reading is ~200 m away and must carry >= 250 m uncertainty to count as honest.")
    it.add(geometry=pt(*agd), label=None, mention="E 273300 N 5918050", source=it.span(doc, "E 273300 N 5918050"),
           tier="explicit", method="coord_parse", resolver=PROJ_RESOLVER, confidence=0.95, uncertainty_m=250,
           status="resolved", source_crs="EPSG:20255", flags=["datum_unsupported"])
    it.add(geometry=pt(*proj(4283, 144.45720, -36.85640)), label=None, mention="-36.85640, 144.45720",
           source=it.span(doc, "-36.85640, 144.45720"), tier="explicit", method="coord_parse", resolver=PROJ_RESOLVER,
           confidence=0.95, uncertainty_m=1, status="resolved", source_crs="EPSG:4283")
    it.add(geometry=pt(144.217, -37.064), label=None, mention="-37.06400, 144.21700",
           source=it.span(doc, "-37.06400, 144.21700"), tier="explicit", method="coord_parse", resolver=PROJ_RESOLVER,
           confidence=0.95, uncertainty_m=1, status="resolved", source_crs="EPSG:4326")
    it.add(geometry=pt(144.279, -36.757), label=None, mention="-36.75700, 144.27900",
           source=it.span(doc, "-36.75700, 144.27900"), tier="explicit", method="coord_parse", resolver=PROJ_RESOLVER,
           confidence=0.95, uncertainty_m=3, status="resolved", source_crs="EPSG:4326 (assumed)", flags=["datum_assumed"])
    items.append(it.write())

    # ---------------------------------------------------------- tier 0: photos
    it = Item("t0-photos", "explicit", "Three photos: GPS with heading and error, GPS only, no GPS.",
              frame={"country": "AU", "state": "VIC"})
    it.dir.mkdir(parents=True, exist_ok=True)
    photos = [
        ("IMG_0001.jpg", (144.4551, -37.2465), {"heading": 270.0, "err": 4.5, "datum": "WGS-84"}),
        ("IMG_0002.jpg", (144.2190, -37.0652), {}),
        ("IMG_0003.jpg", None, {}),
    ]
    for name, ll, extra in photos:
        _write_photo(it.dir / name, ll, extra)
        it.inputs.append(name)
        if ll is None:
            it.add(geometry=None, label=None, mention=f"photo {name}", source=C.make_source(name), tier="explicit",
                   method="exif", resolver={"name": "read_exif"}, confidence=0.0, uncertainty_m=None,
                   status="unresolved", source_crs="unknown")
        else:
            flags = [] if extra else ["datum_assumed", "default_uncertainty"]
            it.add(geometry=pt(*ll), label=None, mention=f"photo {name}", source=C.make_source(name), tier="explicit",
                   method="exif", resolver={"name": "read_exif"}, confidence=0.95,
                   uncertainty_m=extra.get("err", 10.0), status="resolved",
                   source_crs="EPSG:4326" if extra else "EPSG:4326 (assumed)", flags=flags)
    items.append(it.write())

    # ---------------------------------------------------------- tier 0/1: asset table
    it = Item("t1-assets", "structured",
              "Asset register mixing lat/lon rows, MGA2020 grid rows (datum in the header) and a row with no "
              "location (asset lookup needs the network).", frame={"country": "AU", "state": "VIC"})
    taradale = Transformer.from_crs(7844, 7855, always_xy=True).transform(144.3560, -37.1390)
    elph = Transformer.from_crs(7844, 7855, always_xy=True).transform(144.3370, -37.1010)
    rows = [
        ("PS-001", "Kyneton pump station", "-37.24712", "144.45291", "", "", "", "good"),
        ("PS-002", "Malmsbury pump station", "-37.18860", "144.38220", "", "", "", "fair"),
        ("TK-014", "Taradale tank", "", "", f"{taradale[0]:.0f}", f"{taradale[1]:.0f}", "55", "good"),
        ("VL-207", "Elphinstone valve", "", "", f"{elph[0]:.0f}", f"{elph[1]:.0f}", "55", "poor"),
        ("HY-330", "Hydrant (location TBC)", "", "", "", "", "", "unknown"),
    ]
    doc = "assets.csv"
    it.text(doc, "asset_id,name,lat,lon,easting_mga2020,northing_mga2020,zone,condition\n" +
            "\n".join(",".join(r) for r in rows) + "\n")
    for n, r in enumerate(rows, start=2):
        src = C.make_source(doc, row=n)
        if r[2]:
            it.add(geometry=pt(float(r[3]), float(r[2])), label=r[1], mention=f"{r[2]}, {r[3]}", source=src,
                   tier="explicit", method="coord_parse", resolver=R, confidence=0.95, uncertainty_m=3,
                   status="resolved", source_crs="EPSG:4326 (assumed)", flags=["datum_assumed"])
        elif r[4]:
            it.add(geometry=pt(*proj(7855, float(r[4]), float(r[5]))), label=r[1], mention=f"zone 55 E {r[4]} N {r[5]}",
                   source=src, tier="explicit", method="coord_parse", resolver=PROJ_RESOLVER, confidence=0.95,
                   uncertainty_m=1, status="resolved", source_crs="EPSG:7855")
        else:
            it.add(geometry=None, label=r[1], mention=r[0], source=src, tier="structured", method="not_attempted",
                   resolver={"name": "none"}, confidence=0.0, uncertainty_m=None, status="unresolved",
                   source_crs="n/a", flags=["needs_network"])
    it.notes.append("HY-330 has no location in the table; online it needs the customer asset layer, which the "
                    "corpus does not have, so it stays unresolved in both modes.")
    items.append(it.write())

    # ---------------------------------------------------------- tier 1: postcodes and LGAs
    it = Item("t1-areas", "structured", "Counts by postcode and LGA, including an LGA written as 'City of ...' and "
              "a postcode with no postal area.", frame={"country": "AU", "state": "VIC"})
    doc = "requests_by_area.csv"
    rows = [("postcode", "3550", "412"), ("postcode", "3444", "57"), ("lga", "Mount Alexander", "88"),
            ("lga", "City of Greater Bendigo", "1290"), ("postcode", "9726", "3")]
    it.text(doc, "area_type,area,requests\n" + "\n".join(",".join(r) for r in rows) + "\n")
    for n, (kind, area, _) in enumerate(rows, start=2):
        src = C.make_source(doc, row=n)
        abs_name = area.replace("City of ", "")
        try:
            ref = abs_.get(kind, abs_name)
        except KeyError:
            it.add(geometry=None, label=None, mention=area, source=src, tier="structured", method="boundary_join",
                   resolver=ABS_RESOLVER, confidence=0.0, uncertainty_m=None, status="unresolved", source_crs="n/a")
            continue
        label = f"Postcode {area}" if kind == "postcode" else f"{abs_name}, VIC (LGA)"
        it.add(geometry=poly_geojson(ref["geom"]), label=label, mention=area, source=src, tier="structured",
               method="boundary_join", resolver=ABS_RESOLVER, confidence=0.98, uncertainty_m=0, status="resolved",
               source_crs="EPSG:7844")
    it.notes.append("9726 is a PO box postcode with no ABS postal area: unresolved, not guessed.")
    items.append(it.write())

    # ---------------------------------------------------------- tier 1: addresses
    it = Item("t1-addresses", "structured", "Street and landmark addresses in prose. Needs a locator.",
              review="needs_hand_check", frame={"country": "AU", "state": "VIC"})
    doc = "works-notice.txt"
    it.text(doc, """WORKS NOTICE (synthetic test document)
Traffic management will be in place outside Bendigo Town Hall, 189-193 Hargreaves Street, Bendigo VIC 3550 from 6 am.
Equipment staging at the Bendigo railway station car park, Railway Place, Bendigo.
Community drop-in session at Kyneton Town Hall, Hutton Street, Kyneton VIC 3444.
""")
    for mention, ll in [
        ("Bendigo Town Hall, 189-193 Hargreaves Street, Bendigo VIC 3550", (144.2825028, -36.7588188)),
        ("Bendigo railway station car park, Railway Place, Bendigo", (144.2829551, -36.7655937)),
        ("Kyneton Town Hall, Hutton Street, Kyneton VIC 3444", (144.4532558, -37.2470751)),
    ]:
        it.add(network=True, geometry=pt(*ll), label=mention.split(",")[0], mention=mention,
               source=it.span(doc, mention), tier="structured", method="locator", resolver=OSM_RESOLVER,
               confidence=0.9, uncertainty_m=150, status="resolved", source_crs="EPSG:4326")
    it.notes.append("Reference positions are OpenStreetMap features (ODbL); the bar is the nominated locator's "
                    "own result, so re-baseline against it when one is chosen.")
    items.append(it.write())

    # ---------------------------------------------------------- tier 2: Melbourne cluster
    it = Item("t2-melbourne", "named", "Inner-Melbourne names that each exist in other states; the cluster "
              "settles them.", frame={"country": "AU", "state": "VIC", "region": "inner Melbourne"})
    doc = "depot-report.txt"
    it.text(doc, """DEPOT OPERATIONS UPDATE (synthetic test document)
Crews from the Richmond depot will cover Collingwood and Abbotsford this week.
The Cremorne yard is closed for resurfacing; vehicles will park in Richmond.
""")
    rej = [("Richmond, NSW", "the other places named are all inner Melbourne")]
    named(it, abs_, doc, "Richmond", "locality", "Richmond (Vic.)", "Richmond, VIC", 1, rej)
    named(it, abs_, doc, "Collingwood", "locality", "Collingwood (Vic.)", "Collingwood, VIC")
    named(it, abs_, doc, "Abbotsford", "locality", "Abbotsford (Vic.)", "Abbotsford, VIC")
    named(it, abs_, doc, "Cremorne", "locality", "Cremorne (Vic.)", "Cremorne, VIC")
    named(it, abs_, doc, "Richmond", "locality", "Richmond (Vic.)", "Richmond, VIC", 2, rej)
    items.append(it.write())

    # ---------------------------------------------------------- tier 2: Hawkesbury
    it = Item("t2-hawkesbury", "named", "Windsor and Richmond are neighbours in both Melbourne and the "
              "Hawkesbury; only the river settles which.", review="needs_hand_check",
              frame={"country": "AU", "state": "NSW", "region": "Hawkesbury"})
    doc = "flood-note.txt"
    it.text(doc, """FLOOD RESPONSE NOTE (synthetic test document)
Sandbagging continues at Windsor and Richmond. The Hawkesbury River is expected to peak overnight.
""")
    rej = [("Windsor, VIC", "the Hawkesbury River places this in NSW")]
    named(it, abs_, doc, "Windsor", "locality", "Windsor (NSW)", "Windsor, NSW", 1, rej, conf=0.85)
    named(it, abs_, doc, "Richmond", "locality", "Richmond (NSW)", "Richmond, NSW", 1,
          [("Richmond, VIC", "the Hawkesbury River places this in NSW")], conf=0.85)
    it.add(network=True, geometry=pt(150.8170, -33.6020), label="Hawkesbury River", mention="Hawkesbury River",
           source=it.span(doc, "Hawkesbury River"), tier="named", method="gazetteer", resolver=OSM_RESOLVER,
           confidence=0.85, uncertainty_m=5000, status="resolved", source_crs="EPSG:4326")
    it.notes.append("The river's reference is a point near Windsor with 5 km tolerance; a line geometry is "
                    "accepted if it passes within that distance.")
    items.append(it.write())

    # ---------------------------------------------------------- tier 2: Richmond alone
    it = Item("t2-richmond-alone", "named", "A bare place name with no context: must come back ambiguous.",
              frame={"country": "AU"})
    doc = "memo.txt"
    it.text(doc, "Reminder: meet at Richmond on Tuesday at 10.\n")
    cands = []
    for nm in ["Richmond (Vic.)", "Richmond (NSW)", "Richmond (Mackay - Qld)", "Richmond (Richmond - Qld)",
               "Richmond (SA)", "Richmond (Tas.)"]:
        ref = abs_.get("locality", nm)
        cands.append({"label": nm, "rejected": "no state or nearby place in the text", "geometry": pt(*ref["point"])})
    it.add(geometry=None, label=None, mention="Richmond", source=it.span(doc, "Richmond"), tier="named",
           method="gazetteer", resolver=ABS_RESOLVER, confidence=0.17, uncertainty_m=None, status="ambiguous",
           candidates=cands, source_crs="EPSG:7844")
    items.append(it.write())

    # ---------------------------------------------------------- tier 2: Bendigo suburbs
    it = Item("t2-bendigo", "named", "Bendigo suburbs, two of which exist elsewhere, plus a lake the offline "
              "gazetteer does not hold.", frame={"country": "AU", "state": "VIC", "region": "Bendigo"})
    doc = "streetlight-audit.txt"
    it.text(doc, """BENDIGO STREETLIGHT AUDIT (synthetic test document)
Faults logged in Kangaroo Flat, Eaglehawk, Epsom and Strathdale. Two more in Golden Square.
Water quality sampling continues at Lake Eppalock.
""")
    named(it, abs_, doc, "Kangaroo Flat", "locality", "Kangaroo Flat (Vic.)", "Kangaroo Flat, VIC", 1,
          [("Kangaroo Flat, SA", "the other suburbs named are all Bendigo")])
    named(it, abs_, doc, "Eaglehawk", "locality", "Eaglehawk", "Eaglehawk, VIC")
    named(it, abs_, doc, "Epsom", "locality", "Epsom (Vic.)", "Epsom, VIC", 1,
          [("Epsom, QLD", "the other suburbs named are all Bendigo")])
    named(it, abs_, doc, "Strathdale", "locality", "Strathdale", "Strathdale, VIC")
    named(it, abs_, doc, "Golden Square", "locality", "Golden Square", "Golden Square, VIC")
    it.add(network=True, geometry=pt(144.5428476, -36.9045757), label="Lake Eppalock", mention="Lake Eppalock",
           source=it.span(doc, "Lake Eppalock"), tier="named", method="gazetteer", resolver=OSM_RESOLVER,
           confidence=0.9, uncertainty_m=3000, status="resolved", source_crs="EPSG:4326")
    items.append(it.write())

    # ---------------------------------------------------------- tier 2: Springfield
    it = Item("t2-springfield", "named", "Springfield exists in five states; Ipswich settles it. Springfield Lakes "
              "is its own locality and must not collapse into Springfield.",
              frame={"country": "AU", "state": "QLD", "region": "Ipswich"})
    doc = "roadworks.txt"
    it.text(doc, "Road resurfacing in Springfield and Springfield Lakes starts next month, with detours via Ipswich.\n")
    named(it, abs_, doc, "Springfield", "locality", "Springfield (Ipswich - Qld)", "Springfield, QLD", 1,
          [("Springfield, NSW", "Ipswich places this in south-east Queensland")], conf=0.85)
    named(it, abs_, doc, "Springfield Lakes", "locality", "Springfield Lakes", "Springfield Lakes, QLD")
    named(it, abs_, doc, "Ipswich", "locality", "Ipswich", "Ipswich, QLD")
    items.append(it.write())

    # ---------------------------------------------------------- tier 3: relative
    it = Item("t3-relative", "relative", "Offsets, a buffer, a sector and a line along a named road.",
              review="needs_hand_check", frame={"country": "AU", "state": "VIC"})
    doc = "incident-log.txt"
    it.text(doc, """INCIDENT LOG (synthetic test document)
09:10 Smoke reported about 5 km south-east of Castlemaine.
09:40 Spot fires possible within 2 km of Kyneton.
10:15 Lightning strikes reported north of Bendigo.
11:00 Traffic held along the Calder between Kyneton and Castlemaine.
""")
    castle = abs_.get("locality", "Castlemaine")
    kyn = abs_.get("locality", "Kyneton")
    bgo = abs_.get("locality", "Bendigo")
    x, y, _ = GEOD.fwd(castle["point"][0], castle["point"][1], 135, 5000)
    unc = round(castle["radius_m"] + math.hypot(0.25 * 5000, 5000 * math.sin(math.radians(22.5))))
    m = "about 5 km south-east of Castlemaine"
    it.add(geometry=pt(x, y), label="~5 km SE of Castlemaine, VIC", mention=m, source=it.span(doc, m),
           tier="relative", method="constructed", resolver={"name": "reference:pyproj.Geod"}, confidence=0.8,
           uncertainty_m=unc, status="resolved", source_crs="EPSG:7844", flags=["anchor_approximate"])
    ring = [list(GEOD.fwd(kyn["point"][0], kyn["point"][1], az, 2000)[:2]) for az in range(360, 0, -5)]
    ring.append(ring[0])
    m = "within 2 km of Kyneton"
    it.add(geometry={"type": "Polygon", "coordinates": [[[round(a, 6), round(b, 6)] for a, b in ring]]},
           label="Within 2 km of Kyneton, VIC", mention=m, source=it.span(doc, m), tier="relative",
           method="constructed", resolver={"name": "reference:pyproj.Geod"}, confidence=0.8,
           uncertainty_m=kyn["radius_m"], status="resolved", source_crs="EPSG:7844", flags=["anchor_approximate"])
    sector = [list(bgo["point"])]
    for az in range(-22, 23, 3):
        sector.append(list(GEOD.fwd(bgo["point"][0], bgo["point"][1], az % 360, 30000)[:2]))
    sector.append(sector[0])
    sector.reverse()
    m = "north of Bendigo"
    it.add(geometry={"type": "Polygon", "coordinates": [[[round(a, 6), round(b, 6)] for a, b in sector]]},
           label="North of Bendigo, within ~30 km", mention=m, source=it.span(doc, m), tier="relative",
           method="constructed", resolver={"name": "reference:pyproj.Geod"}, confidence=0.75,
           uncertainty_m=bgo["radius_m"], status="resolved", source_crs="EPSG:7844")
    way = [abs_.get("locality", n)["point"] for n in ("Kyneton", "Malmsbury", "Taradale (Vic.)", "Elphinstone (Vic.)")]
    m = "along the Calder between Kyneton and Castlemaine"
    it.add(network=True, geometry={"type": "LineString", "coordinates": [[round(a, 6), round(b, 6)] for a, b in way]},
           label="Calder Freeway, Kyneton to the Castlemaine turn-off", mention=m, source=it.span(doc, m),
           tier="relative", method="trace_segment", resolver={"name": "reference:ABS locality points"},
           confidence=0.8, uncertainty_m=3000, status="resolved", source_crs="EPSG:7844")
    it.notes += ["The sector reach (30 km) and the Calder reference line (locality points of Kyneton, Malmsbury, "
                 "Taradale, Elphinstone) are approximations: geometry type is the primary check, position is "
                 "checked against the stated uncertainty."]
    items.append(it.write())

    # ---------------------------------------------------------- tier 4: implicit
    it = Item("t4-implicit", "implicit", "An organisation's facility with no place name: off by default.",
              frame={"country": "AU"})
    doc = "staff-memo.txt"
    it.text(doc, "The regional water corporation's main treatment plant will run reduced hours. "
                 "Contact the operations manager with questions.\n")
    m = "the regional water corporation's main treatment plant"
    it.add(geometry=None, label=None, mention=m, source=it.span(doc, "The regional water corporation's main treatment plant"),
           tier="implicit", method="not_attempted", resolver={"name": "none"}, confidence=0.0, uncertainty_m=None,
           status="unresolved", source_crs="n/a", flags=["tier4_disabled"])
    items.append(it.write())

    # ---------------------------------------------------------- no places
    it = Item("none-traps", "none", "No places at all: people named after places, versions, money, phone numbers, "
              "and a number pair that looks like coordinates.", frame={"country": "AU"})
    it.text("team-update.txt", """TEAM UPDATE (synthetic test document)
Welcome to Victoria Nguyen and Sydney Smith, who join the analytics team on Monday.
Jordan will run the onboarding. Release 4.2.1 ships on 14/08; build 2026.11.25.
Budget line 1.250, 3.750 approved. Call 1300 555 123.
""")
    items.append(it.write())
    return items


def _write_photo(path: Path, lonlat, extra: dict):
    img = Image.new("RGB", (64, 48), (120, 140, 110))
    exif = img.getexif()
    exif[0x010F] = "SyntheticCam"
    exif[0x0110] = "Model T"
    exif[0x0132] = "2026:03:12 09:15:00"
    if lonlat is not None:
        lon, lat = lonlat

        def dms(v):
            v = abs(v)
            d = int(v)
            m_ = int((v - d) * 60)
            s = round(((v - d) * 60 - m_) * 60, 4)
            return (float(d), float(m_), float(s))

        gps = {0: b"\x02\x03\x00\x00", 1: "S" if lat < 0 else "N", 2: dms(lat), 3: "E" if lon >= 0 else "W",
               4: dms(lon), 29: "2026:03:11", 7: (22.0, 15.0, 0.0)}
        if "heading" in extra:
            gps[16] = "T"
            gps[17] = extra["heading"]
        if "err" in extra:
            gps[31] = extra["err"]
        if "datum" in extra:
            gps[18] = extra["datum"]
        exif[0x8825] = gps
    img.save(path, "JPEG", exif=exif.tobytes(), quality=60)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--abs", type=Path, required=True, help="directory of unzipped ABS shapefiles")
    a = ap.parse_args(argv)
    items = build(ABS(a.abs))
    manifest = {
        "contract_version": C.CONTRACT_VERSION,
        "threshold": C.DEFAULT_THRESHOLD,
        "tolerance_floor_m": {"explicit": 5, "structured": 50, "named": 250, "relative": 500, "implicit": 1000},
        "items": items,
    }
    (CORPUS / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"items": len(items), "features": sum(i["expected_features"] for i in items)}, indent=2))


if __name__ == "__main__":
    main()
