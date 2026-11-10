"""The memory's questions, answered through a real MCP client."""

from __future__ import annotations

import os
import sys

import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gita.history.sync import sync_history
from gita.mcp_server import build_server

TOOLS = {"past_bugs", "lessons", "review_context", "impact", "history", "scoreboard"}


@pytest.fixture
async def server(db_session, test_engine, shop_repo):
    await sync_history(db_session, "shop", shop_repo.root)
    await db_session.commit()  # the tools read through their own sessions
    return build_server(
        "shop", async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    )


async def _call(server, tool, **args):
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    return result


async def test_tools_are_listed_read_only(server):
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


async def test_past_bugs_for_a_file(server, shop_repo):
    result = await _call(server, "past_bugs", path=str(shop_repo.root / "src/shop/cart.py"))
    data = result.structured_content
    assert data["query"]["path"] == "src/shop/cart.py" and data["found"] == 1
    bug = data["bugs"][0]
    assert bug["fix_pr"]["number"] == 3 and bug["introduced_by"]["number"] == 1
    assert bug["what_was_fixed"] == "Discount subtracts the percentage"
    assert bug["where_in_introducing_pr"] == [{"file": "src/shop/cart.py", "lines": "6"}]


async def test_lessons_reach_a_new_file_in_the_same_directory(server):
    data = (await _call(server, "lessons", paths=["src/shop/coupons.py"])).structured_content
    (lesson,) = data["lessons"]
    assert lesson["lesson"] == "Discount subtracts the percentage"
    assert (lesson["match"], lesson["scope"]) == ("same directory", "src/shop")


async def test_review_context_from_a_diff(server):
    diff = "diff --git a/src/shop/cart.py b/src/shop/cart.py\n--- a/src/shop/cart.py\n+++ b/src/shop/cart.py\n"
    data = (await _call(server, "review_context", diff=diff)).structured_content
    (entry,) = data["files"]
    assert entry["path"] == "src/shop/cart.py" and entry["past_bugs"]
    assert data["lessons"][0]["fix_pr"] == 3


async def test_scoreboard(server):
    data = (await _call(server, "scoreboard")).structured_content
    assert data["merged_prs"] == 5 and data["fix_prs"] == 3
    assert data["escape_rate"] == {"merged": 5, "later_needed_a_fix": 1, "rate": 0.2}
    assert data["fixes_of_inherited_code"] == 1


async def test_impact_says_when_the_code_isnt_indexed(server):
    data = (await _call(server, "impact", target="src/shop/cart.py")).structured_content
    assert "gita index" in data["error"]


async def test_an_unsynced_repo_is_an_error_not_an_empty_answer(test_engine):
    server = build_server("nope", async_sessionmaker(test_engine, class_=AsyncSession))
    result = await _call(server, "scoreboard")
    assert result.is_error and "gita history sync" in result.content[0].text


async def test_served_over_stdio_as_an_agent_would_start_it():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gita.cli", "mcp", "--repo", "shop"],
        env=dict(os.environ),
    )
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS


async def test_scoreboard_window(server):
    later = (await _call(server, "scoreboard", since="2027-01-01")).structured_content
    assert "error" in later  # nothing merged in the window
    same = (await _call(server, "scoreboard", since="2026-01-01")).structured_content
    assert same["merged_prs"] == 5
