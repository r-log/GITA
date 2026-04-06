"""Tests for src.tools.base — Tool and ToolResult base classes."""

import pytest

from src.tools.base import Tool, ToolResult


class TestToolResult:
    def test_success_result(self):
        r = ToolResult(success=True, data={"key": "value"})
        assert r.success is True
        assert r.data == {"key": "value"}
        assert r.error is None

    def test_error_result(self):
        r = ToolResult(success=False, error="something broke")
        assert r.success is False
        assert r.data is None
        assert r.error == "something broke"

    def test_result_with_none_data(self):
        r = ToolResult(success=True)
        assert r.data is None


class TestToolExecute:
    async def test_sync_handler(self):
        def handler(x: int) -> ToolResult:
            return ToolResult(success=True, data=x * 2)

        tool = Tool(name="double", description="Doubles", parameters={}, handler=handler)
        result = await tool.execute(x=5)
        assert result.success is True
        assert result.data == 10

    async def test_async_handler(self):
        async def handler(msg: str) -> ToolResult:
            return ToolResult(success=True, data=msg.upper())

        tool = Tool(name="upper", description="Uppercases", parameters={}, handler=handler)
        result = await tool.execute(msg="hello")
        assert result.success is True
        assert result.data == "HELLO"

    async def test_handler_exception_returns_error_result(self):
        def handler():
            raise ValueError("boom")

        tool = Tool(name="fail", description="Fails", parameters={}, handler=handler)
        result = await tool.execute()
        assert result.success is False
        assert "boom" in result.error

    async def test_async_handler_exception_returns_error_result(self):
        async def handler():
            raise RuntimeError("async boom")

        tool = Tool(name="afail", description="Fails async", parameters={}, handler=handler)
        result = await tool.execute()
        assert result.success is False
        assert "async boom" in result.error

    async def test_handler_with_no_kwargs(self):
        async def handler() -> ToolResult:
            return ToolResult(success=True, data="no args")

        tool = Tool(name="noargs", description="No args", parameters={}, handler=handler)
        result = await tool.execute()
        assert result.success is True
        assert result.data == "no args"

    async def test_handler_with_complex_data(self):
        async def handler() -> ToolResult:
            return ToolResult(success=True, data={"nested": {"list": [1, 2, 3]}})

        tool = Tool(name="complex", description="Complex", parameters={}, handler=handler)
        result = await tool.execute()
        assert result.data["nested"]["list"] == [1, 2, 3]


class TestToolSchema:
    def test_to_schema_basic(self):
        tool = Tool(
            name="get_issue",
            description="Fetches a GitHub issue",
            parameters={
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "description": "Issue number"},
                },
                "required": ["number"],
            },
            handler=lambda: None,
        )
        schema = tool.to_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "get_issue"
        assert schema["function"]["description"] == "Fetches a GitHub issue"
        assert "number" in schema["function"]["parameters"]["properties"]

    def test_to_schema_empty_parameters(self):
        tool = Tool(
            name="ping",
            description="Ping",
            parameters={"type": "object", "properties": {}},
            handler=lambda: None,
        )
        schema = tool.to_schema()
        assert schema["function"]["parameters"]["properties"] == {}

    def test_to_schema_structure(self):
        tool = Tool(name="t", description="d", parameters={}, handler=lambda: None)
        schema = tool.to_schema()
        assert set(schema.keys()) == {"type", "function"}
        assert set(schema["function"].keys()) == {"name", "description", "parameters"}
