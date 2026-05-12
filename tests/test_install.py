"""Tests for the install command's state integration."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from pacman_sysext import state
from pacman_sysext.commands import install
from pacman_sysext.config import AppConfig, BuilderConfig, PacmanConfig, SysextConfig
from pacman_sysext.pacman import ResolvedDep, parse_pkg_filename
from pacman_sysext.time_sync import TimeSyncConfig
from pacman_sysext.version import VersionConstraint


def _config(tmp_path: Path, time_sync: TimeSyncConfig | None = None) -> AppConfig:
    base = tmp_path
    return AppConfig(
        pacman=PacmanConfig(
            dbpath=base / "db",
            cachedir=base / "cache",
            config_file=base / "pacman.conf",
            gpgdir=base / "gnupg",
        ),
        builder=BuilderConfig(output_dir=base / "sysexts"),
        sysext=SysextConfig(extensions_dir=base / "extensions"),
        state_db=base / "state.db",
        time_sync=time_sync if time_sync is not None else TimeSyncConfig(),
    )


def _resolved(*filenames: str, repo: str = "extra") -> list[ResolvedDep]:
    """Build a ResolvedDep list mirroring `pacman -Sw --print` for tests.

    Tests don't care about repo/url beyond the time-sync gate, so default
    to `extra` and synthesize plausible URLs.
    """
    deps: list[ResolvedDep] = []
    for filename in filenames:
        name, version = parse_pkg_filename(filename)
        deps.append(
            ResolvedDep(
                repo=repo,
                name=name,
                version=version,
                url=f"https://mirror.example/{repo}/os/x86_64/{filename}",
                filename=filename,
            )
        )
    return deps


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
        "resolve_required_packages",
        "get_package_dependencies",
        "get_package_provides",
        "get_base_snapshot",
        "download_package",
        "find_unsatisfied",
        "query_system_packages",
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
    # The gate now also asks `pacman -Q` for installed versions. Default to an
    # empty map so the gate falls back to the provides-set path (`host_provided`,
    # derived from find_unsatisfied) and existing tests stay correct.
    mocks["query_system_packages"].return_value = {}
    mocks["get_package_provides"].return_value = {}
    mocks["build_sysext"].side_effect = _fake_build


class TestFreshInstall:
    def test_creates_state_with_user_request_and_sysexts(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        )

        def deps_for(name: str, _config: object) -> list[VersionConstraint]:
            return {
                "htop": [VersionConstraint("libcap", ">=", "2.78")],
                "libcap": [],
            }[name]

        mocked["get_package_dependencies"].side_effect = deps_for
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
        # depends recorded per built record
        assert result.sysexts["htop-3.5.1-1"].depends == ["libcap"]
        assert result.sysexts["libcap-2.78-1"].depends == []

    def test_user_request_version_matches_filename_not_pacman_si(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        """Regression: pacman -Si may report a different pkgrel than the file.

        CachyOS rebuilds bump pkgrel from `1` to `1.1`, but `pacman -Si`
        sometimes still reports the upstream string. The UserRequest must
        track the filename-derived version so get_explicit's strict lookup
        keeps working.
        """
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = _resolved(
            "grafana-12.4.2-1.1-x86_64_v4.pkg.tar.zst",
        )
        # pacman -Si reports the upstream version, intentionally divergent
        # from the filename pkgrel.
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"grafana"}
        _seed_cache(config.pacman.cachedir, ["grafana-12.4.2-1.1-x86_64_v4.pkg.tar.zst"])

        install.run("grafana", config)

        result = state.load(config.state_db)
        # The sysext key uses the filename version.
        assert "grafana-12.4.2-1.1" in result.sysexts
        # The user request must point at the same version, not pacman's.
        assert result.user_requests["grafana"].installed_version == "12.4.2-1.1"

    def test_depends_query_failure_records_empty_depends(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        from pacman_sysext.pacman import PacmanError

        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        )

        def deps_for(name: str, _config: object) -> list[VersionConstraint]:
            if name == "libcap":
                raise PacmanError("simulated", returncode=1, stderr="boom")
            return [VersionConstraint("libcap", ">=", "2.78")]

        mocked["get_package_dependencies"].side_effect = deps_for
        mocked["find_unsatisfied"].return_value = {"htop", "libcap"}
        _seed_cache(
            config.pacman.cachedir,
            ["htop-3.5.1-1-x86_64.pkg.tar.zst", "libcap-2.78-1-x86_64.pkg.tar.zst"],
        )

        install.run("htop", config)

        result = state.load(config.state_db)
        assert result.sysexts["htop-3.5.1-1"].depends == ["libcap"]
        # libcap's depends query failed mid-build but the install completed.
        assert result.sysexts["libcap-2.78-1"].depends == []


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

        mocked["resolve_required_packages"].return_value = _resolved(

            "htop-3.5.1-1-x86_64.pkg.tar.zst",

        )
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
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        )
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

        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        )
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

        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        )
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
        mocked["resolve_required_packages"].return_value = _resolved(
            "nodejs-22-1-x86_64.pkg.tar.zst",
        )
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
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
        )
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
        mocked["resolve_required_packages"].return_value = _resolved(
            "nano-7-1-x86_64.pkg.tar.zst",
        )
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
        mocked["resolve_required_packages"].return_value = _resolved(
            "nano-7-1-x86_64.pkg.tar.zst",
        )
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
        mocked["resolve_required_packages"].return_value = _resolved(
            "nano-7-1-x86_64.pkg.tar.zst",
        )
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
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
            "libnl-3.7-1-x86_64.pkg.tar.zst",
        )
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
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
        )
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

    def test_lock_timeout_exits_cleanly(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A `StateError` from `state.locked()` must surface as `typer.Exit(1)`.

        Driving the real flock timeout requires either waiting out
        `_DEFAULT_LOCK_TIMEOUT` (300s) or patching the default arg —
        defaults bind at function-definition time, so reassigning the
        module constant is a no-op. Mocking the entry point is cleaner.
        """
        config = _config(tmp_path)
        _set_defaults(mocked)

        def fake_locked(_path: Path) -> object:
            raise state.StateError("could not acquire lock on /fake/lock within 0s")

        with (
            patch("pacman_sysext.commands.install.state.locked", side_effect=fake_locked),
            pytest.raises(typer.Exit) as exc_info,
        ):
            install.run("htop", config)

        assert exc_info.value.exit_code == 1
        captured = capsys.readouterr()
        assert "Error reading state" in captured.out
        assert "could not acquire lock" in captured.out
        # State must not have been touched.
        assert not config.state_db.exists()

    def test_malformed_state_db_exits_cleanly(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A `StateError` from `state.load()` flows through the same handler."""
        config = _config(tmp_path)
        _set_defaults(mocked)
        config.state_db.parent.mkdir(parents=True, exist_ok=True)
        config.state_db.write_text("{not valid json")
        before = config.state_db.read_bytes()

        with pytest.raises(typer.Exit) as exc_info:
            install.run("htop", config)

        assert exc_info.value.exit_code == 1
        captured = capsys.readouterr()
        assert "Error reading state" in captured.out
        # State must not have been overwritten.
        assert config.state_db.read_bytes() == before


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

        mocked["resolve_required_packages"].return_value = _resolved(

            "htop-3.5.1-1-x86_64.pkg.tar.zst",

        )
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
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        )
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

        mocked["resolve_required_packages"].return_value = _resolved(

            "nodejs-22-1-x86_64.pkg.tar.zst",

        )
        mocked["get_package_dependencies"].return_value = [
            VersionConstraint("libuv", ">=", "2.0"),
        ]
        mocked["find_unsatisfied"].return_value = {"nodejs"}

        with pytest.raises(typer.Exit):
            install.run("nodejs", config)

        out = capsys.readouterr().out
        assert "Known libuv versions in state: 1.45-1" in out


class TestAssumeYes:
    def test_assume_yes_skips_confirm_prompts(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        # We re-patch `_confirm` to a real function that fails if input() is
        # touched, ensuring assume_yes really bypasses interactive code.
        from pacman_sysext.commands import install as install_mod

        def real_confirm(prompt: str, assume_yes: bool = False) -> bool:
            if assume_yes:
                return True
            raise AssertionError("input() should not be called when assume_yes is True")

        mocked["_confirm"].side_effect = real_confirm

        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
        )
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop"}
        _seed_cache(config.pacman.cachedir, ["htop-3.5.1-1-x86_64.pkg.tar.zst"])

        install_mod.run("htop", config, assume_yes=True)

        # Both prompts (build + activate) should have been called with assume_yes=True.
        assert mocked["_confirm"].call_count == 2
        for call in mocked["_confirm"].call_args_list:
            assert call.kwargs.get("assume_yes") is True


class TestAbiGatekeeper:
    """Pre-flight gate stops shadowing installs before download.

    The gate runs after dependency resolution (filename list known) but
    before `download_package`. A block must short-circuit out without
    pulling bandwidth, touching state, or building anything; an override
    flag must let the install proceed with a loud warning.
    """

    def _seed_okular_with_glib_drift(self, config: AppConfig, mocked: dict[str, MagicMock]) -> None:
        """Configure mocks for the SEV1 scenario:

        host has glib2 2.78, but the resolved tree wants 2.80 — the gate
        must block this.
        """
        mocked["resolve_required_packages"].return_value = _resolved(
            "okular-24.12.1-1-x86_64.pkg.tar.zst",
            "glib2-2.80.2-1-x86_64.pkg.tar.zst",
        )
        mocked["get_package_dependencies"].return_value = []
        # Both names exist on host (so they're in host_provided via -T),
        # but glib2's version doesn't match the resolved tree.
        mocked["find_unsatisfied"].return_value = set()
        mocked["query_system_packages"].return_value = {
            "glib2": "2.78.6-1",
            "okular": "23.08.0-1",
        }

    def test_block_aborts_before_download(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        self._seed_okular_with_glib_drift(config, mocked)

        with pytest.raises(typer.Exit) as exc_info:
            install.run("okular", config)

        assert exc_info.value.exit_code == 1
        out = capsys.readouterr().out
        assert "HOST ABI MISMATCH" in out
        assert "glib2" in out
        assert "host has 2.78.6-1" in out
        assert "sysext would ship 2.80.2-1" in out
        # Critical contract: we must NOT pull bandwidth on a rejected install.
        mocked["download_package"].assert_not_called()
        mocked["build_sysext"].assert_not_called()
        # And state must be untouched.
        assert not config.state_db.exists()

    def test_block_message_lists_all_drifting_packages(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = _resolved(
            "okular-24.12-1-x86_64.pkg.tar.zst",
            "glib2-2.80-1-x86_64.pkg.tar.zst",
            "systemd-libs-257-1-x86_64.pkg.tar.zst",
            "pam-1.6.2-1-x86_64.pkg.tar.zst",
        )
        mocked["get_package_dependencies"].return_value = []
        mocked["query_system_packages"].return_value = {
            "glib2": "2.78-1",
            "systemd-libs": "256-1",
            "pam": "1.6.1-1",
        }

        with pytest.raises(typer.Exit):
            install.run("okular", config)

        out = capsys.readouterr().out
        assert "glib2" in out
        assert "systemd-libs" in out
        assert "pam" in out

    def test_override_flag_proceeds_with_loud_warning(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config = _config(tmp_path)
        _set_defaults(mocked)
        self._seed_okular_with_glib_drift(config, mocked)
        _seed_cache(
            config.pacman.cachedir,
            ["okular-24.12.1-1-x86_64.pkg.tar.zst", "glib2-2.80.2-1-x86_64.pkg.tar.zst"],
        )

        install.run("okular", config, allow_host_abi_mismatch=True)

        out = capsys.readouterr().out
        assert "HOST ABI MISMATCH OVERRIDDEN" in out
        assert "You own the risk" in out
        # Build proceeded — both target and the shadowed glib2 became sysexts.
        mocked["download_package"].assert_called_once()
        built_filenames = {c.args[0].name for c in mocked["build_sysext"].call_args_list}
        assert built_filenames == {
            "okular-24.12.1-1-x86_64.pkg.tar.zst",
            "glib2-2.80.2-1-x86_64.pkg.tar.zst",
        }
        # State landed.
        result = state.load(config.state_db)
        assert "okular" in result.user_requests
        assert "glib2-2.80.2-1" in result.sysexts

    def test_safe_shadow_proceeds_with_warning_no_override_needed(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Cosmetic drift (fonts/icons) takes the shadow path, not block."""
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = _resolved(
            "okular-24.12-1-x86_64.pkg.tar.zst",
            "ttf-dejavu-2.38-1-x86_64.pkg.tar.zst",
        )
        mocked["get_package_dependencies"].return_value = []
        mocked["query_system_packages"].return_value = {"ttf-dejavu": "2.37-1"}
        _seed_cache(
            config.pacman.cachedir,
            ["okular-24.12-1-x86_64.pkg.tar.zst", "ttf-dejavu-2.38-1-x86_64.pkg.tar.zst"],
        )

        install.run("okular", config)

        out = capsys.readouterr().out
        assert "safe-shadow exemption" in out
        assert "ttf-dejavu" in out
        # No HOST ABI MISMATCH error — that's reserved for the block path.
        assert "HOST ABI MISMATCH" not in out
        # Shadowed package still gets built into the sysext.
        built_filenames = {c.args[0].name for c in mocked["build_sysext"].call_args_list}
        assert "ttf-dejavu-2.38-1-x86_64.pkg.tar.zst" in built_filenames

    def test_exact_version_match_skips_dep_without_warning(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Version-aware skip: same version on host means the gate filters
        the dep out cleanly — no warning, no block, no build for it.
        """
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
            "libcap-2.78-1-x86_64.pkg.tar.zst",
        )
        mocked["get_package_dependencies"].return_value = []
        mocked["query_system_packages"].return_value = {"libcap": "2.78-1"}
        # libcap is on host; pacman -T agrees. Only target is unsatisfied.
        mocked["find_unsatisfied"].return_value = {"htop"}
        _seed_cache(
            config.pacman.cachedir,
            ["htop-3.5.1-1-x86_64.pkg.tar.zst", "libcap-2.78-1-x86_64.pkg.tar.zst"],
        )

        install.run("htop", config)

        out = capsys.readouterr().out
        assert "HOST ABI MISMATCH" not in out
        assert "safe-shadow" not in out
        # libcap skipped — only target built.
        result = state.load(config.state_db)
        assert set(result.sysexts.keys()) == {"htop-3.5.1-1"}

    def test_target_pkg_bypasses_gate_even_if_host_has_same_name(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
    ) -> None:
        """User-requested target always becomes a sysext — that's the point."""
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
        )
        mocked["get_package_dependencies"].return_value = []
        # Host has an older htop. User wants the newer one as a sysext.
        mocked["query_system_packages"].return_value = {"htop": "3.4.0-1"}
        _seed_cache(config.pacman.cachedir, ["htop-3.5.1-1-x86_64.pkg.tar.zst"])

        install.run("htop", config)

        # Target was built despite the version mismatch on the host.
        built_filenames = {c.args[0].name for c in mocked["build_sysext"].call_args_list}
        assert built_filenames == {"htop-3.5.1-1-x86_64.pkg.tar.zst"}

    def test_gate_runs_before_conflict_check_does_not_short_circuit(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A user-request conflict still wins over the gate when both fire.

        Conflict check runs before the gate in the current pipeline order;
        we verify that a transaction triggering BOTH a conflict and an
        ABI mismatch surfaces the conflict (the earlier check) so users
        see one root cause at a time, not two stacked errors.
        """
        config = _config(tmp_path)
        _set_defaults(mocked)

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

        mocked["resolve_required_packages"].return_value = _resolved(
            "nodejs-22-1-x86_64.pkg.tar.zst",
            "glib2-2.80-1-x86_64.pkg.tar.zst",
        )
        mocked["get_package_dependencies"].return_value = [
            VersionConstraint("libuv", ">=", "2.0"),
        ]
        mocked["query_system_packages"].return_value = {"glib2": "2.78-1"}

        with pytest.raises(typer.Exit):
            install.run("nodejs", config)

        out = capsys.readouterr().out
        assert "cannot install nodejs" in out
        # Gate never fired because conflict aborted first.
        assert "HOST ABI MISMATCH" not in out
        mocked["download_package"].assert_not_called()


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


class TestTimeSyncInstall:
    """Time-sync wiring: sandbox preparation + strict policy gate."""

    def _seed_sandbox(self, sandbox_cache: Path, filenames: Iterable[str]) -> None:
        sandbox_cache.mkdir(parents=True, exist_ok=True)
        for f in filenames:
            (sandbox_cache / f).write_bytes(b"fake package")

    def test_disabled_keeps_phase_one_flow(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        """Regression guard: time_sync.enabled = false must not change behaviour."""
        config = _config(tmp_path)
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = _resolved(
            "htop-3.5.1-1-x86_64.pkg.tar.zst",
        )
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop"}
        _seed_cache(config.pacman.cachedir, ["htop-3.5.1-1-x86_64.pkg.tar.zst"])

        with patch("pacman_sysext.commands.install.time_sync.prepare_sandbox") as prep:
            install.run("htop", config)
        # Sandbox prep must NOT be called when time_sync is disabled.
        prep.assert_not_called()
        # Plain sync_databases path stays in use.
        mocked["sync_databases"].assert_called_once()

    def test_strict_block_on_unmapped_repo_skips_download(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config = _config(
            tmp_path,
            TimeSyncConfig(
                enabled=True,
                snapshot_servers={"core": "https://archive.example/{date}/{repo}/{arch}"},
            ),
        )
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = [
            *_resolved("htop-3.5.1-1-x86_64.pkg.tar.zst", repo="extra"),
        ]
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop"}

        sandbox_pacman = PacmanConfig(
            dbpath=tmp_path / "sandbox" / "db",
            cachedir=tmp_path / "sandbox" / "cache",
            config_file=tmp_path / "sandbox" / "pacman.conf",
            gpgdir=tmp_path / "gnupg",
        )
        from pacman_sysext.time_sync import PreparedSandbox

        prepared = PreparedSandbox(pacman=sandbox_pacman, effective_date=date(2025, 5, 1))
        with (
            patch(
                "pacman_sysext.commands.install.time_sync.prepare_sandbox",
                return_value=prepared,
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            install.run("htop", config)

        assert exc_info.value.exit_code == 1
        out = capsys.readouterr().out
        assert "SNAPSHOT POLICY BLOCK" in out
        assert "htop" in out
        assert "'extra'" in out
        # Bandwidth gate: we must not pull anything for a blocked install.
        mocked["download_package"].assert_not_called()
        mocked["build_sysext"].assert_not_called()
        # State must be untouched.
        assert not config.state_db.exists()

    def test_all_mapped_proceeds_through_sandbox(
        self, tmp_path: Path, mocked: dict[str, MagicMock]
    ) -> None:
        config = _config(
            tmp_path,
            TimeSyncConfig(
                enabled=True,
                snapshot_servers={
                    "core": "https://archive.example/{date}/{repo}/{arch}",
                    "extra": "https://archive.example/{date}/{repo}/{arch}",
                },
            ),
        )
        _set_defaults(mocked)
        mocked["resolve_required_packages"].return_value = [
            *_resolved("glibc-2.39-1-x86_64.pkg.tar.zst", repo="core"),
            *_resolved("htop-3.5.1-1-x86_64.pkg.tar.zst", repo="extra"),
        ]
        mocked["get_package_dependencies"].return_value = []
        mocked["find_unsatisfied"].return_value = {"htop", "glibc"}

        sandbox_pacman = PacmanConfig(
            dbpath=tmp_path / "sandbox" / "db",
            cachedir=tmp_path / "sandbox" / "cache",
            config_file=tmp_path / "sandbox" / "pacman.conf",
            gpgdir=tmp_path / "gnupg",
        )
        from pacman_sysext.time_sync import PreparedSandbox

        prepared = PreparedSandbox(pacman=sandbox_pacman, effective_date=date(2025, 5, 1))
        self._seed_sandbox(
            sandbox_pacman.cachedir,
            ["glibc-2.39-1-x86_64.pkg.tar.zst", "htop-3.5.1-1-x86_64.pkg.tar.zst"],
        )

        with patch(
            "pacman_sysext.commands.install.time_sync.prepare_sandbox",
            return_value=prepared,
        ) as prep:
            install.run("htop", config)

        prep.assert_called_once_with(config.time_sync, config.pacman)
        # download_package was called against the sandbox config, not the
        # base /var/lib/pacman-sysext cachedir.
        assert mocked["download_package"].call_args[0][1] is sandbox_pacman
        # State landed and every record carries the pinned date.
        result = state.load(config.state_db)
        assert "htop" in result.user_requests
        assert {"glibc-2.39-1", "htop-3.5.1-1"} == set(result.sysexts.keys())
        for record in result.sysexts.values():
            assert record.pinned_date == date(2025, 5, 1)

    def test_prepare_sandbox_failure_surfaces_cleanly(
        self,
        tmp_path: Path,
        mocked: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from pacman_sysext.time_sync import TimeSyncError

        config = _config(
            tmp_path,
            TimeSyncConfig(enabled=True, snapshot_servers={"core": "https://x/{date}"}),
        )
        _set_defaults(mocked)

        with (
            patch(
                "pacman_sysext.commands.install.time_sync.prepare_sandbox",
                side_effect=TimeSyncError("no snapshot reachable"),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            install.run("htop", config)

        assert exc_info.value.exit_code == 1
        out = capsys.readouterr().out
        assert "no snapshot reachable" in out
        mocked["download_package"].assert_not_called()
        assert not config.state_db.exists()
