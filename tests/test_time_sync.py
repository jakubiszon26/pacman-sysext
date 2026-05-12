"""Pure-function coverage for the time-sync module.

Real HTTP and the host filesystem are never touched; backends use the
injectable `probe` callable, sync DBs are built on the fly with the
stdlib `tarfile` module, and `prepare_sandbox` runs against `tmp_path`.
"""

from __future__ import annotations

import io
import tarfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from pacman_sysext.config import PacmanConfig
from pacman_sysext.time_sync import (
    TimeSyncConfig,
    TimeSyncError,
    default_ala_servers,
    derive_snapshot_date,
    expand_template,
    find_effective_date,
    prepare_sandbox,
    render_pinned_pacman_conf,
)


def _make_desc(name: str, version: str, build_epoch: int) -> bytes:
    return (
        f"%FILENAME%\n{name}-{version}-x86_64.pkg.tar.zst\n\n"
        f"%NAME%\n{name}\n\n"
        f"%VERSION%\n{version}\n\n"
        f"%BUILDDATE%\n{build_epoch}\n\n"
    ).encode()


def _make_sync_db(path: Path, entries: list[tuple[str, str, int]]) -> None:
    """Build a synthetic pacman sync DB (gzipped tar of `<pkg>-<ver>/desc` blobs)."""
    with tarfile.open(path, "w:gz") as tar:
        for name, version, epoch in entries:
            payload = _make_desc(name, version, epoch)
            info = tarfile.TarInfo(name=f"{name}-{version}/desc")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


class TestTimeSyncConfig:
    def test_defaults_are_disabled(self) -> None:
        cfg = TimeSyncConfig()
        assert cfg.enabled is False
        assert cfg.date is None
        assert cfg.policy == "strict"
        assert cfg.snapshot_servers == {}

    def test_rejects_non_strict_policy(self) -> None:
        with pytest.raises(TimeSyncError, match="strict"):
            TimeSyncConfig(policy="hybrid")  # type: ignore[arg-type]

    def test_rejects_bool_int_conflation(self) -> None:
        with pytest.raises(TimeSyncError, match="bool"):
            TimeSyncConfig(enabled=1)  # type: ignore[arg-type]


class TestDefaultAlaServers:
    def test_emits_three_arch_repos(self) -> None:
        servers = default_ala_servers()
        assert set(servers) == {"core", "extra", "multilib"}
        # Every entry uses ALA host with the documented placeholders.
        for url in servers.values():
            assert "archive.archlinux.org" in url
            assert "{date}" in url
            assert "{repo}" in url
            assert "{arch}" in url


class TestExpandTemplate:
    def test_ala_template(self) -> None:
        url = expand_template(
            "https://archive.archlinux.org/repos/{date}/{repo}/os/{arch}",
            repo="core",
            arch="x86_64",
            snapshot_date=date(2025, 5, 1),
        )
        assert url == "https://archive.archlinux.org/repos/2025/05/01/core/os/x86_64"

    def test_unknown_placeholder_raises(self) -> None:
        with pytest.raises(TimeSyncError, match="unknown placeholder"):
            expand_template(
                "https://example/{date}/{repo}/{nope}",
                repo="core",
                arch="x86_64",
                snapshot_date=date(2025, 5, 1),
            )


class TestDeriveSnapshotDate:
    def test_max_builddate_wins(self, tmp_path: Path) -> None:
        sync_dir = tmp_path / "sync"
        sync_dir.mkdir()
        older = int(datetime(2025, 4, 1, 0, 0, tzinfo=UTC).timestamp())
        newer = int(datetime(2025, 5, 1, 12, 0, tzinfo=UTC).timestamp())
        _make_sync_db(
            sync_dir / "core.db",
            [("glibc", "2.39-1", older), ("htop", "3.5.1-1", newer)],
        )
        assert derive_snapshot_date(sync_dir) == date(2025, 5, 1)

    def test_missing_core_db_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TimeSyncError, match="host sync DB missing"):
            derive_snapshot_date(tmp_path)

    def test_empty_db_raises(self, tmp_path: Path) -> None:
        sync_dir = tmp_path / "sync"
        sync_dir.mkdir()
        _make_sync_db(sync_dir / "core.db", [])
        with pytest.raises(TimeSyncError, match="no BUILDDATE entries"):
            derive_snapshot_date(sync_dir)


