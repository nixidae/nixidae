#!/usr/bin/env agent-python
"""Write a tart image's config and raw NVRAM layers to a directory."""

from __future__ import annotations

import pathlib
import sys

import requests

REGISTRY = "https://ghcr.io"
MEDIA = {
    "application/vnd.cirruslabs.tart.config.v1": "config.json",
    "application/vnd.cirruslabs.tart.nvram.v1": "nvram.bin",
}


def main() -> None:
    repo, tag, out = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    token = requests.get(f"{REGISTRY}/token?scope=repository:{repo}:pull", timeout=30).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    manifest = requests.get(
        f"{REGISTRY}/v2/{repo}/manifests/{tag}",
        headers={**auth, "Accept": "application/vnd.oci.image.manifest.v1+json"},
        timeout=30,
    ).json()
    for layer in manifest["layers"]:
        name = MEDIA.get(layer.get("mediaType", ""))
        if name is None:
            continue
        blob = requests.get(f"{REGISTRY}/v2/{repo}/blobs/{layer['digest']}", headers=auth, timeout=120)
        blob.raise_for_status()
        (out / name).write_bytes(blob.content)
        print(f"{name}: {len(blob.content)} bytes")


if __name__ == "__main__":
    main()
