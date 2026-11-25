"""Vendored copy of the skill's output contract. Do not edit here: change
skills/spatialise-anything/scripts/ and run tools/sync_contract.py."""

from . import validate_features  # noqa: F401  (puts this directory on sys.path, then imports contract)
import contract  # noqa: E402,F401

__all__ = ["contract", "validate_features"]
