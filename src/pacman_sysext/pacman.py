"""Wrappers around the system `pacman` binary.

All sandboxed operations (sync, download, query target packages) go
through `_run_pacman`, which injects the dbpath/cachedir/config/gpgdir
flags from `PacmanConfig`. Host queries (what is actually installed
right now) deliberately bypass the sandbox via `_run_host_pacman`.
"""

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pacman_sysext.config import PacmanConfig
from pacman_sysext.version import VersionConstraint, parse_constraint

logger = logging.getLogger(__name__)


# Force English output so parsers see stable keys regardless of host locale.
_C_ENV = {**os.environ, "LC_ALL": "C"}

# Per Arch packaging spec, version/release/arch never contain hyphens, so the
# last three "-" delimit them; everything before is the (possibly-hyphenated) name.
_PKG_FILENAME_RE = re.compile(
    r"^(?P<name>.+?)-(?P<version>[^-]+)-(?P<release>[^-]+)-(?P<arch>[^-]+)"
    r"\.pkg\.tar\.[^.]+$"
)


class PacmanError(Exception):
    """Pacman command failed."""

    def __init__(self, message: str, returncode: int, stderr: str, stdout: str = ""):
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout

    def __str__(self) -> str:
        parts = [self.message]
        if self.stdout.strip():
            parts.append(f"stdout:\n{self.stdout.rstrip()}")
        if self.stderr.strip():
            parts.append(f"stderr:\n{self.stderr.rstrip()}")
        return "\n".join(parts)


def _run_pacman(args: list[str], config: PacmanConfig) -> subprocess.CompletedProcess[str]:
    """Run pacman against the sandboxed db/cache. Internal helper.

    Does NOT inject --noconfirm — callers add it explicitly when they
    want unattended behavior, so that prompts (replaces, conflicts,
    provider selection) stay visible at call sites instead of being
    silently auto-answered.
    """
    config.dbpath.mkdir(parents=True, exist_ok=True)
    config.cachedir.mkdir(parents=True, exist_ok=True)
    cmd = ["pacman", *args, *config.to_args()]
    return _run(cmd)


