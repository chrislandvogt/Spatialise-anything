#!/usr/bin/env python3
"""Run the real phase 3 exit test against your own ArcGIS organisation.

It drives the server's tools in-process, as you, and does what a Claude run
would: lists resolvers, geocodes two addresses through the locator you
nominate, and - only with --publish - creates one private hosted feature
layer holding four sample features, then reads the row counts back.

    export ARCGIS_TOKEN=<a token for your account>      # e.g. from ArcGIS Pro or generateToken
    python server/scripts/live_smoke.py --config my-config.toml \\
        --address "1 Treasury Place, East Melbourne VIC" --address "200 Spring St, Melbourne VIC" \\
        [--resolver my-locator] [--publish "Spatialise smoke test"]

Nothing is written without --publish. The layer it creates is private to you;
delete it from your content when you are done. Geocodes through a billed
locator cost credits: the script prints the estimate and asks first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import Client  # noqa: E402

from spatialise_mcp import config  # noqa: E402
from spatialise_mcp._contract import contract as C  # noqa: E402
from spatialise_mcp.server import build  # noqa: E402


async def call(client: Client, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, args)
    if result.is_error:
        raise SystemExit(f"{tool} failed: " + " ".join(getattr(c, "text", "") for c in result.content))
    return result.structured_content


def ask(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() == "y"


async def main(a: argparse.Namespace) -> int:
    cfg = config.load(a.config)
    if cfg.auth_mode != "static":
        raise SystemExit("set ARCGIS_TOKEN (and leave SPATIALISE_ARCGIS_CLIENT_ID unset) to run this script")
    app = build(cfg)
    async with Client(app.mcp) as client:
        res = await call(client, "list_resolvers", {"check": True})
        print("resolvers:")
        for kind, word in (("locators", "locator"), ("gazetteers", "gazetteer"), ("boundaries", "boundary")):
            for e in res[kind]:
                print(f"  {word:<10} {e['name']:<24} {e.get('status', '')}"
                      + (f"  BLOCKED: {e['blocked_reason']}" if e.get("blocked_reason") else ""))
        features = []
        if a.address:
            args = {"queries": a.address, **({"resolver": a.resolver} if a.resolver else {})}
            out = await call(client, "geocode", args)
            if out["status"] == "confirmation_required":
                print(out["message"])
                if not ask("Run these geocodes?"):
                    return 1
                out = await call(client, "geocode", {**args, "confirm_token": out["confirm_token"]})
            print(f"geocoded with {out['resolver']}:")
            for r in out["results"]:
                top = r["candidates"][0] if r["candidates"] else None
                print(f"  {r['query']!r} -> " + (f"{top['label']} {top['point']} score {top['score']} "
                                                  f"+/-{top['uncertainty_m']} m ({len(r['candidates'])} candidates)"
                                                  if top else r["status"]))
                if top:
                    features.append(C.make_feature(
                        geometry=C.point(*top["point"]), label=top["label"], mention=r["query"],
                        source=C.make_source("live_smoke.py"), tier="structured", method=top["method"],
                        resolver=top["resolver"], confidence=0.9, uncertainty_m=top["uncertainty_m"],
                        status="resolved", source_crs="EPSG:4326"))
                else:
                    features.append(C.make_feature(
                        geometry=None, label=None, mention=r["query"], source=C.make_source("live_smoke.py"),
                        tier="structured", method="locator", resolver=out["resolver"], confidence=0.0,
                        uncertainty_m=None, status="unresolved", source_crs="n/a"))
        if not a.publish:
            print("no --publish given: nothing was written.")
            return 0
        features += [
            C.make_feature(geometry={"type": "LineString", "coordinates": [[144.95, -37.81], [144.97, -37.82]]},
                           label="sample line", mention="sample line", source=C.make_source("live_smoke.py"),
                           tier="relative", method="constructed", resolver={"name": "live_smoke"}, confidence=0.9,
                           uncertainty_m=100, status="resolved"),
            C.make_feature(geometry={"type": "Polygon", "coordinates": [[[144.95, -37.82], [144.97, -37.82],
                                                                         [144.97, -37.80], [144.95, -37.80],
                                                                         [144.95, -37.82]]]},
                           label="sample polygon", mention="sample polygon", source=C.make_source("live_smoke.py"),
                           tier="structured", method="boundary_join", resolver={"name": "live_smoke"},
                           confidence=0.9, uncertainty_m=0, status="resolved"),
            C.make_feature(geometry=None, label=None, mention="sample ambiguous mention",
                           source=C.make_source("live_smoke.py"), tier="named", method="gazetteer",
                           resolver={"name": "live_smoke"}, confidence=0.3, uncertainty_m=None, status="ambiguous",
                           candidates=[{"label": "A", "rejected": "sample"}, {"label": "B", "rejected": "sample"}]),
        ]
        fc = C.make_collection(features, inputs=["live_smoke.py"], threshold=C.DEFAULT_THRESHOLD)
        plan = await call(client, "publish_features", {"features": fc, "create": a.publish})
        print("publish plan:", json.dumps(plan["plan"], indent=2))
        if not ask("Create this layer?"):
            return 1
        done = await call(client, "publish_features", {"features": fc, "create": a.publish,
                                                       "confirm_token": plan["confirm_token"]})
        print(json.dumps(done, indent=2))
        return 0 if done["status"] == "published" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--address", action="append", default=[], help="an address to geocode (repeatable)")
    ap.add_argument("--resolver", help="locator name; default is the first one policy allows")
    ap.add_argument("--publish", metavar="TITLE", help="create a private hosted feature layer with this title")
    sys.exit(anyio.run(main, ap.parse_args()))
