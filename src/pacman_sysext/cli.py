"""Typer entry point for pacman-sysext."""

import logging
from dataclasses import replace
from datetime import date
from pathlib import Path

import typer

from pacman_sysext.commands import install as install_cmd
from pacman_sysext.commands import status as status_cmd
from pacman_sysext.config import AppConfig
from pacman_sysext.time_sync import default_ala_servers

app = typer.Typer(
    help="Pacman packages as systemd-sysext images",
    no_args_is_help=True,
)


@app.callback()
def root(
    ctx: typer.Context,
    config_file: Path | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """pacman-sysext - install Arch packages as systemd-sysext."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    ctx.obj = AppConfig.load(config_file)


@app.command()
def install(
    ctx: typer.Context,
    package: str,
    assume_yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompts (for automation)."
    ),
    allow_host_abi_mismatch: bool = typer.Option(
        False,
        "--allow-host-abi-mismatch",
        help=(
            "Bypass the ABI Gatekeeper and bundle libraries even when they would "
            "shadow the host with a different version. Dangerous: may crash "
            "native host daemons (GDM, dbus, PAM) on boot."
        ),
    ),
    time_sync_date: str | None = typer.Option(
        None,
        "--time-sync-date",
        metavar="YYYY-MM-DD",
        help=(
            "Override the snapshot date for this install. Implies "
            "time_sync.enabled = true. Useful for reproducible builds and "
            "Arch-Linux-Archive gap-day workarounds."
        ),
    ),
) -> None:
    """Install package as sysext."""
    config: AppConfig = ctx.obj
    if time_sync_date is not None:
        try:
            override = date.fromisoformat(time_sync_date)
        except ValueError as e:
            typer.echo(f"Error: invalid --time-sync-date {time_sync_date!r}: {e}", err=True)
            raise typer.Exit(code=1) from e
        # One-shot flag on a host without TOML-configured snapshot_servers
        # would otherwise hit the strict-policy block for every Arch dep.
        # Fall back to ALA defaults at the CLI layer so the flag is usable
        # on a stock Arch host without writing config.
        fallback_servers = (
            config.time_sync.snapshot_servers
            if config.time_sync.snapshot_servers
            else default_ala_servers()
        )
        config = replace(
            config,
            time_sync=replace(
                config.time_sync,
                enabled=True,
                date=override,
                snapshot_servers=fallback_servers,
            ),
        )

    install_cmd.run(
        package,
        config,
        assume_yes=assume_yes,
        allow_host_abi_mismatch=allow_host_abi_mismatch,
    )


@app.command()
def remove(package: str) -> None:
    """Remove sysext."""
    raise NotImplementedError("`remove` is not implemented yet")


@app.command(name="status")
def status(ctx: typer.Context) -> None:
    """Audit installed sysexts and render a dashboard."""
    status_cmd.run(ctx.obj)


@app.command(name="list")
def list_installed() -> None:
    """List installed sysexts."""
    raise NotImplementedError("`list` is not implemented yet")


def main() -> None:
    """Entry point for the CLI script."""
    app()
