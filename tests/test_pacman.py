import pytest

from pacman_sysext.pacman import _parse_pacman_info, parse_pkg_filename


class TestParsePkgFilename:
    def test_simple(self) -> None:
        assert parse_pkg_filename("htop-3.5.1-1-x86_64.pkg.tar.zst") == ("htop", "3.5.1-1")

    def test_v4_arch(self) -> None:
        assert parse_pkg_filename("htop-3.5.1-1.1-x86_64_v4.pkg.tar.zst") == ("htop", "3.5.1-1.1")

    def test_hyphenated_name(self) -> None:
        assert parse_pkg_filename("zlib-ng-compat-2.2.1-1-x86_64.pkg.tar.zst") == (
            "zlib-ng-compat",
            "2.2.1-1",
        )

    def test_xz_compression(self) -> None:
        assert parse_pkg_filename("foo-1.0-1-any.pkg.tar.xz") == ("foo", "1.0-1")

    def test_invalid_filename(self) -> None:
        with pytest.raises(ValueError):
            parse_pkg_filename("not-a-package.txt")


class TestParsePacmanInfo:
    def test_single_record(self) -> None:
        output = (
            "Name           : htop\n"
            "Version        : 3.5.1-1\n"
            "Description    : Interactive process viewer\n"
        )
        info = _parse_pacman_info(output)
        assert info["Name"] == "htop"
        assert info["Version"] == "3.5.1-1"
        assert info["Description"] == "Interactive process viewer"

    def test_continuation_lines(self) -> None:
        output = (
            "Name           : htop\nDepends On     : ncurses libnl\n                 libcap glibc\n"
        )
        info = _parse_pacman_info(output)
        assert info["Depends On"] == "ncurses libnl libcap glibc"

    def test_empty(self) -> None:
        assert _parse_pacman_info("") == {}
