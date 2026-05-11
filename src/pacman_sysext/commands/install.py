"""Install a pacman package as a sysext image and activate it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer

from pacman_sysext import state
from pacman_sysext.builder import BuildError, build_sysext
from pacman_sysext.config import AppConfig
from pacman_sysext.pacman import (
    PacmanError,
    download_package,
    find_unsatisfied,
    get_base_snapshot,
    get_package_dependencies,
    get_package_provides,
    get_package_version,
    get_required_packages,
    parse_pkg_filename,
    sync_databases,
)
from pacman_sysext.sysext import SysextError, activate_all
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
    target_pkg: str,
    required_filenames: list[str],
    config: AppConfig,
) -> BuildPlan:
    """Decide per dep whether to reuse, leave to host, or build."""
    to_build: list[str] = []
    reused: list[tuple[str, str]] = []
    host_provided: list[str] = []
    integrity_failures: list[str] = []

    parsed = [(parse_pkg_filename(f), f) for f in required_filenames]
    host_unsatisfied = find_unsatisfied([name for (name, _), _ in parsed])

    for (name, version), filename in parsed:
        record = state.get_sysext(state_obj, name, version)
        if record is not None:
            if state.verify_sysext_integrity(record, config.builder.output_dir):
                reused.append((name, version))
                continue
            integrity_failures.append(state.sysext_key(name, version))
            to_build.append(filename)
            continue

        # Target stays as a sysext regardless of host: the user explicitly asked
        # for the sysext form. Only non-target deps may be skipped as host-provided.
        if name != target_pkg and name not in host_unsatisfied:
            host_provided.append(name)
            continue

        to_build.append(filename)

    return BuildPlan(
        to_build=to_build,
        reused=reused,
        host_provided=host_provided,
        integrity_failures=integrity_failures,
    )


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


def run(package: str, config: AppConfig, assume_yes: bool = False) -> None:
    print(f"Installing sysext ({package})...")
    build_outputs: list[Path] = []
    reused_outputs: list[Path] = []

    with state.locked(config.state_db):
        try:
            current_state = state.load(config.state_db)
        except state.StateError as e:
            print(f"Error reading state: {e}")
            raise typer.Exit(code=1) from e

        try:
            current_snapshot = get_base_snapshot()
        except PacmanError as e:
            print(f"Error: {e}")
            raise typer.Exit(code=1) from e

        _warn_about_drift(current_state, current_snapshot)

        try:
            sync_databases(config.pacman)
            required = get_required_packages(package, config.pacman)
            target_version = get_package_version(package, config.pacman)
            target_deps = get_package_dependencies(package, config.pacman)
        except (PacmanError, VersionError) as e:
            print(f"Error: {e}")
            raise typer.Exit(code=1) from e

        try:
            _check_for_conflicts(current_state, package, target_deps)
        except InstallConflictError as e:
            print(f"Error: {e}")
            raise typer.Exit(code=1) from e

        try:
            download_package(package, config.pacman)
        except PacmanError as e:
            print(f"Error: {e}")
            raise typer.Exit(code=1) from e

        cached_names = {f.name for f in config.pacman.cachedir.glob("*.pkg.tar.zst")}
        required_set = set(required)
        missing = required_set - cached_names
        if missing:
            print(f"Error: missing packages in cache: {sorted(missing)}")
            raise typer.Exit(code=1)

        plan = _make_build_plan(current_state, package, sorted(required_set), config)
        _print_plan(plan)

        if not plan.to_build:
            print("\nNothing to build - all deps are already satisfied.")
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
                pkg_path = config.pacman.cachedir / pkg_filename
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
                    provides = get_package_provides(name, config.pacman)
                except PacmanError as e:
                    logger.warning("Could not query provides for %s: %s; recording empty", name, e)
                    provides = {}

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
                    ),
                )

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

    _maybe_activate(build_outputs + reused_outputs, config, assume_yes=assume_yes)


def _maybe_activate(outputs: list[Path], config: AppConfig, assume_yes: bool = False) -> None:
    """Symlink and merge sysexts. Called outside the state lock.

    Known limitation: a SIGKILL between `state.save()` and a successful
    activation leaves state recording the sysexts as installed while
    systemd has not yet merged them. The recovery is for the user to
    re-run install (idempotent reuse path) or `sudo systemd-sysext refresh`
    manually. A future plan may auto-refresh on command startup.
    """
    if not outputs:
        return
    print(f"\nWill link {len(outputs)} sysext(s) to {config.sysext.extensions_dir} and refresh:")
    for raw in outputs:
        print(f"  - {raw.name}")

    if not _confirm("Activate now?", assume_yes=assume_yes):
        print("Built but not activated. Run `sudo systemd-sysext refresh` after linking manually.")
        return

    try:
        activate_all(outputs, config.sysext.extensions_dir)
    except SysextError as e:
        print(f"  ✗ Activation failed: {e}")
        raise typer.Exit(code=1) from e

    print(f"Activated {len(outputs)} sysext(s).")
