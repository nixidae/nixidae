"""Put a graph where a short link can reach it: a secret GitHub gist.

A view link that carries the DOT itself grows with the graph: NixOS/nixpkgs
PR 584677 (hdf4, 201 derivations) gave a 67 KB link, over the 65536
characters of a GitHub comment. A gist's raw file allows any origin, so
GraphvizOnline's `?url=` fetches it in the browser.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode

import httpx

from .buildtimes import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Mapping

API: Final = "https://api.github.com/gists"
VIEWER_PAGE: Final = "https://dreampuf.github.io/GraphvizOnline/"
TOKEN_ENV: Final = "GITHUB_TOKEN"


def url_view(raw: str) -> str:
    """A GraphvizOnline link that fetches the DOT at `raw`, editor folded away."""
    return VIEWER_PAGE + "?" + urlencode({"engine": "dot", "presentation": "editable", "url": raw})


async def gist(
    files: Mapping[str, str],
    description: str,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """Create a secret gist of `files`; answers each file's raw URL.

    A raw URL names the gist's revision, so the link shows this graph even
    after the gist changes."""
    body = {"description": description, "public": False, "files": {n: {"content": t} for n, t in files.items()}}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    async with contextlib.AsyncExitStack() as stack:
        http = client or await stack.enter_async_context(httpx.AsyncClient(timeout=30))
        response = await http.post(API, json=body, headers=headers)
    response.raise_for_status()
    made = response.json()
    return {name: made["files"][name]["raw_url"] for name in files}
