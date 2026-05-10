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
from pathlib import Path

from pacman_sysext.config import PacmanConfig

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


def get_required_packages(package: str, config: PacmanConfig) -> list[str]:
    """Return filenames of all packages pacman would fetch for `package`."""
    result = _run_pacman(["-Sw", package, "--print", "--noconfirm"], config)

    filenames = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        filenames.append(Path(line).name)

    return filenames


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


def query_system_packages() -> dict[str, str]:
    """Map name → version for every package installed on the host."""
    result = _run_host_pacman(["-Q"])
    if result.returncode != 0:
        raise PacmanError(
            f"pacman -Q failed with code {result.returncode}",
            returncode=result.returncode,
            stderr=result.stderr,
            stdout=result.stdout,
        )

    packages = {}
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        name, version = line.split(" ", 1)
        packages[name] = version

    return packages


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