class TestFindEffectiveDate:
    def test_first_day_succeeds(self) -> None:
        servers = {"core": "https://example/{date}/{repo}/{arch}"}
        probed: list[str] = []

        def probe(url: str) -> bool:
            probed.append(url)
            return True

        result = find_effective_date(date(2025, 5, 1), servers, "x86_64", probe=probe)
        assert result == date(2025, 5, 1)
        assert probed == ["https://example/2025/05/01/core/x86_64/core.db"]

    def test_forward_search_skips_gap_day(self) -> None:
        servers = {"core": "https://example/{date}/{repo}/{arch}"}
        good_date = date(2025, 5, 3)

        def probe(url: str) -> bool:
            return f"/{good_date.strftime('%Y/%m/%d')}/" in url

        result = find_effective_date(
            date(2025, 5, 1), servers, "x86_64", probe=probe
        )
        assert result == good_date

    def test_exhausted_search_raises(self) -> None:
        servers = {"core": "https://example/{date}/{repo}/{arch}"}
        result_probes: list[str] = []

        def probe(url: str) -> bool:
            result_probes.append(url)
            return False

        with pytest.raises(TimeSyncError, match="no snapshot reachable"):
            find_effective_date(
                date(2025, 5, 1), servers, "x86_64", probe=probe, max_days=7
            )
        # Each candidate day probes the first repo and short-circuits on
        # failure, so we see one probe per day in the bounded window.
        assert len(result_probes) == 8

    def test_all_repos_probed_per_day(self) -> None:
        servers = {
            "core": "https://example/{date}/{repo}/{arch}",
            "extra": "https://example/{date}/{repo}/{arch}",
        }
        probed: list[str] = []

        def probe(url: str) -> bool:
            probed.append(url)
            return True

        find_effective_date(date(2025, 5, 1), servers, "x86_64", probe=probe)
        # Insertion order preserved; both repos probed on the single OK day.
        assert probed == [
            "https://example/2025/05/01/core/x86_64/core.db",
            "https://example/2025/05/01/extra/x86_64/extra.db",
        ]

    def test_empty_servers_passthrough(self) -> None:
        # With no mapped repos the resolver still works (strict gate will
        # surface the issue downstream); no probes happen.
        called = False

        def probe(url: str) -> bool:
            nonlocal called
            called = True
            return True

        result = find_effective_date(date(2025, 5, 1), {}, "x86_64", probe=probe)
        assert result == date(2025, 5, 1)
        assert called is False


