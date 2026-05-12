"""Install a pacman package as a sysext image and activate it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer

from pacman_sysext import abi_gate, state, time_sync
from pacman_sysext.abi_gate import ClassifiedDep, GateReport
from pacman_sysext.builder import BuildError, build_sysext
from pacman_sysext.config import AppConfig, PacmanConfig
from pacman_sysext.pacman import (
    PacmanError,
    ResolvedDep,
    download_package,
    find_unsatisfied,
    get_base_snapshot,
    get_package_dependencies,
    get_package_provides,
    parse_pkg_filename,
    query_system_packages,
    resolve_required_packages,
    sync_databases,
)
from pacman_sysext.sysext import SysextError, activate_all
from pacman_sysext.time_sync import TimeSyncError
from pacman_sysext.version import (
    VersionConstraint,
    VersionError,
    parse_constraint,
    satisfies,
    vercmp,
)

logger = logging.getLogger(__name__)


class InstallConflictError(Exception):
    """A new install conflicts with an existing user request."""


@dataclass(frozen=True)
class BuildPlan:
    """Per-dep decisions for an install transaction."""

    to_build: list[str] = field(default_factory=list)
    reused: list[tuple[str, str]] = field(default_factory=list)
    host_provided: list[str] = field(default_factory=list)
    integrity_failures: list[str] = field(default_factory=list)


def _prepare_pacman(config: AppConfig) -> PacmanConfig:
    """Return the resolver-facing PacmanConfig (sandbox or plain `pacman -Sy`)."""
    if config.time_sync.enabled:
        return time_sync.prepare_sandbox(config.time_sync, config.pacman)
    sync_databases(config.pacman)
    return config.pacman


def _confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    while True:
        answer = input(f"\n{prompt} [y/n]: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please enter 'y' or 'n'.")


def _format_constraint(c: VersionConstraint) -> str:
    """Operator+version part of a pacman dep spec (e.g. `>=2.78`); empty for unconstrained."""
    if c.operator is None or c.version is None:
        return ""
    return f"{c.operator}{c.version}"


def _pretty(c: VersionConstraint) -> str:
    suffix = _format_constraint(c)
    return f"{c.name}{suffix}" if suffix else c.name


def _constraints_overlap(c1: VersionConstraint, c2: VersionConstraint) -> bool:
    """Conservative overlap check on two single-operator constraints.

    Sound for the common cases (`>=` vs `<=`, `=` vs anything). Permissive
    for ambiguous cases (e.g. `>` vs `<`) — we'd rather let a real install
    proceed than block on a false positive.
    """
    if c1.operator is None or c2.operator is None:
        return True
    assert c1.version is not None and c2.version is not None

    if satisfies(c1.version, c1) and satisfies(c1.version, c2):
        return True
    if satisfies(c2.version, c1) and satisfies(c2.version, c2):
        return True

    # `=` constrains to a single version; if its anchor fails to satisfy the
    # other constraint, the intervals are disjoint with certainty.
    if c1.operator == "=" or c2.operator == "=":
        return False

    lower_ops = {">=", ">"}
    upper_ops = {"<=", "<"}

    if c1.operator in lower_ops and c2.operator in upper_ops:
        lower_v, lower_inc = c1.version, c1.operator == ">="
        upper_v, upper_inc = c2.version, c2.operator == "<="
    elif c2.operator in lower_ops and c1.operator in upper_ops:
        lower_v, lower_inc = c2.version, c2.operator == ">="
        upper_v, upper_inc = c1.version, c1.operator == "<="
    else:
        # Both same direction, or one is `=`/`!=` — anchor check above is authoritative.
        return True

    cmp = vercmp(lower_v, upper_v)
    if cmp < 0:
        return True
    if cmp > 0:
        return False
    return lower_inc and upper_inc


def _check_for_conflicts(
    state_obj: state.State,
    target_name: str,
    target_deps: list[VersionConstraint],
) -> None:
    """Raise InstallConflictError on incompatible constraints with existing user_requests."""
    for new_c in target_deps:
        for existing_user, req in state_obj.user_requests.items():
            if existing_user == target_name:
                # Re-installing the same package replaces its prior requirements.
                continue
            existing_str = req.requirements.get(new_c.name)
            if existing_str is None:
                continue
            existing_c = parse_constraint(
                f"{new_c.name}{existing_str}" if existing_str else new_c.name
            )
            if _constraints_overlap(new_c, existing_c):
                continue

            known_versions = sorted(
                r.version for r in state.find_sysexts_by_name(state_obj, new_c.name)
            )
            versions_line = (
                f"  Known {new_c.name} versions in state: {', '.join(known_versions)}\n"
                if known_versions
                else ""
            )
            raise InstallConflictError(
                f"cannot install {target_name}:\n"
                f"  {target_name} requires {_pretty(new_c)}\n"
                f"  but {existing_user} (already installed) requires {_pretty(existing_c)}\n"
                f"{versions_line}"
                f"  No single {new_c.name} version satisfies both.\n\n"
                f"  To proceed, run `pacman-sysext remove {existing_user}` first."
            )


def _make_build_plan(
    state_obj: state.State,
    gate_report: GateReport,
    config: AppConfig,
) -> BuildPlan:
    """Decide per dep whether to reuse, leave to host, or build.

    `gate_report` is the sole source of truth for the host-vs-resolved
    classification — `skip` means the host already provides it; `bundle`,
    `shadow` and `block` are candidates for building (or reusing from
    state). `block` entries only reach this function when the caller
    decided to honor them (via `--allow-host-abi-mismatch`); without the
    override the caller raises `typer.Exit` first.
    """
    to_build: list[str] = []
    reused: list[tuple[str, str]] = []
    host_provided: list[str] = [dep.name for dep in gate_report.skips]
    integrity_failures: list[str] = []

    for dep in (*gate_report.bundles, *gate_report.shadows, *gate_report.blocks):
        record = state.get_sysext(state_obj, dep.name, dep.resolved_version)
        if record is not None:
            if state.verify_sysext_integrity(record, config.builder.output_dir):
                reused.append((dep.name, dep.resolved_version))
                continue
            integrity_failures.append(state.sysext_key(dep.name, dep.resolved_version))
            to_build.append(dep.filename)
            continue

        to_build.append(dep.filename)

    return BuildPlan(
        to_build=to_build,
        reused=reused,
        host_provided=host_provided,
        integrity_failures=integrity_failures,
    )


def _format_gate_table(deps: list[ClassifiedDep]) -> str:
    """Pretty-print one bucket of ClassifiedDep entries as an aligned table."""
    if not deps:
        return ""
    width = max(len(d.name) for d in deps)
    lines = []
    for dep in deps:
        host = dep.host_version if dep.host_version is not None else "(provides alias)"
        lines.append(
            f"  {dep.name.ljust(width)}  host has {host}, sysext would ship {dep.resolved_version}"
        )
    return "\n".join(lines)


def _print_gate_shadows(shadows: list[ClassifiedDep]) -> None:
    print(
        f"\n⚠ Warning: {len(shadows)} cosmetic package(s) will shadow the host\n"
        "  (safe-shadow exemption — fonts, icon themes; ABI doesn't apply):"
    )
    print(_format_gate_table(shadows))


def _print_unmapped_block(
    target_pkg: str,
    unmapped: list[ResolvedDep],
    snapshot_servers: dict[str, str],
) -> None:
    """Strict-policy block: dep comes from a repo with no snapshot backend."""
    print(f"\nError: SNAPSHOT POLICY BLOCK — refusing to build sysext for {target_pkg}.\n")
    print(
        "Time-sync is enabled with policy = strict, but the following package(s)\n"
        "were resolved from a repo that has no snapshot backend configured:\n"
    )
    width = max(len(d.name) for d in unmapped)
    seen_repos: set[str] = set()
    for dep in unmapped:
        print(f"  {dep.name.ljust(width)}  from repo {dep.repo!r} ({dep.version})")
        seen_repos.add(dep.repo)
    configured = ", ".join(sorted(snapshot_servers)) or "(none)"
    print(
        "\nConfigured snapshot_servers: " + configured + "\n\n"
        "Resolution:\n"
        "  1) Add a snapshot backend for the listed repo(s) in "
        "[time_sync.snapshot_servers].\n"
        "  2) Or remove the third-party repo from the host so the resolver\n"
        "     stops considering it.\n"
        "  3) Or disable time-sync (time_sync.enabled = false) — at the cost of\n"
        "     reintroducing ABI Gatekeeper false positives on rolling-immutable hosts."
    )


def _print_gate_block(target_pkg: str, blocks: list[ClassifiedDep], overridden: bool) -> None:
    header = "\n⚠ HOST ABI MISMATCH OVERRIDDEN" if overridden else "\nError: HOST ABI MISMATCH"
    verb = "will" if overridden else "would"
    suffix = (
        " — proceeding because --allow-host-abi-mismatch was set."
        if overridden
        else f" — refusing to build sysext for {target_pkg}."
    )
    print(f"{header}{suffix}\n")
    print(
        f"Installing {target_pkg} {verb} shadow the host with different versions of\n"
        "the following packages. Native host daemons compiled against the host's\n"
        "ABI may crash on boot (GDM, dbus, PAM, the graphical session):\n"
    )
    print(_format_gate_table(blocks))
    if overridden:
        print("\nYou own the risk. Keep a rescue shell / known-good snapshot handy.")
    else:
        print(
            "\nResolution:\n"
            "  1) Update the host first (sudo pacman -Syu) so its ABI catches up.\n"
            "  2) Wait for the immutable image vendor to bump those libraries.\n"
            "  3) Re-run with --allow-host-abi-mismatch if you understand the risk\n"
            "     and have a recovery plan (rescue shell / known-good snapshot)."
        )


def _gather_host_state(required_filenames: list[str]) -> tuple[dict[str, str], set[str]]:
    """Snapshot the host: installed packages and the names it satisfies (via provides too).

    Returns `(host_packages, host_provided)`. Both are derived from
    host-side pacman calls scoped to just the names in `required_filenames`
    — a full `pacman -Q` is O(host packages) and would put thousands of
    irrelevant entries in the critical install path.
    """
    required_names = sorted({parse_pkg_filename(f)[0] for f in required_filenames})
    host_packages = query_system_packages(required_names)
    host_provided = set(required_names) - find_unsatisfied(required_names)
    return host_packages, host_provided


def _warn_about_drift(state_obj: state.State, current_snapshot: dict[str, str]) -> None:
    drifts = state.find_abi_drift(state_obj, current_snapshot)
    if not drifts:
        return
    print(f"\n⚠ Warning: {len(drifts)} existing sysext(s) may have stale base dependencies:")
    for drift in drifts:
        print(f"  {drift.sysext_key}:")
        for pkg, (old, new) in sorted(drift.differences.items()):
            print(f"    {pkg}: {old} → {new} (current)")
        for pkg in sorted(drift.missing_in_current):
            print(f"    {pkg}: present at build time, missing now")
    print("Run `pacman-sysext rebuild` to refresh them (not yet implemented).\n")


def _print_plan(plan: BuildPlan) -> None:
    if plan.host_provided:
        print(f"\nSkipping {len(plan.host_provided)} packages already in base system:")
        for name in plan.host_provided:
            print(f"  - {name}")
    if plan.reused:
        print(f"\nReusing {len(plan.reused)} existing sysext(s):")
        for name, version in plan.reused:
            print(f"  - {name}-{version}")
    if plan.integrity_failures:
        print(
            f"\n⚠ Integrity check failed for {len(plan.integrity_failures)} "
            f"sysext(s) — will rebuild:"
        )
        for key in plan.integrity_failures:
            print(f"  - {key}")


def _record_user_request(
    state_obj: state.State,
    package: str,
    target_version: str,
    target_deps: list[VersionConstraint],
) -> None:
    state.add_user_request(
        state_obj,
        state.UserRequest(
            name=package,
            installed_version=target_version,
            requested_at=datetime.now(UTC),
            requirements={c.name: _format_constraint(c) for c in target_deps},
        ),
    )


def _resolve_target_version(target_pkg: str, plan: BuildPlan) -> str:
    """Pull the canonical target version from the build plan.

    The filename is the only source of truth that matches what we
    actually stored as `SysextRecord.version` and used in the
    `.raw`/state key. `pacman -Si <target>` can return a different
    string (e.g. CachyOS rebuild pkgrels like `1.1` where Arch's
    metadata still says `1`), which would desync UserRequest from
    SysextRecord and break later graph lookups.
    """
    for filename in plan.to_build:
        name, version = parse_pkg_filename(filename)
        if name == target_pkg:
            return version
    for name, version in plan.reused:
        if name == target_pkg:
            return version
    raise RuntimeError(f"target {target_pkg!r} missing from build plan (to_build + reused)")


def _collect_reused_outputs(
    state_obj: state.State, reused: list[tuple[str, str]], output_dir: Path
) -> list[Path]:
    out: list[Path] = []
    for name, version in reused:
        record = state.get_sysext(state_obj, name, version)
        if record is None:  # pragma: no cover - plan invariant
            continue
        out.append(output_dir / record.raw_filename)
    return out


def _cleanup_outputs(outputs: list[Path]) -> None:
    """Remove .raw files built in a transaction that didn't commit to state."""
    for output in outputs:
        try:
            output.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not clean up orphaned %s: %s", output, e)


