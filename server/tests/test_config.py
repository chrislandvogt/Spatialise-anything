import pytest

from spatialise_mcp import config as C
from spatialise_mcp.errors import ConfigError, PolicyError

S = "s" * 40


def cfg(data=None, **env):
    return C.from_dict(data or {}, env)


def test_defaults_are_closed():
    c = cfg()
    assert c.auth_mode == "none" and c.policy.allow_public_geocoder is False
    assert c.portal.trusts("https://services3.arcgis.com/x") and c.portal.trusts("https://www.arcgis.com/")
    assert not c.portal.trusts("https://evil.example/arcgis.com") and not c.portal.trusts("https://arcgis.com.evil.example")


def test_enterprise_portal_trusts_only_named_hosts():
    c = cfg({"portal": {"url": "https://gis.agency.gov.au/portal", "trusted_hosts": ["maps.agency.gov.au"]}})
    assert c.portal.trusts("https://gis.agency.gov.au/server/rest") and c.portal.trusts("https://maps.agency.gov.au/x")
    assert not c.portal.trusts("https://services.arcgis.com/x")
    assert c.portal.rest == "https://gis.agency.gov.au/portal/sharing/rest"


@pytest.mark.parametrize("pattern", ["*", "*.com", "*.*"])
def test_broad_trust_patterns_are_refused(pattern):
    with pytest.raises(ConfigError, match="too broad"):
        cfg({"portal": {"url": "https://p.example.org", "trusted_hosts": [pattern]}})


def test_user_token_is_never_configured_for_an_untrusted_host():
    with pytest.raises(ConfigError, match="not a\\s+trusted host"):
        cfg({"portal": {"url": "https://p.example.org"},
             "gazetteers": [{"name": "g", "url": "https://other.example.com/FeatureServer/0", "name_field": "N",
                             "auth": "user"}]})
    c = cfg({"portal": {"url": "https://p.example.org"},
             "gazetteers": [{"name": "g", "url": "https://other.example.com/FeatureServer/0", "name_field": "N"}]})
    assert c.gazetteers[0].auth == "none"  # default for an untrusted host: public, no token


def test_public_geocoder_cannot_be_relabelled():
    world = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer"
    with pytest.raises(ConfigError, match="kind must be 'public'"):
        cfg({"locators": [{"name": "w", "url": world, "kind": "customer"}]})
    with pytest.raises(ConfigError, match="for_storage"):
        cfg({"locators": [{"name": "w", "url": world, "kind": "public", "for_storage": False}]})
    c = cfg({"locators": [{"name": "w", "url": world, "kind": "public"}]})
    assert c.locators[0].for_storage and c.locators[0].credits_per_1000 == 40


def test_resolver_order_and_public_switch():
    locs = [{"name": "w", "url": "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer", "kind": "public"},
            {"name": "org", "url": "https://utility.arcgis.com/x/GeocodeServer", "kind": "org"},
            {"name": "cust", "url": "https://services.arcgis.com/x/GeocodeServer", "kind": "customer"}]
    c = cfg({"locators": locs})
    assert [l.name for l in c.ordered_locators()] == ["cust", "org", "w"]
    assert c.locator(None).name == "cust"
    with pytest.raises(PolicyError, match="disabled by server policy"):
        c.locator("w")
    only_public = cfg({"locators": locs[:1]})
    with pytest.raises(ConfigError, match="blocked by policy"):
        only_public.locator(None)
    assert cfg({"locators": locs[:1], "policy": {"allow_public_geocoder": True}}).locator(None).name == "w"
    with pytest.raises(ConfigError, match="no locator named"):
        c.locator("nope")


def test_oauth_needs_client_id_and_secret():
    with pytest.raises(ConfigError, match="SPATIALISE_ARCGIS_CLIENT_ID"):
        cfg({"server": {"auth_mode": "oauth"}}, SPATIALISE_SECRET=S)
    with pytest.raises(ConfigError, match="SPATIALISE_SECRET"):
        cfg({}, SPATIALISE_ARCGIS_CLIENT_ID="abc", SPATIALISE_SECRET="short")
    c = cfg({}, SPATIALISE_ARCGIS_CLIENT_ID="abc", SPATIALISE_SECRET=S, SPATIALISE_PUBLIC_URL="https://mcp.example.org/")
    assert c.auth_mode == "oauth" and c.public_url == "https://mcp.example.org"
    assert cfg({}, ARCGIS_TOKEN="t").auth_mode == "static"


@pytest.mark.parametrize("data,match", [
    ({"polcy": {}}, "unknown setting"),
    ({"policy": {"allow_everything": True}}, "unknown setting"),
    ({"locators": [{"name": "a", "url": "http://insecure.example/GeocodeServer"}]}, "https"),
    ({"locators": [{"name": "a", "url": "https://a.arcgis.com/G"}, {"name": "a", "url": "https://b.arcgis.com/G"}]}, "used twice"),
    ({"gazetteers": [{"name": "g", "url": "https://a.arcgis.com/0"}]}, "name_field"),
    ({"boundaries": [{"name": "b", "url": "https://a.arcgis.com/0"}]}, "code_field"),
    ({"boundaries": [{"name": "b", "url": "https://a.arcgis.com/0", "code_field": "c",
                      "generalised_url": "https://elsewhere.example/1"}]}, "same host"),
    ({"server": {"public_url": "ftp://x"}}, "https"),
])
def test_bad_config_is_rejected_with_a_reason(data, match):
    with pytest.raises(ConfigError, match=match):
        cfg(data)


def test_load_file(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[policy]\nmax_batch = 50\n[[boundaries]]\nname = "sa2"\nurl = "https://geo.abs.gov.au/x/0"\n'
                 'code_field = "sa2_code_2021"\n')
    c = C.load(p, {})
    assert c.policy.max_batch == 50 and c.boundaries[0].auth == "none"
    with pytest.raises(ConfigError, match="not found"):
        C.load(tmp_path / "missing.toml", {})
    p.write_text("not = [toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        C.load(p, {})


def test_example_config_loads():
    from pathlib import Path
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    c = C.load(example, {})
    assert {b.name for b in c.boundaries} >= {"sa2", "lga"} and c.policy.allow_public_geocoder is False
