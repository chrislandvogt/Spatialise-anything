"""The server validates features with the skill's own contract code. If the
vendored copy drifts from the skill, publishing would accept or reject
different features than the skill produces."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SKILL = ROOT / "skills" / "spatialise-anything" / "scripts"
VENDORED = ROOT / "server" / "spatialise_mcp" / "_contract"


@pytest.mark.parametrize("name", ["contract.py", "validate_features.py"])
def test_vendored_contract_matches_the_skill(name):
    assert (VENDORED / name).read_bytes() == (SKILL / name).read_bytes(), \
        f"{name} differs: run python tools/sync_contract.py"


def test_publish_schema_covers_every_contract_property():
    from spatialise_mcp import publish
    from spatialise_mcp._contract import validate_features as V
    stored = {name for name, *_ in publish.FIELDS}
    mapping = {"source": {"source_doc", "source_page", "source_sheet", "source_row", "source_column", "char_start",
                          "char_end"},
               "resolver": {"resolver_name", "resolver_version"}, "candidates": {"candidates_json", "candidate_count"}}
    for prop in (*V.REQUIRED_PROPS, *V.OPTIONAL_PROPS):
        assert mapping.get(prop, {prop}) <= stored, f"contract property {prop!r} is not stored when publishing"
