#!/usr/bin/env python3
"""Read GPS position, heading and time from photo EXIF, before anything
looks at the picture itself.

Pure standard library: parses JPEG APP1 EXIF and bare TIFF files. HEIC and
other containers are reported as unsupported rather than guessed at.

A photo with no GPS block still becomes a feature (unresolved), so a photo
set always comes back with one feature per photo.

Usage:
    read_exif.py PHOTO [PHOTO ...] [--format features|records]
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract as C  # noqa: E402

RESOLVER = C.resolver("read_exif", C.CONTRACT_VERSION)
DEFAULT_GPS_UNCERTAINTY_M = 10.0  # typical phone GNSS when no error is recorded

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8}

GPS_TAGS = {
    0: "GPSVersionID", 1: "GPSLatitudeRef", 2: "GPSLatitude", 3: "GPSLongitudeRef", 4: "GPSLongitude",
    5: "GPSAltitudeRef", 6: "GPSAltitude", 7: "GPSTimeStamp", 16: "GPSImgDirectionRef",
    17: "GPSImgDirection", 18: "GPSMapDatum", 29: "GPSDateStamp", 31: "GPSHPositioningError",
}
EXIF_TAGS = {0x9003: "DateTimeOriginal", 0x9011: "OffsetTimeOriginal"}
IFD0_TAGS = {0x010F: "Make", 0x0110: "Model", 0x0132: "DateTime", 0x8825: "GPSInfo", 0x8769: "ExifIFD"}


class ExifError(Exception):
    pass


def _tiff_block(data: bytes) -> bytes | None:
    """Return the TIFF structure inside a JPEG APP1 Exif segment, or the
    file itself when it is a TIFF."""
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return data
    if data[:2] != b"\xff\xd8":
        raise ExifError("not a JPEG or TIFF file")
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            raise ExifError("corrupt JPEG marker stream")
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):  # end of image / start of scan: no EXIF before pixels
            return None
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            return data[i + 10:i + 2 + seg_len]
        i += 2 + seg_len
    return None


def _read_ifd(tiff: bytes, offset: int, endian: str, names: dict) -> dict:
    out = {}
    if offset + 2 > len(tiff):
        return out
    n = struct.unpack(endian + "H", tiff[offset:offset + 2])[0]
    for k in range(n):
        e = offset + 2 + 12 * k
        if e + 12 > len(tiff):
            break
        tag, typ, count = struct.unpack(endian + "HHI", tiff[e:e + 8])
        size = _TYPE_SIZES.get(typ, 1) * count
        raw = tiff[e + 8:e + 12] if size <= 4 else tiff[struct.unpack(endian + "I", tiff[e + 8:e + 12])[0]:][:size]
        name = names.get(tag)
        if name is None:
            continue
        out[name] = _decode(raw, typ, count, endian)
    return out


def _decode(raw: bytes, typ: int, count: int, endian: str):
    if typ == 2:
        return raw[:count].split(b"\x00")[0].decode("ascii", "replace").strip()
    if typ in (1, 7):
        return list(raw[:count])
    if typ == 3:
        v = list(struct.unpack(endian + "H" * count, raw[:2 * count]))
    elif typ == 4:
        v = list(struct.unpack(endian + "I" * count, raw[:4 * count]))
    elif typ == 9:
        v = list(struct.unpack(endian + "i" * count, raw[:4 * count]))
    elif typ in (5, 10):
        fmt = "I" if typ == 5 else "i"
        nums = struct.unpack(endian + fmt * (2 * count), raw[:8 * count])
        v = [(nums[2 * j] / nums[2 * j + 1]) if nums[2 * j + 1] else None for j in range(count)]
    else:
        return None
    return v[0] if count == 1 else v


def read_tags(path: Path) -> dict:
    data = path.read_bytes()
    tiff = _tiff_block(data)
    if tiff is None:
        return {}
    endian = "<" if tiff[:2] == b"II" else ">"
    ifd0 = struct.unpack(endian + "I", tiff[4:8])[0]
    tags = _read_ifd(tiff, ifd0, endian, IFD0_TAGS)
    if "GPSInfo" in tags:
        tags["GPS"] = _read_ifd(tiff, int(tags.pop("GPSInfo")), endian, GPS_TAGS)
    if "ExifIFD" in tags:
        tags.update(_read_ifd(tiff, int(tags.pop("ExifIFD")), endian, EXIF_TAGS))
    return tags


def _dms(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if any(x is None for x in v):
        return None
    d = v[0] + (v[1] if len(v) > 1 else 0) / 60 + (v[2] if len(v) > 2 else 0) / 3600
    return d


def record(path: Path) -> dict:
    rec = {"file": path.name, "warnings": []}
    try:
        tags = read_tags(path)
    except (ExifError, struct.error, OSError) as e:
        rec["error"] = str(e)
        return rec
    gps = tags.get("GPS", {})
    lat, lon = _dms(gps.get("GPSLatitude")), _dms(gps.get("GPSLongitude"))
    if lat is not None and lon is not None:
        if str(gps.get("GPSLatitudeRef", "N")).upper().startswith("S"):
            lat = -lat
        if str(gps.get("GPSLongitudeRef", "E")).upper().startswith("W"):
            lon = -lon
        if lat == 0 and lon == 0:
            rec["warnings"].append("GPS reads 0,0 - the camera had no fix")
        else:
            rec["lat"], rec["lon"] = lat, lon
    alt = gps.get("GPSAltitude")
    if alt is not None:
        rec["altitude_m"] = -alt if gps.get("GPSAltitudeRef") in ([1], 1) else alt
    if gps.get("GPSImgDirection") is not None:
        rec["heading_deg"] = gps["GPSImgDirection"]
        rec["heading_ref"] = {"T": "true", "M": "magnetic"}.get(str(gps.get("GPSImgDirectionRef", "")).upper(), "unknown")
    if gps.get("GPSHPositioningError") is not None:
        rec["h_positioning_error_m"] = gps["GPSHPositioningError"]
    if gps.get("GPSMapDatum"):
        rec["datum"] = gps["GPSMapDatum"]
    ts = gps.get("GPSTimeStamp")
    if gps.get("GPSDateStamp") and isinstance(ts, list) and all(x is not None for x in ts):
        rec["gps_time_utc"] = f"{gps['GPSDateStamp'].replace(':', '-')}T{int(ts[0]):02d}:{int(ts[1]):02d}:{int(ts[2]):02d}Z"
    if tags.get("DateTimeOriginal"):
        rec["taken_local"] = tags["DateTimeOriginal"]
        if tags.get("OffsetTimeOriginal"):
            rec["taken_offset"] = tags["OffsetTimeOriginal"]
    for k in ("Make", "Model"):
        if tags.get(k):
            rec[k.lower()] = tags[k]
    return rec


def to_feature(rec: dict, doc: str | None = None) -> dict:
    doc = doc or rec["file"]
    mention = f"photo {rec['file']}"
    if "lat" not in rec:
        reason = rec.get("error") or (rec["warnings"][0] if rec["warnings"] else "no GPS position in EXIF")
        return C.make_feature(geometry=None, label=None, mention=mention, source=C.make_source(doc),
                              tier="explicit", method="exif", resolver=RESOLVER, confidence=0.0,
                              uncertainty_m=None, status="unresolved", source_crs="unknown",
                              candidates=[], flags=[])
    flags = []
    datum = str(rec.get("datum", "")).upper().replace("-", "").replace(" ", "")
    if not datum:
        flags.append("datum_assumed")
        crs = "EPSG:4326 (assumed)"
    elif datum.startswith("WGS84") or datum == "WGS1984":
        crs = "EPSG:4326"
    else:
        crs = f"EXIF datum {rec['datum']}"
    unc = rec.get("h_positioning_error_m")
    if unc is None:
        unc = DEFAULT_GPS_UNCERTAINTY_M
        flags.append("default_uncertainty")
    label = f"Photo {rec['file']}"
    if rec.get("taken_local"):
        label += f", {rec['taken_local']}"
    if rec.get("heading_deg") is not None:
        label += f", facing {rec['heading_deg']:.0f}° {rec.get('heading_ref', '')}".rstrip()
    return C.make_feature(geometry=C.point(rec["lon"], rec["lat"]), label=label, mention=mention,
                          source=C.make_source(doc), tier="explicit", method="exif", resolver=RESOLVER,
                          confidence=0.95, uncertainty_m=unc, status="resolved", source_crs=crs, flags=flags)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("photos", nargs="+")
    ap.add_argument("--format", choices=["features", "records"], default="features")
    a = ap.parse_args(argv)
    recs = [record(Path(p)) for p in a.photos]
    if a.format == "records":
        print(json.dumps(recs, indent=2, default=str))
        return 0
    feats = [to_feature(r) for r in recs]
    C.dump(C.make_collection(feats, inputs=[r["file"] for r in recs], threshold=C.DEFAULT_THRESHOLD), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
