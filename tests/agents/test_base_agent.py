"""Tests for src.agents.base — BaseAgent, AgentContext, AgentResult, run_tool_loop."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.base import AgentContext, AgentResult, BaseAgent
from src.tools.base import Tool, ToolResult

from tests.conftest import make_llm_response, make_tool_call, make_tool


# --- Concrete subclass for testing ---

class ConcreteAgent(BaseAgent):
    """Minimal concrete agent that bypasses real OpenAI client init."""

    def __init__(self, tools=None, model="test-model", system_prompt="You are a test agent."):
        self.name = "test_agent"
        self.description = "A test agent"
        self.tools = tools or []
        self.model = model
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
        self._tool_map = {t.name: t for t in self.tools}
        self.system_prompt = system_prompt
        self._client = AsyncMock()

    async def handle(self, context: AgentContext) -> AgentResult:
        return AgentResult(agent_name=self.name, status="success")


class TestAgentContext:
    def test_defaults(self):
        ctx = AgentContext(
            event_type="issues.opened",
            event_payload={"action": "opened"},
            repo_full_name="owner/repo",
            installation_id=1,
        )
        assert ctx.repo_id == 0
        assert ctx.additional_data == {}

    def test_all_fields(self):
        ctx = AgentContext(
            event_type="push",
            event_payload={"ref": "main"},
            repo_full_name="o/r",
            installation_id=42,
            repo_id=7,
            additional_data={"key": "val"},
        )
        assert ctx.repo_id == 7
        assert ctx.additional_data["key"] == "val"


class TestAgentResult:
    def test_defaults(self):
        r = AgentResult(agent_name="test", status="success")
        assert r.actions_taken == []
        assert r.recommendations == []
        assert r.data == {}
        assert r.confidence == 0.0
        assert r.should_notify is False
        assert r.comment_body is None

    def test_to_dict(self):
        r = AgentResult(
            agent_name="issue_analyst",
            status="needs_review",
            actions_taken=[{"action": "commented"}],
            confidence=0.85,
            comment_body="Analysis complete.",
        )
        d = r.to_dict()
        assert d["agent_name"] == "issue_analyst"
        assert d["status"] == "needs_review"
        assert d["confidence"] == 0.85
        assert d["comment_body"] == "Analysis complete."
        assert len(d["actions_taken"]) == 1


class TestRunToolLoop:
    async def test_llm_returns_text_immediately(self):
        """When LLM returns text without tool calls, loop ends."""
        agent = ConcreteAgent()
        agent._client.chat.completions.create = AsyncMock(
            return_value=make_llm_response(content="Done.", finish_reason="stop")
        )

        text, log = await agent.run_tool_loop([{"role": "user", "content": "hi"}])
        assert text == "Done."
        assert log == []

    async def test_single_tool_call_then_text(self):
        """LLM calls one tool, then returns final text."""
        tool = make_tool(name="get_issue", result=ToolResult(success=True, data={"title": "Bug"}))
        agent = ConcreteAgent(tools=[tool])

        # First response: tool call. Second response: final text.
        tc = make_tool_call("get_issue", {"number": 1})
        agent._client.chat.completions.create = AsyncMock(side_effect=[
            make_llm_response(content=None, tool_calls=[tc], finish_reason="tool_calls"),
            make_llm_response(content="Issue #1 is a bug.", finish_reason="stop"),
        ])

        text, log = await agent.run_tool_loop([{"role": "user", "content": "check issue 1"}])
        assert text == "Issue #1 is a bug."
        assert len(log) == 1
        assert log[0]["tool"] == "get_issue"
        assert log[0]["result"]["success"] is True

    async def test_unknown_tool_returns_error(self):
        """Unknown tool name handled gracefully."""
        agent = ConcreteAgent(tools=[])

        tc = make_tool_call("nonexistent_tool", {})
        agent._client.chat.completions.create = AsyncMock(side_effect=[
            make_llm_response(content=None, tool_calls=[tc], finish_reason="tool_calls"),
            make_llm_response(content="I couldn't find that tool.", finish_reason="stop"),
        ])

        text, log = await agent.run_tool_loop([{"role": "user", "content": "test"}])
        assert len(log) == 1
        assert log[0]["result"]["success"] is False
        assert "Unknown tool" in log[0]["result"]["error"]

    async def test_tool_exception_handled(self):
        """Tool that raises exception is caught gracefully."""
        async def bad_handler(**kwargs):
            raise RuntimeError("kaboom")

        tool = Tool(name="exploder", description="Explodes", parameters={}, handler=bad_handler)
        agent = ConcreteAgent(tools=[tool])

        tc = make_tool_call("exploder", {})
        agent._client.chat.completions.create = AsyncMock(side_effect=[
            make_llm_response(content=None, tool_calls=[tc], finish_reason="tool_calls"),
            make_llm_response(content="Tool failed.", finish_reason="stop"),
        ])

        text, log = await agent.run_tool_loop([{"role": "user", "content": "test"}])
        assert len(log) == 1
        assert log[0]["result"]["success"] is False
        assert "kaboom" in log[0]["result"]["error"]

    async def test_max_calls_limit(self):
        """Loop stops when max_calls is hit."""
        tool = make_tool(name="repeat_tool")
        agent = ConcreteAgent(tools=[tool])

        tc = make_tool_call("repeat_tool", {})
        # Always return tool calls — never returns text
        agent._client.chat.completions.create = AsyncMock(
            return_value=make_llm_response(content=None, tool_calls=[tc], finish_reason="tool_calls")
        )

        text, log = await agent.run_tool_loop(
            [{"role": "user", "content": "test"}],
            max_calls=3,
        )
        assert len(log) == 3
        assert "limit" in text.lower()

    async def test_token_usage_tracked(self):
        """Token usage is accumulated across LLM calls."""
        agent = ConcreteAgent(model="anthropic/claude-sonnet-4")
        agent._client.chat.completions.create = AsyncMock(
            return_value=make_llm_response(
                content="Done.",
                finish_reason="stop",
                prompt_tokens=200,
                completion_tokens=100,
            )
        )

        await agent.run_tool_loop([{"role": "user", "content": "hi"}])
        assert agent._usage["prompt_tokens"] == 200
        assert agent._usage["completion_tokens"] == 100
        assert agent._usage["llm_calls"] == 1
        assert "anthropic/claude-sonnet-4" in agent._usage["by_model"]

    async def test_per_model_usage_tracked(self):
        """Usage is tracked per model in by_model dict."""
        tool = make_tool(name="t")
        agent = ConcreteAgent(tools=[tool], model="model-a")

        tc = make_tool_call("t", {})
        agent._client.chat.completions.create = AsyncMock(side_effect=[
            make_llm_response(content=None, tool_calls=[tc], finish_reason="tool_calls",
                              prompt_tokens=50, completion_tokens=25),
            make_llm_response(content="done", finish_reason="stop",
                              prompt_tokens=100, completion_tokens=50),
        ])

        await agent.run_tool_loop([{"role": "user", "content": "test"}])
        assert agent._usage["prompt_tokens"] == 150
        assert agent._usage["completion_tokens"] == 75
        assert agent._usage["llm_calls"] == 2
        assert agent._usage["by_model"]["model-a"]["prompt_tokens"] == 150

    async def test_json_decode_error_uses_empty_dict(self):
        """Invalid JSON in tool args falls back to empty dict."""
        tool = make_tool(name="my_tool")
        agent = ConcreteAgent(tools=[tool])

        tc = make_tool_call("my_tool", {})
        tc.function.arguments = "not valid json {"  # Override with invalid JSON

        agent._client.chat.completions.create = AsyncMock(side_effect=[
            make_llm_response(content=None, tool_calls=[tc], finish_reason="tool_calls"),
            make_llm_response(content="ok", finish_reason="stop"),
        ])

        text, log = await agent.run_tool_loop([{"role": "user", "content": "test"}])
        assert log[0]["args"] == {}  # Fell back to empty dict

    async def test_empty_content_returns_empty_string(self):
        """When LLM returns None content, we get empty string."""
        agent = ConcreteAgent()
        agent._client.chat.completions.create = AsyncMock(
            return_value=make_llm_response(content=None, finish_reason="stop")
        )
        # Manually set content to None
        agent._client.chat.completions.create.return_value.choices[0].message.content = None

        text, log = await agent.run_tool_loop([{"role": "user", "content": "test"}])
        assert text == ""
