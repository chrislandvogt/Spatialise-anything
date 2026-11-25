"""Confirmation tokens: the count is reported before the batch runs.

A tool that would spend credits, send many place strings to a geocoder, or
write to a portal first returns a dry run with a token. Running it for real
needs that token back. The token is an HMAC over exactly what was reported -
the action, the caller and a digest of the arguments - so it cannot be
guessed up front or reused for a different batch, and it expires.

This makes "nothing is geocoded silently" a property of the server rather
than a promise in a prompt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

TTL_S = 900
_process_key = secrets.token_bytes(32)  # used when no SPATIALISE_SECRET is set: tokens die with the process


def _key(secret: str) -> bytes:
    return hashlib.sha256(b"spatialise-confirm:" + secret.encode()).digest() if secret else _process_key


def args_digest(args: Any) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def issue(secret: str, action: str, subject: str | None, args: Any, now: float | None = None) -> str:
    exp = int((now or time.time()) + TTL_S)
    mac = hmac.new(_key(secret), f"{action}|{subject or ''}|{exp}|{args_digest(args)}".encode(), hashlib.sha256)
    return f"{exp}.{mac.hexdigest()}"


def verify(secret: str, token: str, action: str, subject: str | None, args: Any, now: float | None = None) -> str | None:
    """None when the token is good, else why it is not."""
    try:
        exp_s, mac_hex = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return "the confirmation token is malformed"
    if exp < (now or time.time()):
        return "the confirmation token has expired"
    want = hmac.new(_key(secret), f"{action}|{subject or ''}|{exp}|{args_digest(args)}".encode(), hashlib.sha256)
    if not hmac.compare_digest(want.hexdigest(), mac_hex):
        return "the confirmation token does not match this request (the arguments changed since the dry run)"
    return None
