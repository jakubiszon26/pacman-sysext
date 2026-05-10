# pacman-sysext

Install pacman packages as [systemd-sysext](https://www.freedesktop.org/software/systemd/man/latest/systemd-sysext.html)
images on Arch-based distributions (Arch, CachyOS, EndeavourOS, Manjaro).

Each package and its dependencies become a read-only `.raw` image mounted on top
of `/usr` — `/usr` itself stays untouched, and any sysext can be removed simply
by deleting its image and refreshing.

## Status

Early alpha. The `install` command works partialy; `remove` and `list` are
stubs that raise `NotImplementedError`. Only the squashfs and erofs backends
are wired up, and packages that ship `/etc` or `/var` are dropped with a
warning until the systemd factory pattern is implemented.

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

- **Code Generation:** Scaffolding components, writing specific functions, and reducing boilerplate.
- **Architecture & Design:** Brainstorming structural decisions and evaluating design patterns.
- **Debugging & Refactoring:** Spotting logical errors, suggesting optimizations, and improving overall code readability.

**Verification and Testing Workflow**
While AI provided significant assistance, it did not operate autonomously. To maintain high security and code quality standards, a strict review process was enforced:

- **Manual Review:** Every single AI suggestion was read, understood, and manually evaluated before being integrated into the codebase.

- **Testing:** AI-generated logic was rigorously validated through testing. This ensures that the generated functions actually work as intended, handle edge cases, and do not introduce silent regressions or vulnerabilities.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
