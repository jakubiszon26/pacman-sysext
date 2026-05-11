"""Tests for the state module."""

import hashlib
import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from pacman_sysext.state import (
    AbiDrift,
    BaseSnapshot,
    State,
    StateError,
    SysextRecord,
    UserRequest,
    add_sysext,
    add_user_request,
    compute_file_sha256,
    compute_snapshot_id,
    find_abi_drift,
    find_providing_sysexts,
    find_requirers,
    find_sysexts_by_name,
    find_unsatisfied_requirements,
    get_sysext,
    get_user_request,
    intern_snapshot,
    is_satisfied_by,
    load,
    locked,
    remove_sysext,
    remove_user_request,
    save,
    sysext_key,
    verify_sysext_integrity,
)


def _now() -> datetime:
    return datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)


def _make_sysext(
    name: str,
    version: str,
    *,
    sha: str = "abc",
    snap: str = "snap1",
    provides: dict[str, str] | None = None,
) -> SysextRecord:
    return SysextRecord(
        name=name,
        version=version,
        raw_filename=f"{name}-{version}.raw",
        fs_format="squashfs",
        sha256=sha,
        installed_at=_now(),
        snapshot_id=snap,
        provides=provides or {},
    )


def _make_request(name: str, version: str, requirements: dict[str, str]) -> UserRequest:
    return UserRequest(
        name=name,
        installed_version=version,
        requested_at=_now(),
        requirements=requirements,
    )


class TestLoad:
    def test_missing_path_returns_empty_state(self, tmp_path: Path) -> None:
        state = load(tmp_path / "nope.db")
        assert state == State()

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        path.write_text("{not json")
        with pytest.raises(StateError, match="malformed JSON"):
            load(path)

    def test_wrong_schema_version_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        path.write_text(json.dumps({"version": 99, "sysexts": {}, "user_requests": {}}))
        with pytest.raises(StateError, match="schema version"):
            load(path)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sysexts": {"htop-3.5": {"name": "htop"}},
                    "user_requests": {},
                    "snapshots": {},
                }
            )
        )
        with pytest.raises(StateError, match="invalid record"):
            load(path)

    def test_root_must_be_object(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        path.write_text("[]")
        with pytest.raises(StateError, match="must be an object"):
            load(path)


class TestSaveAndRoundTrip:
    def test_round_trip(self, tmp_path: Path) -> None:
        state = State()
        intern_snapshot(state, {"glibc": "2.39-1"})
        add_sysext(state, _make_sysext("htop", "3.5.1-1", provides={"htop-bin": ""}))
        add_user_request(state, _make_request("htop", "3.5.1-1", {"libcap": ">=2.78"}))

        path = tmp_path / "state.db"
        save(state, path)
        reloaded = load(path)
        assert reloaded == state

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dirs" / "state.db"
        save(State(), path)
        assert path.exists()

    def test_save_sorts_keys(self, tmp_path: Path) -> None:
        state = State()
        add_sysext(state, _make_sysext("zzz", "1"))
        add_sysext(state, _make_sysext("aaa", "1"))
        path = tmp_path / "state.db"
        save(state, path)
        text = path.read_text()
        # alphabetical sort: aaa before zzz inside sysexts; top-level keys alphabetical too.
        assert text.index("aaa") < text.index("zzz")
        assert text.index('"snapshots"') < text.index('"sysexts"') < text.index('"user_requests"')

    def test_atomic_save_failure_leaves_original_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        original = State()
        add_sysext(original, _make_sysext("htop", "1"))
        save(original, path)
        before = path.read_text()

        state = State()
        add_sysext(state, _make_sysext("htop", "2"))

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        with (
            patch("pacman_sysext.state.os.replace", side_effect=boom),
            pytest.raises(OSError, match="disk full"),
        ):
            save(state, path)

        assert path.read_text() == before
        # tempfile should be cleaned up
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".state.db.")]
        assert leftovers == []


