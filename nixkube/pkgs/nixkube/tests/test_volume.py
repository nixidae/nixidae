# SPDX-License-Identifier: MIT

from pathlib import Path

from src.volume import is_mount


class TestIsMount:
    """Tests for is_mount() using a temp mounts file."""

    def write_mounts(self, tmp_path: Path, content: str) -> Path:
        mounts_file = tmp_path / "mounts"
        mounts_file.write_text(content)
        return mounts_file

    def test_path_present_returns_true(self, tmp_path: Path):
        """Path listed as mountpoint should return True."""
        target = tmp_path / "target"
        target.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"tmpfs {target.resolve()} tmpfs rw,nosuid,nodev 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is True

    def test_path_absent_returns_false(self, tmp_path: Path):
        """Path not listed as mountpoint should return False."""
        target = tmp_path / "target"
        target.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"tmpfs {other.resolve()} tmpfs rw 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is False

    def test_empty_mounts_file_returns_false(self, tmp_path: Path):
        """Empty mounts file should return False."""
        target = tmp_path / "target"
        target.mkdir()
        mounts = self.write_mounts(tmp_path, "")
        assert is_mount(target, mounts_file=mounts) is False

    def test_malformed_line_skipped(self, tmp_path: Path):
        """Lines with fewer than 2 fields should be skipped gracefully."""
        target = tmp_path / "target"
        target.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"incomplete\n"  # only 1 field
            f"tmpfs {target.resolve()} tmpfs rw 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is True

    def test_oserror_returns_false(self, tmp_path: Path):
        """OSError reading the mounts file should return False, not raise."""
        target = tmp_path / "target"
        target.mkdir()
        nonexistent = tmp_path / "does_not_exist"
        assert is_mount(target, mounts_file=nonexistent) is False

    def test_multiple_mounts_finds_correct_one(self, tmp_path: Path):
        """Correct path is found among multiple mount entries."""
        target = tmp_path / "target"
        target.mkdir()
        other1 = tmp_path / "other1"
        other1.mkdir()
        other2 = tmp_path / "other2"
        other2.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"tmpfs {other1.resolve()} tmpfs rw 0 0\n"
            f"tmpfs {target.resolve()} tmpfs rw 0 0\n"
            f"tmpfs {other2.resolve()} tmpfs rw 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is True

    def test_partial_path_match_not_counted(self, tmp_path: Path):
        """A path that is a prefix of a mount should not match."""
        target = tmp_path / "target"
        target.mkdir()
        subdir = target / "subdir"
        subdir.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"tmpfs {subdir.resolve()} tmpfs rw 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is False
