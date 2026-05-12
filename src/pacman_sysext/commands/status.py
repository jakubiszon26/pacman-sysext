"""Render a read-only audit dashboard of installed sysexts."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pacman_sysext import state, time_sync
from pacman_sysext.config import AppConfig
from pacman_sysext.pacman import PacmanError, get_package_dependencies
from pacman_sysext.state import IntegrityReport, SysextRecord
from pacman_sysext.time_sync import TimeSyncError

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024


def run(config: AppConfig, console: Console | None = None) -> None:
    """Audit installed sysexts and render the dashboard to `console`.

    The console parameter is None in production (a fresh one is created)
    and overridden in tests with a recording Console so output can be
    asserted on.
    """
    console = console or Console()

    # Resolver is invoked only by get_implicit when a record has depends=[]
    # — i.e. a legacy entry or a true leaf. Records persisted by the current
    # install command always carry their direct deps in state, so this
    # fallback is rarely exercised on fresh systems.
    def _fallback_resolver(name: str) -> list[str]:
        try:
            return [c.name for c in get_package_dependencies(name, config.pacman)]
        except PacmanError as e:
            logger.warning("pacman -Si %s failed; treating as no deps: %s", name, e)
            return []

    try:
        # Hold the lock across the on-disk audit so a concurrent install
        # cannot mid-build expose a partial .raw to glob() / stat(). Pure
        # rendering happens after the lock is dropped.
        with state.locked(config.state_db):
            current_state = state.load(config.state_db)
            report = state.audit_integrity(current_state, config.builder.output_dir)
            explicit = state.get_explicit(current_state)
            implicit = state.get_implicit(current_state, dep_resolver=_fallback_resolver)
            orphans = state.get_orphans(current_state, explicit=explicit, implicit=implicit)
            sizes = _collect_sizes(current_state.sysexts.values(), config.builder.output_dir)
    except state.StateError as e:
        print(f"Error reading state: {e}")
        raise typer.Exit(code=1) from e

    host_snapshot_date = _maybe_host_snapshot_date(
        [*explicit, *implicit, *orphans]
    )
    _render(
        console,
        report=report,
        explicit=explicit,
        implicit=implicit,
        orphans=orphans,
        sizes=sizes,
        host_snapshot_date=host_snapshot_date,
    )


_HOST_SYNC_DIR = Path("/var/lib/pacman/sync")


def _maybe_host_snapshot_date(records: list[SysextRecord]) -> date | None:
    """Best-effort *observed* host snapshot date for drift detection.

    Fires whenever any record carries a `pinned_date` — that signal is
    record-driven (an `--time-sync-date` one-shot install leaves a pinned
    record even when config returns to disabled), so we recompute the
    live host DB age every time the state would benefit from a comparison.
    Returns None on any backend hiccup so status stays renderable.
    """
    if not any(r.pinned_date is not None for r in records):
        return None
    try:
        return time_sync.derive_snapshot_date(_HOST_SYNC_DIR)
    except TimeSyncError as e:
        logger.info("could not derive host snapshot date for drift hint: %s", e)
        return None


def _collect_sizes(records: Iterable[SysextRecord], output_dir: Path) -> dict[str, int]:
    """Map sysext_key → file size in bytes. Missing or unreadable files are 0."""
    sizes: dict[str, int] = {}
    for record in records:
        key = state.sysext_key(record.name, record.version)
        path = output_dir / record.raw_filename
        try:
            sizes[key] = path.stat().st_size
        except OSError:
            sizes[key] = 0
    return sizes


def _render(
    console: Console,
    *,
    report: IntegrityReport,
    explicit: list[SysextRecord],
    implicit: list[SysextRecord],
    orphans: list[SysextRecord],
    sizes: dict[str, int],
    host_snapshot_date: date | None,
) -> None:
    has_audit_issues = bool(report.missing_files or report.unregistered_files or report.scan_error)
    if has_audit_issues:
        console.print(_audit_panel(report))

    console.print(_explicit_table(explicit))

    if orphans:
        console.print(_orphans_panel(orphans, sizes))

    console.print(_summary_table(explicit, implicit, orphans, sizes))

    if host_snapshot_date is not None:
        hint = _snapshot_drift_hint(explicit + implicit, host_snapshot_date)
        if hint is not None:
            console.print(hint)


def _snapshot_drift_hint(
    records: list[SysextRecord], host_snapshot_date: date
) -> Text | None:
    """One-line drift hint when pinned dates lag the host's current snapshot.

    Records without a `pinned_date` (legacy or non-time-sync installs)
    do not count toward drift — they were never pinned, so they can't drift.
    """
    pinned = [r for r in records if r.pinned_date is not None]
    stale = [r for r in pinned if r.pinned_date != host_snapshot_date]
    if not stale:
        return None
    stale_dates = sorted({r.pinned_date.isoformat() for r in stale if r.pinned_date})
    return Text(
        f"\n⚠ {len(stale)} sysext(s) were pinned to {', '.join(stale_dates)}; "
        f"host snapshot is now {host_snapshot_date.isoformat()}. "
        f"Rebuild for ABI consistency.",
        style="yellow",
    )


def _audit_panel(report: IntegrityReport) -> Panel:
    body = Text()
    if report.scan_error:
        body.append("Output directory scan failed:\n", style="bold")
        body.append(f"  {report.scan_error}\n")
    if report.missing_files:
        if report.scan_error:
            body.append("\n")
        body.append("Recorded in state, missing on disk:\n", style="bold")
        for record in report.missing_files:
            body.append(f"  - {record.raw_filename}\n")
    if report.unregistered_files:
        if report.missing_files or report.scan_error:
            body.append("\n")
        body.append("Present on disk, unknown to state:\n", style="bold")
        for path in report.unregistered_files:
            # Basename only — the panel can wrap long pytest tmp paths and
            # the directory is identical for every entry. Hint about the dir
            # belongs above the list, not on every line.
            body.append(f"  - {path.name}\n")
    body.rstrip()
    return Panel(body, title="Integrity issues", border_style="red", style="red")


def _explicit_table(explicit: list[SysextRecord]) -> Table:
    table = Table(title="Explicit packages", title_style="bold", header_style="bold")
    table.add_column("Package")
    table.add_column("Version")
    table.add_column("Pinned")
    table.add_column("Installed at")
    for record in explicit:
        pinned = record.pinned_date.isoformat() if record.pinned_date else "—"
        table.add_row(
            record.name,
            record.version,
            pinned,
            record.installed_at.isoformat(timespec="seconds"),
        )
    if not explicit:
        table.add_row("(none)", "", "", "")
    return table


def _orphans_panel(orphans: list[SysextRecord], sizes: dict[str, int]) -> Panel:
    body = Text()
    body.append(
        "These sysexts are not reachable from any user request and can be removed safely:\n\n",
    )
    for record in orphans:
        key = state.sysext_key(record.name, record.version)
        size_mib = sizes.get(key, 0) / _MIB
        body.append(f"  - {record.name}-{record.version}  ", style="bold")
        body.append(f"{size_mib:.2f} MiB\n")
    body.rstrip()
    return Panel(body, title="Orphan sysexts", border_style="yellow", style="yellow")


def _summary_table(
    explicit: list[SysextRecord],
    implicit: list[SysextRecord],
    orphans: list[SysextRecord],
    sizes: dict[str, int],
) -> Table:
    total_bytes = sum(sizes.values())
    table = Table(title="Summary", title_style="bold", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    table.add_row("Total images", str(len(sizes)))
    table.add_row("Explicit", str(len(explicit)))
    table.add_row("Implicit", str(len(implicit)))
    table.add_row("Orphans", str(len(orphans)))
    table.add_row("Disk usage", f"{total_bytes / _MIB:.2f} MiB")
    return table
