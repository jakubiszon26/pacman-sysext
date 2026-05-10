from pathlib import Path

import pytest

from pacman_sysext.config import AppConfig, ConfigError


class TestAppConfigDefault:
    def test_default_paths(self) -> None:
        cfg = AppConfig.default()
        assert cfg.pacman.dbpath == Path("/var/lib/pacman-sysext/db")
        assert cfg.pacman.config_file == Path("/etc/pacman.conf")
        assert cfg.builder.fs_format == "squashfs"
        assert cfg.sysext.extensions_dir == Path("/var/lib/extensions")

    def test_pacman_to_args(self) -> None:
        cfg = AppConfig.default()
        args = cfg.pacman.to_args()
        assert "--dbpath" in args
        assert "--cachedir" in args
        assert "--config" in args
        assert "--gpgdir" in args


class TestAppConfigLoad:
    def test_implicit_missing_returns_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("pacman_sysext.config._DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")
        cfg = AppConfig.load()
        assert cfg == AppConfig.default()

    def test_explicit_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            AppConfig.load(tmp_path / "nope.toml")

    def test_overrides_merged(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[builder]\n"
            'fs_format = "erofs"\n'
            'output_dir = "/custom/sysexts"\n'
            "\n"
            "[sysext]\n"
            "use_refresh = false\n"
        )
        cfg = AppConfig.load(config_file)
        assert cfg.builder.fs_format == "erofs"
        assert cfg.builder.output_dir == Path("/custom/sysexts")
        assert cfg.sysext.use_refresh is False
        # Untouched values inherit from defaults.
        assert cfg.pacman.config_file == Path("/etc/pacman.conf")

    def test_invalid_toml_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.toml"
        config_file.write_text("not = valid = toml")
        with pytest.raises(ConfigError, match="Invalid TOML"):
            AppConfig.load(config_file)

    def test_invalid_fs_format_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text('[builder]\nfs_format = "ext4"\n')
        with pytest.raises(ConfigError, match="fs_format"):
            AppConfig.load(config_file)

    def test_unknown_top_level_section_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text('[builders]\nfs_format = "erofs"\n')
        with pytest.raises(ConfigError, match="Unknown top-level"):
            AppConfig.load(config_file)

    def test_unknown_key_in_section_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text('[builder]\nbogus = "value"\n')
        with pytest.raises(ConfigError, match="Unknown key in \\[builder\\]"):
            AppConfig.load(config_file)

    def test_wrong_type_for_path_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[pacman]\ndbpath = 42\n")
        with pytest.raises(ConfigError, match=r"\[pacman\]\.dbpath"):
            AppConfig.load(config_file)

    def test_wrong_type_for_state_db_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("state_db = 42\n")
        with pytest.raises(ConfigError, match="state_db"):
            AppConfig.load(config_file)

    def test_use_refresh_must_be_bool(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text('[sysext]\nuse_refresh = "no"\n')
        with pytest.raises(ConfigError, match="use_refresh"):
            AppConfig.load(config_file)

    def test_use_refresh_int_rejected(self, tmp_path: Path) -> None:
        # TOML has no native distinction here, but Python's bool/int conflation
        # would otherwise let `use_refresh = 1` through silently.
        config_file = tmp_path / "config.toml"
        config_file.write_text("[sysext]\nuse_refresh = 1\n")
        with pytest.raises(ConfigError, match="use_refresh"):
            AppConfig.load(config_file)

    def test_tilde_in_path_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        config_file = tmp_path / "config.toml"
        # state_db must come before any section header (TOML scoping).
        config_file.write_text('state_db = "~/state.db"\n\n[builder]\noutput_dir = "~/sysexts"\n')
        cfg = AppConfig.load(config_file)
        assert cfg.builder.output_dir == tmp_path / "sysexts"
        assert cfg.state_db == tmp_path / "state.db"

    def test_extensions_dir_override(self, tmp_path: Path) -> None:
        # Regression guard: extensions_dir is the only Path on SysextConfig,
        # ensures the auto-derived path-field set picks it up.
        config_file = tmp_path / "config.toml"
        config_file.write_text('[sysext]\nextensions_dir = "/custom/ext"\n')
        cfg = AppConfig.load(config_file)
        assert cfg.sysext.extensions_dir == Path("/custom/ext")