def run(
    package: str,
    config: AppConfig,
    assume_yes: bool = False,
    allow_host_abi_mismatch: bool = False,
) -> None:
    print(f"Installing sysext ({package})...")
    build_outputs: list[Path] = []
    reused_outputs: list[Path] = []

    try:
        # `state.locked()` itself raises `StateError` on lock-acquisition
        # timeout; `state.load()` raises it on a malformed state.db. The
        # outer handler covers both with the same user-facing message.
        with state.locked(config.state_db):
            current_state = state.load(config.state_db)

            try:
                current_snapshot = get_base_snapshot()
            except PacmanError as e:
                print(f"Error: {e}")
                raise typer.Exit(code=1) from e

            _warn_about_drift(current_state, current_snapshot)

            try:
                effective_pacman = _prepare_pacman(config)
            except TimeSyncError as e:
                print(f"Error: {e}")
                raise typer.Exit(code=1) from e

            try:
                resolved = resolve_required_packages(package, effective_pacman)
                target_deps = get_package_dependencies(package, effective_pacman)
            except (PacmanError, VersionError) as e:
                print(f"Error: {e}")
                raise typer.Exit(code=1) from e

            if config.time_sync.enabled and config.time_sync.policy == "strict":
                unmapped = [
                    d for d in resolved if d.repo not in config.time_sync.snapshot_servers
                ]
                if unmapped:
                    _print_unmapped_block(package, unmapped, config.time_sync.snapshot_servers)
                    raise typer.Exit(code=1)

            required = [d.filename for d in resolved]

            try:
                _check_for_conflicts(current_state, package, target_deps)
            except InstallConflictError as e:
                print(f"Error: {e}")
                raise typer.Exit(code=1) from e

            # Pre-flight ABI Gatekeeper: classify before download/build so we
            # never spend bandwidth on a transaction we are going to reject.
            try:
                host_packages, host_provided = _gather_host_state(required)
                gate_report = abi_gate.classify(
                    resolved_filenames=required,
                    target_pkg=package,
                    host_packages=host_packages,
                    host_provided=host_provided,
                )
            except (PacmanError, VersionError) as e:
                print(f"Error: {e}")
                raise typer.Exit(code=1) from e
            if gate_report.shadows:
                _print_gate_shadows(gate_report.shadows)
            if gate_report.blocks:
                _print_gate_block(package, gate_report.blocks, overridden=allow_host_abi_mismatch)
                if not allow_host_abi_mismatch:
                    raise typer.Exit(code=1)

            try:
                download_package(package, effective_pacman)
            except PacmanError as e:
                print(f"Error: {e}")
                raise typer.Exit(code=1) from e

            cached_names = {f.name for f in effective_pacman.cachedir.glob("*.pkg.tar.zst")}
            required_set = set(required)
            missing = required_set - cached_names
            if missing:
                print(f"Error: missing packages in cache: {sorted(missing)}")
                raise typer.Exit(code=1)

            plan = _make_build_plan(current_state, gate_report, config)
            _print_plan(plan)

            if not plan.to_build:
                print("\nNothing to build - all deps are already satisfied.")
                target_version = _resolve_target_version(package, plan)
                _record_user_request(current_state, package, target_version, target_deps)
                try:
                    state.save(current_state, config.state_db)
                except OSError as e:
                    print(f"Error saving state: {e}")
                    raise typer.Exit(code=1) from e
                reused_outputs = _collect_reused_outputs(
                    current_state, plan.reused, config.builder.output_dir
                )
                _maybe_activate(reused_outputs, config, assume_yes=assume_yes)
                return

            print(f"\nWill build {len(plan.to_build)} sysext(s):")
            for pkg in plan.to_build:
                print(f"  - {pkg}")

            if not _confirm("Proceed with build?", assume_yes=assume_yes):
                print("Aborted by user")
                return

            snapshot_id = state.intern_snapshot(current_state, current_snapshot)

            try:
                for pkg_filename in plan.to_build:
                    pkg_path = effective_pacman.cachedir / pkg_filename
                    print(f"Building sysext from {pkg_filename}...")
                    output = build_sysext(
                        pkg_path,
                        config.builder.output_dir,
                        fs_format=config.builder.fs_format,
                    )
                    build_outputs.append(output)
                    print(f"  ✓ {output.name}")

                    name, version_str = parse_pkg_filename(pkg_path)
                    sha = state.compute_file_sha256(output)
                    try:
                        provides = get_package_provides(name, effective_pacman)
                    except PacmanError as e:
                        logger.warning(
                            "Could not query provides for %s: %s; recording empty", name, e
                        )
                        provides = {}
                    try:
                        depends = [
                            c.name for c in get_package_dependencies(name, effective_pacman)
                        ]
                    except PacmanError as e:
                        logger.warning(
                            "Could not query depends for %s: %s; recording empty", name, e
                        )
                        depends = []

                    state.add_sysext(
                        current_state,
                        state.SysextRecord(
                            name=name,
                            version=version_str,
                            raw_filename=output.name,
                            fs_format=config.builder.fs_format,
                            sha256=sha,
                            installed_at=datetime.now(UTC),
                            snapshot_id=snapshot_id,
                            provides=provides,
                            depends=depends,
                        ),
                    )

                target_version = _resolve_target_version(package, plan)
                _record_user_request(current_state, package, target_version, target_deps)
                state.save(current_state, config.state_db)
            except BuildError as e:
                _cleanup_outputs(build_outputs)
                print(f"  ✗ Error: {e}")
                raise typer.Exit(code=1) from e
            except OSError as e:
                _cleanup_outputs(build_outputs)
                print(f"Error saving state: {e}")
                raise typer.Exit(code=1) from e

            print(f"\nBuilt {len(build_outputs)} sysext(s) in {config.builder.output_dir}")
            reused_outputs = _collect_reused_outputs(
                current_state, plan.reused, config.builder.output_dir
            )
    except state.StateError as e:
        print(f"Error reading state: {e}")
        raise typer.Exit(code=1) from e

    _maybe_activate(build_outputs + reused_outputs, config, assume_yes=assume_yes)


