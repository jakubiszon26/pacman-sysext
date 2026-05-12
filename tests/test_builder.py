import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pacman_sysext.builder import (
    BuildError,
    _clean_arch_metadata,
    _escape_tmpfiles_field,
    _extract_package,
    _make_image,
    _strip_unsupported_dirs,
    _systemd_arch,
    _tmpfiles_line,
    _translate_etc_and_var,
    _validate_archive_members,
    sanitize_image_name,
)


class TestSystemdArch:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            ("x86_64", "x86-64"),
            ("aarch64", "arm64"),
            ("i686", "x86"),
            ("riscv64", "riscv64"),  # passthrough for unmapped values
        ],
    )
    def test_arch_mapping(self, machine: str, expected: str) -> None:
        with patch("pacman_sysext.builder.platform.machine", return_value=machine):
            assert _systemd_arch() == expected


class TestCleanArchMetadata:
    def test_removes_known_files(self, tmp_path: Path) -> None:
        for name in (".PKGINFO", ".MTREE", ".BUILDINFO"):
            (tmp_path / name).write_text("dummy")
        (tmp_path / "usr").mkdir()

        _clean_arch_metadata(tmp_path)

        assert not (tmp_path / ".PKGINFO").exists()
        assert not (tmp_path / ".MTREE").exists()
        assert (tmp_path / "usr").exists()

    def test_missing_files_ok(self, tmp_path: Path) -> None:
        _clean_arch_metadata(tmp_path)


class TestStripUnsupportedDirs:
    def test_keeps_usr_and_opt_drops_strays(self, tmp_path: Path) -> None:
        (tmp_path / "usr").mkdir()
        (tmp_path / "opt").mkdir()
        (tmp_path / "stray.txt").write_text("x")
        (tmp_path / "stray-dir").mkdir()

        _strip_unsupported_dirs(tmp_path, "pkg-1.0-1")

        assert (tmp_path / "usr").exists()
        assert (tmp_path / "opt").exists()
        assert not (tmp_path / "stray.txt").exists()
        assert not (tmp_path / "stray-dir").exists()

    def test_etc_and_var_are_translator_responsibility(self, tmp_path: Path) -> None:
        # _translate_etc_and_var runs before _strip_unsupported_dirs in the
        # real pipeline; by the time strip runs, /etc and /var are gone.
        # If they happened to still exist (someone reordered the pipeline),
        # strip would NOT silently drop them — but it also doesn't single
        # them out anymore. Document the contract by exercising the
        # post-translator state.
        (tmp_path / "usr").mkdir()
        _strip_unsupported_dirs(tmp_path, "pkg-1.0-1")
        assert (tmp_path / "usr").exists()


class TestEscapeTmpfilesField:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("/etc/htop", "/etc/htop"),
            ("/etc/with space", "/etc/with\\sspace"),
            ("/etc/back\\slash", "/etc/back\\\\slash"),
            ("/etc/tab\there", "/etc/tab\\there"),
            ("/etc/newline\n", "/etc/newline\\n"),
            ("/etc/cr\r", "/etc/cr\\r"),
            ("-", "-"),  # default marker passes through untouched
        ],
    )
    def test_escapes(self, raw: str, expected: str) -> None:
        assert _escape_tmpfiles_field(raw) == expected


class TestTmpfilesLine:
    def test_minimal_directory(self) -> None:
        assert (
            _tmpfiles_line("d", "/var/lib/foo", "0755", "0", "0") == "d /var/lib/foo 0755 0 0 - -"
        )

    def test_copy_with_source(self) -> None:
        assert (
            _tmpfiles_line(
                "C", "/etc/foo.conf", "0644", "0", "0", "-", "/usr/share/skel/etc/foo.conf"
            )
            == "C /etc/foo.conf 0644 0 0 - /usr/share/skel/etc/foo.conf"
        )

    def test_symlink(self) -> None:
        assert (
            _tmpfiles_line("L+", "/etc/foo", "-", "-", "-", "-", "/usr/share/foo")
            == "L+ /etc/foo - - - - /usr/share/foo"
        )

    def test_path_with_space_escaped_but_dash_preserved(self) -> None:
        # Mode/User/Group "-" must not get mangled into "\s" or similar.
        result = _tmpfiles_line("C", "/etc/a b", "-", "-", "-", "-", "/usr/share/a b")
        assert result == "C /etc/a\\sb - - - - /usr/share/a\\sb"


