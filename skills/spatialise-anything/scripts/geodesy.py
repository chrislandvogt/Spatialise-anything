"""Geodesy helpers, pure standard library.

* Transverse Mercator via the Krüger n-series to order n^6 (Karney 2011),
  accurate to well under a millimetre within a UTM/MGA zone.
* UTM / MGA grid <-> geographic, and MGRS strings -> UTM.
* GDA94 -> GDA2020 using the ICSM conformal 7-parameter transformation
  (coordinate-frame rotation convention). The published conformal +
  distortion grid adds at most a few decimetres more, which is below the
  uncertainty this tool reports.
* Geodesic direct and inverse problems on the ellipsoid (Vincenty), used to
  build offsets, buffers and sectors in metres.

GRS80 (GDA94, GDA2020) and WGS84 differ in the flattening at the 1e-11
level; the difference is far below a millimetre and both use GRS80 here.
"""

from __future__ import annotations

import math

# GRS80 ellipsoid
A_GRS80 = 6378137.0
F_GRS80 = 1 / 298.257222101
E2 = F_GRS80 * (2 - F_GRS80)
E = math.sqrt(E2)
B_GRS80 = A_GRS80 * (1 - F_GRS80)

UTM_K0 = 0.9996
UTM_E0 = 500000.0
UTM_N0_SOUTH = 10000000.0

_n = F_GRS80 / (2 - F_GRS80)
_n2, _n3, _n4, _n5, _n6 = _n**2, _n**3, _n**4, _n**5, _n**6
_A = A_GRS80 / (1 + _n) * (1 + _n2 / 4 + _n4 / 64 + _n6 / 256)
_ALPHA = (
    _n / 2 - 2 * _n2 / 3 + 5 * _n3 / 16 + 41 * _n4 / 180 - 127 * _n5 / 288 + 7891 * _n6 / 37800,
    13 * _n2 / 48 - 3 * _n3 / 5 + 557 * _n4 / 1440 + 281 * _n5 / 630 - 1983433 * _n6 / 1935360,
    61 * _n3 / 240 - 103 * _n4 / 140 + 15061 * _n5 / 26880 + 167603 * _n6 / 181440,
    49561 * _n4 / 161280 - 179 * _n5 / 168 + 6601661 * _n6 / 7257600,
    34729 * _n5 / 80640 - 3418889 * _n6 / 1995840,
    212378941 * _n6 / 319334400,
)
_BETA = (
    _n / 2 - 2 * _n2 / 3 + 37 * _n3 / 96 - _n4 / 360 - 81 * _n5 / 512 + 96199 * _n6 / 604800,
    _n2 / 48 + _n3 / 15 - 437 * _n4 / 1440 + 46 * _n5 / 105 - 1118711 * _n6 / 3870720,
    17 * _n3 / 480 - 37 * _n4 / 840 - 209 * _n5 / 4480 + 5569 * _n6 / 90720,
    4397 * _n4 / 161280 - 11 * _n5 / 504 - 830251 * _n6 / 7257600,
    4583 * _n5 / 161280 - 108847 * _n6 / 3991680,
    20648693 * _n6 / 638668800,
)


# ---------------------------------------------------------------- TM / UTM

def tm_forward(lon: float, lat: float, lon0: float, k0: float = UTM_K0) -> tuple[float, float]:
    """Geographic (degrees) to TM (x, y) in metres about central meridian lon0,
    before false easting/northing."""
    phi = math.radians(lat)
    lam = math.radians(lon - lon0)
    sphi = math.sin(phi)
    t = math.sinh(math.atanh(sphi) - E * math.atanh(E * sphi))
    xi_p = math.atan2(t, math.cos(lam))
    eta_p = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    xi, eta = xi_p, eta_p
    for j, a in enumerate(_ALPHA, start=1):
        xi += a * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
        eta += a * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
    return k0 * _A * eta, k0 * _A * xi


