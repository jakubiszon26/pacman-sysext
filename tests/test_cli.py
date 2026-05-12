"""Typer-level smoke tests for the CLI.

These hit the Typer wiring around `pacman-sysext install` so flag parsing
and config-override logic stay testable without driving the full install
pipeline.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from pacman_sysext.cli import app
from pacman_sysext.config import AppConfig


def _stub_config(tmp_path: Path) -> AppConfig:
    # AppConfig.default() points at /var/lib/... — fine for a non-running
    # smoke test, but we redirect everything under tmp_path so any
    # surprise filesystem touch lands somewhere harmless.
    cfg = AppConfig.default()
    return cfg


def test_time_sync_date_enables_time_sync_with_ala_fallback(tmp_path: Path) -> None:
    runner = CliRunner()
    with (
        patch("pacman_sysext.cli.AppConfig.load", return_value=_stub_config(tmp_path)),
        patch("pacman_sysext.cli.install_cmd.run") as run_mock,
    ):
        result = runner.invoke(app, ["install", "htop", "--time-sync-date", "2025-05-01"])

    assert result.exit_code == 0, result.output
    run_mock.assert_called_once()
    forwarded = run_mock.call_args.args[1]
    assert forwarded.time_sync.enabled is True
    assert forwarded.time_sync.date == date(2025, 5, 1)
    # Empty config falls back to ALA so the flag is usable standalone.
    assert set(forwarded.time_sync.snapshot_servers) == {"core", "extra", "multilib"}


def test_time_sync_date_preserves_existing_snapshot_servers(tmp_path: Path) -> None:
    """A vendor-configured snapshot_servers map must NOT be clobbered."""
    from dataclasses import replace

    from pacman_sysext.time_sync import TimeSyncConfig

    base = AppConfig.default()
    vendor_servers = {"core": "https://mirror.vendor.example/{date}/{repo}/{arch}"}
    cfg = replace(
        base, time_sync=TimeSyncConfig(snapshot_servers=vendor_servers)
    )

    runner = CliRunner()
    with (
        patch("pacman_sysext.cli.AppConfig.load", return_value=cfg),
        patch("pacman_sysext.cli.install_cmd.run") as run_mock,
    ):
        result = runner.invoke(app, ["install", "htop", "--time-sync-date", "2025-05-01"])

    assert result.exit_code == 0, result.output
    forwarded = run_mock.call_args.args[1]
    assert forwarded.time_sync.snapshot_servers == vendor_servers


def test_time_sync_date_invalid_format_exits_with_error(tmp_path: Path) -> None:
    runner = CliRunner()
    with (
        patch("pacman_sysext.cli.AppConfig.load", return_value=_stub_config(tmp_path)),
        patch("pacman_sysext.cli.install_cmd.run") as run_mock,
    ):
        result = runner.invoke(app, ["install", "htop", "--time-sync-date", "not-a-date"])

    assert result.exit_code == 1
    assert "invalid --time-sync-date" in result.output or "invalid --time-sync-date" in (
        result.stderr or ""
    )
    run_mock.assert_not_called()


def test_install_without_time_sync_date_leaves_config_disabled(tmp_path: Path) -> None:
    runner = CliRunner()
    with (
        patch("pacman_sysext.cli.AppConfig.load", return_value=_stub_config(tmp_path)),
        patch("pacman_sysext.cli.install_cmd.run") as run_mock,
    ):
        result = runner.invoke(app, ["install", "htop"])

    assert result.exit_code == 0, result.output
    forwarded = run_mock.call_args.args[1]
    assert forwarded.time_sync.enabled is False
    assert forwarded.time_sync.date is None