class TestTranslateEtcAndVar:
    def test_emits_directives_and_relocates_files(self, tmp_path: Path) -> None:
        (tmp_path / "etc" / "htop").mkdir(parents=True)
        cfg = tmp_path / "etc" / "htop" / "htoprc"
        cfg.write_text("color=blue\n")
        cfg.chmod(0o644)
        (tmp_path / "var" / "lib" / "myapp").mkdir(parents=True)

        _translate_etc_and_var(tmp_path, "pkg-1.0-1")

        # Source /etc and /var stripped from staging
        assert not (tmp_path / "etc").exists()
        assert not (tmp_path / "var").exists()

        # File relocated under the image-specific skel root
        relocated = tmp_path / "usr/share/pacman-sysext/skel/pkg-1.0-1/etc/htop/htoprc"
        assert relocated.is_file()
        assert relocated.read_text() == "color=blue\n"

        # Recipe written under /usr/lib/tmpfiles.d/
        recipe = tmp_path / "usr/lib/tmpfiles.d/pacman-sysext-pkg-1.0-1.conf"
        assert recipe.is_file()
        content = recipe.read_text()
        assert content.startswith("# Generated by pacman-sysext for pkg-1.0-1.\n")
        # Directory entry for /etc/htop
        assert any(line.startswith("d /etc/htop ") for line in content.splitlines()), content
        # Copy entry pointing at the relocated source. uid/gid reflect
        # whoever the test runs as — Arch packages would normally show 0/0
        # at build time, but the translator preserves whatever it sees.
        uid = os.getuid()
        gid = os.getgid()
        assert (
            f"C /etc/htop/htoprc 0644 {uid} {gid} - "
            f"/usr/share/pacman-sysext/skel/pkg-1.0-1/etc/htop/htoprc"
        ) in content
        # /var directory entry
        assert any(line.startswith("d /var/lib/myapp ") for line in content.splitlines()), content

    def test_etc_symlinks_emit_L_not_clobbering(self, tmp_path: Path) -> None:
        # /etc is admin-mutable; `L` only creates the symlink when the host
        # path is empty, preserving any custom override the admin made.
        (tmp_path / "etc").mkdir()
        link = tmp_path / "etc" / "foo"
        link.symlink_to("/usr/share/htop/foo")

        _translate_etc_and_var(tmp_path, "pkg-1.0-1")

        recipe = tmp_path / "usr/lib/tmpfiles.d/pacman-sysext-pkg-1.0-1.conf"
        content = recipe.read_text()
        assert "L /etc/foo - - - - /usr/share/htop/foo" in content
        # The symlink itself should not appear in skel — its target is
        # encoded entirely in the directive.
        assert not (tmp_path / "usr/share/pacman-sysext/skel/pkg-1.0-1/etc/foo").exists()
        assert not (tmp_path / "usr/share/pacman-sysext/skel/pkg-1.0-1/etc/foo").is_symlink()

    def test_var_symlinks_emit_L_plus(self, tmp_path: Path) -> None:
        # /var is package-managed state; `L+` is correct so a stale symlink
        # left over from a prior install is overwritten.
        (tmp_path / "var" / "lib").mkdir(parents=True)
        link = tmp_path / "var" / "lib" / "foo"
        link.symlink_to("/usr/share/foo")

        _translate_etc_and_var(tmp_path, "pkg-1.0-1")

        content = (tmp_path / "usr/lib/tmpfiles.d/pacman-sysext-pkg-1.0-1.conf").read_text()
        assert "L+ /var/lib/foo - - - - /usr/share/foo" in content

    def test_no_recipe_when_etc_and_var_absent(self, tmp_path: Path) -> None:
        (tmp_path / "usr").mkdir()

        _translate_etc_and_var(tmp_path, "pkg-1.0-1")

        assert not (tmp_path / "usr/lib/tmpfiles.d").exists()
        assert not (tmp_path / "usr/share/pacman-sysext").exists()

    def test_preserves_mode_and_owner(self, tmp_path: Path) -> None:
        (tmp_path / "etc").mkdir()
        cfg = tmp_path / "etc" / "secret.conf"
        cfg.write_text("token")
        cfg.chmod(0o600)

        _translate_etc_and_var(tmp_path, "pkg-1.0-1")

        recipe_text = (tmp_path / "usr/lib/tmpfiles.d/pacman-sysext-pkg-1.0-1.conf").read_text()
        uid = os.getuid()
        gid = os.getgid()
        assert (
            f"C /etc/secret.conf 0600 {uid} {gid} - "
            f"/usr/share/pacman-sysext/skel/pkg-1.0-1/etc/secret.conf"
        ) in recipe_text

    def test_parent_directories_come_before_children(self, tmp_path: Path) -> None:
        # systemd-tmpfiles auto-creates parents, but a recipe that lists
        # them out-of-order is hard to read. We commit to pre-order.
        (tmp_path / "etc" / "a" / "b").mkdir(parents=True)
        (tmp_path / "etc" / "a" / "b" / "leaf").write_text("x")

        _translate_etc_and_var(tmp_path, "pkg-1.0-1")

        content = (tmp_path / "usr/lib/tmpfiles.d/pacman-sysext-pkg-1.0-1.conf").read_text()
        lines = [line for line in content.splitlines() if not line.startswith("#")]
        # Find the index of each path; parent must come first.
        idx_a = next(i for i, line in enumerate(lines) if " /etc/a " in line)
        idx_b = next(i for i, line in enumerate(lines) if " /etc/a/b " in line)
        idx_leaf = next(i for i, line in enumerate(lines) if line.startswith("C /etc/a/b/leaf "))
        assert idx_a < idx_b < idx_leaf

    def test_fifo_is_skipped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # pacman would never ship a FIFO in /etc, but a hostile or
        # corrupted archive could. `C` source must be a regular file or
        # directory — anything else gets dropped with a warning rather
        # than silently embedded as a non-functional directive.
        (tmp_path / "etc").mkdir()
        fifo = tmp_path / "etc" / "named.pipe"
        os.mkfifo(fifo)

        with caplog.at_level("WARNING"):
            _translate_etc_and_var(tmp_path, "pkg-1.0-1")

        # No recipe should reference the fifo
        recipe = tmp_path / "usr/lib/tmpfiles.d/pacman-sysext-pkg-1.0-1.conf"
        if recipe.exists():
            assert "/etc/named.pipe" not in recipe.read_text()
        # Skel must not contain the fifo either
        assert not (tmp_path / "usr/share/pacman-sysext/skel/pkg-1.0-1/etc/named.pipe").exists()
        assert any("non-regular" in r.message.lower() for r in caplog.records)

    def test_header_is_escaped_defense_in_depth(self, tmp_path: Path) -> None:
        # sanitize_image_name rejects pathological inputs upstream, but the
        # translator escapes the header anyway. We bypass the validator
        # here by calling the translator directly with a payload that
        # would otherwise synthesize a live tmpfiles directive on the
        # line following the comment. Slashes are excluded from the
        # payload so the recipe filename remains a single path component;
        # the escape concern is about the header *body*, not the filename.
        (tmp_path / "etc").mkdir()
        (tmp_path / "etc" / "real.conf").write_text("ok")

        payload = "evil\nINJECTED_DIRECTIVE"
        _translate_etc_and_var(tmp_path, payload)

        recipe_dir = tmp_path / "usr/lib/tmpfiles.d"
        recipes = list(recipe_dir.iterdir())
        assert len(recipes) == 1
        content = recipes[0].read_text()
        # The header must not break into a second line; the embedded
        # newline must materialise as a literal backslash-n inside the
        # comment, leaving the parser on a single # line.
        lines = content.splitlines()
        # First line is the header comment — must contain BOTH the prefix
        # and the escaped payload, all in one line.
        assert lines[0].startswith("# Generated by pacman-sysext for evil\\nINJECTED_DIRECTIVE")
        # No real newline split inside the header.
        assert "INJECTED_DIRECTIVE" not in [line.lstrip("# ").strip() for line in lines[1:]]