def _maybe_activate(outputs: list[Path], config: AppConfig, assume_yes: bool = False) -> None:
    """Symlink and merge sysexts. Called outside the state lock.

    Known crash-recovery gaps:

    * SIGKILL between `state.save()` and `activate_all()` leaves state
      recording the sysexts as installed while systemd has not yet merged
      them. Recovery: re-run install (idempotent reuse path) or
      `sudo systemd-sysext refresh && sudo systemd-tmpfiles --create`.
    * SIGKILL inside `activate_all()` between the `merge`/`refresh` call
      and the trailing `apply_tmpfiles` is the more painful variant: the
      sysext is live, /usr is overlaid, but /etc and /var content has not
      been materialised. Programs may start without their configuration.
      Recovery is the same `systemd-tmpfiles --create` command above; the
      tmpfiles directives we ship use `C`/`L` for /etc and so are safe to
      re-apply over an admin-edited host. A future plan may auto-run this
      pair on command startup.
    """
    if not outputs:
        return
    print(f"\nWill link {len(outputs)} sysext(s) to {config.sysext.extensions_dir}, refresh,")
    print("and apply tmpfiles (materialises /etc and /var content from each sysext):")
    for raw in outputs:
        print(f"  - {raw.name}")

    if not _confirm("Activate now?", assume_yes=assume_yes):
        print(
            "Built but not activated. Run `sudo systemd-sysext refresh && "
            "sudo systemd-tmpfiles --create` after linking manually."
        )
        return

    try:
        activate_all(outputs, config.sysext.extensions_dir)
    except SysextError as e:
        print(f"  ✗ Activation failed: {e}")
        raise typer.Exit(code=1) from e

    print(f"Activated {len(outputs)} sysext(s).")