def _run_host_pacman(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run pacman against the host db (no sandbox flags).

    Used for read-only queries about what is installed on the host
    (`-T`, `-Q`). Returns even on non-zero exit — callers inspect
    returncode themselves (e.g. `pacman -T` exits 127 when packages
    are missing, which is the answer, not an error).
    """
    try:
        return subprocess.run(
            ["pacman", *args],
            capture_output=True,
            text=True,
            env=_C_ENV,
        )
    except FileNotFoundError as e:
        raise PacmanError(
            "pacman not found - is it installed?",
            returncode=-1,
            stderr=str(e),
        ) from e


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            env=_C_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise PacmanError(
            f"pacman {' '.join(cmd[1:])} failed with code {e.returncode}",
            returncode=e.returncode,
            stderr=e.stderr,
            stdout=e.stdout,
        ) from e
    except FileNotFoundError as e:
        raise PacmanError(
            "pacman not found - is it installed?",
            returncode=-1,
            stderr=str(e),
        ) from e


def sync_databases(config: PacmanConfig) -> None:
    """Run `pacman -Sy` to refresh sync databases."""
    result = _run_pacman(["-Sy", "--noconfirm"], config)
    logger.info("pacman -Sy:\n%s", result.stdout.rstrip())


def download_package(package: str, config: PacmanConfig) -> None:
    """Run `pacman -Sw <package>` to fetch the package and its deps into cache.

    --noconfirm here picks pacman's defaults for any provider/replace prompts.
    Callers that need to know what landed in cache should call
    `get_required_packages` and verify against `config.cachedir`.
    """
    _run_pacman(["-Sw", package, "--noconfirm"], config)


def parse_pkg_filename(pkg_file: Path | str) -> tuple[str, str]:
    """Parse package filename into (name, version-release).

    Example:
        "htop-3.5.1-1.1-x86_64_v4.pkg.tar.zst" → ("htop", "3.5.1-1.1")
    """
    filename = pkg_file.name if isinstance(pkg_file, Path) else pkg_file

    match = _PKG_FILENAME_RE.match(filename)
    if not match:
        raise ValueError(f"Cannot parse package filename: {filename}")

    return match["name"], f"{match['version']}-{match['release']}"


@dataclass(frozen=True)
class ResolvedDep:
    """One package pacman would fetch for an install transaction.

    `repo` is the source pacman db (`core`, `extra`, `multilib`,
    `cachyos-v4`, …) as printed by `%r`. `url` is the full download
    location pacman would use; `filename` is its basename.
    """

    repo: str
    name: str
    version: str
    url: str
    filename: str


_RESOLVE_FIELD_SEP = "|"
_RESOLVE_PRINT_FORMAT = f"%r{_RESOLVE_FIELD_SEP}%n{_RESOLVE_FIELD_SEP}%v{_RESOLVE_FIELD_SEP}%l"


def resolve_required_packages(package: str, config: PacmanConfig) -> list[ResolvedDep]:
    """Return structured resolution of every package pacman would fetch for `package`.

    Backed by `pacman -Sw <pkg> --print --print-format "%r|%n|%v|%l"`. The
    repo source (`%r`) is the authoritative classification — far cheaper
    than N follow-up `pacman -Si` probes and semantically correct for
    time-sync repo policy gating.

    The field separator is `|` rather than `\\t` because some immutable
    distros (Arkane, Garuda-immutable, …) ship `/usr/bin/pacman` as a
    shell wrapper that re-tokenizes argv via unquoted `$@`. With a tab
    inside the format string the wrapper would split it on IFS and the
    real pacman would see `%n`/`%v`/`%l` as positional targets and fail
    with `target not found: %n`. `|` is not in default IFS and never
    appears inside the four fields, so it survives the wrapper intact.
    """
    result = _run_pacman(
        ["-Sw", package, "--print", "--print-format", _RESOLVE_PRINT_FORMAT, "--noconfirm"],
        config,
    )

    resolved: list[ResolvedDep] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        parts = line.split(_RESOLVE_FIELD_SEP)
        if len(parts) != 4:
            raise PacmanError(
                f"unexpected --print-format output line: {line!r}",
                returncode=0,
                stderr="",
                stdout=result.stdout,
            )
        repo, name, version, url = parts
        resolved.append(
            ResolvedDep(
                repo=repo,
                name=name,
                version=version,
                url=url,
                filename=Path(url).name,
            )
        )
    return resolved


def get_required_packages(package: str, config: PacmanConfig) -> list[str]:
    """Return filenames of all packages pacman would fetch for `package`."""
    return [d.filename for d in resolve_required_packages(package, config)]


def get_package_info(package: str, config: PacmanConfig) -> dict[str, str]:
    """Run `pacman -Si <package>` and parse the output to a dict."""
    result = _run_pacman(["-Si", package], config)
    return _parse_pacman_info(result.stdout)


def find_unsatisfied(pkg_names: list[str]) -> set[str]:
    """Return subset of pkg_names not installed/provided by the host system.

    Uses `pacman -T` which respects `provides` relations
    (e.g. zlib-ng-compat provides zlib).
    """
    if not pkg_names:
        return set()
    # exit 0 = all satisfied; 127 = some unsatisfied (names listed on stdout).
    result = _run_host_pacman(["-T", *pkg_names])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def query_system_packages(names: list[str] | None = None) -> dict[str, str]:
    """Map name → version for host-installed packages.

    With `names`, scopes the query to those entries via `pacman -Q
    <names...>`. This is the form to prefer in hot paths — a bare
    `pacman -Q` lists every package on the host (often thousands) and
    spends measurable time on lines we are going to discard.

    Names from `names` that the host does not have are silently omitted.
    `pacman -Q` exits 1 in that case while still printing the satisfied
    entries on stdout, so we parse the output regardless of exit code
    when `names` is set. Without `names`, a non-zero exit is a real
    failure and gets raised.
    """
    if names is None:
        result = _run_host_pacman(["-Q"])
        if result.returncode != 0:
            raise PacmanError(
                f"pacman -Q failed with code {result.returncode}",
                returncode=result.returncode,
                stderr=result.stderr,
                stdout=result.stdout,
            )
    elif not names:
        return {}
    else:
        result = _run_host_pacman(["-Q", *sorted(set(names))])

    packages = {}
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        # Defensive split: keep going on a malformed line rather than crashing
        # the install over a single weird `pacman -Q` row.
        try:
            name, version = line.split(" ", 1)
        except ValueError:
            continue
        packages[name] = version

    return packages


# Packages whose version changes are most likely to affect sysext ABI compatibility.
# This list is a heuristic starting point. Configurable overrides are a follow-up.
ABI_RELEVANT_PACKAGES: frozenset[str] = frozenset(
    {
        "glibc",
        "gcc-libs",
        "ncurses",
        "openssl",
        "zlib",
        "icu",
        "libxml2",
        "readline",
        "pcre2",
        "expat",
    }
)


def get_package_version(package: str, config: PacmanConfig) -> str:
    """Return the version of `package` as known to the sync databases."""
    info = get_package_info(package, config)
    version = info.get("Version", "").strip()
    if not version:
        raise PacmanError(
            f"pacman -Si {package} returned no Version field",
            returncode=0,
            stderr="",
            stdout="",
        )
    return version


def get_package_dependencies(package: str, config: PacmanConfig) -> list[VersionConstraint]:
    """Parsed `Depends On` for `package`. Returns empty list when 'None'."""
    info = get_package_info(package, config)
    raw = info.get("Depends On", "").strip()
    if not raw or raw == "None":
        return []
    return [parse_constraint(entry) for entry in raw.split() if entry]


def get_package_provides(package: str, config: PacmanConfig) -> dict[str, str]:
    """Parsed `Provides` for `package`. Pinned `name=version` stays pinned; bare name maps to ''."""
    info = get_package_info(package, config)
    raw = info.get("Provides", "").strip()
    if not raw or raw == "None":
        return {}
    result: dict[str, str] = {}
    for entry in raw.split():
        name, sep, version = entry.partition("=")
        result[name] = version if sep else ""
    return result


def get_base_snapshot(packages: frozenset[str] = ABI_RELEVANT_PACKAGES) -> dict[str, str]:
    """Return name → version for ABI-relevant host packages that are actually installed.

    Packages from `packages` not installed on the host are silently
    omitted; the set is a heuristic and not all distros ship all of
    these. `pacman -Q` exits 1 when any requested package is missing but
    still prints info for the present ones, so we parse stdout
    regardless of exit code.
    """
    if not packages:
        return {}
    result = _run_host_pacman(["-Q", *sorted(packages)])
    snapshot: dict[str, str] = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            name, version = stripped.split(" ", 1)
        except ValueError:
            continue
        snapshot[name] = version
    return snapshot


def _parse_pacman_info(output: str) -> dict[str, str]:
    """Parse `pacman -Si` (single-package) output into a dict.

    Format produced by pacman:
        Key            : value
                         continuation of value
        Other Key      : ...

    Multi-package output (multiple records separated by blank lines)
    is not supported — last record wins. Caller must query one package
    at a time.
    """
    info: dict[str, str] = {}
    current_key: str | None = None

    for line in output.splitlines():
        # Key lines start at column 0; continuation lines are indented.
        if line and not line[0].isspace():
            key, sep, value = line.partition(" : ")
            if not sep:
                current_key = None
                continue
            current_key = key.strip()
            info[current_key] = value.strip()
        elif current_key and (cont := line.strip()):
            info[current_key] += " " + cont

    return info
