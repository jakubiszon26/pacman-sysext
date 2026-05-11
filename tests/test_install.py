"""Tests for the install command's state integration."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from pacman_sysext import state
from pacman_sysext.commands import install
from pacman_sysext.config import AppConfig, BuilderConfig, PacmanConfig, SysextConfig
from pacman_sysext.version import VersionConstraint


def _config(tmp_path: Path) -> AppConfig:
    base = tmp_path
    return AppConfig(
        pacman=PacmanConfig(
            dbpath=base / "db",
            cachedir=base / "cache",
            config_file=base / "pacman.conf",
            gpgdir=base / "gnupg",
        ),
        builder=BuilderConfig(output_dir=base / "sysexts", staging_dir=base / "staging"),
        sysext=SysextConfig(extensions_dir=base / "extensions"),
        state_db=base / "state.db",
    )


def _seed_cache(cachedir: Path, filenames: Iterable[str]) -> None:
    cachedir.mkdir(parents=True, exist_ok=True)
    for f in filenames:
        (cachedir / f).write_bytes(b"fake package")


def _fake_build(pkg_path: Path, output_dir: Path, fs_format: str = "squashfs") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    name = pkg_path.name.replace(".pkg.tar.zst", ".raw").rsplit("-", 1)[0] + ".raw"
    raw = output_dir / name
    raw.write_bytes(b"raw image bytes for " + pkg_path.name.encode())
    return raw


@pytest.fixture
def mocked(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Patch every pacman/build/activate boundary so tests stay hermetic."""
    mocks: dict[str, MagicMock] = {}
    for name in (
        "sync_databases",
        "get_required_packages",
        "get_package_version",
        "get_package_dependencies",
        "get_package_provides",
        "get_base_snapshot",
        "download_package",
        "find_unsatisfied",
        "build_sysext",
        "activate_all",
        "_confirm",
    ):
        mock = MagicMock(name=name)
        monkeypatch.setattr(f"pacman_sysext.commands.install.{name}", mock)
        mocks[name] = mock
    return mocks


def _set_defaults(mocks: dict[str, MagicMock]) -> None:
    mocks["sync_databases"].return_value = None
    mocks["download_package"].return_value = None
    mocks["activate_all"].return_value = None
    mocks["_confirm"].return_value = True
    mocks["get_base_snapshot"].return_value = {"glibc": "2.39-1"}
    mocks["find_unsatisfied"].return_value = set()  # everything host-satisfied by default
    mocks["get_package_provides"].return_value = {}
    mocks["build_sysext"].side_effect = _fake_build


