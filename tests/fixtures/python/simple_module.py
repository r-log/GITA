"""A simple module with top-level functions and imports."""
import os
from typing import Optional

GLOBAL_CONSTANT = 42


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def multiply(a: int, b: int) -> int:
    return a * b


async def fetch_data(url: str) -> Optional[str]:
    return None
