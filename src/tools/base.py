"""
Base classes for the shared tool layer.
Tools are stateless functions that agents call. They're grouped by domain but shared across agents.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional


@dataclass
class ToolResult:
    """What a tool returns after execution."""
    success: bool
    data: Any = None
    error: Optional[str] = None


@dataclass
class Tool:
    """
    A single tool that an agent can call.

    Tools are simple, stateless functions grouped by domain (github, db, ai).
    Each agent gets only the tools it needs (least privilege).
    """
    name: str
    description: str
    parameters: dict  # JSON schema of inputs
    handler: Callable[..., Any]

    async def execute(self, **kwargs) -> ToolResult:
        """Run the tool and return its output. Handles both sync and async handlers."""
        try:
            result = self.handler(**kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def to_schema(self) -> dict:
        """Return the tool definition as a JSON schema dict (for LLM function calling)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
