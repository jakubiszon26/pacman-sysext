"""Build systemd-sysext images from pacman packages."""

import logging
import platform
import posixpath
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from pacman_sysext.config import FsFormat
from pacman_sysext.pacman import parse_pkg_filename

logger = logging.getLogger(__name__)


# uname -m → systemd's architecture identifier (see systemd.unit(5))
_SYSTEMD_ARCH_MAP = {
    "x86_64": "x86-64",
    "aarch64": "arm64",
    "i686": "x86",
}

# Arch package metadata that must not ship inside a sysext image.
_ARCH_METADATA_FILES = {
    ".PKGINFO",
    ".MTREE",
    ".BUILDINFO",
    ".INSTALL",
    ".CHANGELOG",
}

# Top-level directories valid inside a sysext.
_ALLOWED_TOP_DIRS = {"usr", "opt"}

# Top-level directories that would need the systemd factory pattern
# (/usr/share/factory/...) to work in a sysext. We currently drop them
# with a warning; implementing factory layout is a TODO.
_FACTORY_PATTERN_DIRS = {"etc", "var"}


class BuildError(Exception):
    """Sysext build failed."""


@contextmanager
def _staging_dir() -> Iterator[Path]:
    """Create a temporary staging directory, cleanup on exit."""
    tmp = tempfile.mkdtemp(prefix="pacman-sysext-")
    try:
        yield Path(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(cmd: list[str], failure_msg: str) -> None:
    """Run a subprocess; raise BuildError on missing binary or non-zero exit.

    Includes both stderr and stdout in the error so we don't lose context
    when a tool reports failure on either stream (or leaves stderr empty).
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise BuildError(f"{failure_msg}: command not found: {cmd[0]}") from e

    if result.returncode != 0:
        msg = failure_msg
        if result.stderr.strip():
            msg += f"\nstderr: {result.stderr.rstrip()}"
        if result.stdout.strip():
            msg += f"\nstdout: {result.stdout.rstrip()}"
        raise BuildError(msg)


_MAX_UNSAFE_PATHS_REPORTED = 10


def _validate_archive_members(pkg_file: Path) -> None:
    """List archive members and refuse anything that could escape the staging dir.

    GNU tar 1.32+ already rejects `..` and symlink-based escapes during
    extraction, but we run as root and don't want to rely on that — a
    pre-flight check fails fast, deterministically, and survives regressions
    in tar or a non-GNU implementation on PATH.
    """
    try:
        result = subprocess.run(
            ["tar", "-tf", str(pkg_file)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise BuildError(f"tar not found while validating {pkg_file.name}") from e
    except subprocess.CalledProcessError as e:
        msg = f"tar failed to list members of {pkg_file.name}"
        if e.stderr.strip():
            msg += f"\nstderr: {e.stderr.rstrip()}"
        raise BuildError(msg) from e

    unsafe: list[str] = []
    for raw_line in result.stdout.splitlines():
        member = raw_line.rstrip("/")
        if not member:
            continue
        if "\x00" in member or member.startswith("/") or _has_parent_segment(member):
            unsafe.append(raw_line)
            if len(unsafe) >= _MAX_UNSAFE_PATHS_REPORTED:
                break

    if unsafe:
        listing = "\n".join(f"  {p}" for p in unsafe)
        raise BuildError(
            f"refusing to extract {pkg_file.name}: archive contains unsafe paths:\n{listing}"
        )


def _has_parent_segment(member: str) -> bool:
    """True if any path segment of `member` is `..` after posix normalization."""
    normalized = posixpath.normpath(member)
    return any(segment == ".." for segment in normalized.split("/"))


def _extract_package(pkg_file: Path, dest: Path) -> None:
    """Extract a pacman package to destination directory.

    Uses system `tar` because Python's tarfile gained native zstd
    support only in 3.14, while we target 3.13. GNU tar autodetects
    compression from magic bytes, so this works for .pkg.tar.zst /
    .pkg.tar.xz / .pkg.tar.gz alike.
    """
    _validate_archive_members(pkg_file)
    logger.debug("Extracting %s to %s", pkg_file.name, dest)
    _run(
        ["tar", "-xf", str(pkg_file), "-C", str(dest)],
        f"tar failed extracting {pkg_file.name}",
    )


def _clean_arch_metadata(staging: Path) -> None:
    """Remove Arch-specific metadata files from staging."""
    for filename in _ARCH_METADATA_FILES:
        (staging / filename).unlink(missing_ok=True)


def _strip_unsupported_dirs(staging: Path, image_name: str) -> None:
    """Drop top-level entries that don't belong in a sysext.

    sysext is mounted read-only over /, so only /usr and /opt are valid
    targets. /etc and /var would need the factory pattern; until that
    lands we drop them and warn loudly so the user knows config/state
    files were lost.
    """
    for entry in staging.iterdir():
        if entry.name in _ALLOWED_TOP_DIRS:
            continue

        if entry.name in _FACTORY_PATTERN_DIRS:
            logger.warning(
                "Package %s ships /%s — dropping (factory pattern not implemented)",
                image_name,
                entry.name,
            )
        else:
            logger.warning(
                "Package %s has unexpected top-level entry: %s — removing",
                image_name,
                entry.name,
            )

        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _systemd_arch() -> str:
    """Return current architecture in systemd's identifier form."""
    machine = platform.machine()
    return _SYSTEMD_ARCH_MAP.get(machine, machine)


def _write_extension_release(staging: Path, image_name: str) -> None:
    """Write systemd-sysext metadata file.

    The filename must equal the image basename (without .raw) — that's
    how systemd-sysext locates metadata for a given image. Mismatch
    yields "No medium found" on merge/refresh.
    """
    release_dir = staging / "usr" / "lib" / "extension-release.d"
    release_dir.mkdir(parents=True, exist_ok=True)

    os_info = platform.freedesktop_os_release()
    lines = [
        f"ID={os_info.get('ID', '_any')}",
        f"ARCHITECTURE={_systemd_arch()}",
    ]
    if version := os_info.get("VERSION_ID"):
        lines.append(f"VERSION_ID={version}")

    release_file = release_dir / f"extension-release.{image_name}"
    release_file.write_text("\n".join(lines) + "\n")
    logger.debug("Wrote extension-release: %s", release_file)


def _make_erofs(source: Path, output: Path) -> None:
    """Create EROFS image."""
    logger.debug("Building erofs: %s", output)
    if shutil.which("mkfs.erofs") is None:
        raise BuildError("mkfs.erofs not found. Install 'erofs-utils' package.")
    _run(
        ["mkfs.erofs", str(output), str(source)],
        "mkfs.erofs failed",
    )


def _make_squashfs(source: Path, output: Path) -> None:
    """Create SquashFS image."""
    logger.debug("Building squashfs: %s", output)
    if shutil.which("mksquashfs") is None:
        raise BuildError("mksquashfs not found. Install 'squashfs-tools' package.")
    _run(
        ["mksquashfs", str(source), str(output), "-quiet", "-noappend", "-comp", "zstd"],
        "mksquashfs failed",
    )


_BACKENDS: dict[FsFormat, Callable[[Path, Path], None]] = {
    "erofs": _make_erofs,
    "squashfs": _make_squashfs,
}


def _make_image(source: Path, output: Path, fs_format: FsFormat) -> None:
    backend = _BACKENDS.get(fs_format)
    if backend is None:
        raise BuildError(
            f"Unsupported filesystem format: {fs_format!r}. Supported: {sorted(_BACKENDS)}"
        )
    # mkfs tools refuse to overwrite an existing image
    output.unlink(missing_ok=True)
    backend(source, output)


def sanitize_image_name(pkg_name: str, pkg_version: str) -> str:
    """Form the on-disk image basename, escaping characters overlayfs reserves.

    Overlayfs uses `:` as the `lowerdir` separator and `,` as the option
    separator. Pacman versions may contain `:` for epoch (e.g. `1:0.3.4`),
    which would make `systemd-sysext refresh` concatenate paths ambiguously
    when assembling the lowerdir list — the kernel reports "No such file or
    directory" on the resulting bogus components. `,` doesn't appear in real
    pacman versions, but we escape it defensively.

    The `version` field stored in state stays untouched so vercmp still
    compares against the upstream version; only the filename is escaped.
    """
    return f"{pkg_name}-{pkg_version}".replace(":", "+").replace(",", "_")


def build_sysext(
    pkg_file: Path,
    output_dir: Path,
    fs_format: FsFormat = "squashfs",
) -> Path:
    """Build a sysext .raw image from a pacman package.

    Args:
        pkg_file: Path to .pkg.tar.zst
        output_dir: Where to write .raw
        fs_format: Filesystem format ('erofs' or 'squashfs')

    Returns:
        Path to the created .raw file
    """
    if not pkg_file.is_file():
        raise BuildError(f"Package file not found: {pkg_file}")

    pkg_name, pkg_version = parse_pkg_filename(pkg_file)
    image_name = sanitize_image_name(pkg_name, pkg_version)
    logger.info("Building sysext for %s (%s)", image_name, fs_format)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{image_name}.raw"

    with _staging_dir() as staging:
        _extract_package(pkg_file, staging)
        _clean_arch_metadata(staging)
        _strip_unsupported_dirs(staging, image_name)
        _write_extension_release(staging, image_name)
        _make_image(staging, output_file, fs_format)

    logger.info("Built %s", output_file)
    return output_file