def tm_inverse(x: float, y: float, lon0: float, k0: float = UTM_K0) -> tuple[float, float]:
    """TM (x, y) metres about lon0 (no false origin) to (lon, lat) degrees."""
    xi = y / (k0 * _A)
    eta = x / (k0 * _A)
    xi_p, eta_p = xi, eta
    for j, b in enumerate(_BETA, start=1):
        xi_p -= b * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        eta_p -= b * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    sinh_eta = math.sinh(eta_p)
    cos_xi = math.cos(xi_p)
    tau_p = math.sin(xi_p) / math.sqrt(sinh_eta**2 + cos_xi**2)
    lam = math.atan2(sinh_eta, cos_xi)
    # Solve for tau = tan(phi) from the conformal tau_p (Karney 2011, eq. 19-21).
    tau = tau_p
    for _ in range(8):
        sigma = math.sinh(E * math.atanh(E * tau / math.sqrt(1 + tau * tau)))
        tau_i = tau * math.sqrt(1 + sigma * sigma) - sigma * math.sqrt(1 + tau * tau)
        d = (tau_p - tau_i) / math.sqrt(1 + tau_i * tau_i) * (1 + (1 - E2) * tau * tau) / (
            (1 - E2) * math.sqrt(1 + tau * tau))
        tau += d
        if abs(d) < 1e-14:
            break
    return lon0 + math.degrees(lam), math.degrees(math.atan(tau))


def utm_central_meridian(zone: int) -> float:
    return -183.0 + 6.0 * zone


