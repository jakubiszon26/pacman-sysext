"""Typer entry point for pacman-sysext."""

import logging
from pathlib import Path

import typer

from pacman_sysext.commands import install as install_cmd
from pacman_sysext.config import AppConfig

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
def install(ctx: typer.Context, package: str) -> None:
    """Install package as sysext."""
    install_cmd.run(package, ctx.obj)


@app.command()
def remove(package: str) -> None:
    """Remove sysext."""
    raise NotImplementedError("`remove` is not implemented yet")


@app.command(name="list")
def list_installed() -> None:
    """List installed sysexts."""
    raise NotImplementedError("`list` is not implemented yet")


def main() -> None:
    """Entry point for the CLI script."""
    app()
