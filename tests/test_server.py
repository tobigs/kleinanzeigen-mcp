"""Server-level metadata and the stdio transport."""

import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from kleinanzeigen import server as main

pytestmark = pytest.mark.anyio

MAIN = Path(__file__).resolve().parent.parent / "main.py"


READ_TOOLS = {
    "search_kleinanzeigen", "get_ad_detail", "get_ad_images", "get_seller_info",
    "search_locations", "get_category_filters", "list_categories",
    "get_my_listings", "get_messages",
}
WRITE_TOOLS = {
    "create_listing", "update_listing", "pause_listing", "activate_listing",
    "delete_listing", "upload_images", "send_message",
}
DESTRUCTIVE = {"delete_listing", "update_listing"}
IDEMPOTENT = {"pause_listing", "activate_listing", "update_listing"}


async def test_read_and_write_tools_are_marked():
    async with Client(main.mcp) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == READ_TOOLS | WRITE_TOOLS
    for tool in tools:
        assert tool.annotations.open_world_hint is (tool.name != "list_categories"), tool.name
        if tool.name in READ_TOOLS:
            assert tool.annotations.read_only_hint is True, tool.name
            continue
        assert tool.annotations.read_only_hint is False, tool.name
        assert tool.annotations.destructive_hint is (tool.name in DESTRUCTIVE), tool.name
        assert tool.annotations.idempotent_hint is (tool.name in IDEMPOTENT), tool.name


async def test_tool_list_may_be_cached_for_a_day():
    async with Client(main.mcp) as client:
        listing = await client.list_tools()
    assert listing.ttl_ms == 24 * 60 * 60 * 1000
    assert listing.cache_scope == "public"


async def test_stdio_transport_serves_the_same_tools():
    server = StdioServerParameters(command=sys.executable, args=[str(MAIN), "--stdio"], cwd=str(MAIN.parent))
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool("list_categories", {"query": "pc-zubehör"})
    assert {"search_kleinanzeigen", "get_ad_detail", "list_categories"} <= names
    assert '"id":225' in result.content[0].text
