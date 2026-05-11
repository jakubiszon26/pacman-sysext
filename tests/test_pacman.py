import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from pacman_sysext.config import PacmanConfig
from pacman_sysext.pacman import (
    ABI_RELEVANT_PACKAGES,
    PacmanError,
    _parse_pacman_info,
    get_base_snapshot,
    get_package_dependencies,
    get_package_provides,
    get_package_version,
    parse_pkg_filename,
)
from pacman_sysext.version import VersionConstraint


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


class TestGetBaseSnapshot:
    def _fake(self, stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["pacman", "-Q"], returncode=returncode, stdout=stdout, stderr=""
        )

    def test_returns_versions_for_installed(self) -> None:
        out = "glibc 2.39-1\nncurses 6.6-2.1\n"
        with patch("pacman_sysext.pacman._run_host_pacman", return_value=self._fake(out)) as m:
            snap = get_base_snapshot(frozenset({"glibc", "ncurses"}))
        assert snap == {"glibc": "2.39-1", "ncurses": "6.6-2.1"}
        called_args = m.call_args[0][0]
        assert called_args[0] == "-Q"
        assert set(called_args[1:]) == {"glibc", "ncurses"}

    def test_missing_packages_omitted(self) -> None:
        # pacman -Q exits 1 when any package is missing but still emits info for present ones.
        out = "glibc 2.39-1\n"
        err = "error: package 'this-does-not-exist-pkg' was not found\n"
        result = subprocess.CompletedProcess(
            args=["pacman", "-Q"], returncode=1, stdout=out, stderr=err
        )
        with patch("pacman_sysext.pacman._run_host_pacman", return_value=result):
            snap = get_base_snapshot(frozenset({"glibc", "this-does-not-exist-pkg"}))
        assert snap == {"glibc": "2.39-1"}

    def test_empty_input_returns_empty(self) -> None:
        with patch("pacman_sysext.pacman._run_host_pacman") as m:
            assert get_base_snapshot(frozenset()) == {}
        m.assert_not_called()

    def test_default_set_contains_glibc(self) -> None:
        # Sanity: the default set must include the canonical ABI offender.
        assert "glibc" in ABI_RELEVANT_PACKAGES


def _pacman_config(tmp_path: Path) -> PacmanConfig:
    return PacmanConfig(
        dbpath=tmp_path / "db",
        cachedir=tmp_path / "cache",
        config_file=tmp_path / "pacman.conf",
        gpgdir=tmp_path / "gnupg",
    )


def _info_output(**fields: str) -> str:
    return "".join(f"{k:14}: {v}\n" for k, v in fields.items())


class TestGetPackageVersion:
    def test_returns_version(self, tmp_path: Path) -> None:
        out = _info_output(Name="htop", Version="3.5.1-1")
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")
        with patch("pacman_sysext.pacman._run_pacman", return_value=result):
            assert get_package_version("htop", _pacman_config(tmp_path)) == "3.5.1-1"

    def test_missing_version_raises(self, tmp_path: Path) -> None:
        out = _info_output(Name="htop")
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")
        with (
            patch("pacman_sysext.pacman._run_pacman", return_value=result),
            pytest.raises(PacmanError, match="no Version field"),
        ):
            get_package_version("htop", _pacman_config(tmp_path))


class TestGetPackageDependencies:
    def test_parses_simple_list(self, tmp_path: Path) -> None:
        out = _info_output(Name="htop", **{"Depends On": "ncurses libnl libcap glibc"})
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")
        with patch("pacman_sysext.pacman._run_pacman", return_value=result):
            deps = get_package_dependencies("htop", _pacman_config(tmp_path))
        assert deps == [
            VersionConstraint("ncurses", None, None),
            VersionConstraint("libnl", None, None),
            VersionConstraint("libcap", None, None),
            VersionConstraint("glibc", None, None),
        ]

    def test_handles_operators(self, tmp_path: Path) -> None:
        out = _info_output(**{"Depends On": "glibc>=2.38 ncurses=6.6 libcap"})
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")
        with patch("pacman_sysext.pacman._run_pacman", return_value=result):
            deps = get_package_dependencies("htop", _pacman_config(tmp_path))
        assert deps == [
            VersionConstraint("glibc", ">=", "2.38"),
            VersionConstraint("ncurses", "=", "6.6"),
            VersionConstraint("libcap", None, None),
        ]

    def test_none_returns_empty(self, tmp_path: Path) -> None:
        out = _info_output(**{"Depends On": "None"})
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")
        with patch("pacman_sysext.pacman._run_pacman", return_value=result):
            assert get_package_dependencies("foo", _pacman_config(tmp_path)) == []

    def test_missing_field_returns_empty(self, tmp_path: Path) -> None:
        out = _info_output(Name="foo")
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")
        with patch("pacman_sysext.pacman._run_pacman", return_value=result):
            assert get_package_dependencies("foo", _pacman_config(tmp_path)) == []


class TestGetPackageProvides:
    def test_pinned_and_bare(self, tmp_path: Path) -> None:
        out = _info_output(Provides="libbz2.so=1.0-64 bzip2-tools")
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")
        with patch("pacman_sysext.pacman._run_pacman", return_value=result):
            assert get_package_provides("bzip2", _pacman_config(tmp_path)) == {
                "libbz2.so": "1.0-64",
                "bzip2-tools": "",
            }

    def test_none_returns_empty(self, tmp_path: Path) -> None:
        out = _info_output(Provides="None")
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")
        with patch("pacman_sysext.pacman._run_pacman", return_value=result):
            assert get_package_provides("foo", _pacman_config(tmp_path)) == {}
