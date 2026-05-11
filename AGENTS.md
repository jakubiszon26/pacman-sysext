# AGENTS.md

Guidance for AI coding agents working on `pacman-sysext`. Keep it short, follow it.

## What this project is

A CLI that converts pacman packages into [systemd-sysext](https://www.freedesktop.org/software/systemd/man/latest/systemd-sysext.html) images (`.raw`) and activates them via `/var/lib/extensions`. Target audience: Arch / CachyOS / EndeavourOS users who want immutable-style add-ons without mutating `/usr`.

## Stack

- Python **3.13+**
- [uv](https://docs.astral.sh/uv/) for env, build, deps — `uv sync`, `uv run`, `uv build`
- [Typer](https://typer.tiangolo.com/) for the CLI (sub-apps + shared context via `ctx.obj`)
- Stdlib for everything else (subprocess, pathlib, tarfile, platform)

Don't add runtime dependencies without strong justification. Stdlib first.

## Layout

```
src/pacman_sysext/
  cli.py         # Typer entry point, top-level commands
  config.py      # frozen dataclasses, AppConfig.load()
  pacman.py      # wrappers around the `pacman` binary (sandboxed --dbpath)
  builder.py     # build .raw image from .pkg.tar.zst
  sysext.py      # symlink + `systemd-sysext` merge/refresh
  commands/      # one module per CLI command, each exposes `run(...)`
tests/           # mirrors src/ layout
```

Domain logic lives in the top-level modules. Files in `commands/` are thin glue between CLI args and domain calls — no business logic there.

## Code style

- **English everywhere** — code, comments, docstrings, log messages, commits, PR text. No mixed-language leftovers.
- Type hints on every public function and method. Use `Path | None`, `list[str]`, etc. — no `Optional`/`List` from `typing`.
- Always `pathlib.Path` for filesystem paths, never raw strings.
- `subprocess.run(..., capture_output=True, text=True)`; check `returncode` and raise a domain exception (`PacmanError`, `BuildError`, …). Never let `CalledProcessError` cross a module boundary.
- One custom exception class per module is enough.
- Pass config in (`config: AppConfig`). Don't read globals or env vars from helpers.
- `logging.getLogger(__name__)` for library code. `print()` is only allowed in `commands/` for user-facing output.
- Private helpers prefixed with `_`.

### Comments and docstrings

- Public API: short docstring (1–3 lines), with `Args:`/`Returns:` only when the signature isn't self-evident.
- Write a comment only when **why** is non-obvious — a workaround, a hidden constraint, a surprising invariant. If removing the comment wouldn't confuse a future reader, don't write it.
- Don't restate the code. Don't reference task/PR/issue numbers in comments — that lives in the commit and PR description.

## Tooling

```bash
uv run ruff check          # lint
uv run ruff format         # format
uv run mypy src            # type-check
uv run pytest              # tests
```

All four must pass before a change can be merged. Configure in `pyproject.toml`.

## Testing

- `pytest`. Unit tests only, with mocks; tests run as a normal user, never as root.
- Mock `subprocess.run` and filesystem boundaries. Use the `tmp_path` fixture for real, throwaway files.
- Don't shell out to real `pacman` / `systemd-sysext` / `tar` in tests.
  - **Exception:** `tests/test_version.py` calls the real `vercmp` binary. It is hermetic (pure function of args), tiny, stable, and part of pacman; reimplementing its semantics in mocks defeats the point of the module.
- Integration testing on real systems is the maintainer's job, not CI's.
- Tests live in `tests/` and mirror the `src/pacman_sysext/` layout (`tests/test_builder.py`, etc.).

## CLI conventions (Typer)

- Shared state flows through `ctx.obj` (currently `AppConfig`). No module-level singletons.
- A new top-level command: register it in `cli.py`, implement `run(...)` in `commands/<name>.py`.
- User-facing output: `typer.echo()` or `print()` in command modules. To exit non-zero, `raise typer.Exit(code=1)` — don't `sys.exit`.

## Scope

In scope:
- Arch-based distros (Arch, CachyOS, EndeavourOS, Manjaro)
- Official + third-party pacman repos
- AUR via helpers (`paru`, `yay`) when relevant

Out of scope:
- Non-pacman distros
- Building packages from source
- Replacing or vendoring pacman itself

## Domain gotchas

These have already cost real time. Read them.

- The tool runs pacman with a sandboxed `--dbpath` / `--cachedir`. That sandbox has no view of installed packages, so `pacman -Sw --print` returns *all* dependencies. Filter against the host with `pacman -T` — it respects `provides` (e.g. `zlib-ng-compat` provides `zlib`, so plain name comparison misses it).
- The `extension-release` filename **must** equal the image basename (`htop-3.5.1-1.1`, no `.raw`). Otherwise `systemd-sysext merge`/`refresh` fails with "No medium found".
- `ID=` and `ARCHITECTURE=` in `extension-release` come from the host's `/etc/os-release` (use `platform.freedesktop_os_release()`) and `uname -m`. systemd's identifier is `x86-64` with a hyphen, not `x86_64`.
- Most paths require root (`/var/lib/pacman-sysext`, `/var/lib/extensions`, `systemd-sysext`). Tests must not require it.
- Python 3.13 `tarfile` does not support zstd — shell out to system `tar --zstd`. (Native zstd lands in 3.14.)

## Git and pull requests

- Conventional Commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
- Imperative, lowercase subject, ≤72 chars. Body explains *why*, not *what*.
- One concern per PR. Don't bundle a refactor with a behavior change.
- No "Generated with…" / AI-assistant trailers in commit messages.
- Default branch for PRs is `main`.

## Don't

- Don't add comments or docstrings that restate the code.
- Don't broadly catch `Exception` to silence a failing test — fix the root cause.
- Don't bypass `pacman.py` / `sysext.py`. Go through the wrappers so error handling stays consistent.
- Don't add runtime deps that duplicate stdlib (e.g. `requests` over `urllib`, `attrs` over `dataclasses`).
- Don't introduce module-level mutable state.
- Don't read or write outside the paths declared in `AppConfig`.
