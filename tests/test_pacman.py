import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from pacman_sysext.config import PacmanConfig
from pacman_sysext.pacman import (
    ABI_RELEVANT_PACKAGES,
    PacmanError,
    ResolvedDep,
    _parse_pacman_info,
    get_base_snapshot,
    get_package_dependencies,
    get_package_provides,
    get_package_version,
    get_required_packages,
    parse_pkg_filename,
    resolve_required_packages,
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


class TestResolveRequiredPackages:
    @staticmethod
    def _fake(stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    def test_parses_multi_repo_lines(self, tmp_path: Path) -> None:
        out = (
            "core\tglibc\t2.39-1\thttps://mirror.example/core/os/x86_64/glibc-2.39-1-x86_64.pkg.tar.zst\n"
            "extra\thtop\t3.5.1-1\thttps://mirror.example/extra/os/x86_64/htop-3.5.1-1-x86_64.pkg.tar.zst\n"
            "multilib\tlib32-glibc\t2.39-1\thttps://mirror.example/multilib/os/x86_64/lib32-glibc-2.39-1-x86_64.pkg.tar.zst\n"
        )
        with patch("pacman_sysext.pacman._run_pacman", return_value=self._fake(out)) as m:
            deps = resolve_required_packages("htop", _pacman_config(tmp_path))
        assert deps == [
            ResolvedDep(
                repo="core",
                name="glibc",
                version="2.39-1",
                url="https://mirror.example/core/os/x86_64/glibc-2.39-1-x86_64.pkg.tar.zst",
                filename="glibc-2.39-1-x86_64.pkg.tar.zst",
            ),
            ResolvedDep(
                repo="extra",
                name="htop",
                version="3.5.1-1",
                url="https://mirror.example/extra/os/x86_64/htop-3.5.1-1-x86_64.pkg.tar.zst",
                filename="htop-3.5.1-1-x86_64.pkg.tar.zst",
            ),
            ResolvedDep(
                repo="multilib",
                name="lib32-glibc",
                version="2.39-1",
                url="https://mirror.example/multilib/os/x86_64/lib32-glibc-2.39-1-x86_64.pkg.tar.zst",
                filename="lib32-glibc-2.39-1-x86_64.pkg.tar.zst",
            ),
        ]
        # Sanity: pacman is invoked with the structured print-format and --noconfirm.
        called_args = m.call_args[0][0]
        assert "--print" in called_args
        assert "--print-format" in called_args
        assert called_args[called_args.index("--print-format") + 1] == "%r\t%n\t%v\t%l"
        assert "--noconfirm" in called_args

    def test_url_with_embedded_spaces_passes_through(self, tmp_path: Path) -> None:
        # Defensive: tab-separated splitting must preserve any whitespace
        # inside fields. Pacman shouldn't emit literal spaces in URLs, but
        # the parser must not split on them either way.
        out = "core\tfoo\t1.0-1\tfile:///cache with spaces/foo-1.0-1-x86_64.pkg.tar.zst\n"
        with patch("pacman_sysext.pacman._run_pacman", return_value=self._fake(out)):
            deps = resolve_required_packages("foo", _pacman_config(tmp_path))
        assert deps == [
            ResolvedDep(
                repo="core",
                name="foo",
                version="1.0-1",
                url="file:///cache with spaces/foo-1.0-1-x86_64.pkg.tar.zst",
                filename="foo-1.0-1-x86_64.pkg.tar.zst",
            ),
        ]

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        out = (
            "\n"
            "core\tglibc\t2.39-1\thttps://mirror.example/core/os/x86_64/glibc-2.39-1-x86_64.pkg.tar.zst\n"
            "\n"
        )
        with patch("pacman_sysext.pacman._run_pacman", return_value=self._fake(out)):
            deps = resolve_required_packages("glibc", _pacman_config(tmp_path))
        assert len(deps) == 1
        assert deps[0].name == "glibc"

    def test_malformed_line_raises(self, tmp_path: Path) -> None:
        out = "core glibc 2.39-1 url\n"  # spaces instead of tabs
        with (
            patch("pacman_sysext.pacman._run_pacman", return_value=self._fake(out)),
            pytest.raises(PacmanError, match="unexpected --print-format"),
        ):
            resolve_required_packages("glibc", _pacman_config(tmp_path))

    def test_get_required_packages_wraps_resolver(self, tmp_path: Path) -> None:
        out = (
            "core\tglibc\t2.39-1\thttps://mirror.example/core/os/x86_64/glibc-2.39-1-x86_64.pkg.tar.zst\n"
            "extra\thtop\t3.5.1-1\thttps://mirror.example/extra/os/x86_64/htop-3.5.1-1-x86_64.pkg.tar.zst\n"
        )
        with patch("pacman_sysext.pacman._run_pacman", return_value=self._fake(out)):
            filenames = get_required_packages("htop", _pacman_config(tmp_path))
        assert filenames == [
            "glibc-2.39-1-x86_64.pkg.tar.zst",
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
        ]
