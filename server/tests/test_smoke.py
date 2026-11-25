import pytest

from conftest import call

pytestmark = pytest.mark.anyio


async def test_tools_are_listed_with_schemas_and_annotations(make_app):
    from mcp import Client
    async with Client(make_app().mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"list_resolvers", "geocode", "reverse_geocode", "search_gazetteer", "lookup_boundary",
                          "publish_features"}
    assert tools["list_resolvers"].annotations.read_only_hint is True
    assert tools["publish_features"].annotations.read_only_hint is False
    assert tools["geocode"].annotations.read_only_hint is False  # it can spend credits and sends text out
    for t in tools.values():
        assert t.description and len(t.description) > 80
        assert t.input_schema["type"] == "object"
    assert "confirm_token" in tools["geocode"].input_schema["properties"]


async def test_oauth_end_to_end_over_http(web, fake):
    async with web() as w:
        tokens = await w.sign_in()
        out = await w.tool(tokens["access_token"], "list_resolvers")
        assert out["signed_in_as"] == "jane" and out["auth_mode"] == "oauth"
        out = await w.tool(tokens["access_token"], "geocode", {"queries": ["Kyneton Town Hall, Hutton Street"]})
        assert out["results"][0]["candidates"][0]["label"].startswith("Hutton Street, Kyneton")


async def test_results_are_compact_text_plus_structured_content(make_app):
    from mcp import Client
    async with Client(make_app().mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool("lookup_boundary", {"boundary": "sa2", "codes": ["202021027"]})
    assert tools["lookup_boundary"].output_schema is not None
    text = result.content[0].text
    assert "\n" not in text and ": " not in text                 # no indentation: a polygon stays affordable
    import json
    assert json.loads(text) == result.structured_content
