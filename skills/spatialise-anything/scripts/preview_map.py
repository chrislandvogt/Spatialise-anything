#!/usr/bin/env python3
"""Write a self-contained HTML map preview of a contract FeatureCollection.

Resolved features are drawn with their uncertainty (a circle for points);
ambiguous features show their candidates as hollow markers; every
ambiguous and unresolved mention is listed in a table under the map, so
nothing is silently missing from the picture.

Basemap tiles are requested from a public tile server, which reveals the
area being viewed. For sensitive work use --basemap none.

Usage:
    preview_map.py features.geojson -o preview.html [--basemap osm|none] [--title "..."]
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

LEAFLET_CSS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"
LEAFLET_JS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="__CSS__">
<style>
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--ok:#1f7a4d;--amb:#b26b00;--un:#9b2c2c;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--line:#34332f;--ok:#4cc38a;--amb:#f0a23b;--un:#ef7b7b;--card:#1f1f1d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:14px 16px 8px}h1{font-size:17px;margin:0 0 4px}.sum{color:var(--muted)}
.sum b{font-weight:600}.k{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 4px 0 10px;vertical-align:0}
#map{height:62vh;min-height:320px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--card)}
main{padding:8px 16px 24px;overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:560px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}
.st{font-weight:600}.resolved{color:var(--ok)}.ambiguous{color:var(--amb)}.unresolved{color:var(--un)}
.leaflet-popup-content{font:13px/1.4 system-ui,sans-serif}.pp b{font-weight:600}.pp small{color:#666}
</style></head><body>
<header><h1>__TITLE__</h1><div class="sum" id="sum"></div></header>
<div id="map"></div>
<main><h2 style="font-size:15px">Needs a look</h2><div id="list"></div></main>
<script src="__JS__"></script>
<script>
const FC = __DATA__;
const BASEMAP = "__BASEMAP__";
const css = getComputedStyle(document.documentElement);
const col = s => css.getPropertyValue(s==='resolved'?'--ok':s==='ambiguous'?'--amb':'--un').trim();
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const map = L.map('map', {preferCanvas:true}).setView([-27, 134], 4);  // a view must exist before circles are added
if (BASEMAP === 'osm') {
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19,
    attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
}
const group = L.featureGroup().addTo(map);
const counts = {resolved:0, ambiguous:0, unresolved:0};
const todo = [];
function popup(p){
  const c = (p.candidates||[]).map(x=>`<li>${esc(x.label)} <small>- ${esc(x.rejected)}</small></li>`).join('');
  return `<div class="pp"><b>${esc(p.label||p.mention)}</b><br>"${esc(p.mention)}"<br>
  <small>${esc(p.tier)} &middot; ${esc(p.method)} &middot; ${esc(p.resolver&&p.resolver.name)}</small><br>
  confidence ${p.confidence} &middot; &plusmn;${p.uncertainty_m ?? '-'} m &middot; ${esc(p.source_crs)}
  ${(p.flags||[]).length?'<br><small>flags: '+esc(p.flags.join(', '))+'</small>':''}
  ${c?'<br>other candidates:<ul style="margin:2px 0 0 16px;padding:0">'+c+'</ul>':''}</div>`;
}
for (const f of FC.features) {
  const p = f.properties; counts[p.status] = (counts[p.status]||0)+1;
  const color = col(p.status);
  if (f.geometry) {
    const lyr = L.geoJSON(f, {
      pointToLayer: (ft, ll) => L.circleMarker(ll, {radius:6, color, weight:2, fillOpacity:.8}),
      style: () => ({color, weight:2, fillOpacity:.15})
    }).bindPopup(popup(p)).addTo(group);
    if (f.geometry.type === 'Point' && p.uncertainty_m) {
      const [x,y] = f.geometry.coordinates;
      L.circle([y,x], {radius:p.uncertainty_m, color, weight:1, fillOpacity:.06, interactive:false}).addTo(group);
    }
  } else {
    todo.push(f);
    for (const c of (p.candidates||[])) {
      if (c.geometry && c.geometry.type === 'Point') {
        const [x,y] = c.geometry.coordinates;
        L.circleMarker([y,x], {radius:6, color, weight:2, fillOpacity:0, dashArray:'3'})
          .bindPopup(popup(Object.assign({}, p, {label: c.label + ' (candidate)'}))).addTo(group);
      }
    }
  }
}
document.getElementById('sum').innerHTML =
  `${FC.features.length} mentions<span class="k" style="background:${col('resolved')}"></span><b>${counts.resolved}</b> resolved`+
  `<span class="k" style="background:${col('ambiguous')}"></span><b>${counts.ambiguous}</b> ambiguous`+
  `<span class="k" style="background:${col('unresolved')}"></span><b>${counts.unresolved}</b> unresolved`;
const b = group.getBounds();
if (b.isValid()) map.fitBounds(b.pad(0.15), {maxZoom:15});
document.getElementById('list').innerHTML = todo.length ? `<table><thead><tr><th>Mention</th><th>Status</th>
  <th>Source</th><th>Candidates</th></tr></thead><tbody>${todo.map(f=>{const p=f.properties; const s=p.source||{};
  const where=[s.doc, s.page&&('p.'+s.page), s.row&&('row '+s.row)].filter(Boolean).join(', ');
  return `<tr><td>"${esc(p.mention)}"</td><td class="st ${p.status}">${p.status}</td><td>${esc(where)}</td>
  <td>${(p.candidates||[]).map(c=>esc(c.label)+' <small>('+esc(c.rejected)+')</small>').join('<br>')||'-'}</td></tr>`}).join('')}
  </tbody></table>` : '<p class="sum">Nothing - every mention resolved.</p>';
</script></body></html>
"""


def render(fc: dict, title: str, basemap: str) -> str:
    data = json.dumps(fc, ensure_ascii=False).replace("</", "<\\/")
    return (TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__CSS__", LEAFLET_CSS)
            .replace("__JS__", LEAFLET_JS).replace("__BASEMAP__", basemap).replace("__DATA__", data))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("-o", "--out", default="preview.html")
    ap.add_argument("--basemap", choices=["osm", "none"], default="osm")
    ap.add_argument("--title", default="Spatialise Anything preview")
    a = ap.parse_args(argv)
    fc = json.loads(Path(a.path).read_text(encoding="utf-8"))
    Path(a.out).write_text(render(fc, a.title, a.basemap), encoding="utf-8")
    print(json.dumps({"out": a.out, "features": len(fc.get("features", []))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
