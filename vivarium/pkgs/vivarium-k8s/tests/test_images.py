from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest
from vivarium_k8s.images import ImageError, image_refs, latest, listed_as, normalized, store_path


def archive(path: Path, files: dict[str, object], *, gzipped: bool = False) -> Path:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, body in files.items():
            data = json.dumps(body).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(gzip.compress(buffer.getvalue()) if gzipped else buffer.getvalue())
    return path


@pytest.mark.parametrize("gzipped", [False, True])
def test_a_docker_save_archive_names_its_tags(tmp_path: Path, gzipped: bool) -> None:
    tarball = archive(
        tmp_path / "x.tar",
        {"manifest.json": [{"RepoTags": ["busybox:1", "vivarium.test/app:2"]}]},
        gzipped=gzipped,
    )
    assert image_refs(tarball) == ["docker.io/library/busybox:1", "vivarium.test/app:2"]


def test_an_oci_archive_names_its_refs(tmp_path: Path) -> None:
    tarball = archive(
        tmp_path / "x.tar",
        {"index.json": {"manifests": [{"annotations": {"io.containerd.image.name": "ghcr.io/a/b:3"}}]}},
    )
    assert image_refs(tarball) == ["ghcr.io/a/b:3"]


@pytest.mark.parametrize(
    "files",
    [{"manifest.json": [{"RepoTags": None}]}, {"index.json": {"manifests": [{}]}}, {"other": 1}],
)
def test_an_archive_with_no_name_is_refused(tmp_path: Path, files: dict[str, object]) -> None:
    with pytest.raises(ImageError):
        image_refs(archive(tmp_path / "x.tar", files))


def test_a_file_that_is_no_tar_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.tar").write_text("no")
    with pytest.raises(ImageError):
        image_refs(tmp_path / "x.tar")


@pytest.mark.parametrize(
    ("ref", "full"),
    [
        ("busybox:1", "docker.io/library/busybox:1"),
        ("me/app:1", "docker.io/me/app:1"),
        ("localhost/app:1", "localhost/app:1"),
        ("reg:5000/app:1", "reg:5000/app:1"),
        ("vivarium.test/busybox:1", "vivarium.test/busybox:1"),
    ],
)
def test_normalized_as_containerd_lists_it(ref: str, full: str) -> None:
    assert normalized(ref) == full


def test_podman_lists_a_hub_image_under_localhost() -> None:
    assert listed_as("docker.io/library/app:1") == {"docker.io/library/app:1", "localhost/app:1"}


@pytest.mark.parametrize(
    ("ref", "pulled"),
    [("a/b", True), ("a/b:latest", True), ("a/b:1", False), ("reg:5000/b", True), ("a/b@sha256:00", False)],
)
def test_latest(ref: str, pulled: bool) -> None:
    assert latest(ref) is pulled


def test_store_path() -> None:
    assert store_path(Path("/nix/store/abc-image/x.tar.gz")) == Path("/nix/store/abc-image")
    assert store_path(Path("/nix/store/abc-image.tar")) == Path("/nix/store/abc-image.tar")
    assert store_path(Path("/tmp/x.tar")) is None
    assert store_path(Path("/nix/store")) is None
