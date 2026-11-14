from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping, Sequence

import httpx
import pytest

from pynix_review.cli import skipped
from pynix_review.github import API, PullRequest, ci_run, pull_request, rebuilds, tarball

REPO = "NixOS/nixpkgs"
HEAD = "d8189128adb2e9cfe7f1811004cc2caf0183e7e3"
BASE = "aaaa"
MERGE_BASE = "36eedf740abb0234c3d4f6020f7858cc34a6682a"
DOWNLOAD = "https://api.github.com/repos/NixOS/nixpkgs/actions/artifacts/7/zip"
CHANGED = {"rebuildsByPlatform": {"x86_64-linux": ["hdf4", "adscan", "hdf4"], "aarch64-linux": ["other"]}}


def artifact_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def github(
    runs: Sequence[Mapping[str, object]], artifacts: Sequence[Mapping[str, object]], sent: list[httpx.Request]
) -> httpx.MockTransport:
    routes: dict[str, httpx.Response] = {
        f"{API}/repos/{REPO}/pulls/584677": httpx.Response(
            200, json={"title": "hdf4: 4.2.15 -> 4.3.0", "head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "master"}}
        ),
        f"{API}/repos/{REPO}/compare/{BASE}...{HEAD}": httpx.Response(
            200, json={"merge_base_commit": {"sha": MERGE_BASE}}
        ),
        f"{API}/repos/{REPO}/actions/runs": httpx.Response(200, json={"workflow_runs": runs}),
        f"{API}/repos/{REPO}/actions/runs/42/artifacts": httpx.Response(200, json={"artifacts": artifacts}),
        DOWNLOAD: httpx.Response(200, content=artifact_zip({"changed-paths.json": json.dumps(CHANGED)})),
    }

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return routes[str(request.url.copy_with(query=None))]

    return httpx.MockTransport(handle)


async def test_a_pull_request_has_its_merge_base_and_ci_s_rebuilds() -> None:
    sent: list[httpx.Request] = []
    runs = [{"id": 41, "name": "PR", "conclusion": "failure"}, {"id": 42, "name": "PR", "conclusion": "success"}]
    artifacts = [{"name": "comparison", "expired": False, "archive_download_url": DOWNLOAD}]
    client = httpx.AsyncClient(transport=github(runs, artifacts, sent))
    pr = await pull_request(584677, token="t0ken", system="x86_64-linux", client=client)
    assert pr == PullRequest(
        repo=REPO,
        number=584677,
        title="hdf4: 4.2.15 -> 4.3.0",
        head=HEAD,
        base_ref="master",
        merge_base=MERGE_BASE,
        ci_run=42,
        attrs=["adscan", "hdf4"],
    )
    assert all(r.headers["Authorization"] == "Bearer t0ken" for r in sent)
    assert "Authorization" not in client.headers


async def test_an_expired_artifact_gives_no_rebuild_list() -> None:
    runs = [{"id": 42, "name": "PR", "conclusion": "success"}]
    artifacts = [{"name": "comparison", "expired": True, "archive_download_url": DOWNLOAD}]
    client = httpx.AsyncClient(transport=github(runs, artifacts, []))
    pr = await pull_request(584677, token="t", system="x86_64-linux", client=client)
    assert (pr.ci_run, pr.attrs) == (42, None)
    assert skipped(pr, None, 1000) == "CI's comparison artifact is missing"


def test_the_pr_run_is_a_successful_one_when_there_is_one() -> None:
    assert ci_run([{"id": 1, "name": "Eval"}, {"id": 2, "name": "PR", "conclusion": None}]) == 2
    assert ci_run([{"id": 2, "name": "PR"}, {"id": 3, "name": "PR", "conclusion": "success"}]) == 3
    assert ci_run([{"id": 1, "name": "Eval", "conclusion": "success"}]) is None


def test_rebuilds_are_one_system_s_sorted_and_unique() -> None:
    assert rebuilds(json.dumps(CHANGED), "x86_64-linux") == ["adscan", "hdf4"]
    assert rebuilds(json.dumps(CHANGED), "riscv64-linux") == []


def test_a_tarball_is_github_s_archive_of_the_commit() -> None:
    assert tarball(REPO, HEAD) == f"https://github.com/NixOS/nixpkgs/archive/{HEAD}.tar.gz"


@pytest.mark.parametrize(("attrs", "why"), [([], "CI lists no rebuild"), (["hdf4"], None)])
def test_skipped_before_the_diff(attrs: list[str], why: str | None) -> None:
    pr = PullRequest(
        repo=REPO, number=1, title="t", head=HEAD, base_ref="master", merge_base=MERGE_BASE, ci_run=1, attrs=attrs
    )
    assert skipped(pr, None, 1000) == why