class TestSanitizeImageNameValidation:
    @pytest.mark.parametrize(
        "bad_name",
        [
            "foo\n",
            "foo with space",
            "foo\tbar",
            "foo;bar",
            "foo/bar",  # path separator, the most dangerous one
            "foo$bar",
        ],
    )
    def test_rejects_unsafe_chars(self, bad_name: str) -> None:
        with pytest.raises(BuildError, match="whitelist"):
            sanitize_image_name(bad_name, "1-1")

    def test_rejects_unsafe_version(self) -> None:
        with pytest.raises(BuildError, match="whitelist"):
            sanitize_image_name("htop", "3.5\n1-1")

    def test_accepts_real_pacman_names(self) -> None:
        # Smoke test: nothing in the real Arch repo should trip the whitelist.
        for n, v in [
            ("htop", "3.5.1-1"),
            ("gtk+", "2.24.33-3"),
            ("lib32-glibc", "2.39-1"),
            ("lensfun", "1:0.3.4-6.1"),  # epoch → '+'
            ("zlib-ng-compat", "2.1.6-1"),
        ]:
            assert sanitize_image_name(n, v)


class TestMakeImage:
    def test_unsupported_format(self, tmp_path: Path) -> None:
        with pytest.raises(BuildError, match="Unsupported"):
            _make_image(tmp_path, tmp_path / "out.raw", "ext4")  # type: ignore[arg-type]