class TestRenderPinnedPacmanConf:
    def test_arch_only_replaces_include_lines(self) -> None:
        conf = (
            "[options]\n"
            "Architecture = auto\n"
            "\n"
            "[core]\n"
            "Include = /etc/pacman.d/mirrorlist\n"
            "\n"
            "[extra]\n"
            "Include = /etc/pacman.d/mirrorlist\n"
            "\n"
            "[multilib]\n"
            "Include = /etc/pacman.d/mirrorlist\n"
        )
        out = render_pinned_pacman_conf(
            conf,
            date(2025, 5, 1),
            default_ala_servers(),
            arch="x86_64",
        )
        # Every mapped repo gets exactly one Server line, no leftover Include.
        for repo in ("core", "extra", "multilib"):
            expanded = (
                f"Server = https://archive.archlinux.org/repos/2025/05/01/{repo}/os/x86_64"
            )
            assert expanded in out
        assert "Include = /etc/pacman.d/mirrorlist" not in out
        # Options section is preserved unchanged.
        assert "Architecture = auto" in out

    def test_unmapped_repo_passes_through(self) -> None:
        conf = (
            "[core]\n"
            "Include = /etc/pacman.d/mirrorlist\n"
            "\n"
            "[cachyos-v4]\n"
            "Include = /etc/pacman.d/cachyos-v4-mirrorlist\n"
            "SigLevel = Required\n"
        )
        out = render_pinned_pacman_conf(
            conf,
            date(2025, 5, 1),
            {"core": "https://archive.archlinux.org/repos/{date}/{repo}/os/{arch}"},
            arch="x86_64",
        )
        # Core was rewritten.
        assert "Server = https://archive.archlinux.org/repos/2025/05/01/core/os/x86_64" in out
        # CachyOS passes through untouched — the strict policy gate will
        # block downstream against ResolvedDep.repo.
        assert "[cachyos-v4]" in out
        assert "Include = /etc/pacman.d/cachyos-v4-mirrorlist" in out
        assert "SigLevel = Required" in out

    def test_multiple_server_lines_collapse_to_one(self) -> None:
        conf = (
            "[core]\n"
            "Server = https://mirror.example/core/os/x86_64\n"
            "Server = https://mirror2.example/core/os/x86_64\n"
            "Include = /etc/pacman.d/mirrorlist\n"
        )
        out = render_pinned_pacman_conf(
            conf,
            date(2025, 5, 1),
            default_ala_servers(),
            arch="x86_64",
        )
        assert out.count("Server = ") == 1
        assert "mirror.example" not in out
        assert "mirror2.example" not in out

    def test_preserves_comments_and_other_directives(self) -> None:
        conf = (
            "[core]\n"
            "# upstream-recommended ordering\n"
            "SigLevel = Required DatabaseOptional\n"
            "Usage = Sync Search\n"
            "Include = /etc/pacman.d/mirrorlist\n"
        )
        out = render_pinned_pacman_conf(
            conf,
            date(2025, 5, 1),
            default_ala_servers(),
            arch="x86_64",
        )
        assert "# upstream-recommended ordering" in out
        assert "SigLevel = Required DatabaseOptional" in out
        assert "Usage = Sync Search" in out

    def test_strips_weakening_siglevel_from_options(self) -> None:
        conf = (
            "[options]\n"
            "SigLevel = Never\n"
            "Architecture = auto\n"
            "\n"
            "[core]\n"
            "Include = /etc/pacman.d/mirrorlist\n"
        )
        out = render_pinned_pacman_conf(
            conf,
            date(2025, 5, 1),
            default_ala_servers(),
            arch="x86_64",
        )
        assert "SigLevel = Never" not in out
        assert "Architecture = auto" in out

    @pytest.mark.parametrize(
        "weakening_line",
        [
            "SigLevel = Never",
            "SigLevel = PackageNever",
            "SigLevel = Optional TrustAll",
            "SigLevel = PackageOptional DatabaseRequired",
            "SigLevel = Required TrustAll",
        ],
    )
    def test_strips_weakening_siglevel_in_mapped_repo_section(
        self, weakening_line: str
    ) -> None:
        """Any weakening SigLevel inside a mapped [repo] is also dropped.

        The pinned config routes mapped repos through the snapshot
        backend; we refuse to inherit a relaxed trust policy alongside
        the pin.
        """
        conf = (
            "[core]\n"
            f"{weakening_line}\n"
            "Include = /etc/pacman.d/mirrorlist\n"
        )
        out = render_pinned_pacman_conf(
            conf,
            date(2025, 5, 1),
            default_ala_servers(),
            arch="x86_64",
        )
        assert weakening_line not in out
        # Server replacement still landed.
        assert "Server = https://archive.archlinux.org/" in out

    def test_keeps_default_database_optional(self) -> None:
        """`DatabaseOptional` is pacman's compiled-in default — preserve it."""
        conf = (
            "[core]\n"
            "SigLevel = Required DatabaseOptional\n"
            "Include = /etc/pacman.d/mirrorlist\n"
        )
        out = render_pinned_pacman_conf(
            conf,
            date(2025, 5, 1),
            default_ala_servers(),
            arch="x86_64",
        )
        assert "SigLevel = Required DatabaseOptional" in out

    def test_unmapped_repo_keeps_its_weak_siglevel(self) -> None:
        """We only sanitize sections we redirect — strict policy handles the rest."""
        conf = (
            "[core]\n"
            "Include = /etc/pacman.d/mirrorlist\n"
            "\n"
            "[shady-repo]\n"
            "SigLevel = Never\n"
            "Server = https://shady.example/repo\n"
        )
        out = render_pinned_pacman_conf(
            conf,
            date(2025, 5, 1),
            {"core": "https://archive.archlinux.org/repos/{date}/{repo}/os/{arch}"},
            arch="x86_64",
        )
        # Untouched — the strict policy gate will block any dep from this
        # repo downstream; the SigLevel here never gets exercised.
        assert "SigLevel = Never" in out