def utm_zone_for(lon: float) -> int:
    return int((lon + 180) // 6) % 60 + 1


def utm_to_geographic(easting: float, northing: float, zone: int, south: bool = True) -> tuple[float, float]:
    """UTM / MGA grid to (lon, lat). MGA is UTM on GRS80 with the same
    parameters, so this serves MGA94 and MGA2020 alike (the datum is a
    separate question)."""
    if not 1 <= zone <= 60:
        raise ValueError(f"UTM zone {zone} out of range")
    x = easting - UTM_E0
    y = northing - (UTM_N0_SOUTH if south else 0.0)
    return tm_inverse(x, y, utm_central_meridian(zone))


def geographic_to_utm(lon: float, lat: float, zone: int | None = None) -> tuple[float, float, int, bool]:
    zone = zone or utm_zone_for(lon)
    x, y = tm_forward(lon, lat, utm_central_meridian(zone))
    south = lat < 0
    return x + UTM_E0, y + (UTM_N0_SOUTH if south else 0.0), zone, south


# ---------------------------------------------------------------- MGRS

_BANDS = "CDEFGHJKLMNPQRSTUVWX"  # 8 degree bands from -80, X is 12 degrees
_COL_SETS = ("STUVWXYZ", "ABCDEFGH", "JKLMNPQR")  # index by zone % 3
_ROWS = "ABCDEFGHJKLMNPQRSTUV"  # 20 letters, I and O omitted


def band_south_lat(band: str) -> float:
    i = _BANDS.index(band.upper())
    return -80.0 + 8.0 * i


def mgrs_to_utm(zone: int, band: str, col: str, row: str, e_digits: str, n_digits: str) -> tuple[float, float, bool, float]:
    """Decode MGRS parts to (easting, northing, south, precision_m).

    precision_m is the size of the grid cell the digits name (e.g. 10 m for
    4+4 digits); the true position is anywhere in that cell, and the
    returned easting/northing is the cell centre.
    """
    band = band.upper()
    col = col.upper()
    row = row.upper()
    if band not in _BANDS:
        raise ValueError(f"invalid MGRS latitude band {band!r}")
    cols = _COL_SETS[zone % 3]
    if col not in cols:
        raise ValueError(f"100 km column {col!r} not valid in zone {zone}")
    if row not in _ROWS:
        raise ValueError(f"invalid 100 km row letter {row!r}")
    if len(e_digits) != len(n_digits) or len(e_digits) > 5:
        raise ValueError("MGRS easting and northing need the same number of digits (0-5)")
    k = len(e_digits)
    precision = 10 ** (5 - k)
    e_off = int(e_digits) * precision if k else 0
    n_off = int(n_digits) * precision if k else 0
    easting = (cols.index(col) + 1) * 100000 + e_off + precision / 2
    row_idx = _ROWS.index(row)
    if zone % 2 == 0:
        row_idx = (row_idx - 5) % 20
    northing = row_idx * 100000 + n_off + precision / 2
    south = band < "N"
    # Lift northing into the band: add 2,000 km cycles until it reaches the
    # band's southern edge (less a 100 km margin for parallel curvature).
    lat_s = band_south_lat(band)
    _, y_min = tm_forward(utm_central_meridian(zone), lat_s, utm_central_meridian(zone))
    y_min += UTM_N0_SOUTH if south else 0.0
    y_min -= 100000
    while northing < y_min:
        northing += 2000000
    return easting, northing, south, float(precision)


# ---------------------------------------------------------------- datums

def geodetic_to_ecef(lon: float, lat: float, h: float = 0.0) -> tuple[float, float, float]:
    phi, lam = math.radians(lat), math.radians(lon)
    n = A_GRS80 / math.sqrt(1 - E2 * math.sin(phi) ** 2)
    return ((n + h) * math.cos(phi) * math.cos(lam),
            (n + h) * math.cos(phi) * math.sin(lam),
            (n * (1 - E2) + h) * math.sin(phi))


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    phi = math.atan2(z, p * (1 - E2))
    h = 0.0
    for _ in range(10):
        n = A_GRS80 / math.sqrt(1 - E2 * math.sin(phi) ** 2)
        h = p / math.cos(phi) - n
        new = math.atan2(z, p * (1 - E2 * n / (n + h)))
        if abs(new - phi) < 1e-14:
            phi = new
            break
        phi = new
    return math.degrees(lon), math.degrees(phi), h


# ICSM GDA2020 Technical Manual: GDA94 -> GDA2020 conformal transformation.
_GDA94_TO_GDA2020 = dict(tx=0.06155, ty=-0.01087, tz=-0.04019,
                         rx=-0.0394924, ry=-0.0327221, rz=-0.0328979, sc=-0.009994)


def gda94_to_gda2020(lon: float, lat: float, h: float = 0.0) -> tuple[float, float]:
    p = _GDA94_TO_GDA2020
    sec = math.pi / (180 * 3600)
    rx, ry, rz = p["rx"] * sec, p["ry"] * sec, p["rz"] * sec
    s = 1 + p["sc"] * 1e-6
    x, y, z = geodetic_to_ecef(lon, lat, h)
    # Coordinate-frame rotation convention (EPSG method 1032).
    x2 = p["tx"] + s * (x + rz * y - ry * z)
    y2 = p["ty"] + s * (-rz * x + y + rx * z)
    z2 = p["tz"] + s * (ry * x - rx * y + z)
    lon2, lat2, _ = ecef_to_geodetic(x2, y2, z2)
    return lon2, lat2


# ---------------------------------------------------------------- geodesics

def destination(lon: float, lat: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Vincenty direct problem on GRS80: start point, initial bearing
    (degrees clockwise from true north) and distance -> end point."""
    a, b, f = A_GRS80, B_GRS80, F_GRS80
    alpha1 = math.radians(bearing_deg)
    s_a1, c_a1 = math.sin(alpha1), math.cos(alpha1)
    tan_u1 = (1 - f) * math.tan(math.radians(lat))
    cos_u1 = 1 / math.sqrt(1 + tan_u1 * tan_u1)
    sin_u1 = tan_u1 * cos_u1
    sigma1 = math.atan2(tan_u1, c_a1)
    sin_alpha = cos_u1 * s_a1
    cos2_alpha = 1 - sin_alpha * sin_alpha
    u2 = cos2_alpha * (a * a - b * b) / (b * b)
    big_a = 1 + u2 / 16384 * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)))
    big_b = u2 / 1024 * (256 + u2 * (-128 + u2 * (74 - 47 * u2)))
    sigma = distance_m / (b * big_a)
    for _ in range(200):
        cos_2sm = math.cos(2 * sigma1 + sigma)
        s_s, c_s = math.sin(sigma), math.cos(sigma)
        d_sigma = big_b * s_s * (cos_2sm + big_b / 4 * (c_s * (-1 + 2 * cos_2sm**2)
                  - big_b / 6 * cos_2sm * (-3 + 4 * s_s**2) * (-3 + 4 * cos_2sm**2)))
        new = distance_m / (b * big_a) + d_sigma
        if abs(new - sigma) < 1e-12:
            sigma = new
            break
        sigma = new
    s_s, c_s = math.sin(sigma), math.cos(sigma)
    cos_2sm = math.cos(2 * sigma1 + sigma)
    tmp = sin_u1 * s_s - cos_u1 * c_s * c_a1
    lat2 = math.atan2(sin_u1 * c_s + cos_u1 * s_s * c_a1,
                      (1 - f) * math.sqrt(sin_alpha**2 + tmp * tmp))
    lam = math.atan2(s_s * s_a1, cos_u1 * c_s - sin_u1 * s_s * c_a1)
    c = f / 16 * cos2_alpha * (4 + f * (4 - 3 * cos2_alpha))
    big_l = lam - (1 - c) * f * sin_alpha * (sigma + c * s_s * (cos_2sm + c * c_s * (-1 + 2 * cos_2sm**2)))
    lon2 = (lon + math.degrees(big_l) + 540) % 360 - 180
    return lon2, math.degrees(lat2)


def inverse(lon1: float, lat1: float, lon2: float, lat2: float) -> tuple[float, float]:
    """Vincenty inverse on GRS80 -> (distance_m, initial bearing degrees).
    Falls back to a spherical answer for near-antipodal points, where
    Vincenty does not converge (never the case for a local document)."""
    if lon1 == lon2 and lat1 == lat2:
        return 0.0, 0.0
    a, b, f = A_GRS80, B_GRS80, F_GRS80
    big_l = math.radians(lon2 - lon1)
    u1 = math.atan((1 - f) * math.tan(math.radians(lat1)))
    u2 = math.atan((1 - f) * math.tan(math.radians(lat2)))
    s_u1, c_u1, s_u2, c_u2 = math.sin(u1), math.cos(u1), math.sin(u2), math.cos(u2)
    lam = big_l
    for _ in range(200):
        s_l, c_l = math.sin(lam), math.cos(lam)
        s_sigma = math.hypot(c_u2 * s_l, c_u1 * s_u2 - s_u1 * c_u2 * c_l)
        if s_sigma == 0:
            return 0.0, 0.0
        c_sigma = s_u1 * s_u2 + c_u1 * c_u2 * c_l
        sigma = math.atan2(s_sigma, c_sigma)
        s_alpha = c_u1 * c_u2 * s_l / s_sigma
        c2_alpha = 1 - s_alpha**2
        c_2sm = c_sigma - 2 * s_u1 * s_u2 / c2_alpha if c2_alpha else 0.0
        c = f / 16 * c2_alpha * (4 + f * (4 - 3 * c2_alpha))
        prev = lam
        lam = big_l + (1 - c) * f * s_alpha * (sigma + c * s_sigma * (c_2sm + c * c_sigma * (-1 + 2 * c_2sm**2)))
        if abs(lam - prev) < 1e-12:
            break
    else:
        return haversine(lon1, lat1, lon2, lat2), _sph_bearing(lon1, lat1, lon2, lat2)
    u_sq = c2_alpha * (a * a - b * b) / (b * b)
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    d_sigma = big_b * s_sigma * (c_2sm + big_b / 4 * (c_sigma * (-1 + 2 * c_2sm**2)
              - big_b / 6 * c_2sm * (-3 + 4 * s_sigma**2) * (-3 + 4 * c_2sm**2)))
    dist = b * big_a * (sigma - d_sigma)
    brg = math.degrees(math.atan2(c_u2 * math.sin(lam), c_u1 * s_u2 - s_u1 * c_u2 * math.cos(lam)))
    return dist, brg % 360


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def _sph_bearing(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def metres_per_degree_lat() -> float:
    return 111132.0
