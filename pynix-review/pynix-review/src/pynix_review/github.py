"""A nixpkgs pull request, from GitHub: its head, its merge base, and what
CI says it rebuilds.

The rebuild list is `rebuildsByPlatform` in `changed-paths.json`, in the
`comparison` artifact of the head's `PR` workflow run. The run's
`diff-<system>` artifact lists the same attributes (NixOS/nixpkgs#584677:
183 in both) at 70 times the size. An artifact download needs a token, even
for a public repository.
"""

from __future__ import annotations

import contextlib
import io
import json
import zipfile
from typing import TYPE_CHECKING, Final

import httpx
from pydantic import BaseModel, ConfigDict

from .buildtimes import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

API: Final = "https://api.github.com"
NIXPKGS: Final = "NixOS/nixpkgs"
CI_WORKFLOW: Final = "PR"
ARTIFACT: Final = "comparison"
CHANGED_PATHS: Final = "changed-paths.json"


class PullRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    number: int
    title: str
    head: str
    """The head commit."""
    base_ref: str
    merge_base: str
    """The commit the diff is against: what GitHub shows the PR's changes from."""
    ci_run: int | None
    """The `PR` workflow run whose `comparison` artifact gave `attrs`."""
    attrs: list[str] | None
    """CI's rebuilds on the system asked for; None when no run or artifact holds them."""


def tarball(repo: str, commit: str) -> str:
    """GitHub's archive of `commit`. A fork's PR head is served from the base repository too."""
    return f"https://github.com/{repo}/archive/{commit}.tar.gz"


def rebuilds(changed_paths: str, system: str) -> list[str]:
    """The attributes `changed-paths.json` says rebuild on `system`."""
    return sorted(set(json.loads(changed_paths).get("rebuildsByPlatform", {}).get(system, [])))


def ci_run(runs: Sequence[Mapping[str, object]]) -> int | None:
    """The `PR` workflow run to read: a successful one first, else the newest."""
    mine = [r for r in runs if r.get("name") == CI_WORKFLOW]
    picked = [r for r in mine if r.get("conclusion") == "success"] or mine
    return int(str(picked[0]["id"])) if picked else None


class _GitHub:
    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self.http = http
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        }

    async def get(self, url: str) -> httpx.Response:
        response = await self.http.get(url, headers=self.headers, follow_redirects=True)
        response.raise_for_status()
        return response

    async def json(self, path: str) -> dict[str, object]:
        # Read by key, through `_dict`/`_list`: the full shape is GitHub's to change.
        return _dict((await self.get(f"{API}/{path}")).json())


@contextlib.asynccontextmanager
async def _github(token: str, client: httpx.AsyncClient | None) -> AsyncIterator[_GitHub]:
    async with contextlib.AsyncExitStack() as stack:
        http = client or await stack.enter_async_context(httpx.AsyncClient(timeout=60))
        yield _GitHub(http, token)


async def pull_request(
    number: int,
    *,
    token: str,
    system: str,
    repo: str = NIXPKGS,
    client: httpx.AsyncClient | None = None,
) -> PullRequest:
    async with _github(token, client) as gh:
        pr = await gh.json(f"repos/{repo}/pulls/{number}")
        head = _sha(pr["head"])
        base = pr["base"]
        compared = await gh.json(f"repos/{repo}/compare/{_sha(base)}...{head}")
        runs = await gh.json(f"repos/{repo}/actions/runs?head_sha={head}&per_page=100")
        run = ci_run(_list(runs["workflow_runs"]))
        attrs = None if run is None else await _rebuilds(gh, repo, run, system)
    return PullRequest(
        repo=repo,
        number=number,
        title=str(pr["title"]),
        head=head,
        base_ref=str(_dict(base)["ref"]),
        merge_base=_sha(compared["merge_base_commit"]),
        ci_run=run,
        attrs=attrs,
    )


async def _rebuilds(gh: _GitHub, repo: str, run: int, system: str) -> list[str] | None:
    listed = await gh.json(f"repos/{repo}/actions/runs/{run}/artifacts?name={ARTIFACT}")
    artifacts = [a for a in _list(listed["artifacts"]) if a.get("name") == ARTIFACT and not a.get("expired")]
    if not artifacts:
        return None
    response = await gh.get(str(artifacts[0]["archive_download_url"]))
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        if CHANGED_PATHS not in archive.namelist():
            return None
        return rebuilds(archive.read(CHANGED_PATHS).decode(), system)


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"GitHub answered {type(value).__name__} where an object belongs")
    return value


def _list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise TypeError(f"GitHub answered {type(value).__name__} where a list belongs")
    return [_dict(v) for v in value]


def _sha(value: object) -> str:
    return str(_dict(value)["sha"])