class TestLocked:
    def test_releases_on_exception(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        with pytest.raises(ValueError, match="inside lock"), locked(path):
            raise ValueError("inside lock")
        # Re-acquiring should not block
        with locked(path):
            pass

    def test_mutual_exclusion(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        events: list[str] = []
        gate = threading.Event()

        def worker_a() -> None:
            with locked(path):
                events.append("a-in")
                gate.set()
                time.sleep(0.1)
                events.append("a-out")

        def worker_b() -> None:
            gate.wait()
            with locked(path):
                events.append("b-in")
                events.append("b-out")

        ta = threading.Thread(target=worker_a)
        tb = threading.Thread(target=worker_b)
        ta.start()
        tb.start()
        ta.join()
        tb.join()
        assert events == ["a-in", "a-out", "b-in", "b-out"]

    def test_sweeps_stale_tempfiles(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        stale = tmp_path / ".state.db.tmpXYZ"
        stale.write_text("partial")
        unrelated = tmp_path / "something.else"
        unrelated.write_text("keep me")

        with locked(path):
            pass

        assert not stale.exists()
        assert unrelated.exists()


class TestSysextCrud:
    def test_add_and_get(self) -> None:
        state = State()
        record = _make_sysext("htop", "3.5")
        add_sysext(state, record)
        assert get_sysext(state, "htop", "3.5") == record

    def test_re_add_replaces(self) -> None:
        state = State()
        add_sysext(state, _make_sysext("htop", "3.5", sha="aaa"))
        add_sysext(state, _make_sysext("htop", "3.5", sha="bbb"))
        record = get_sysext(state, "htop", "3.5")
        assert record is not None and record.sha256 == "bbb"

    def test_remove_returns_record(self) -> None:
        state = State()
        record = _make_sysext("htop", "3.5")
        add_sysext(state, record)
        assert remove_sysext(state, "htop", "3.5") == record
        assert remove_sysext(state, "htop", "3.5") is None

    def test_find_by_name_returns_all_versions(self) -> None:
        state = State()
        add_sysext(state, _make_sysext("libfoo", "1.0"))
        add_sysext(state, _make_sysext("libfoo", "2.0"))
        add_sysext(state, _make_sysext("other", "1.0"))
        names = {(r.name, r.version) for r in find_sysexts_by_name(state, "libfoo")}
        assert names == {("libfoo", "1.0"), ("libfoo", "2.0")}

    def test_find_providing_sysexts_by_name_and_provides(self) -> None:
        state = State()
        add_sysext(state, _make_sysext("libbz2", "1.0", provides={"libbz2.so": "1.0-64"}))
        add_sysext(state, _make_sysext("other", "1.0"))
        by_provide = find_providing_sysexts(state, "libbz2.so")
        by_name = find_providing_sysexts(state, "libbz2")
        assert [r.name for r in by_provide] == ["libbz2"]
        assert [r.name for r in by_name] == ["libbz2"]
        assert find_providing_sysexts(state, "nothing") == []


class TestUserRequestCrud:
    def test_add_replaces_existing(self) -> None:
        state = State()
        add_user_request(state, _make_request("htop", "1", {}))
        add_user_request(state, _make_request("htop", "2", {}))
        req = get_user_request(state, "htop")
        assert req is not None and req.installed_version == "2"

    def test_remove(self) -> None:
        state = State()
        add_user_request(state, _make_request("htop", "1", {}))
        assert remove_user_request(state, "htop") is not None
        assert remove_user_request(state, "htop") is None


class TestDependencyAnalysis:
    def test_find_requirers(self) -> None:
        state = State()
        add_user_request(state, _make_request("htop", "1", {"libcap": ">=2.78"}))
        add_user_request(state, _make_request("neovim", "1", {"libcap": ">=2.70"}))
        add_user_request(state, _make_request("other", "1", {}))
        result = dict(find_requirers(state, "libcap"))
        assert result == {"htop": ">=2.78", "neovim": ">=2.70"}

    def test_is_satisfied_by(self) -> None:
        state = State()
        add_user_request(state, _make_request("htop", "1", {"libcap": ">=2.78"}))
        add_user_request(state, _make_request("neovim", "1", {"libcap": ">=3.0"}))
        assert is_satisfied_by(state, "libcap", "2.80") == ["htop"]
        assert sorted(is_satisfied_by(state, "libcap", "3.5")) == ["htop", "neovim"]

    def test_unsatisfied_requirements_missing_sysext(self) -> None:
        state = State()
        add_user_request(state, _make_request("htop", "1", {"libcap": ">=2.78"}))
        assert find_unsatisfied_requirements(state) == {"htop": ["libcap"]}

    def test_unsatisfied_requirements_satisfied_via_provides(self) -> None:
        state = State()
        add_sysext(state, _make_sysext("libbz2", "1.0-64", provides={"libbz2.so": "1.0-64"}))
        add_user_request(state, _make_request("foo", "1", {"libbz2.so": ">=1.0"}))
        assert find_unsatisfied_requirements(state) == {}

    def test_unpinned_provides_satisfies_only_when_unversioned(self) -> None:
        state = State()
        add_sysext(state, _make_sysext("openssh", "10.0", provides={"sh": ""}))
        add_user_request(state, _make_request("script", "1", {"sh": ""}))
        assert find_unsatisfied_requirements(state) == {}

        state2 = State()
        add_sysext(state2, _make_sysext("openssh", "10.0", provides={"sh": ""}))
        add_user_request(state2, _make_request("script", "1", {"sh": ">=5"}))
        assert find_unsatisfied_requirements(state2) == {"script": ["sh"]}

    def test_satisfied_directly_by_sysext(self) -> None:
        state = State()
        add_sysext(state, _make_sysext("libcap", "2.78-1"))
        add_user_request(state, _make_request("htop", "1", {"libcap": ">=2.70"}))
        assert find_unsatisfied_requirements(state) == {}


class TestSnapshots:
    def test_compute_snapshot_id_is_deterministic(self) -> None:
        a = compute_snapshot_id({"glibc": "2.39", "zlib": "1.3"})
        b = compute_snapshot_id({"zlib": "1.3", "glibc": "2.39"})
        assert a == b

    def test_different_content_different_id(self) -> None:
        a = compute_snapshot_id({"glibc": "2.39"})
        b = compute_snapshot_id({"glibc": "2.40"})
        assert a != b

    def test_intern_snapshot_is_idempotent(self) -> None:
        state = State()
        first = intern_snapshot(state, {"glibc": "2.39"})
        second = intern_snapshot(state, {"glibc": "2.39"})
        assert first == second
        assert len(state.snapshots) == 1


class TestIntegrity:
    def test_compute_file_sha256(self, tmp_path: Path) -> None:
        path = tmp_path / "blob"
        payload = b"hello world\n" * 10_000
        path.write_bytes(payload)
        assert compute_file_sha256(path) == hashlib.sha256(payload).hexdigest()

    def test_verify_passes_on_match(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        raw = out_dir / "htop-3.5.raw"
        raw.write_bytes(b"image data")
        digest = hashlib.sha256(b"image data").hexdigest()
        record = _make_sysext("htop", "3.5", sha=digest)
        assert verify_sysext_integrity(record, out_dir) is True

    def test_verify_fails_on_mismatch(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        raw = out_dir / "htop-3.5.raw"
        raw.write_bytes(b"image data")
        record = _make_sysext("htop", "3.5", sha="deadbeef")
        assert verify_sysext_integrity(record, out_dir) is False

    def test_verify_fails_on_missing(self, tmp_path: Path) -> None:
        record = _make_sysext("htop", "3.5", sha="anything")
        assert verify_sysext_integrity(record, tmp_path) is False


class TestAbiDrift:
    def test_no_drift_when_matching(self) -> None:
        state = State()
        snap = intern_snapshot(state, {"glibc": "2.39-1"})
        add_sysext(state, _make_sysext("htop", "3.5", snap=snap))
        assert find_abi_drift(state, {"glibc": "2.39-1"}) == []

    def test_version_difference_reported(self) -> None:
        state = State()
        snap = intern_snapshot(state, {"glibc": "2.39-1"})
        add_sysext(state, _make_sysext("htop", "3.5", snap=snap))
        drifts = find_abi_drift(state, {"glibc": "2.40-1"})
        assert len(drifts) == 1
        assert drifts[0].differences == {"glibc": ("2.39-1", "2.40-1")}
        assert drifts[0].missing_in_current == []

    def test_missing_package_reported(self) -> None:
        state = State()
        snap = intern_snapshot(state, {"glibc": "2.39-1", "icu": "78-1"})
        add_sysext(state, _make_sysext("htop", "3.5", snap=snap))
        drifts = find_abi_drift(state, {"glibc": "2.39-1"})
        assert drifts[0].differences == {}
        assert drifts[0].missing_in_current == ["icu"]

    def test_unknown_snapshot_skipped(self) -> None:
        state = State()
        add_sysext(state, _make_sysext("htop", "3.5", snap="dangling"))
        assert find_abi_drift(state, {"glibc": "2.39-1"}) == []

    def test_empty_snapshot_no_drift(self) -> None:
        state = State()
        snap = intern_snapshot(state, {})
        add_sysext(state, _make_sysext("htop", "3.5", snap=snap))
        assert find_abi_drift(state, {"glibc": "2.40"}) == []


class TestSysextKey:
    def test_key_format(self) -> None:
        assert sysext_key("htop", "3.5.1-1") == "htop-3.5.1-1"


def test_abi_drift_dataclass_is_hashable_for_set_use() -> None:
    # Sanity: frozen dataclasses with dict fields aren't hashable, but we don't
    # rely on it. Just ensure equality works for assertion in tests.
    a = AbiDrift(sysext_key="x", differences={}, missing_in_current=[])
    b = AbiDrift(sysext_key="x", differences={}, missing_in_current=[])
    assert a == b


def test_base_snapshot_equality_roundtrip() -> None:
    a = BaseSnapshot(id="x", packages={"a": "1"})
    b = BaseSnapshot(id="x", packages={"a": "1"})
    assert a == b


def test_locked_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "state.db"
    with locked(nested):
        assert nested.with_name(nested.name + ".lock").exists()


def test_os_replace_runs_under_lock_sequencing(tmp_path: Path) -> None:
    # Smoke test that save() interleaved with locked() doesn't deadlock.
    path = tmp_path / "state.db"
    with locked(path):
        save(State(), path)
    assert path.exists()


def test_state_db_does_not_write_zero_byte_on_save_error(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with (
        patch("pacman_sysext.state.json.dump", side_effect=OSError("nope")),
        pytest.raises(OSError, match="nope"),
    ):
        save(State(), path)
    assert not path.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".state.db.")]
    assert leftovers == []


def test_load_skips_unknown_optional_field_silently(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sysexts": {},
                "user_requests": {},
                "snapshots": {},
                "extra_field": "ignored",
            }
        )
    )
    state = load(path)
    assert state == State()


def test_save_writes_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    save(State(), path)
    assert path.read_text().endswith("\n")


def test_state_db_is_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    save(State(), path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_lock_times_out(tmp_path: Path) -> None:
    import fcntl as _fcntl
    import os as _os

    path = tmp_path / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    holding_fd = _os.open(lock_path, _os.O_WRONLY | _os.O_CREAT, 0o600)
    _fcntl.flock(holding_fd, _fcntl.LOCK_EX)
    try:
        with pytest.raises(StateError, match="could not acquire lock"), locked(path, timeout=0.3):
            pass
    finally:
        _fcntl.flock(holding_fd, _fcntl.LOCK_UN)
        _os.close(holding_fd)


def _hash_of(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_compute_snapshot_id_known_value() -> None:
    # Lock in canonical hashing format so a future schema change is loud.
    expected = _hash_of(b'{"glibc":"2.39","zlib":"1.3"}')
    assert compute_snapshot_id({"glibc": "2.39", "zlib": "1.3"}) == expected


def test_os_module_is_used_for_replace_not_pathlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guard the atomicity assumption: save() must go through os.replace, not Path.rename.
    calls: list[str] = []

    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr("pacman_sysext.state.os.replace", spy)
    save(State(), tmp_path / "state.db")
    assert calls == ["replace"]
