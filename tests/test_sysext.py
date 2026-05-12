import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pacman_sysext.sysext import (
    SysextError,
    activate_all,
    activate_sysext,
    apply_sysusers,
    apply_tmpfiles,
    daemon_reload,
    deactivate_sysext,
)


def _completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = ""
    proc.returncode = returncode
    return proc


class TestActivateSysext:
    def test_creates_symlink(self, tmp_path: Path) -> None:
        raw = tmp_path / "src" / "htop-3.5.1-1.raw"
        raw.parent.mkdir()
        raw.write_text("dummy image")
        ext_dir = tmp_path / "extensions"

        link = activate_sysext(raw, ext_dir)

        assert link.is_symlink()
        assert link.resolve() == raw.resolve()

    def test_replaces_existing(self, tmp_path: Path) -> None:
        raw = tmp_path / "htop-3.5.1-1.raw"
        raw.write_text("v1")
        ext_dir = tmp_path / "extensions"

        activate_sysext(raw, ext_dir)
        link = activate_sysext(raw, ext_dir)

        assert link.is_symlink()

    def test_replaces_broken_symlink(self, tmp_path: Path) -> None:
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        broken = ext_dir / "htop-3.5.1-1.raw"
        broken.symlink_to(tmp_path / "nonexistent.raw")

        raw = tmp_path / "htop-3.5.1-1.raw"
        raw.write_text("ok")

        link = activate_sysext(raw, ext_dir)
        assert link.resolve() == raw.resolve()

    def test_missing_source(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            activate_sysext(tmp_path / "nope.raw", tmp_path / "ext")


class TestDeactivateSysext:
    def test_removes_existing(self, tmp_path: Path) -> None:
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        link = ext_dir / "htop-3.5.1-1.raw"
        link.symlink_to(tmp_path / "anything")

        assert deactivate_sysext("htop-3.5.1-1.raw", ext_dir) is True
        assert not link.is_symlink()

    def test_missing_returns_false(self, tmp_path: Path) -> None:
        assert deactivate_sysext("nope.raw", tmp_path) is False


class TestDaemonReload:
    def test_invokes_systemctl_daemon_reload(self) -> None:
        with patch("pacman_sysext.sysext.subprocess.run", return_value=_completed()) as run_mock:
            daemon_reload()
        run_mock.assert_called_once()
        assert run_mock.call_args.args[0] == ["systemctl", "daemon-reload"]

    def test_failure_becomes_sysext_error(self) -> None:
        err = subprocess.CalledProcessError(returncode=1, cmd=["systemctl"], stderr="bus error")
        with (
            patch("pacman_sysext.sysext.subprocess.run", side_effect=err),
            pytest.raises(SysextError, match="systemctl daemon-reload"),
        ):
            daemon_reload()


class TestApplySysusers:
    def test_invokes_systemd_sysusers(self) -> None:
        with patch("pacman_sysext.sysext.subprocess.run", return_value=_completed()) as run_mock:
            apply_sysusers()
        run_mock.assert_called_once()
        assert run_mock.call_args.args[0] == ["systemd-sysusers"]

    def test_failure_becomes_sysext_error(self) -> None:
        err = subprocess.CalledProcessError(returncode=1, cmd=["systemd-sysusers"], stderr="nope")
        with (
            patch("pacman_sysext.sysext.subprocess.run", side_effect=err),
            pytest.raises(SysextError, match="systemd-sysusers"),
        ):
            apply_sysusers()


class TestApplyTmpfiles:
    def test_invokes_systemd_tmpfiles_globally(self) -> None:
        # No positional args, no --prefix: the call must process every
        # /usr/lib/tmpfiles.d/*.conf — including package-native files like
        # /usr/lib/tmpfiles.d/mariadb.conf that materialise /run/mariadb.
        # Scoping would silently break daemon packages on first start.
        with patch("pacman_sysext.sysext.subprocess.run", return_value=_completed()) as run_mock:
            apply_tmpfiles()
        run_mock.assert_called_once()
        assert run_mock.call_args.args[0] == ["systemd-tmpfiles", "--create"]

    def test_failure_becomes_sysext_error(self) -> None:
        err = subprocess.CalledProcessError(returncode=1, cmd=["systemd-tmpfiles"], stderr="nope")
        with (
            patch("pacman_sysext.sysext.subprocess.run", side_effect=err),
            pytest.raises(SysextError, match="systemd-tmpfiles"),
        ):
            apply_tmpfiles()


class TestActivateAll:
    def test_post_refresh_sequence(self, tmp_path: Path) -> None:
        raw = tmp_path / "htop-3.5.1-1.raw"
        raw.write_text("img")
        ext_dir = tmp_path / "extensions"

        parent = MagicMock()
        with (
            patch("pacman_sysext.sysext.is_refresh_supported", return_value=True),
            patch("pacman_sysext.sysext.refresh") as refresh_mock,
            patch("pacman_sysext.sysext.daemon_reload") as daemon_reload_mock,
            patch("pacman_sysext.sysext.apply_sysusers") as sysusers_mock,
            patch("pacman_sysext.sysext.apply_tmpfiles") as tmpfiles_mock,
        ):
            parent.attach_mock(refresh_mock, "refresh")
            parent.attach_mock(daemon_reload_mock, "daemon_reload")
            parent.attach_mock(sysusers_mock, "apply_sysusers")
            parent.attach_mock(tmpfiles_mock, "apply_tmpfiles")
            activate_all([raw], ext_dir)

        # Strict ordering, each step paid for in a production incident:
        #   refresh        — swap /usr overlay
        #   daemon-reload  — PID 1 sees new units
        #   sysusers       — accounts exist before being chown'd to
        #   tmpfiles       — /etc, /var, /run materialise last
        # Any swap silently breaks at least one real daemon package.
        assert [c[0] for c in parent.mock_calls] == [
            "refresh",
            "daemon_reload",
            "apply_sysusers",
            "apply_tmpfiles",
        ]

    def test_post_merge_fallback_sequence(self, tmp_path: Path) -> None:
        raw = tmp_path / "htop-3.5.1-1.raw"
        raw.write_text("img")
        ext_dir = tmp_path / "extensions"

        parent = MagicMock()
        with (
            patch("pacman_sysext.sysext.is_refresh_supported", return_value=False),
            patch("pacman_sysext.sysext.unmerge") as unmerge_mock,
            patch("pacman_sysext.sysext.merge") as merge_mock,
            patch("pacman_sysext.sysext.daemon_reload") as daemon_reload_mock,
            patch("pacman_sysext.sysext.apply_sysusers") as sysusers_mock,
            patch("pacman_sysext.sysext.apply_tmpfiles") as tmpfiles_mock,
        ):
            parent.attach_mock(unmerge_mock, "unmerge")
            parent.attach_mock(merge_mock, "merge")
            parent.attach_mock(daemon_reload_mock, "daemon_reload")
            parent.attach_mock(sysusers_mock, "apply_sysusers")
            parent.attach_mock(tmpfiles_mock, "apply_tmpfiles")
            activate_all([raw], ext_dir)

        assert [c[0] for c in parent.mock_calls] == [
            "unmerge",
            "merge",
            "daemon_reload",
            "apply_sysusers",
            "apply_tmpfiles",
        ]
