from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx

from pynix_review.share import API, gist, url_view

RAW = "https://gist.githubusercontent.com/someone/abc/raw/0123/changed.dot"


def test_url_view_fetches_the_raw_file_with_the_editor_folded() -> None:
    link = urlsplit(url_view(RAW))
    assert f"{link.scheme}://{link.netloc}{link.path}" == "https://dreampuf.github.io/GraphvizOnline/"
    assert parse_qs(link.query) == {"engine": ["dot"], "presentation": ["editable"], "url": [RAW]}


async def test_gist_is_secret_and_answers_each_raw_url() -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json={"files": {"changed.dot": {"raw_url": RAW}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    raw = await gist({"changed.dot": "digraph changed {}\n"}, "pynix-review diff: x", "t0ken", client)
    assert raw == {"changed.dot": RAW}
    [request] = sent
    assert str(request.url) == API
    assert request.headers["Authorization"] == "Bearer t0ken"
    body = json.loads(request.content)
    assert body["public"] is False
    assert body["files"] == {"changed.dot": {"content": "digraph changed {}\n"}}