class TestFreshInstall:
    def test_creates_state_with_user_request_and_sysexts(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["get_required_packages"].return_value = [
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        ]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = [
            VersionConstraint("libcap", ">=", "2.78"),
        ]
        # libcap is not on host → it must be built; htop always built
        mocked["find_unsatisfied"].return_value = {"htop", "libcap"}
        _seed_cache(
            config.pacman.cachedir,
            ["htop-3.5.1-1-x86_64.pkg.tar.zst", "libcap-2.78-1-x86_64.pkg.tar.zst"],
        )

        install.run("htop", config)

        result = state.load(config.state_db)
        assert "htop" in result.user_requests
        assert result.user_requests["htop"].installed_version == "3.5.1-1"
        assert result.user_requests["htop"].requirements == {"libcap": ">=2.78"}
        # both packages get sysext records
        keys = set(result.sysexts.keys())
        assert {"htop-3.5.1-1", "libcap-2.78-1"} == keys
        # all share the same snapshot id
        snap_ids = {r.snapshot_id for r in result.sysexts.values()}
        assert len(snap_ids) == 1
        # snapshot interned once
        assert list(result.snapshots.keys()) == list(snap_ids)


class TestReinstall:
    def test_user_request_replaced_and_new_sysext_added(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        # Pre-seed state with htop 3.5.0
        initial = state.State()
        snap_id = state.intern_snapshot(initial, {"glibc": "2.39-1"})
        state.add_sysext(
            initial,
            state.SysextRecord(
                name="htop",
                version="3.5.0-1",
                raw_filename="htop-3.5.0-1.raw",
                fs_format="squashfs",
                sha256="old",
                installed_at=datetime.now(UTC),
                snapshot_id=snap_id,
                provides={},
            ),
        )
        state.add_user_request(
            initial,
            state.UserRequest(
                name="htop",
                installed_version="3.5.0-1",
                requested_at=datetime.now(UTC),
                requirements={},
            ),
        )
        state.save(initial, config.state_db)

        mocked["get_required_packages"].return_value = ["htop-3.5.1-1-x86_64.pkg.tar.zst"]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop"}
        _seed_cache(config.pacman.cachedir, ["htop-3.5.1-1-x86_64.pkg.tar.zst"])

        install.run("htop", config)

        result = state.load(config.state_db)
        # User request now points to the new version
        assert result.user_requests["htop"].installed_version == "3.5.1-1"
        # Both old and new SysextRecords coexist (cleanup is remove's job)
        assert "htop-3.5.0-1" in result.sysexts
        assert "htop-3.5.1-1" in result.sysexts


class TestHostSatisfied:
    def test_only_target_gets_built_when_all_deps_in_host(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["get_required_packages"].return_value = [
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        ]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = []
        # only htop is unsatisfied; libcap is on host
        mocked["find_unsatisfied"].return_value = {"htop"}
        _seed_cache(
            config.pacman.cachedir,
            ["htop-3.5.1-1-x86_64.pkg.tar.zst", "libcap-2.78-1-x86_64.pkg.tar.zst"],
        )

        install.run("htop", config)

        result = state.load(config.state_db)
        # Only the target gets a sysext record
        assert set(result.sysexts.keys()) == {"htop-3.5.1-1"}


class TestSysextReuse:
    def test_existing_sysext_with_valid_hash_is_reused(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)

        # Pre-seed a libcap sysext with a matching on-disk hash
        config.builder.output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = config.builder.output_dir / "libcap-2.78-1.raw"
        raw_path.write_bytes(b"valid libcap image")
        digest = hashlib.sha256(b"valid libcap image").hexdigest()

        initial = state.State()
        snap_id = state.intern_snapshot(initial, {"glibc": "2.39-1"})

        state.add_sysext(
            initial,
            state.SysextRecord(
                name="libcap",
                version="2.78-1",
                raw_filename="libcap-2.78-1.raw",
                fs_format="squashfs",
                sha256=digest,
                installed_at=datetime.now(UTC),
                snapshot_id=snap_id,
                provides={},
            ),
        )
        state.save(initial, config.state_db)

        mocked["get_required_packages"].return_value = [
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        ]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop", "libcap"}
        _seed_cache(
            config.pacman.cachedir,
            ["htop-3.5.1-1-x86_64.pkg.tar.zst", "libcap-2.78-1-x86_64.pkg.tar.zst"],
        )

        install.run("htop", config)

        # libcap is reused — only htop is built
        built_paths = [c.args[0].name for c in mocked["build_sysext"].call_args_list]
        assert built_paths == ["htop-3.5.1-1-x86_64.pkg.tar.zst"]

    def test_integrity_failure_forces_rebuild(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)

        # Pre-seed a libcap sysext with a corrupted on-disk file
        config.builder.output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = config.builder.output_dir / "libcap-2.78-1.raw"
        raw_path.write_bytes(b"corrupted")

        initial = state.State()
        snap_id = state.intern_snapshot(initial, {"glibc": "2.39-1"})

        state.add_sysext(
            initial,
            state.SysextRecord(
                name="libcap",
                version="2.78-1",
                raw_filename="libcap-2.78-1.raw",
                fs_format="squashfs",
                sha256="aaaaa-wrong-hash",
                installed_at=datetime.now(UTC),
                snapshot_id=snap_id,
                provides={},
            ),
        )
        state.save(initial, config.state_db)

        mocked["get_required_packages"].return_value = [
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        ]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop", "libcap"}
        _seed_cache(
            config.pacman.cachedir,
            ["htop-3.5.1-1-x86_64.pkg.tar.zst", "libcap-2.78-1-x86_64.pkg.tar.zst"],
        )

        install.run("htop", config)

        built_filenames = {c.args[0].name for c in mocked["build_sysext"].call_args_list}
        assert built_filenames == {
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        }
        # Sysext record now has a fresh hash matching the rebuilt file
        result = state.load(config.state_db)
        new_record = result.sysexts["libcap-2.78-1"]
        assert new_record.sha256 != "aaaaa-wrong-hash"


class TestConflict:
    def test_incompatible_constraints_raise_and_state_untouched(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)

        # Pre-seed: neovim requires libuv<2.0
        initial = state.State()

        state.add_user_request(
            initial,
            state.UserRequest(
                name="neovim",
                installed_version="0.10",
                requested_at=datetime.now(UTC),
                requirements={"libuv": "<2.0"},
            ),
        )
        state.save(initial, config.state_db)
        before = config.state_db.read_text()

        # Now install nodejs which wants libuv>=2.0
        mocked["get_required_packages"].return_value = ["nodejs-22-1-x86_64.pkg.tar.zst"]
        mocked["get_package_version"].return_value = "22-1"
        mocked["get_package_dependencies"].return_value = [
            VersionConstraint("libuv", ">=", "2.0"),
        ]
        mocked["find_unsatisfied"].return_value = {"nodejs"}

        with pytest.raises(typer.Exit):
            install.run("nodejs", config)

        # State file must be unchanged
        assert config.state_db.read_text() == before
        # build_sysext must not have been called
        mocked["build_sysext"].assert_not_called()


class TestBuildFailure:
    def test_build_failure_does_not_save_state(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["get_required_packages"].return_value = ["htop-3.5.1-1-x86_64.pkg.tar.zst"]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop"}
        _seed_cache(config.pacman.cachedir, ["htop-3.5.1-1-x86_64.pkg.tar.zst"])

        from pacman_sysext.builder import BuildError

        mocked["build_sysext"].side_effect = BuildError("boom")

        with pytest.raises(typer.Exit):
            install.run("htop", config)

        assert not config.state_db.exists()


class TestDriftWarning:
    def test_warning_printed_when_drift_present(
        self, tmp_path: Path, mocked: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)

        # Pre-seed a sysext built against an older glibc
        initial = state.State()
        snap_id = state.intern_snapshot(initial, {"glibc": "2.38-1"})

        state.add_sysext(
            initial,
            state.SysextRecord(
                name="htop",
                version="3.5.0-1",
                raw_filename="htop-3.5.0-1.raw",
                fs_format="squashfs",
                sha256="x",
                installed_at=datetime.now(UTC),
                snapshot_id=snap_id,
                provides={},
            ),
        )
        state.save(initial, config.state_db)

        mocked["get_base_snapshot"].return_value = {"glibc": "2.40-1"}
        mocked["get_required_packages"].return_value = ["nano-7-1-x86_64.pkg.tar.zst"]
        mocked["get_package_version"].return_value = "7-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"nano"}
        _seed_cache(config.pacman.cachedir, ["nano-7-1-x86_64.pkg.tar.zst"])

        install.run("nano", config)

        out = capsys.readouterr().out
        assert "may have stale base dependencies" in out
        assert "glibc: 2.38-1 → 2.40-1" in out

    def test_drift_does_not_block_install(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        initial = state.State()
        snap_id = state.intern_snapshot(initial, {"glibc": "2.38-1"})

        state.add_sysext(
            initial,
            state.SysextRecord(
                name="htop",
                version="3.5.0-1",
                raw_filename="htop-3.5.0-1.raw",
                fs_format="squashfs",
                sha256="x",
                installed_at=datetime.now(UTC),
                snapshot_id=snap_id,
                provides={},
            ),
        )
        state.save(initial, config.state_db)

        mocked["get_base_snapshot"].return_value = {"glibc": "2.40-1"}
        mocked["get_required_packages"].return_value = ["nano-7-1-x86_64.pkg.tar.zst"]
        mocked["get_package_version"].return_value = "7-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"nano"}
        _seed_cache(config.pacman.cachedir, ["nano-7-1-x86_64.pkg.tar.zst"])

        install.run("nano", config)

        # Install proceeded despite the drift — new sysext landed in state.
        result = state.load(config.state_db)
        assert "nano-7-1" in result.sysexts

    def test_no_warning_when_no_drift(
        self, tmp_path: Path, mocked: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["get_required_packages"].return_value = ["nano-7-1-x86_64.pkg.tar.zst"]
        mocked["get_package_version"].return_value = "7-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"nano"}
        _seed_cache(config.pacman.cachedir, ["nano-7-1-x86_64.pkg.tar.zst"])

        install.run("nano", config)

        out = capsys.readouterr().out
        assert "may have stale base dependencies" not in out


class TestSnapshotInterning:
    def test_snapshot_captured_once_for_all_built_sysexts(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["get_required_packages"].return_value = [
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
            "libnl-3.7-1-x86_64.pkg.tar.zst",
        ]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop", "libcap", "libnl"}
        _seed_cache(
            config.pacman.cachedir,
            [
                "htop-3.5.1-1-x86_64.pkg.tar.zst",
                "libcap-2.78-1-x86_64.pkg.tar.zst",
                "libnl-3.7-1-x86_64.pkg.tar.zst",
            ],
        )

        install.run("htop", config)

        result = state.load(config.state_db)
        # Only one snapshot interned even though 3 sysexts built
        assert len(result.snapshots) == 1
        assert mocked["get_base_snapshot"].call_count == 1


class TestLocking:
    def test_lock_is_acquired_around_state_operations(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["get_required_packages"].return_value = ["htop-3.5.1-1-x86_64.pkg.tar.zst"]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop"}
        _seed_cache(config.pacman.cachedir, ["htop-3.5.1-1-x86_64.pkg.tar.zst"])

        with patch("pacman_sysext.commands.install.state.locked") as locked_mock:
            ctx = MagicMock()
            ctx.__enter__.return_value = None
            ctx.__exit__.return_value = False
            locked_mock.return_value = ctx
            install.run("htop", config)

        locked_mock.assert_called_once_with(config.state_db)


class TestTargetReuse:
    def test_target_reused_when_state_has_matching_hash(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)

        # Pre-seed htop-3.5.1-1 with matching on-disk hash.
        config.builder.output_dir.mkdir(parents=True, exist_ok=True)
        raw = config.builder.output_dir / "htop-3.5.1-1.raw"
        raw.write_bytes(b"valid htop image")
        digest = hashlib.sha256(b"valid htop image").hexdigest()

        initial = state.State()
        snap_id = state.intern_snapshot(initial, {"glibc": "2.39-1"})
        state.add_sysext(
            initial,
            state.SysextRecord(
                name="htop",
                version="3.5.1-1",
                raw_filename="htop-3.5.1-1.raw",
                fs_format="squashfs",
                sha256=digest,
                installed_at=datetime.now(UTC),
                snapshot_id=snap_id,
                provides={},
            ),
        )
        state.save(initial, config.state_db)

        mocked["get_required_packages"].return_value = ["htop-3.5.1-1-x86_64.pkg.tar.zst"]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop"}
        _seed_cache(config.pacman.cachedir, ["htop-3.5.1-1-x86_64.pkg.tar.zst"])

        install.run("htop", config)

        # Nothing was built — target reused.
        mocked["build_sysext"].assert_not_called()
        # User request still recorded (and reactivation prompt fired).
        result = state.load(config.state_db)
        assert "htop" in result.user_requests


class TestOrphanCleanup:
    def test_raw_files_removed_when_build_fails_midway(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        from pacman_sysext.builder import BuildError

        call_count = [0]

        def build_then_fail(pkg_path: Path, output_dir: Path, fs_format: str = "squashfs") -> Path:
            call_count[0] += 1
            if call_count[0] == 2:
                raise BuildError("second build fails")
            return _fake_build(pkg_path, output_dir, fs_format)

        mocked["build_sysext"].side_effect = build_then_fail
        mocked["get_required_packages"].return_value = [
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        ]
        mocked["get_package_version"].return_value = "3.5.1-1"
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop", "libcap"}
        _seed_cache(
            config.pacman.cachedir,
            ["htop-3.5.1-1-x86_64.pkg.tar.zst", "libcap-2.78-1-x86_64.pkg.tar.zst"],
        )

        with pytest.raises(typer.Exit):
            install.run("htop", config)

        # No .raw files should remain — the successful first build was cleaned up.
        if config.builder.output_dir.exists():
            assert list(config.builder.output_dir.glob("*.raw")) == []


class TestConflictMessage:
    def test_message_includes_known_versions(
        self, tmp_path: Path, mocked: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)

        # Pre-seed: neovim requires libuv<2.0, and a libuv-1.45-1 sysext exists.

        initial = state.State()
        snap_id = state.intern_snapshot(initial, {"glibc": "2.39-1"})
        state.add_sysext(
            initial,
            state.SysextRecord(
                name="libuv",
                version="1.45-1",
                raw_filename="libuv-1.45-1.raw",
                fs_format="squashfs",
                sha256="x",
                installed_at=datetime.now(UTC),
                snapshot_id=snap_id,
                provides={},
            ),
        )
        state.add_user_request(
            initial,
            state.UserRequest(
                name="neovim",
                installed_version="0.10",
                requested_at=datetime.now(UTC),
                requirements={"libuv": "<2.0"},
            ),
        )
        state.save(initial, config.state_db)

        mocked["get_required_packages"].return_value = ["nodejs-22-1-x86_64.pkg.tar.zst"]
        mocked["get_package_version"].return_value = "22-1"
        mocked["get_package_dependencies"].return_value = [
            VersionConstraint("libuv", ">=", "2.0"),
        ]
        mocked["find_unsatisfied"].return_value = {"nodejs"}

        with pytest.raises(typer.Exit):
            install.run("nodejs", config)

        out = capsys.readouterr().out
        assert "Known libuv versions in state: 1.45-1" in out


class TestConstraintsOverlap:
    def test_unconstrained_overlaps_anything(self) -> None:
        assert install._constraints_overlap(
            VersionConstraint("x", None, None), VersionConstraint("x", ">=", "1.0")
        )

    def test_ge_lt_disjoint(self) -> None:
        assert not install._constraints_overlap(
            VersionConstraint("x", ">=", "2.0"), VersionConstraint("x", "<", "2.0")
        )

    def test_ge_le_with_gap(self) -> None:
        assert install._constraints_overlap(
            VersionConstraint("x", ">=", "1.0"), VersionConstraint("x", "<=", "3.0")
        )

    def test_eq_match(self) -> None:
        assert install._constraints_overlap(
            VersionConstraint("x", "=", "1.0"), VersionConstraint("x", ">=", "1.0")
        )

    def test_eq_mismatch(self) -> None:
        assert not install._constraints_overlap(
            VersionConstraint("x", "=", "1.0"), VersionConstraint("x", "=", "2.0")
        )
