"""Run the Spatialise MCP server.

    spatialise-mcp serve [--config FILE] [--host 127.0.0.1] [--port 8000]
        Streamable HTTP at /mcp. With an OAuth app configured, clients sign in
        through the ArcGIS portal. This is the remote server claude.ai connects to.

    spatialise-mcp stdio [--config FILE]
        For Claude Code or desktop Cowork on your own machine. Uses ARCGIS_TOKEN
        from the environment if set; otherwise only public resources work.

    spatialise-mcp check [--config FILE]
        Load the config, contact every resolver, and print what answered.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import anyio

from . import __version__, config
from .arcgis import ArcGIS, Anonymous, StaticIdentity
from .errors import SpatialiseError
from .resolvers import Resolvers
from .server import build


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="spatialise-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "stdio", "check"):
        p = sub.add_parser(name)
        p.add_argument("--config", help="TOML config file (default: $SPATIALISE_CONFIG)")
        if name == "serve":
            p.add_argument("--host", default="127.0.0.1", help="interface to bind (0.0.0.0 in a container)")
            p.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore", "mcp"):   # per-request lines add nothing and name every service called
        logging.getLogger(noisy).setLevel(logging.WARNING)
    os.umask(0o077)  # anything this process creates (the sign-in store and its journal) is private to it
    try:
        cfg = config.load(a.config)
        if a.cmd == "stdio":
            if cfg.auth_mode == "oauth":
                raise SpatialiseError("stdio has no browser sign-in. Unset SPATIALISE_ARCGIS_CLIENT_ID and use "
                                      "ARCGIS_TOKEN, or run 'serve' and connect over HTTP.")
            build(cfg).mcp.run("stdio")
            return 0
        if a.cmd == "check":
            return anyio.run(_check, cfg)
        import uvicorn
        app = build(cfg)
        asgi = app.asgi(host=a.host)
        logging.getLogger("spatialise_mcp").info(
            "serving %s/mcp (auth: %s, locators: %d, gazetteers: %d, boundary sets: %d)", cfg.public_url,
            cfg.auth_mode, len(cfg.locators), len(cfg.gazetteers), len(cfg.boundaries))
        uvicorn.run(asgi, host=a.host, port=a.port, log_level="warning", proxy_headers=True,
                    forwarded_allow_ips="*", access_log=False)
        return 0
    except SpatialiseError as e:
        print(f"spatialise-mcp: {e}", file=sys.stderr)
        return 2


async def _check(cfg: config.Config) -> int:
    import httpx
    async with httpx.AsyncClient(timeout=30) as http:
        identity = StaticIdentity(cfg.static_token) if cfg.static_token else Anonymous()
        out = await Resolvers(cfg, ArcGIS(cfg, http, identity)).list_resolvers(check=True)
    print(json.dumps(out, indent=2))
    entries = out["locators"] + out["gazetteers"] + out["boundaries"]
    bad = [e["name"] for e in entries if e.get("status", "ok") not in ("ok", "not checked (blocked by policy)")
           and not (e["needs_sign_in"] and not cfg.static_token)]
    if bad:
        print(f"spatialise-mcp: not answering: {bad}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
