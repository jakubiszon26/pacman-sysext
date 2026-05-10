"""Activate/deactivate sysexts via systemd-sysext."""

import contextlib
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class SysextError(Exception):
    """systemd-sysext command failed."""


def activate_sysext(raw_path: Path, extensions_dir: Path) -> Path:
    """Activate a sysext by symlinking it into the extensions directory.

    Args:
        raw_path: Path to .raw file (e.g. /var/lib/pacman-sysext/sysexts/htop-3.5.1-1.1.raw)
        extensions_dir: Where systemd-sysext looks (e.g. /var/lib/extensions)

    Returns:
        Path to the symlink in extensions_dir.
    """
    if not raw_path.exists():
        raise FileNotFoundError(f"Sysext not found: {raw_path}")

    extensions_dir.mkdir(parents=True, exist_ok=True)
    link_path = extensions_dir / raw_path.name

    # exists() returns False for broken symlinks, so we need both checks
    # to detect (and replace) a stale link to a removed image.
    if link_path.exists() or link_path.is_symlink():
        logger.debug("Removing existing %s", link_path)
        link_path.unlink()

    # Resolve so the symlink survives moves of `extensions_dir`.
    link_path.symlink_to(raw_path.resolve())
    logger.info("Linked %s → %s", link_path, raw_path)

    return link_path


def deactivate_sysext(name: str, extensions_dir: Path) -> bool:
    """Remove sysext symlink. Returns True if removed, False if absent."""
    link_path = extensions_dir / name

    if not link_path.exists() and not link_path.is_symlink():
        return False

    link_path.unlink()
    logger.info("Unlinked %s", link_path)
    return True


def merge() -> None:
    """Run `systemd-sysext merge`."""
    logger.info("Merging sysexts")
    _run_sysext(["systemd-sysext", "merge"])


def unmerge() -> None:
    """Run `systemd-sysext unmerge`."""
    logger.info("Unmerging sysexts")
    _run_sysext(["systemd-sysext", "unmerge"])


def refresh() -> None:
    """Refresh sysexts atomically (systemd 256+)."""
    logger.info("Refreshing sysexts")
    _run_sysext(["systemd-sysext", "refresh"])


def is_refresh_supported() -> bool:
    """Check whether systemd-sysext understands the `refresh` verb."""
    try:
        result = subprocess.run(
            ["systemd-sysext", "--help"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise SysextError("systemd-sysext not found - is systemd 254+ installed?") from e
    return "refresh" in result.stdout


def activate_all(raw_paths: list[Path], extensions_dir: Path) -> None:
    """Symlink every image and trigger a merge/refresh.

    Uses `refresh` if available (atomic, no service interruption),
    otherwise falls back to unmerge+merge.
    """
    for raw in raw_paths:
        activate_sysext(raw, extensions_dir)

    if is_refresh_supported():
        refresh()
    else:
        # `unmerge` errors when nothing is currently merged, which is
        # a valid starting state for the very first activation.
        with contextlib.suppress(SysextError):
            unmerge()
        merge()


def _run_sysext(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        msg = f"{' '.join(cmd)} failed with code {e.returncode}"
        if e.stderr.strip():
            msg += f"\nstderr: {e.stderr.rstrip()}"
        raise SysextError(msg) from e
    except FileNotFoundError as e:
        raise SysextError(f"command not found: {cmd[0]}") from e
