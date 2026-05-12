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


def daemon_reload() -> None:
    """Force PID 1 to pick up unit files from the freshly merged /usr layer.

    `systemd-sysext refresh` swaps the /usr overlay atomically but does not
    notify PID 1 that new `.service` / `.socket` / `.timer` files exist —
    systemd keeps its old in-memory unit table until told to reload, so
    `systemctl start <new-unit>` returns "Unit not found" for anything the
    just-merged layer ships. Running this immediately after refresh closes
    that gap before sysusers/tmpfiles touch the host.
    """
    logger.info("Reloading systemd daemon")
    _run_sysext(["systemctl", "daemon-reload"])


def apply_sysusers() -> None:
    """Register system users and groups declared in /usr/lib/sysusers.d/.

    Packages with daemons (valkey, redis, postgres, …) ship sysusers.d
    snippets that declare the system accounts their files must be owned by.
    After `systemd-sysext refresh` makes those snippets visible on the live
    root, we have to commit them to the host's /etc/passwd and /etc/group
    *before* running `systemd-tmpfiles`: tmpfiles directives that chown a
    /var/lib/<daemon> tree to its service user otherwise fail silently when
    the account does not yet exist, leaving the daemon unable to start.

    No scope locking here (unlike `apply_tmpfiles`): `systemd-sysusers` is
    strictly additive — it creates missing entries and never modifies or
    removes existing ones — so re-applying every sysusers.d file on the
    host is safe and idempotent.
    """
    logger.info("Registering system users")
    _run_sysext(["systemd-sysusers"])


def apply_tmpfiles() -> None:
    """Run `systemd-tmpfiles --create` globally against the live root.

    A merged sysext ships two kinds of tmpfiles.d snippets:

    1. Our generated `pacman-sysext-<image>.conf` — materialises /etc and
       /var content the translator pulled out at build time.
    2. *Package-native* `<pkg>.conf` straight from Arch upstream
       (mariadb.conf, redis.conf, postgresql.conf, …) — typically
       declares the daemon's runtime tree under /run, which is tmpfs and
       does not survive reboot.

    An earlier revision scoped this call to (1) only, on the principle of
    minimum blast radius. That broke (2) outright: the canonical reproducer
    was MariaDB failing with `/run/mariadb/wsrep-start-position: No such
    file or directory` immediately after install — its native tmpfiles.d
    rule was silently skipped, so /run/mariadb/ was never created.

    We deliberately revert to the unscoped form. The blast radius is now
    the same as `systemd-tmpfiles-setup.service` at every boot — any
    `f`/`F`/`w`/`L+` rule may re-create a file the admin had deleted —
    but that file would be re-created at the next reboot anyway, so we
    are not introducing a new failure mode. Daemon correctness wins.
    """
    logger.info("Applying tmpfiles recipes")
    _run_sysext(["systemd-tmpfiles", "--create"])


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

    # Order matters and was paid for in production incidents:
    #   1. daemon-reload   — PID 1 picks up new `.service`/`.socket`/`.timer`
    #                        units from the just-swapped /usr; without this,
    #                        `systemctl start <new-unit>` returns "not found".
    #   2. sysusers        — commits accounts from /usr/lib/sysusers.d/* to
    #                        /etc/passwd so the next step has someone to
    #                        chown to.
    #   3. tmpfiles        — creates /etc, /var AND /run trees (the last
    #                        is critical for daemon packages whose runtime
    #                        state lives on tmpfs).
    # Any swap silently breaks at least one daemon package.
    daemon_reload()
    apply_sysusers()
    apply_tmpfiles()


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
