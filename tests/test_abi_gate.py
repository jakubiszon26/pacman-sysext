"""Tests for the pre-flight ABI Gatekeeper.

The gate uses the real `vercmp` binary (same exception as test_version.py):
it is hermetic, tiny, stable, and reimplementing its semantics in mocks
defeats the point of the module.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pacman_sysext import abi_gate
from pacman_sysext.abi_gate import ClassifiedDep, GateReport, classify
from pacman_sysext.version import VersionError


def _f(name: str, version: str, arch: str = "x86_64") -> str:
    """Build a realistic pacman package filename."""
    return f"{name}-{version}-{arch}.pkg.tar.zst"


def _bucket_names(report: GateReport, bucket: str) -> list[str]:
    return [d.name for d in getattr(report, bucket)]


class TestClassifyBundle:
    def test_host_missing_goes_to_bundle(self) -> None:
        report = classify(
            resolved_filenames=[_f("libcap", "2.78-1")],
            target_pkg="htop",
            host_packages={},
            host_provided=set(),
        )
        assert _bucket_names(report, "bundles") == ["libcap"]
        assert report.skips == [] == report.shadows == report.blocks

    def test_target_always_bundles_even_when_host_has_same_version(self) -> None:
        """User explicitly asked for the sysext form of the target."""
        report = classify(
            resolved_filenames=[_f("htop", "3.5.1-1")],
            target_pkg="htop",
            host_packages={"htop": "3.5.1-1"},
            host_provided={"htop"},
        )
        assert _bucket_names(report, "bundles") == ["htop"]
        assert report.skips == []

    def test_target_always_bundles_even_when_host_has_different_version(self) -> None:
        report = classify(
            resolved_filenames=[_f("htop", "3.5.1-1")],
            target_pkg="htop",
            host_packages={"htop": "3.4.0-1"},
            host_provided={"htop"},
        )
        assert _bucket_names(report, "bundles") == ["htop"]
        assert report.blocks == []


class TestClassifySkip:
    def test_exact_version_match_skips(self) -> None:
        report = classify(
            resolved_filenames=[
                _f("htop", "3.5.1-1"),
                _f("libcap", "2.78-1"),
            ],
            target_pkg="htop",
            host_packages={"libcap": "2.78-1"},
            host_provided={"libcap"},
        )
        assert _bucket_names(report, "skips") == ["libcap"]
        assert _bucket_names(report, "bundles") == ["htop"]

    def test_provides_alias_skips_without_version(self) -> None:
        """Host has zlib-ng-compat providing zlib — we can't compare versions
        without a separate -Qi query, so skip conservatively rather than risk
        shadowing the alias.
        """
        report = classify(
            resolved_filenames=[
                _f("htop", "3.5.1-1"),
                _f("zlib", "1.3-1"),
            ],
            target_pkg="htop",
            host_packages={},
            host_provided={"zlib"},
        )
        assert _bucket_names(report, "skips") == ["zlib"]
        skip = report.skips[0]
        assert skip.host_version is None


class TestClassifyBlock:
    def test_newer_resolved_blocks_on_critical_lib(self) -> None:
        """The SEV1 scenario: host has glib2 2.78, sysext would ship 2.80."""
        report = classify(
            resolved_filenames=[
                _f("okular", "24.12.1-1"),
                _f("glib2", "2.80.2-1"),
            ],
            target_pkg="okular",
            host_packages={"glib2": "2.78.6-1"},
            host_provided={"glib2"},
        )
        assert _bucket_names(report, "blocks") == ["glib2"]
        block = report.blocks[0]
        assert block.resolved_version == "2.80.2-1"
        assert block.host_version == "2.78.6-1"

    def test_older_resolved_blocks_too(self) -> None:
        """Downgrade-shadowing is just as catastrophic — host expects 2.80, sysext ships 2.78."""
        report = classify(
            resolved_filenames=[_f("glib2", "2.78.6-1")],
            target_pkg="foo",
            host_packages={"glib2": "2.80.2-1"},
            host_provided={"glib2"},
        )
        assert _bucket_names(report, "blocks") == ["glib2"]

    def test_multiple_blocks_collected(self) -> None:
        report = classify(
            resolved_filenames=[
                _f("okular", "24.12-1"),
                _f("glib2", "2.80-1"),
                _f("systemd-libs", "257-1"),
                _f("pam", "1.6.2-1"),
            ],
            target_pkg="okular",
            host_packages={
                "glib2": "2.78-1",
                "systemd-libs": "256-1",
                "pam": "1.6.1-1",
            },
            host_provided={"glib2", "systemd-libs", "pam"},
        )
        assert sorted(_bucket_names(report, "blocks")) == ["glib2", "pam", "systemd-libs"]


class TestClassifyShadow:
    @pytest.mark.parametrize(
        "name",
        [
            "ttf-dejavu",
            "ttf-liberation",
            "otf-fira-code",
            "noto-fonts",
            "adobe-source-code-pro-fonts",
            "papirus-icons",
            "adwaita-icon-theme",
            "hicolor-icon-theme",
            "font-bh-ttf",
        ],
    )
    def test_cosmetic_package_version_drift_goes_to_shadow(self, name: str) -> None:
        report = classify(
            resolved_filenames=[_f(name, "2.0-1")],
            target_pkg="okular",
            host_packages={name: "1.0-1"},
            host_provided={name},
        )
        assert _bucket_names(report, "shadows") == [name]
        assert report.blocks == []

    def test_non_cosmetic_package_with_similar_name_still_blocks(self) -> None:
        """`fontconfig` doesn't match font-*; it's the library, not a font."""
        report = classify(
            resolved_filenames=[_f("fontconfig", "2.16-1")],
            target_pkg="okular",
            host_packages={"fontconfig": "2.15-1"},
            host_provided={"fontconfig"},
        )
        assert _bucket_names(report, "blocks") == ["fontconfig"]


class TestSafeShadowMatcher:
    @pytest.mark.parametrize(
        "name",
        [
            "ttf-dejavu",
            "font-bh-ttf",
            "papirus-icons",
            "adwaita-icon-theme",
            "noto-fonts-cjk",
            "adobe-source-han-sans-cn-fonts",
        ],
    )
    def test_matches(self, name: str) -> None:
        assert abi_gate._is_safe_shadow(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "fontconfig",  # libfontconfig — ABI-critical
            "glib2",
            "systemd-libs",
            "pam",
            "wayland",
            "gtk4",
            "qt6-base",
            "libxkbcommon",
        ],
    )
    def test_does_not_match_libraries(self, name: str) -> None:
        assert abi_gate._is_safe_shadow(name) is False


class TestClassifyMixed:
    def test_realistic_okular_install_mix(self) -> None:
        """Target bundled, host-matching deps skipped, font shadowed, glib2 blocked."""
        report = classify(
            resolved_filenames=[
                _f("okular", "24.12.1-1"),
                _f("libcap", "2.78-1"),  # exact host match → skip
                _f("zlib", "1.3-1"),  # provides alias → skip
                _f("ttf-dejavu", "2.38-1"),  # cosmetic drift → shadow
                _f("glib2", "2.80.2-1"),  # critical drift → block
                _f("libnewdep", "1.0-1"),  # missing on host → bundle
            ],
            target_pkg="okular",
            host_packages={
                "libcap": "2.78-1",
                "ttf-dejavu": "2.37-1",
                "glib2": "2.78.6-1",
            },
            host_provided={"libcap", "zlib", "ttf-dejavu", "glib2"},
        )
        assert sorted(_bucket_names(report, "bundles")) == ["libnewdep", "okular"]
        assert sorted(_bucket_names(report, "skips")) == ["libcap", "zlib"]
        assert _bucket_names(report, "shadows") == ["ttf-dejavu"]
        assert _bucket_names(report, "blocks") == ["glib2"]

    def test_empty_resolved_tree_yields_empty_report(self) -> None:
        report = classify(
            resolved_filenames=[],
            target_pkg="okular",
            host_packages={},
            host_provided=set(),
        )
        assert report == GateReport(target_pkg="okular")


class TestClassifyErrorPropagation:
    def test_vercmp_failure_propagates_as_version_error(self) -> None:
        """If `vercmp` itself blows up on malformed input, the gate must
        surface a `VersionError` to the caller so install.py can render
        a proper error instead of crashing with a traceback.
        """
        with (
            patch(
                "pacman_sysext.abi_gate.vercmp",
                side_effect=VersionError("vercmp produced non-integer output: 'oops'"),
            ),
            pytest.raises(VersionError, match="non-integer"),
        ):
            classify(
                resolved_filenames=[_f("glib2", "2.80-1")],
                target_pkg="okular",
                host_packages={"glib2": "2.78-1"},
                host_provided={"glib2"},
            )


class TestClassifiedDepFields:
    def test_filename_preserved(self) -> None:
        report = classify(
            resolved_filenames=["htop-3.5.1-1-x86_64.pkg.tar.zst"],
            target_pkg="htop",
            host_packages={},
            host_provided=set(),
        )
        assert report.bundles[0] == ClassifiedDep(
            name="htop",
            resolved_version="3.5.1-1",
            host_version=None,
            bucket="bundle",
            filename="htop-3.5.1-1-x86_64.pkg.tar.zst",
        )

    def test_shadow_carries_host_version(self) -> None:
        report = classify(
            resolved_filenames=[_f("ttf-dejavu", "2.38-1")],
            target_pkg="okular",
            host_packages={"ttf-dejavu": "2.37-1"},
            host_provided={"ttf-dejavu"},
        )
        assert report.shadows[0].host_version == "2.37-1"
        assert report.shadows[0].resolved_version == "2.38-1"
        assert report.shadows[0].bucket == "shadow"
