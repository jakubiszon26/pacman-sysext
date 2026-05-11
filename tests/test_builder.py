from pathlib import Path
from unittest.mock import patch

import pytest

from pacman_sysext.builder import (
    BuildError,
    _clean_arch_metadata,
    _make_image,
    _strip_unsupported_dirs,
    _systemd_arch,
    sanitize_image_name,
)


class TestSystemdArch:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            ("x86_64", "x86-64"),
            ("aarch64", "arm64"),
            ("i686", "x86"),
            ("riscv64", "riscv64"),  # passthrough for unmapped values
        ],
    )
    def test_arch_mapping(self, machine: str, expected: str) -> None:
        with patch("pacman_sysext.builder.platform.machine", return_value=machine):
            assert _systemd_arch() == expected


class TestCleanArchMetadata:
    def test_removes_known_files(self, tmp_path: Path) -> None:
        for name in (".PKGINFO", ".MTREE", ".BUILDINFO"):
            (tmp_path / name).write_text("dummy")
        (tmp_path / "usr").mkdir()

        _clean_arch_metadata(tmp_path)

        assert not (tmp_path / ".PKGINFO").exists()
        assert not (tmp_path / ".MTREE").exists()
        assert (tmp_path / "usr").exists()

    def test_missing_files_ok(self, tmp_path: Path) -> None:
        _clean_arch_metadata(tmp_path)


class TestStripUnsupportedDirs:
    def test_keeps_usr_and_opt(self, tmp_path: Path) -> None:
        (tmp_path / "usr").mkdir()
        (tmp_path / "opt").mkdir()
        (tmp_path / "etc").mkdir()
        (tmp_path / "stray.txt").write_text("x")

        _strip_unsupported_dirs(tmp_path, "pkg-1.0-1")

        assert (tmp_path / "usr").exists()
        assert (tmp_path / "opt").exists()
        assert not (tmp_path / "etc").exists()
        assert not (tmp_path / "stray.txt").exists()


class TestMakeImage:
    def test_unsupported_format(self, tmp_path: Path) -> None:
        with pytest.raises(BuildError, match="Unsupported"):
            _make_image(tmp_path, tmp_path / "out.raw", "ext4")  # type: ignore[arg-type]


class TestSanitizeImageName:
    def test_no_special_chars_is_identity(self) -> None:
        assert sanitize_image_name("htop", "3.5.1-1") == "htop-3.5.1-1"

    def test_epoch_colon_replaced_with_plus(self) -> None:
        # `:` is the overlayfs lowerdir separator — must be escaped or
        # `systemd-sysext refresh` fails with "No such file or directory".
        assert sanitize_image_name("lensfun", "1:0.3.4-6.1") == "lensfun-1+0.3.4-6.1"

    def test_comma_replaced_with_underscore(self) -> None:
        # Defensive: `,` is the overlayfs mount option separator.
        assert sanitize_image_name("pkg", "1,0-1") == "pkg-1_0-1"
