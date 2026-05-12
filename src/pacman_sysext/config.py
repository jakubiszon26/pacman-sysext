"""Application configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args, get_type_hints

if TYPE_CHECKING:
    from pacman_sysext.time_sync import TimeSyncConfig

FsFormat = Literal["erofs", "squashfs"]
_VALID_FS_FORMATS = frozenset(get_args(FsFormat))

_DEFAULT_CONFIG_PATH = Path("/etc/pacman-sysext/config.toml")
_KNOWN_TOP_LEVEL = frozenset({"pacman", "builder", "sysext", "state_db", "time_sync"})


class ConfigError(Exception):
    """Configuration file is malformed."""


@dataclass(frozen=True)
class PacmanConfig:
    """Sandbox configuration for pacman."""

    dbpath: Path
    cachedir: Path
    config_file: Path
    gpgdir: Path

    def to_args(self) -> list[str]:
        return [
            "--dbpath",
            str(self.dbpath),
            "--cachedir",
            str(self.cachedir),
            "--config",
            str(self.config_file),
            "--gpgdir",
            str(self.gpgdir),
        ]


@dataclass(frozen=True)
class BuilderConfig:
    """.raw builder configuration."""

    output_dir: Path
    fs_format: FsFormat = "squashfs"

    def __post_init__(self) -> None:
        if self.fs_format not in _VALID_FS_FORMATS:
            raise ConfigError(
                f"Invalid fs_format: {self.fs_format!r}. "
                f"Must be one of: {sorted(_VALID_FS_FORMATS)}"
            )


@dataclass(frozen=True)
class SysextConfig:
    """systemd-sysext configuration."""

    extensions_dir: Path = Path("/var/lib/extensions")
    use_refresh: bool = True

    def __post_init__(self) -> None:
        # `isinstance(1, bool)` is False, so this rejects `use_refresh = 1`
        # in TOML — bool/int conflation would otherwise pass silently.
        if not isinstance(self.use_refresh, bool):
            raise ConfigError(f"Invalid use_refresh: {self.use_refresh!r}. Expected boolean.")


@dataclass(frozen=True)
class AppConfig:
    """Application configuration, loaded from file or defaults."""

    pacman: PacmanConfig
    builder: BuilderConfig
    sysext: SysextConfig
    state_db: Path
    time_sync: TimeSyncConfig

    @classmethod
    def default(cls) -> AppConfig:
        """Default configuration using FHS-style paths under /var/lib/pacman-sysext."""
        from pacman_sysext.time_sync import TimeSyncConfig

        base = Path("/var/lib/pacman-sysext")
        return cls(
            pacman=PacmanConfig(
                dbpath=base / "db",
                cachedir=base / "cache",
                config_file=Path("/etc/pacman.conf"),
                gpgdir=Path("/etc/pacman.d/gnupg"),
            ),
            builder=BuilderConfig(output_dir=base / "sysexts"),
            sysext=SysextConfig(),
            state_db=base / "state.db",
            time_sync=TimeSyncConfig(),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        """Load config from TOML, falling back to defaults.

        Missing keys in the file inherit from `default()`. An explicitly
        passed `path` that does not exist is an error; the implicit
        `/etc/pacman-sysext/config.toml` being absent is not.
        """
        explicit = path is not None
        if path is None:
            path = _DEFAULT_CONFIG_PATH

        if not path.exists():
            if explicit:
                raise ConfigError(f"Config file not found: {path}")
            return cls.default()

        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in {path}: {e}") from e

        return cls.default()._merge(data)

    def _merge(self, data: dict[str, Any]) -> AppConfig:
        unknown = set(data) - _KNOWN_TOP_LEVEL
        if unknown:
            raise ConfigError(
                f"Unknown top-level keys: {sorted(unknown)}. Known: {sorted(_KNOWN_TOP_LEVEL)}"
            )
        state_db = self.state_db
        if "state_db" in data:
            state_db = _coerce_path("state_db", data["state_db"])
        return replace(
            self,
            pacman=_merge_dataclass("pacman", self.pacman, data.get("pacman")),
            builder=_merge_dataclass("builder", self.builder, data.get("builder")),
            sysext=_merge_dataclass("sysext", self.sysext, data.get("sysext")),
            state_db=state_db,
            time_sync=_merge_time_sync(self.time_sync, data.get("time_sync")),
        )


def _path_field_names(cls: type) -> frozenset[str]:
    """Names of dataclass fields whose annotated type is exactly `Path`."""
    return frozenset(name for name, t in get_type_hints(cls).items() if t is Path)


_PATH_FIELDS: dict[type, frozenset[str]] = {
    PacmanConfig: _path_field_names(PacmanConfig),
    BuilderConfig: _path_field_names(BuilderConfig),
    SysextConfig: _path_field_names(SysextConfig),
}


def _coerce_path(qualified_name: str, value: Any) -> Path:
    try:
        return Path(value).expanduser()
    except TypeError as e:
        raise ConfigError(
            f"Invalid value for {qualified_name}: expected path string, got {type(value).__name__}"
        ) from e


_TIME_SYNC_KNOWN_KEYS = frozenset({"enabled", "date", "policy", "snapshot_servers"})


def _merge_time_sync(
    current: TimeSyncConfig, overrides: dict[str, Any] | None
) -> TimeSyncConfig:
    """Apply `[time_sync]` overrides, coercing `date` and validating subtable shape."""
    from pacman_sysext.time_sync import TimeSyncError

    if not overrides:
        return current
    unknown = set(overrides) - _TIME_SYNC_KNOWN_KEYS
    if unknown:
        raise ConfigError(
            f"Unknown key in [time_sync]: {sorted(unknown)}. Known: {sorted(_TIME_SYNC_KNOWN_KEYS)}"
        )
    kwargs: dict[str, Any] = {}
    if "enabled" in overrides:
        kwargs["enabled"] = overrides["enabled"]
    if "policy" in overrides:
        kwargs["policy"] = overrides["policy"]
    if "date" in overrides:
        kwargs["date"] = _coerce_date(overrides["date"])
    if "snapshot_servers" in overrides:
        servers = overrides["snapshot_servers"]
        if not isinstance(servers, dict):
            raise ConfigError(
                "[time_sync.snapshot_servers] must be a table of repo -> template, "
                f"got {type(servers).__name__}"
            )
        for repo, template in servers.items():
            if not isinstance(repo, str) or not isinstance(template, str):
                raise ConfigError(
                    "[time_sync.snapshot_servers] entries must be string -> string"
                )
        kwargs["snapshot_servers"] = dict(servers)
    try:
        return replace(current, **kwargs)
    except TimeSyncError as e:
        raise ConfigError(f"invalid [time_sync]: {e}") from e
    except TypeError as e:
        raise ConfigError(f"invalid [time_sync]: {e}") from e


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as e:
            raise ConfigError(f"invalid time_sync.date: {value!r}: {e}") from e
    raise ConfigError(
        f"time_sync.date must be a date or ISO string, got {type(value).__name__}"
    )


def _merge_dataclass[T](section: str, current: T, overrides: dict[str, Any] | None) -> T:
    """Apply TOML overrides to a frozen dataclass, coercing path fields to Path."""
    if not overrides:
        return current
    path_fields = _PATH_FIELDS[type(current)]
    coerced: dict[str, Any] = {}
    for key, value in overrides.items():
        coerced[key] = _coerce_path(f"[{section}].{key}", value) if key in path_fields else value
    try:
        # `replace` is typed via a Protocol that an unbound TypeVar can't satisfy;
        # all call sites pass a real frozen dataclass, so the runtime is sound.
        return replace(current, **coerced)  # type: ignore[type-var]
    except TypeError as e:
        raise ConfigError(f"Unknown key in [{section}]: {e}") from e
