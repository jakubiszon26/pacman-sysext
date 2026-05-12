"""Build systemd-sysext images from pacman packages."""

import logging
import os
import platform
import posixpath
import re
import shutil
import stat
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

# Top-level directories sysext won't overlay (only /usr and /opt are merged).
# We translate them to a tmpfiles.d recipe shipped inside the sysext image.
_TRANSLATED_TOP_DIRS = ("etc", "var")

# Where translated file content lives inside the sysext image. Mirrors the
# host layout (etc/foo → <skel>/<image>/etc/foo) so the tmpfiles `C` source
# argument is a straightforward concatenation.
_SKEL_DIR = "usr/share/pacman-sysext/skel"

# tmpfiles.d recipes shipped inside the image. systemd-tmpfiles scans this
# directory after sysext merge, materialising /etc and /var on the live root.
_TMPFILES_DIR = "usr/lib/tmpfiles.d"

# Conservative whitelist for image_name: we embed it in a tmpfiles.d recipe
# (header comment, source path, recipe filename) that runs as root via
# `systemd-tmpfiles --create`. Anything outside this set could either break
# parsing or — worse — let a hostile filename inject a directive. Real Arch
# package names + versions only use a strict subset of this character class.
_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")


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
    overlay targets. /etc and /var are handled separately by
    `_translate_etc_and_var` (called before this); anything else
    (stray /root, /tmp, …) is removed with a warning.
    """
    for entry in staging.iterdir():
        if entry.name in _ALLOWED_TOP_DIRS:
            continue

        logger.warning(
            "Package %s has unexpected top-level entry: %s — removing",
            image_name,
            entry.name,
        )

        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


# C-escape table per tmpfiles.d(5) / systemd's cunescape(). `\s` is the
# documented form for ASCII space (more idiomatic than the equivalent
# `\x20`). All five escapes have been valid since systemd 240+, well
# below our minimum (sysext requires 254+).
_TMPFILES_ESCAPE_TABLE = str.maketrans(
    {
        "\\": "\\\\",
        " ": "\\s",
        "\t": "\\t",
        "\n": "\\n",
        "\r": "\\r",
    }
)


def _escape_tmpfiles_field(value: str) -> str:
    """C-escape whitespace and backslash per tmpfiles.d(5) parsing rules.

    The default-marker `-` must pass through verbatim — it signals "use the
    default" for Mode/User/Group/Age/Argument. Other fields (paths,
    symlink targets) are unconditionally escaped.
    """
    if value == "-":
        return value
    return value.translate(_TMPFILES_ESCAPE_TABLE)


def _tmpfiles_line(
    kind: str,
    host_path: str,
    mode: str = "-",
    uid: str = "-",
    gid: str = "-",
    age: str = "-",
    argument: str = "-",
) -> str:
    """Format one tmpfiles.d directive. Path and Argument are C-escaped."""
    return " ".join(
        [
            kind,
            _escape_tmpfiles_field(host_path),
            mode,
            uid,
            gid,
            age,
            _escape_tmpfiles_field(argument),
        ]
    )


def _iter_entries(root: Path) -> Iterator[Path]:
    """Pre-order walk of `root` without following symlinks.

    Pre-order ensures parent directories appear before their children in
    the emitted recipe, which keeps the file readable; systemd-tmpfiles
    creates intermediate parents automatically regardless.
    """
    for child in sorted(root.iterdir()):
        yield child
        if child.is_dir() and not child.is_symlink():
            yield from _iter_entries(child)


# Symlink directive per top-level dir.
#
# /etc: `L` creates the symlink only if the target path is empty. The admin
# may have replaced our shipped /etc/foo with a custom symlink (or file);
# `L+` would clobber that. We err on the side of preserving admin state.
#
# /var: `L+` recreates unconditionally. /var is package-managed state, not
# admin configuration; a stale symlink from an old install is the failure
# mode we actively want to clear.
_SYMLINK_KIND_FOR_TOP = {"etc": "L", "var": "L+"}


def _translate_etc_and_var(staging: Path, image_name: str) -> None:
    """Relocate /etc and /var into the sysext-owned skel and emit a tmpfiles
    recipe that re-materialises them on the live root after merge.

    sysext only overlays /usr and /opt; raw /etc and /var contents would be
    silently dropped at merge time. We move file content into
    `/usr/share/pacman-sysext/skel/<image>/...` inside the image and write
    `/usr/lib/tmpfiles.d/pacman-sysext-<image>.conf` with:

    - `d` for directories — created with the package's mode/owner.
    - `C` for regular files — copied to /etc or /var only if missing, so
      admin edits made after first activation are preserved across refresh.
    - `L`/`L+` for symlinks — see `_SYMLINK_KIND_FOR_TOP` above.

    Non-regular, non-directory, non-symlink entries (FIFOs, sockets, device
    nodes) are dropped with a warning: systemd-tmpfiles can't materialise
    them from a `C` source, and they have no business being shipped in a
    pacman package's /etc or /var.

    The original /etc and /var trees are deleted from the staging area
    after translation: an empty /etc inside the .raw would shadow the
    host's real /etc through the overlay.
    """
    lines: list[str] = []
    skel_root_rel = Path(_SKEL_DIR) / image_name
    skel_root_abs = staging / skel_root_rel

    for top in _TRANSLATED_TOP_DIRS:
        top_dir = staging / top
        if not top_dir.is_dir() or top_dir.is_symlink():
            continue
        symlink_kind = _SYMLINK_KIND_FOR_TOP[top]
        for entry in _iter_entries(top_dir):
            directive = _translate_entry(entry, staging, skel_root_abs, skel_root_rel, symlink_kind)
            if directive is not None:
                lines.append(directive)
        shutil.rmtree(top_dir)

    if not lines:
        return

    recipe_dir = staging / _TMPFILES_DIR
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / f"pacman-sysext-{image_name}.conf"
    # Header is escaped too: image_name is whitelist-validated upstream,
    # but escaping costs nothing on a clean string and survives any future
    # weakening of the whitelist. A `\n` smuggled in here would otherwise
    # synthesize a live tmpfiles directive on the very next line.
    safe_image_name = _escape_tmpfiles_field(image_name)
    header = (
        f"# Generated by pacman-sysext for {safe_image_name}.\n"
        f"# Materialises files this package ships into /etc and /var.\n"
    )
    recipe.write_text(header + "\n".join(lines) + "\n")
    logger.debug("Wrote tmpfiles recipe: %s (%d lines)", recipe, len(lines))


def _translate_entry(
    entry: Path,
    staging: Path,
    skel_root_abs: Path,
    skel_root_rel: Path,
    symlink_kind: str,
) -> str | None:
    """Translate one staged /etc or /var entry into a tmpfiles directive.

    Returns `None` for entries we deliberately skip (FIFOs, sockets, device
    nodes — see caller). Side effect: regular files are moved into the
    skel tree; symlinks and skipped entries are unlinked. Directories are
    left in place so the iteration can descend into them; the whole
    `etc/`/`var/` top-level tree is removed by the caller afterwards.
    """
    rel = entry.relative_to(staging)
    host_path = "/" + rel.as_posix()
    entry_stat = entry.lstat()
    mode_bits = stat.S_IMODE(entry_stat.st_mode)
    mode = f"{mode_bits:04o}"
    uid = str(entry_stat.st_uid)
    gid = str(entry_stat.st_gid)

    if entry.is_symlink():
        target = os.readlink(entry)
        entry.unlink()
        return _tmpfiles_line(symlink_kind, host_path, "-", "-", "-", "-", target)

    if entry.is_dir():
        return _tmpfiles_line("d", host_path, mode, uid, gid)

    if not stat.S_ISREG(entry_stat.st_mode):
        logger.warning(
            "Skipping non-regular file %s (mode=%#o): tmpfiles `C` can only "
            "materialise regular files and directories",
            host_path,
            entry_stat.st_mode,
        )
        entry.unlink()
        return None

    dest = skel_root_abs / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(entry), dest)
    source_host_path = "/" + (skel_root_rel / rel).as_posix()
    return _tmpfiles_line("C", host_path, mode, uid, gid, "-", source_host_path)


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
    """Form the on-disk image basename, escaping overlayfs-reserved characters
    and rejecting anything outside the safe whitelist.

    Overlayfs uses `:` as the `lowerdir` separator and `,` as the option
    separator. Pacman versions may contain `:` for epoch (e.g. `1:0.3.4`),
    which would make `systemd-sysext refresh` concatenate paths ambiguously
    when assembling the lowerdir list — the kernel reports "No such file or
    directory" on the resulting bogus components. `,` doesn't appear in real
    pacman versions, but we escape it defensively.

    The `version` field stored in state stays untouched so vercmp still
    compares against the upstream version; only the filename is escaped.

    The post-escape result is validated against a strict whitelist
    (`[A-Za-z0-9._+-]`) because `image_name` is later embedded in a
    tmpfiles.d recipe executed as root. Whitespace or control bytes
    sneaking through here could be parsed as a synthesized directive.
    Real Arch package names and versions only use a subset of the
    whitelist, so a rejection here signals a malformed or hostile input.
    """
    name = f"{pkg_name}-{pkg_version}".replace(":", "+").replace(",", "_")
    if not _IMAGE_NAME_RE.fullmatch(name):
        raise BuildError(
            f"refusing to build: image name {name!r} contains characters "
            f"outside the safe whitelist [A-Za-z0-9._+-]"
        )
    return name


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
        _translate_etc_and_var(staging, image_name)
        _strip_unsupported_dirs(staging, image_name)
        _write_extension_release(staging, image_name)
        _make_image(staging, output_file, fs_format)

    logger.info("Built %s", output_file)
    return output_file
