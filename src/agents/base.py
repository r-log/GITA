"""
Base agent framework.
Every specialist agent extends BaseAgent and implements the handle() method.
"""

from __future__ import annotations

import time
import json
import structlog
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional
from pathlib import Path

from openai import AsyncOpenAI

from src.core.config import settings
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


@dataclass
class AgentContext:
    """What gets passed to an agent when it's invoked."""
    event_type: str
    event_payload: dict
    repo_full_name: str
    installation_id: int
    repo_id: int = 0  # DB primary key, resolved by webhook handler
    additional_data: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    """What an agent returns after completing its work."""
    agent_name: str
    status: str  # "success", "partial", "failed", "needs_review"
    actions_taken: List[dict] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    confidence: float = 0.0
    should_notify: bool = False
    comment_body: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "status": self.status,
            "actions_taken": self.actions_taken,
            "recommendations": self.recommendations,
            "data": self.data,
            "confidence": self.confidence,
            "should_notify": self.should_notify,
            "comment_body": self.comment_body,
        }


class BaseAgent(ABC):
    """
    Every specialist agent extends this.

    The agent runs an autonomous tool-calling loop:
      1. Reason about the context
      2. Pick a tool to call
      3. Execute the tool
      4. Reason about the result
      5. Repeat or return final answer
    """

    name: str
    description: str
    tools: List[Tool]
    model: str

    def __init__(
        self,
        name: str,
        description: str,
        tools: List[Tool],
        model: Optional[str] = None,
        system_prompt_file: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ):
        self.name = name
        self.description = description
        self.tools = tools
        self.model = model or settings.ai_default_model
        self._tool_map = {t.name: t for t in tools}

        # Load system prompt from file or use provided string
        if system_prompt_file:
            prompt_path = Path("prompts") / system_prompt_file
            if prompt_path.exists():
                self.system_prompt = prompt_path.read_text(encoding="utf-8")
            else:
                raise FileNotFoundError(f"System prompt not found: {prompt_path}")
        elif system_prompt:
            self.system_prompt = system_prompt
        else:
            self.system_prompt = f"You are {name}. {description}"

        self._client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.openrouter_api_key,
        )

    @abstractmethod
    async def handle(self, context: AgentContext) -> AgentResult:
        """
        Main entry point. Receives context (event data, repo info, etc.)
        Returns structured result (actions taken, analysis, recommendations).
        """
        ...

    async def run_tool_loop(
        self,
        messages: list[dict],
        max_calls: Optional[int] = None,
    ) -> tuple[str, list[dict]]:
        """
        Run the autonomous tool-calling loop.

        Sends messages to the LLM, executes any tool calls it requests,
        appends results, and repeats until the LLM returns a final text
        response or we hit the max tool call limit.

        Returns (final_text, tool_call_log).
        """
        max_calls = max_calls or settings.agent_max_tool_calls
        tool_schemas = [t.to_schema() for t in self.tools] or None
        tool_call_log: list[dict] = []
        calls_made = 0

        while calls_made < max_calls:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tool_schemas,
                temperature=0.2,
            )
            choice = response.choices[0]

            # If the model returns a final text answer (no tool calls), we're done
            if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
                return choice.message.content or "", tool_call_log

            # Process each tool call
            messages.append(choice.message.model_dump())
            for tc in choice.message.tool_calls:
                calls_made += 1
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                log.info("tool_call", agent=self.name, tool=tool_name, args=tool_args)

                tool = self._tool_map.get(tool_name)
                if tool:
                    result = await tool.execute(**tool_args)
                else:
                    result = ToolResult(success=False, error=f"Unknown tool: {tool_name}")

                tool_call_log.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "result": {"success": result.success, "data": str(result.data)[:500], "error": result.error},
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        {"success": result.success, "data": result.data, "error": result.error},
                        default=str,
                    ),
                })

        # Hit the limit — return whatever we have
        log.warning("tool_loop_limit", agent=self.name, calls_made=calls_made)
        return f"[Agent {self.name} hit tool call limit ({max_calls})]", tool_call_log
