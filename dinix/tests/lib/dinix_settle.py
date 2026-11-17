"""A settle whose check says whether it holds and what it saw."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from vivarium_runner import Machine


async def until(
    what: str,
    check: Callable[[], Awaitable[tuple[bool, str]]],
    timeout: float,
    node: Machine,
) -> None:
    """Call *check* until it holds; on timeout the error carries what it last saw."""
    seen = "(not asked yet)"

    async def holds() -> Literal[True] | None:
        nonlocal seen
        ok, seen = await check()
        return True if ok else None

    async def evidence() -> str:
        return seen

    await node.until(what, holds, timeout, evidence=evidence)
