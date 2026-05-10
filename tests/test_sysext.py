from pathlib import Path

import pytest

from pacman_sysext.sysext import activate_sysext, deactivate_sysext


class TestActivateSysext:
    def test_creates_symlink(self, tmp_path: Path) -> None:
        raw = tmp_path / "src" / "htop-3.5.1-1.raw"
        raw.parent.mkdir()
        raw.write_text("dummy image")
        ext_dir = tmp_path / "extensions"

        link = activate_sysext(raw, ext_dir)

        assert link.is_symlink()
        assert link.resolve() == raw.resolve()

    def test_replaces_existing(self, tmp_path: Path) -> None:
        raw = tmp_path / "htop-3.5.1-1.raw"
        raw.write_text("v1")
        ext_dir = tmp_path / "extensions"

        activate_sysext(raw, ext_dir)
        link = activate_sysext(raw, ext_dir)

        assert link.is_symlink()

    def test_replaces_broken_symlink(self, tmp_path: Path) -> None:
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        broken = ext_dir / "htop-3.5.1-1.raw"
        broken.symlink_to(tmp_path / "nonexistent.raw")

        raw = tmp_path / "htop-3.5.1-1.raw"
        raw.write_text("ok")

        link = activate_sysext(raw, ext_dir)
        assert link.resolve() == raw.resolve()

    def test_missing_source(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            activate_sysext(tmp_path / "nope.raw", tmp_path / "ext")


class TestDeactivateSysext:
    def test_removes_existing(self, tmp_path: Path) -> None:
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        link = ext_dir / "htop-3.5.1-1.raw"
        link.symlink_to(tmp_path / "anything")

        assert deactivate_sysext("htop-3.5.1-1.raw", ext_dir) is True
        assert not link.is_symlink()

    def test_missing_returns_false(self, tmp_path: Path) -> None:
        assert deactivate_sysext("nope.raw", tmp_path) is False