class TestSanitizeImageName:
    def test_no_special_chars_is_identity(self) -> None:
        assert sanitize_image_name("htop", "3.5.1-1") == "htop-3.5.1-1"

    def test_epoch_colon_replaced_with_plus(self) -> None:
        # `:` is the overlayfs lowerdir separator — must be escaped or
        # `systemd-sysext refresh` fails with "No such file or directory".
        assert sanitize_image_name("lensfun", "1:0.3.4-6.1") == "lensfun-1+0.3.4-6.1"

    def test_comma_replaced_with_underscore(self) -> None:
        # Defensive: `,` is the overlayfs mount option separator.
        assert sanitize_image_name("pkg", "1,0-1") == "pkg-1_0-1"


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


class TestValidateArchiveMembers:
    def test_clean_listing_passes(self, tmp_path: Path) -> None:
        listing = ".PKGINFO\n.MTREE\nusr/\nusr/bin/htop\nusr/share/man/man1/htop.1.gz\n"
        with patch("pacman_sysext.builder.subprocess.run", return_value=_completed(stdout=listing)):
            _validate_archive_members(tmp_path / "fake.pkg.tar.zst")

    @pytest.mark.parametrize(
        "bad_member",
        [
            "../etc/passwd",
            "../../tmp/pwned",
            "usr/bin/../../../etc/cron.d/x",
            "/etc/passwd",
            "/absolute/path",
        ],
    )
    def test_rejects_unsafe_paths(self, tmp_path: Path, bad_member: str) -> None:
        listing = f"usr/bin/htop\n{bad_member}\n"
        with (
            patch("pacman_sysext.builder.subprocess.run", return_value=_completed(stdout=listing)),
            pytest.raises(BuildError, match="unsafe paths"),
        ):
            _validate_archive_members(tmp_path / "fake.pkg.tar.zst")

    def test_null_byte_rejected(self, tmp_path: Path) -> None:
        listing = "usr/bin/htop\nusr/evil\x00.txt\n"
        with (
            patch("pacman_sysext.builder.subprocess.run", return_value=_completed(stdout=listing)),
            pytest.raises(BuildError, match="unsafe paths"),
        ):
            _validate_archive_members(tmp_path / "fake.pkg.tar.zst")

    def test_tar_failure_becomes_build_error(self, tmp_path: Path) -> None:
        err = subprocess.CalledProcessError(returncode=2, cmd=["tar"], stderr="bad magic")
        with (
            patch("pacman_sysext.builder.subprocess.run", side_effect=err),
            pytest.raises(BuildError, match="failed to list members"),
        ):
            _validate_archive_members(tmp_path / "fake.pkg.tar.zst")

    def test_missing_tar_becomes_build_error(self, tmp_path: Path) -> None:
        with (
            patch("pacman_sysext.builder.subprocess.run", side_effect=FileNotFoundError),
            pytest.raises(BuildError, match="tar not found"),
        ):
            _validate_archive_members(tmp_path / "fake.pkg.tar.zst")


class TestExtractPackageValidates:
    def test_validation_runs_before_extraction(self, tmp_path: Path) -> None:
        # tar -tf returns an unsafe path → no second call to tar -xf.
        listing = _completed(stdout="../../etc/passwd\n")
        with patch("pacman_sysext.builder.subprocess.run", return_value=listing) as run_mock:
            with pytest.raises(BuildError, match="unsafe paths"):
                _extract_package(tmp_path / "fake.pkg.tar.zst", tmp_path / "dest")
            # Only the listing call ran; extraction was skipped.
            assert run_mock.call_count == 1
            assert run_mock.call_args.args[0][:2] == ["tar", "-tf"]
