"""What every client loads up front: the server instructions and tool listing."""

import inspect
import json

import pytest
from mcp import Client

from kleinanzeigen import server as main

pytestmark = pytest.mark.anyio


async def test_instructions_reach_the_client_and_name_every_tool():
    async with Client(main.mcp) as client:
        instructions = client.instructions
        tools = (await client.list_tools()).tools
    assert instructions == main.INSTRUCTIONS
    for tool in tools:
        assert tool.name in instructions, tool.name


async def test_descriptions_are_dedented_and_the_listing_stays_small():
    async with Client(main.mcp) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        assert tool.description == inspect.cleandoc(tool.description), tool.name
        assert not tool.description.startswith(" ")
    listing = json.dumps([tool.model_dump(exclude_none=True) for tool in tools], ensure_ascii=False)
    # Discovery tools sit just under 8k. The seller tools add their own schemas;
    # the cap still rejects a listing that has grown by another large tool.
    assert len(listing) < 15000


def _schema_titles(node) -> bool:
    """True when a schema node carries pydantic's ``title`` metadata. A property
    that is itself named title is the parameter, not that metadata."""
    if isinstance(node, dict):
        if "title" in node:
            return True
        for key, child in node.items():
            if key == "properties" and isinstance(child, dict):
                if any(_schema_titles(prop) for prop in child.values()):
                    return True
            elif _schema_titles(child):
                return True
    elif isinstance(node, list):
        return any(_schema_titles(item) for item in node)
    return False


async def test_schemas_carry_no_titles_or_null_branches():
    async with Client(main.mcp) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        text = json.dumps(tool.input_schema)
        assert not _schema_titles(tool.input_schema) and '"null"' not in text, tool.name
