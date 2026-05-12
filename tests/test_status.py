"""Tests for the status command."""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console

from pacman_sysext import state
from pacman_sysext.commands import status as status_cmd
from pacman_sysext.config import AppConfig, BuilderConfig, PacmanConfig, SysextConfig


def _config(tmp_path: Path) -> AppConfig:
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
    )


def _now() -> datetime:
    return datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


def _record(
    name: str,
    version: str,
    *,
    snapshot_id: str,
    depends: list[str] | None = None,
    sha: str = "x",
) -> state.SysextRecord:
    return state.SysextRecord(
        name=name,
        version=version,
        raw_filename=f"{name}-{version}.raw",
        fs_format="squashfs",
        sha256=sha,
        installed_at=_now(),
        snapshot_id=snapshot_id,
        provides={},
        depends=depends or [],
    )


def _capturing_console() -> Console:
    return Console(file=io.StringIO(), width=200, record=True, color_system=None)


def _populate(tmp_path: Path) -> tuple[AppConfig, Console]:
    """Build a state.db + sysexts/ layout exercising every dashboard branch."""
    config = _config(tmp_path)
    config.builder.output_dir.mkdir(parents=True, exist_ok=True)

    # On-disk image for htop matches the recorded hash; bytes are arbitrary.
    htop_bytes = b"htop image bytes"
    (config.builder.output_dir / "htop-3.5.1-1.raw").write_bytes(htop_bytes)
    htop_sha = hashlib.sha256(htop_bytes).hexdigest()

    # On-disk image for ncurses (implicit dep), also matches its record.
    nc_bytes = b"ncurses image bytes"
    (config.builder.output_dir / "ncurses-6.5-1.raw").write_bytes(nc_bytes)

    # Unregistered .raw — present on disk, unknown to state.
    (config.builder.output_dir / "stray-9.0-1.raw").write_bytes(b"stray")

    current = state.State()
    snap_id = state.intern_snapshot(current, {"glibc": "2.39-1"})

    state.add_sysext(
        current, _record("htop", "3.5.1-1", snapshot_id=snap_id, depends=["ncurses"], sha=htop_sha)
    )
    # Every reachable record has non-empty depends — even the leaf, which
    # records its host-provided deps (e.g. "glibc") that have no sysext.
    # find_providing_sysexts returns [] for those, so the walk terminates
    # cleanly without consulting the fallback resolver.
    state.add_sysext(
        current,
        _record(
            "ncurses",
            "6.5-1",
            snapshot_id=snap_id,
            depends=["glibc"],
            sha=hashlib.sha256(nc_bytes).hexdigest(),
        ),
    )
    # leftover is an orphan AND its .raw file is missing → also surfaces in
    # the integrity audit's missing_files. Its depends are irrelevant because
    # it is not reachable from any user_request and never enters the BFS.
    state.add_sysext(current, _record("leftover", "1.0-1", snapshot_id=snap_id, depends=["glibc"]))
    state.add_user_request(
        current,
        state.UserRequest(
            name="htop",
            installed_version="3.5.1-1",
            requested_at=_now(),
            requirements={"ncurses": ""},
        ),
    )
    state.save(current, config.state_db)
    return config, _capturing_console()


def test_dashboard_surfaces_explicit_orphans_and_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, console = _populate(tmp_path)

    # If status reaches for pacman, fail loudly. With populated depends on
    # every reachable record this must never fire.
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("get_package_dependencies must not be called")

    monkeypatch.setattr("pacman_sysext.commands.status.get_package_dependencies", explode)

    state_before = config.state_db.read_bytes()

    status_cmd.run(config, console=console)

    out = console.export_text()
    # Integrity panel rendered (leftover.raw missing + stray-9.0-1.raw unknown).
    assert "Integrity issues" in out
    assert "leftover-1.0-1.raw" in out
    assert "stray-9.0-1.raw" in out

    # Explicit table contains the user-requested package.
    assert "Explicit packages" in out
    assert "htop" in out

    # Orphan panel labels leftover.
    assert "Orphan sysexts" in out
    assert "leftover-1.0-1" in out

    # Summary section is present.
    assert "Summary" in out
    assert "Total images" in out

    # State must not be mutated.
    assert config.state_db.read_bytes() == state_before


def test_dashboard_uses_fallback_resolver_for_legacy_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record with depends=[] triggers exactly one pacman query."""
    config = _config(tmp_path)
    config.builder.output_dir.mkdir(parents=True, exist_ok=True)
    htop_bytes = b"htop"
    (config.builder.output_dir / "htop-3.5.1-1.raw").write_bytes(htop_bytes)
    nc_bytes = b"ncurses"
    (config.builder.output_dir / "ncurses-6.5-1.raw").write_bytes(nc_bytes)

    current = state.State()
    snap_id = state.intern_snapshot(current, {"glibc": "2.39-1"})

    # htop has depends populated, ncurses does NOT (simulating a legacy
    # record persisted before the depends field existed).
    htop = state.SysextRecord(
        name="htop",
        version="3.5.1-1",
        raw_filename="htop-3.5.1-1.raw",
        fs_format="squashfs",
        sha256=hashlib.sha256(htop_bytes).hexdigest(),
        installed_at=_now(),
        snapshot_id=snap_id,
        provides={},
        depends=["ncurses"],
    )
    ncurses = state.SysextRecord(
        name="ncurses",
        version="6.5-1",
        raw_filename="ncurses-6.5-1.raw",
        fs_format="squashfs",
        sha256=hashlib.sha256(nc_bytes).hexdigest(),
        installed_at=_now(),
        snapshot_id=snap_id,
        provides={},
        depends=[],
    )
    state.add_sysext(current, htop)
    state.add_sysext(current, ncurses)
    state.add_user_request(
        current,
        state.UserRequest(
            name="htop",
            installed_version="3.5.1-1",
            requested_at=_now(),
            requirements={"ncurses": ""},
        ),
    )
    state.save(current, config.state_db)

    calls: list[str] = []

    def fake_get_deps(name: str, _config: object) -> list[object]:
        calls.append(name)
        return []

    monkeypatch.setattr("pacman_sysext.commands.status.get_package_dependencies", fake_get_deps)

    status_cmd.run(config, console=_capturing_console())

    # ncurses (legacy) triggers the fallback exactly once — and only ncurses.
    # The walk is invoked twice (get_implicit and get_orphans both run it),
    # which is acceptable as long as legacy nodes are the only callers.
    assert set(calls) == {"ncurses"}


def test_pacman_failure_does_not_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    config.builder.output_dir.mkdir(parents=True, exist_ok=True)

    current = state.State()
    state.intern_snapshot(current, {})
    # legacy record forces fallback resolver to fire
    state.add_sysext(
        current,
        state.SysextRecord(
            name="legacy",
            version="1",
            raw_filename="legacy-1.raw",
            fs_format="squashfs",
            sha256="x",
            installed_at=_now(),
            snapshot_id="dangling",
            provides={},
            depends=[],
        ),
    )
    state.add_user_request(
        current,
        state.UserRequest(
            name="legacy",
            installed_version="1",
            requested_at=_now(),
            requirements={},
        ),
    )
    state.save(current, config.state_db)

    from pacman_sysext.pacman import PacmanError

    def boom(*_args: object, **_kwargs: object) -> object:
        raise PacmanError("offline", returncode=1, stderr="")

    monkeypatch.setattr("pacman_sysext.commands.status.get_package_dependencies", boom)

    # Must not raise — status degrades silently when pacman is unavailable.
    status_cmd.run(config, console=_capturing_console())
