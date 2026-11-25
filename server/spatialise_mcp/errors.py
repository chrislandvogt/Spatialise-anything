"""Errors the tools raise on purpose. Each message tells the model what to do next."""

from __future__ import annotations


class SpatialiseError(Exception):
    """A failure with a message that is safe and useful to show the model."""


class ConfigError(SpatialiseError):
    """The server's configuration is wrong. Raised at start-up, never per request."""


class PolicyError(SpatialiseError):
    """The request is well-formed but the server's policy does not allow it."""


class SignInRequired(SpatialiseError):
    """The resource needs the user's ArcGIS sign-in and there is none (or it expired)."""


class ArcGISError(SpatialiseError):
    """An ArcGIS REST operation returned an error object."""

    def __init__(self, code: int | None, message: str, details: list[str] | None = None, host: str = ""):
        self.code = code
        self.arcgis_message = message
        self.details = [d for d in (details or []) if d]
        self.host = host
        text = f"ArcGIS error {code} from {host}: {message}"
        if self.details:
            text += " (" + "; ".join(self.details[:3]) + ")"
        super().__init__(text)
