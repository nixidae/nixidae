"""`VerbatimURL::lastPathSegment`, the name of a download."""

import pytest

from huggorm_bindings import url_last_path_segment


@pytest.mark.parametrize(
    ("url", "segment"),
    [
        ("https://example.com/path/to/file.txt?query=value", "file.txt"),
        ("https://example.com/archive/abc.tar.gz", "abc.tar.gz"),
        ("https://example.com/dir/", "dir"),
        ("https://example.com/a%20b", "a b"),
        ("file:///tmp/x.json", "x.json"),
        ("https://example.com/", None),
        ("https://example.com", None),
    ],
)
def test_the_last_non_empty_segment_names_the_download(url: str, segment: str | None) -> None:
    assert url_last_path_segment(url) == segment
