"""Tests for the version comparison and constraint parsing module.

This module is the documented exception to the "no real subprocess in tests"
rule: vercmp is hermetic (pure function of args, no I/O), tiny, stable, and
part of pacman which is required on every dev machine running this project.
"""

import subprocess
from unittest.mock import patch

import pytest

from pacman_sysext.version import (
    VersionConstraint,
    VersionError,
    parse_constraint,
    satisfies,
    select_best_version,
    vercmp,
)


class TestVercmp:
    def test_less_than(self) -> None:
        assert vercmp("1.0", "2.0") == -1

    def test_greater_than(self) -> None:
        assert vercmp("2.0", "1.0") == 1

    def test_equal(self) -> None:
        assert vercmp("1.0", "1.0") == 0

    def test_pkgrel_matters(self) -> None:
        assert vercmp("1.0-1", "1.0-2") == -1

    def test_epoch_dominates(self) -> None:
        assert vercmp("1:1.0", "2.0") == 1

    def test_missing_binary_raises(self) -> None:
        with (
            patch("pacman_sysext.version.subprocess.run", side_effect=FileNotFoundError("vercmp")),
            pytest.raises(VersionError, match="not found"),
        ):
            vercmp("1.0", "2.0")

    def test_garbage_output_raises(self) -> None:
        fake = subprocess.CompletedProcess(args=["vercmp", "a", "b"], returncode=0, stdout="oops\n")
        with (
            patch("pacman_sysext.version.subprocess.run", return_value=fake),
            pytest.raises(VersionError, match="non-integer"),
        ):
            vercmp("a", "b")


class TestParseConstraint:
    def test_name_only(self) -> None:
        c = parse_constraint("libfoo")
        assert c == VersionConstraint(name="libfoo", operator=None, version=None)

    def test_with_ge(self) -> None:
        assert parse_constraint("libfoo>=1.5") == VersionConstraint("libfoo", ">=", "1.5")

    def test_with_spaces(self) -> None:
        assert parse_constraint("libfoo >= 1.5") == VersionConstraint("libfoo", ">=", "1.5")

    def test_with_ne(self) -> None:
        assert parse_constraint("libfoo!=1.0") == VersionConstraint("libfoo", "!=", "1.0")

    def test_with_eq(self) -> None:
        assert parse_constraint("libfoo=2.0") == VersionConstraint("libfoo", "=", "2.0")

    def test_with_lt(self) -> None:
        assert parse_constraint("libfoo<3.0") == VersionConstraint("libfoo", "<", "3.0")

    def test_strips_optdep_description(self) -> None:
        c = parse_constraint("nodejs: for runtime support")
        assert c == VersionConstraint(name="nodejs", operator=None, version=None)

    def test_strips_optdep_with_constraint(self) -> None:
        c = parse_constraint("nodejs>=20: for runtime")
        assert c == VersionConstraint(name="nodejs", operator=">=", version="20")

    def test_empty_raises(self) -> None:
        with pytest.raises(VersionError):
            parse_constraint("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(VersionError):
            parse_constraint("   ")

    def test_operator_without_version_raises(self) -> None:
        with pytest.raises(VersionError):
            parse_constraint("libfoo>=")

    def test_hyphenated_name(self) -> None:
        c = parse_constraint("gcc-libs>=14.0")
        assert c == VersionConstraint(name="gcc-libs", operator=">=", version="14.0")

    def test_epoch_in_version(self) -> None:
        c = parse_constraint("libfoo>=1:2.0")
        assert c == VersionConstraint(name="libfoo", operator=">=", version="1:2.0")

    def test_epoch_plus_optdep_description(self) -> None:
        c = parse_constraint("libfoo>=1:2.0: needed at runtime")
        assert c == VersionConstraint(name="libfoo", operator=">=", version="1:2.0")


class TestSatisfies:
    def test_unconstrained_always_true(self) -> None:
        assert satisfies("1.0", parse_constraint("libfoo")) is True

    def test_ge_holds(self) -> None:
        assert satisfies("1.5", parse_constraint("libfoo>=1.0")) is True

    def test_ge_fails(self) -> None:
        assert satisfies("0.9", parse_constraint("libfoo>=1.0")) is False

    def test_lt_fails(self) -> None:
        assert satisfies("1.5", parse_constraint("libfoo<1.0")) is False

    def test_eq_holds(self) -> None:
        assert satisfies("1.5", parse_constraint("libfoo=1.5")) is True

    def test_ne_fails(self) -> None:
        assert satisfies("1.5", parse_constraint("libfoo!=1.5")) is False

    def test_unknown_operator_raises(self) -> None:
        bad = VersionConstraint(name="libfoo", operator="~", version="1.0")
        with pytest.raises(VersionError):
            satisfies("1.5", bad)


class TestSelectBestVersion:
    def test_picks_newest_satisfying_all(self) -> None:
        cs = [parse_constraint("x>=1.2"), parse_constraint("x<2.0")]
        assert select_best_version(["1.0", "1.5", "2.0"], cs) == "1.5"

    def test_no_candidates(self) -> None:
        assert select_best_version([], [parse_constraint("x>=1.0")]) is None

    def test_none_satisfy(self) -> None:
        assert select_best_version(["1.0"], [parse_constraint("x>=2.0")]) is None

    def test_picks_largest_when_unconstrained(self) -> None:
        assert select_best_version(["1.0", "3.0", "2.0"], []) == "3.0"
