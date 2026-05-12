"""Render a read-only audit dashboard of installed sysexts."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pacman_sysext import state
from pacman_sysext.config import AppConfig
from pacman_sysext.pacman import PacmanError, get_package_dependencies
from pacman_sysext.state import IntegrityReport, SysextRecord

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024


def run(config: AppConfig, console: Console | None = None) -> None:
    """Audit installed sysexts and render the dashboard to `console`.

    The console parameter is None in production (a fresh one is created)
    and overridden in tests with a recording Console so output can be
    asserted on.
    """
    console = console or Console()

    with state.locked(config.state_db):
        current_state = state.load(config.state_db)

    # Resolver is invoked only by get_implicit/get_orphans when a record has
    # depends=[] — i.e. a legacy entry or a true leaf. Records persisted by
    # the current install command always carry their direct deps in state,
    # so this fallback is rarely exercised on fresh systems.
    def _fallback_resolver(name: str) -> list[str]:
        try:
            return [c.name for c in get_package_dependencies(name, config.pacman)]
        except PacmanError as e:
            logger.warning("pacman -Si %s failed; treating as no deps: %s", name, e)
            return []

    report = state.audit_integrity(current_state, config.builder.output_dir)
    explicit = state.get_explicit(current_state)
    implicit = state.get_implicit(current_state, dep_resolver=_fallback_resolver)
    orphans = state.get_orphans(current_state, dep_resolver=_fallback_resolver)

    sizes = _collect_sizes(current_state.sysexts.values(), config.builder.output_dir)

    _render(
        console,
        report=report,
        explicit=explicit,
        implicit=implicit,
        orphans=orphans,
        sizes=sizes,
    )


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
) -> None:
    has_audit_issues = bool(report.missing_files or report.unregistered_files)
    if has_audit_issues:
        console.print(_audit_panel(report))

    console.print(_explicit_table(explicit))

    if orphans:
        console.print(_orphans_panel(orphans, sizes))

    console.print(_summary_table(explicit, implicit, orphans, sizes))


def _audit_panel(report: IntegrityReport) -> Panel:
    body = Text()
    if report.missing_files:
        body.append("Recorded in state, missing on disk:\n", style="bold")
        for record in report.missing_files:
            body.append(f"  - {record.raw_filename}\n")
    if report.unregistered_files:
        if report.missing_files:
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
    table.add_column("Installed at")
    for record in explicit:
        table.add_row(
            record.name,
            record.version,
            record.installed_at.isoformat(timespec="seconds"),
        )
    if not explicit:
        table.add_row("(none)", "", "")
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
