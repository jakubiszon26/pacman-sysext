"""Snapshot-pinned sandbox preparation for time-sync installs.

`prepare_sandbox` copies the host's pacman sync DBs into a date-namespaced
sandbox and renders a pacman.conf whose `Server =` lines point at a
snapshot backend (Arch Linux Archive by default). The resolver then
sees the host's worldview — which is what ABI Gatekeeper checks against —
while still pulling actual `.pkg.tar.zst` files from a date-pinned URL.

Backends own URL synthesis and any retry / forward-search for gap days.
The rest of the codebase remains backend-agnostic.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pacman_sysext.config import PacmanConfig

logger = logging.getLogger(__name__)


_ALA_TEMPLATE = "https://archive.archlinux.org/repos/{date}/{repo}/os/{arch}"
_MAX_FORWARD_SEARCH_DAYS = 7
_PROBE_TIMEOUT_S = 5.0
_SECTION_RE = re.compile(r"^\[(.+?)\]\s*$")
_KEY_RE = re.compile(r"^\s*([A-Za-z]\w*)\s*=")

Policy = Literal["strict"]


class TimeSyncError(Exception):
    """Time-sync sandbox preparation failed."""


@dataclass(frozen=True)
class PreparedSandbox:
    """Outcome of `prepare_sandbox`: resolver-facing PacmanConfig + the date pinned to it."""

    pacman: PacmanConfig
    effective_date: date


@dataclass(frozen=True)
class TimeSyncConfig:
    """Configuration for `[time_sync]` section.

    `snapshot_servers` maps repo name → URL template. Recognised
    placeholders: `{date}` (YYYY/MM/DD UTC), `{repo}`, `{arch}`. Repos
    absent from the map are *not* pinned and will be caught by the
    strict policy gate downstream.
    """

    enabled: bool = False
    date: date | None = None
    policy: Policy = "strict"
    snapshot_servers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TimeSyncError(
                f"time_sync.enabled must be bool, got {type(self.enabled).__name__}"
            )
        if self.policy != "strict":
            raise TimeSyncError(
                f"time_sync.policy = {self.policy!r} not supported; only 'strict' is available"
            )
        if not isinstance(self.snapshot_servers, dict):
            raise TimeSyncError(
                "time_sync.snapshot_servers must be a table of repo -> template"
            )


def default_ala_servers() -> dict[str, str]:
    """Emit the three Arch-Linux-Archive entries (`core`, `extra`, `multilib`)."""
    return {
        "core": _ALA_TEMPLATE,
        "extra": _ALA_TEMPLATE,
        "multilib": _ALA_TEMPLATE,
    }


def expand_template(template: str, repo: str, arch: str, snapshot_date: date) -> str:
    """Render a snapshot URL template into a concrete URL prefix.

    Placeholders: `{date}` (YYYY/MM/DD), `{repo}`, `{arch}`. Any other
    `{...}` placeholder is a configuration error.
    """
    try:
        return template.format(
            date=snapshot_date.strftime("%Y/%m/%d"),
            repo=repo,
            arch=arch,
        )
    except (KeyError, IndexError) as e:
        raise TimeSyncError(
            f"unknown placeholder in snapshot template {template!r}: {e}"
        ) from e


def derive_snapshot_date(host_sync_dir: Path) -> date:
    """Lower bound on the snapshot date — `max(BUILDDATE)` in host `core.db`.

    The DB literally *is* the host's worldview; its latest BUILDDATE is
    the youngest day on which the host could have legitimately resolved
    packages.
    """
    core_db = host_sync_dir / "core.db"
    if not core_db.exists():
        raise TimeSyncError(f"host sync DB missing: {core_db}")
    return _max_builddate_from_db(core_db)


def find_effective_date(
    start_date: date,
    snapshot_servers: dict[str, str],
    arch: str,
    *,
    max_days: int = _MAX_FORWARD_SEARCH_DAYS,
    probe: Callable[[str], bool] | None = None,
) -> date:
    """Walk forward from `start_date` (up to `max_days`) until every mapped
    repo has a reachable `<repo>.db`.

    Bounded forward search papers over ALA gap days transparently; on a
    non-gappy backend the first probe succeeds immediately. Tests
    inject `probe` to avoid real HTTP.
    """
    probe_fn = probe or _http_probe
    if not snapshot_servers:
        return start_date

    last_failed: str | None = None
    for offset in range(max_days + 1):
        candidate = start_date + timedelta(days=offset)
        all_ok = True
        for repo, template in snapshot_servers.items():
            url = f"{expand_template(template, repo, arch, candidate)}/{repo}.db"
            if not probe_fn(url):
                last_failed = url
                all_ok = False
                break
        if all_ok:
            if offset:
                logger.info(
                    "snapshot date %s missing, falling forward to %s",
                    start_date.isoformat(),
                    candidate.isoformat(),
                )
            return candidate

    deadline = start_date + timedelta(days=max_days)
    suffix = f" (last probed {last_failed})" if last_failed else ""
    raise TimeSyncError(
        f"no snapshot reachable in {start_date.isoformat()}..{deadline.isoformat()}{suffix}. "
        "Pass --time-sync-date YYYY-MM-DD manually."
    )


def render_pinned_pacman_conf(
    host_conf_text: str,
    snapshot_date: date,
    snapshot_servers: dict[str, str],
    arch: str,
) -> str:
    """Rewrite a pacman.conf so mapped repos point at the snapshot backend.

    Semantics:
      * `[options]` passes through, except `SigLevel` overrides containing
        the bare `Never` token are dropped (loud or silent skip of
        signature verification).
      * `[repo]` where `repo in snapshot_servers`: every `Server =` and
        `Include =` line is replaced with exactly one synthesized
        `Server = <expanded template>`; other directives pass through.
      * `[repo]` where `repo not in snapshot_servers`: pass through
        verbatim. Strict-policy enforcement happens downstream against
        `ResolvedDep.repo` — dropping unmapped repos here would surface
        as a confusing "package not found".
    """
    out_lines: list[str] = []
    current_section: str | None = None
    mapped_section = False
    server_written = False

    for raw_line in host_conf_text.splitlines():
        stripped = raw_line.strip()
        section_match = _SECTION_RE.match(stripped)
        if section_match:
            current_section = section_match.group(1)
            mapped_section = current_section in snapshot_servers
            server_written = False
            out_lines.append(raw_line)
            continue

        if mapped_section and not stripped.startswith("#"):
            key_match = _KEY_RE.match(raw_line)
            if key_match and key_match.group(1) in ("Server", "Include"):
                if not server_written and current_section is not None:
                    url = expand_template(
                        snapshot_servers[current_section],
                        current_section,
                        arch,
                        snapshot_date,
                    )
                    out_lines.append(f"Server = {url}")
                    server_written = True
                continue

        if current_section == "options" and _is_weakening_siglevel(raw_line):
            logger.info("dropping weakening SigLevel override from pinned pacman.conf")
            continue

        out_lines.append(raw_line)

    rendered = "\n".join(out_lines)
    if host_conf_text.endswith("\n") and not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def prepare_sandbox(
    time_sync_cfg: TimeSyncConfig,
    base_pacman_cfg: PacmanConfig,
    *,
    host_sync_dir: Path = Path("/var/lib/pacman/sync"),
    host_pacman_conf: Path = Path("/etc/pacman.conf"),
    arch: str | None = None,
    probe: Callable[[str], bool] | None = None,
) -> PreparedSandbox:
    """Build a date-pinned sandbox and return its `PacmanConfig` + effective date.

    The returned config's `dbpath` and `cachedir` are namespaced by the
    *effective* snapshot date (post forward-search), so two installs
    that resolve to the same date share their cache. The date is
    exposed alongside the config so state-layer code can persist
    `pinned_date` without re-running the forward search.
    """
    if not time_sync_cfg.enabled:
        raise TimeSyncError(
            "time_sync.enabled = false; prepare_sandbox must not be called"
        )

    resolved_arch = arch if arch is not None else platform.machine()
    lower_bound = time_sync_cfg.date or derive_snapshot_date(host_sync_dir)
    effective_date = find_effective_date(
        lower_bound,
        time_sync_cfg.snapshot_servers,
        resolved_arch,
        probe=probe,
    )

    namespace = f"ala-{effective_date.isoformat()}"
    sandbox_dbpath = base_pacman_cfg.dbpath / namespace
    sandbox_cachedir = base_pacman_cfg.cachedir / namespace
    sandbox_dbpath.mkdir(parents=True, exist_ok=True)
    sandbox_cachedir.mkdir(parents=True, exist_ok=True)

    sync_dest = sandbox_dbpath / "sync"
    sync_dest.mkdir(parents=True, exist_ok=True)
    _copy_sync_dbs(host_sync_dir, sync_dest)

    pinned_dir = sandbox_dbpath / ".pinned"
    pinned_dir.mkdir(parents=True, exist_ok=True)
    pinned_conf = pinned_dir / "pacman.conf"
    try:
        host_conf_text = host_pacman_conf.read_text()
    except OSError as e:
        raise TimeSyncError(
            f"cannot read host pacman.conf {host_pacman_conf}: {e}"
        ) from e
    rendered = render_pinned_pacman_conf(
        host_conf_text,
        effective_date,
        time_sync_cfg.snapshot_servers,
        resolved_arch,
    )
    pinned_conf.write_text(rendered)

    pacman_cfg = replace(
        base_pacman_cfg,
        dbpath=sandbox_dbpath,
        cachedir=sandbox_cachedir,
        config_file=pinned_conf,
    )
    return PreparedSandbox(pacman=pacman_cfg, effective_date=effective_date)


def _copy_sync_dbs(host_sync_dir: Path, dest_dir: Path) -> None:
    """Copy only `*.db` from host sync dir. `.files` DBs are unused by the resolver."""
    if not host_sync_dir.is_dir():
        raise TimeSyncError(f"host sync dir missing: {host_sync_dir}")
    copied = 0
    for db_file in sorted(host_sync_dir.glob("*.db")):
        target = dest_dir / db_file.name
        try:
            shutil.copy2(db_file, target)
            # fsync — pacman never re-reads a DB it didn't write, so a
            # crash mid-copy leaving a torn file would silently break the
            # next install. Cheap on the ~MB-sized syncs.
            with target.open("rb") as fh:
                os.fsync(fh.fileno())
        except OSError as e:
            raise TimeSyncError(f"cannot copy sync DB {db_file} -> {target}: {e}") from e
        copied += 1
    if copied == 0:
        raise TimeSyncError(f"no *.db files in {host_sync_dir}")


def _max_builddate_from_db(db_path: Path) -> date:
    max_ts: int | None = None
    try:
        with tarfile.open(db_path, "r:*") as tar:
            for member in tar:
                if not member.isfile() or not member.name.endswith("/desc"):
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                desc = fh.read().decode("utf-8", "replace")
                ts = _extract_builddate(desc)
                if ts is None:
                    continue
                if max_ts is None or ts > max_ts:
                    max_ts = ts
    except (tarfile.TarError, OSError) as e:
        raise TimeSyncError(f"cannot read sync DB {db_path}: {e}") from e
    if max_ts is None:
        raise TimeSyncError(f"no BUILDDATE entries in {db_path}")
    return datetime.fromtimestamp(max_ts, tz=UTC).date()


def _extract_builddate(desc: str) -> int | None:
    """Parse a single pacman `desc` text blob for its BUILDDATE epoch."""
    lines = desc.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "%BUILDDATE%":
            continue
        if i + 1 >= len(lines):
            return None
        value = lines[i + 1].strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _is_weakening_siglevel(raw_line: str) -> bool:
    """`SigLevel = ... Never ...` overrides skip signature verification."""
    if "=" not in raw_line:
        return False
    key, value = raw_line.split("=", 1)
    if key.strip() != "SigLevel":
        return False
    tokens = value.split()
    return any(tok.lower() == "never" for tok in tokens)


def _http_probe(url: str) -> bool:
    """HEAD-style availability check. True on 2xx/3xx, False otherwise."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
            status = int(resp.status)
            return 200 <= status < 400
    except urllib.error.HTTPError as e:
        return 200 <= int(e.code) < 400
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
