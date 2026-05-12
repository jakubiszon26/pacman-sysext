# pacman-sysext

Install pacman packages as [systemd-sysext](https://www.freedesktop.org/software/systemd/man/latest/systemd-sysext.html)
images on Arch-based distributions (Arch, CachyOS, EndeavourOS, Manjaro).

Each package and its dependencies become a read-only `.raw` image mounted on top
of `/usr` — `/usr` itself stays untouched, and any sysext can be removed simply
by deleting its image and refreshing.

## Status

Alpha. The `install` and `status` commands are functional. `remove`, `list`,
and `rebuild` are not implemented yet and will raise `NotImplementedError`.
The tool tracks state in a JSON file (default `/var/lib/pacman-sysext/state.db`),
including per-sysext SHA-256 integrity, dependency constraints, content-hashed
base snapshots, and ABI-drift warnings on subsequent installs. Packages that
ship `/etc` or `/var` are translated into tmpfiles recipes so their content is
materialized on the host after activation.

## Features

- Build systemd-sysext images from pacman packages and their dependencies
  (squashfs or erofs).
- Sandboxed pacman db/cache for sync and downloads; host `pacman -T` is used to
  skip dependencies already provided by the base system.
- Activation via `systemd-sysext refresh` when available, with merge fallback.
- Post-activation hooks run in order: `systemctl daemon-reload`,
  `systemd-sysusers`, `systemd-tmpfiles --create`.
- `/etc` and `/var` payloads are moved into the image and materialized via
  generated tmpfiles.d recipes.
- State tracking with locking, integrity checks, dependency metadata, and base
  ABI snapshots.

## Commands

- `pacman-sysext install <package>` - build and activate sysexts for a target
  package and its dependencies.
- `pacman-sysext status` - audit sysext integrity and show explicit/implicit
  packages, orphans, and disk usage.
- `pacman-sysext remove <package>` - not implemented yet.
- `pacman-sysext list` - not implemented yet.
- `pacman-sysext rebuild` - not implemented yet.

## Requirements

- Python ≥ 3.13
- Arch-based distro with `pacman`
- `systemd` ≥ 254 (256+ recommended for atomic `refresh`)
- `squashfs-tools` (default) or `erofs-utils`
- `tar` with zstd support

## Configuration

Optional TOML at `/etc/pacman-sysext/config.toml`:

```toml
state_db = "/var/lib/pacman-sysext/state.db"

[pacman]
dbpath = "/var/lib/pacman-sysext/db"
cachedir = "/var/lib/pacman-sysext/cache"
config_file = "/etc/pacman.conf"
gpgdir = "/etc/pacman.d/gnupg"

[builder]
output_dir = "/var/lib/pacman-sysext/sysexts"
staging_dir = "/tmp/pacman-sysext-staging"
fs_format = "squashfs"   # or "erofs"

[sysext]
extensions_dir = "/var/lib/extensions"
use_refresh = true
```

Missing keys inherit from defaults — you only need to override what you change.

## 🤖 AI Transparency Disclosure

In the spirit of full transparency, Artificial Intelligence tools (such as LLMs and coding assistants) were used collaboratively during the development of this project.

The AI was actively utilized for:

Parts of the work were completed autonomously by AI agents and then reviewed through a mix of human checks and verification by other AI models, with a strong emphasis on security and testing.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