class TestPrepareSandbox:
    def _base_config(self, tmp_path: Path) -> PacmanConfig:
        return PacmanConfig(
            dbpath=tmp_path / "db",
            cachedir=tmp_path / "cache",
            config_file=tmp_path / "host" / "pacman.conf",
            gpgdir=tmp_path / "gnupg",
        )

    def _populate_host(self, tmp_path: Path, builddate_epoch: int) -> tuple[Path, Path]:
        host_sync = tmp_path / "host" / "sync"
        host_sync.mkdir(parents=True)
        _make_sync_db(host_sync / "core.db", [("glibc", "2.39-1", builddate_epoch)])
        _make_sync_db(host_sync / "extra.db", [("htop", "3.5.1-1", builddate_epoch)])
        host_conf = tmp_path / "host" / "pacman.conf"
        host_conf.write_text(
            "[options]\nArchitecture = auto\n\n"
            "[core]\nInclude = /etc/pacman.d/mirrorlist\n\n"
            "[extra]\nInclude = /etc/pacman.d/mirrorlist\n"
        )
        return host_sync, host_conf

    def test_disabled_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TimeSyncError, match="enabled = false"):
            prepare_sandbox(TimeSyncConfig(enabled=False), self._base_config(tmp_path))

    def test_builds_sandbox(self, tmp_path: Path) -> None:
        epoch = int(datetime(2025, 5, 1, 12, 0, tzinfo=UTC).timestamp())
        host_sync, host_conf = self._populate_host(tmp_path, epoch)

        cfg = TimeSyncConfig(enabled=True, snapshot_servers=default_ala_servers())
        result = prepare_sandbox(
            cfg,
            self._base_config(tmp_path),
            host_sync_dir=host_sync,
            host_pacman_conf=host_conf,
            arch="x86_64",
            probe=lambda url: True,
        )

        assert result.effective_date == date(2025, 5, 1)
        pacman = result.pacman
        namespace = "ala-2025-05-01"
        assert pacman.dbpath == tmp_path / "db" / namespace
        assert pacman.cachedir == tmp_path / "cache" / namespace
        assert pacman.config_file == pacman.dbpath / ".pinned" / "pacman.conf"

        # Sync DBs copied (only *.db, not the rendered config).
        assert (pacman.dbpath / "sync" / "core.db").exists()
        assert (pacman.dbpath / "sync" / "extra.db").exists()

        rendered = pacman.config_file.read_text()
        assert (
            "Server = https://archive.archlinux.org/repos/2025/05/01/core/os/x86_64" in rendered
        )

    def test_explicit_date_overrides_derivation(self, tmp_path: Path) -> None:
        # Derivation would point at 2025-04-01; the explicit override forces 2025-06-15.
        epoch = int(datetime(2025, 4, 1, tzinfo=UTC).timestamp())
        host_sync, host_conf = self._populate_host(tmp_path, epoch)
        cfg = TimeSyncConfig(
            enabled=True,
            date=date(2025, 6, 15),
            snapshot_servers=default_ala_servers(),
        )
        result = prepare_sandbox(
            cfg,
            self._base_config(tmp_path),
            host_sync_dir=host_sync,
            host_pacman_conf=host_conf,
            arch="x86_64",
            probe=lambda url: True,
        )
        assert result.effective_date == date(2025, 6, 15)
        assert result.pacman.dbpath.name == "ala-2025-06-15"

    def test_removes_stale_sandbox_dbs_before_copy(self, tmp_path: Path) -> None:
        """A repo dropped from the host must not linger in the sandbox.

        Reusing `ala-<date>/sync/` would otherwise let the resolver see a
        repo the host no longer has — violating the 'DB is the host
        worldview' invariant.
        """
        epoch = int(datetime(2025, 5, 1, tzinfo=UTC).timestamp())
        host_sync, host_conf = self._populate_host(tmp_path, epoch)
        cfg = TimeSyncConfig(enabled=True, snapshot_servers=default_ala_servers())

        # First prepare with an extra repo present on host.
        _make_sync_db(host_sync / "removed.db", [("removed", "1", epoch)])
        result1 = prepare_sandbox(
            cfg,
            self._base_config(tmp_path),
            host_sync_dir=host_sync,
            host_pacman_conf=host_conf,
            arch="x86_64",
            probe=lambda url: True,
        )
        assert (result1.pacman.dbpath / "sync" / "removed.db").exists()

        # Host drops the repo; re-prepare with the same effective date.
        (host_sync / "removed.db").unlink()
        result2 = prepare_sandbox(
            cfg,
            self._base_config(tmp_path),
            host_sync_dir=host_sync,
            host_pacman_conf=host_conf,
            arch="x86_64",
            probe=lambda url: True,
        )
        # Same namespace dir, but the stale DB is gone.
        assert result1.pacman.dbpath == result2.pacman.dbpath
        assert not (result2.pacman.dbpath / "sync" / "removed.db").exists()

    def test_symlinked_sync_db_is_refused(self, tmp_path: Path) -> None:
        epoch = int(datetime(2025, 5, 1, tzinfo=UTC).timestamp())
        host_sync, host_conf = self._populate_host(tmp_path, epoch)
        # Replace extra.db with a symlink pointing somewhere unrelated.
        attacker_payload = tmp_path / "elsewhere.db"
        attacker_payload.write_bytes(b"not a real DB")
        (host_sync / "extra.db").unlink()
        (host_sync / "extra.db").symlink_to(attacker_payload)

        cfg = TimeSyncConfig(enabled=True, snapshot_servers=default_ala_servers())
        result = prepare_sandbox(
            cfg,
            self._base_config(tmp_path),
            host_sync_dir=host_sync,
            host_pacman_conf=host_conf,
            arch="x86_64",
            probe=lambda url: True,
        )
        # core.db copies fine; the symlinked extra.db is skipped entirely.
        assert (result.pacman.dbpath / "sync" / "core.db").exists()
        assert not (result.pacman.dbpath / "sync" / "extra.db").exists()

    def test_files_dbs_are_not_copied(self, tmp_path: Path) -> None:
        epoch = int(datetime(2025, 5, 1, tzinfo=UTC).timestamp())
        host_sync, host_conf = self._populate_host(tmp_path, epoch)
        # Drop a fake .files alongside; copy must ignore it.
        (host_sync / "core.files").write_bytes(b"junk")

        cfg = TimeSyncConfig(enabled=True, snapshot_servers=default_ala_servers())
        result = prepare_sandbox(
            cfg,
            self._base_config(tmp_path),
            host_sync_dir=host_sync,
            host_pacman_conf=host_conf,
            arch="x86_64",
            probe=lambda url: True,
        )
        assert not (result.pacman.dbpath / "sync" / "core.files").exists()
