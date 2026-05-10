"""Install a pacman package as a sysext image and activate it."""

import typer

from pacman_sysext.builder import BuildError, build_sysext
from pacman_sysext.config import AppConfig
from pacman_sysext.pacman import (
    PacmanError,
    download_package,
    find_unsatisfied,
    get_required_packages,
    parse_pkg_filename,
    sync_databases,
)
from pacman_sysext.sysext import SysextError, activate_all


def _confirm(prompt: str) -> bool:
    while True:
        answer = input(f"\n{prompt} [y/n]: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please enter 'y' or 'n'.")


def run(package: str, config: AppConfig) -> None:
    print(f"Installing sysext ({package})...")
    try:
        sync_databases(config.pacman)
        required = get_required_packages(package, config.pacman)
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

    # Filter out packages already provided by the host (respects `provides`).
    parsed = [(parse_pkg_filename(f)[0], f) for f in required_set]
    unsatisfied = find_unsatisfied([name for name, _ in parsed])

    to_build = [f for name, f in parsed if name in unsatisfied]
    skipped = [name for name, _ in parsed if name not in unsatisfied]

    if skipped:
        print(f"Skipping {len(skipped)} packages already in base system:")
        for name in skipped:
            print(f"  - {name}")

    if not to_build:
        print("Nothing to build - all required packages are already in base system")
        return

    print(f"\nWill build {len(to_build)} sysext(s):")
    for pkg in to_build:
        print(f"  - {pkg}")

    if not _confirm("Proceed with build?"):
        print("Aborted by user")
        return

    outputs = []
    for pkg_filename in to_build:
        pkg_path = config.pacman.cachedir / pkg_filename
        print(f"Building sysext from {pkg_filename}...")
        try:
            output = build_sysext(
                pkg_path,
                config.builder.output_dir,
                fs_format=config.builder.fs_format,
            )
        except BuildError as e:
            print(f"  ✗ Error: {e}")
            raise typer.Exit(code=1) from e
        outputs.append(output)
        print(f"  ✓ {output.name}")

    print(f"\nBuilt {len(outputs)} sysext(s) in {config.builder.output_dir}")

    print(f"\nWill link {len(outputs)} sysext(s) to {config.sysext.extensions_dir} and refresh:")
    for raw in outputs:
        print(f"  - {raw.name}")

    if not _confirm("Activate now?"):
        print("Built but not activated. Run `sudo systemd-sysext refresh` after linking manually.")
        return

    try:
        activate_all(outputs, config.sysext.extensions_dir)
    except SysextError as e:
        print(f"  ✗ Activation failed: {e}")
        raise typer.Exit(code=1) from e

    print(f"Activated {len(outputs)} sysext(s).")
