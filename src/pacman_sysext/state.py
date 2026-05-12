"""On-disk state for installed sysexts, user requests, and ABI snapshots.

State lives in a single JSON file (default `/var/lib/pacman-sysext/state.db`).
Read-modify-write cycles are protected by `fcntl.flock` on a sibling `.lock`
file. Atomic writes via `tempfile + fsync + os.replace`.

The schema is versioned at the top level so future migrations can detect
incompatibility cleanly.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pacman_sysext.config import FsFormat
from pacman_sysext.version import parse_constraint, satisfies, vercmp

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_VALID_FS_FORMATS: frozenset[str] = frozenset({"erofs", "squashfs"})


class StateError(Exception):
    """State file is malformed or unreadable."""


@dataclass(frozen=True)
class BaseSnapshot:
    """ABI-relevant host package versions, content-addressed.

    `id` is `sha256(canonical-JSON(packages))`; equal contents always
    produce the same id so multiple sysexts built in the same
    transaction share one snapshot record.
    """

    id: str
    packages: dict[str, str]


@dataclass(frozen=True)
class SysextRecord:
    """Metadata for one built sysext image. Identity is (name, version).

    `depends` is the list of direct dependency names recorded at build
    time. Status and other graph-walking commands read this field
    instead of re-querying pacman per record. The field defaults to an
    empty list so records persisted before the field existed keep
    loading; consumers must treat an empty `depends` as 'unknown' and
    fall back to pacman only when correctness demands it.
    """

    name: str
    version: str
    raw_filename: str
    fs_format: FsFormat
    sha256: str
    installed_at: datetime
    snapshot_id: str
    provides: dict[str, str]
    depends: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UserRequest:
    """A package the user explicitly asked for via `pacman-sysext install`."""

    name: str
    installed_version: str
    requested_at: datetime
    requirements: dict[str, str]


@dataclass
class State:
    """In-memory mutable state. Persisted as JSON."""

    sysexts: dict[str, SysextRecord] = field(default_factory=dict)
    user_requests: dict[str, UserRequest] = field(default_factory=dict)
    snapshots: dict[str, BaseSnapshot] = field(default_factory=dict)


@dataclass(frozen=True)
class AbiDrift:
    """A divergence between a sysext's recorded base snapshot and the host."""

    sysext_key: str
    differences: dict[str, tuple[str, str]]
    missing_in_current: list[str]


@dataclass(frozen=True)
class IntegrityReport:
    """Audit of state.db against the actual sysext output directory."""

    missing_files: list[SysextRecord]
    unregistered_files: list[Path]


def sysext_key(name: str, version: str) -> str:
    """Canonical key for a SysextRecord in `state.sysexts`."""
    return f"{name}-{version}"


def load(path: Path) -> State:
    """Load state from disk. Returns empty `State` if `path` does not exist."""
    if not path.exists():
        return State()
    try:
        with path.open("rb") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise StateError(f"malformed JSON in {path}: {e}") from e
    except OSError as e:
        raise StateError(f"cannot read {path}: {e}") from e
    return _state_from_json(data)


def save(state: State, path: Path) -> None:
    """Atomically write state to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(_state_to_json(state), f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # mkstemp already creates the tempfile with 0o600 and os.replace
        # preserves it, so this chmod is defensive — guards against an
        # umask-shifting fork or a future refactor that swaps mkstemp for
        # NamedTemporaryFile. State lists every sysext, dep, and host
        # pkg version at snapshot time — not secrets, but no reason to
        # expose to other local users.
        path.chmod(0o600)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


_DEFAULT_LOCK_TIMEOUT = 300.0


@contextmanager
def locked(path: Path, timeout: float = _DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Acquire an exclusive flock on `<path>.lock` for the block's duration.

    Raises `StateError` if the lock cannot be acquired within `timeout`
    seconds. We never force-remove the .lock file on timeout: if the
    holder is still alive, force-removing would let two processes write
    state concurrently and corrupt it. The user is responsible for
    identifying the stuck process (e.g. `lsof <lock_path>`).

    On acquisition, sweeps any stale `.{path.name}.*` tempfiles left over
    by a previous `save()` killed mid-write. The sweep runs under the
    lock, so it never races a concurrent writer.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        _acquire_with_timeout(fd, lock_path, timeout)
        _sweep_stale_tempfiles(path)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _acquire_with_timeout(fd: int, lock_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    delay = 0.05
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StateError(
                    f"could not acquire lock on {lock_path} within {timeout:.0f}s. "
                    f"Another pacman-sysext process may be running or stuck; "
                    f"identify it with `lsof {lock_path}` before proceeding."
                ) from None
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 5.0)


def _sweep_stale_tempfiles(path: Path) -> None:
    parent = path.parent
    prefix = f".{path.name}."
    if not parent.exists():
        return
    for entry in parent.iterdir():
        if not entry.is_file() or not entry.name.startswith(prefix):
            continue
        try:
            entry.unlink()
            logger.info("Removed stale tempfile %s", entry)
        except OSError as e:
            logger.warning("Failed to remove stale tempfile %s: %s", entry, e)


def compute_snapshot_id(packages: dict[str, str]) -> str:
    """Content-hash for a snapshot dict. Stable across runs."""
    canonical = json.dumps(packages, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def intern_snapshot(state: State, packages: dict[str, str]) -> str:
    """Return snapshot id for `packages`, inserting a new BaseSnapshot if novel."""
    snapshot_id = compute_snapshot_id(packages)
    if snapshot_id not in state.snapshots:
        state.snapshots[snapshot_id] = BaseSnapshot(id=snapshot_id, packages=dict(packages))
    return snapshot_id


def add_sysext(state: State, record: SysextRecord) -> None:
    """Add or replace a sysext record."""
    key = sysext_key(record.name, record.version)
    if key in state.sysexts:
        logger.info("Replacing sysext record %s", key)
    else:
        logger.info("Adding sysext record %s", key)
    state.sysexts[key] = record


def remove_sysext(state: State, name: str, version: str) -> SysextRecord | None:
    """Remove a sysext record. Returns the removed record or None."""
    return state.sysexts.pop(sysext_key(name, version), None)


def get_sysext(state: State, name: str, version: str) -> SysextRecord | None:
    """Look up a sysext by name+version."""
    return state.sysexts.get(sysext_key(name, version))


def find_sysexts_by_name(state: State, name: str) -> list[SysextRecord]:
    """All SysextRecords with the given name, across all versions."""
    return [r for r in state.sysexts.values() if r.name == name]


def find_providing_sysexts(state: State, dep_name: str) -> list[SysextRecord]:
    """All SysextRecords matching `dep_name` either by `.name` or via `.provides`."""
    return [r for r in state.sysexts.values() if r.name == dep_name or dep_name in r.provides]


def add_user_request(state: State, request: UserRequest) -> None:
    """Record a user-requested install, replacing any prior request for the same name."""
    if request.name in state.user_requests:
        previous = state.user_requests[request.name]
        logger.info(
            "Replaced user request: %s %s -> %s",
            request.name,
            previous.installed_version,
            request.installed_version,
        )
    else:
        logger.info("Recording user request: %s", request.name)
    state.user_requests[request.name] = request


def remove_user_request(state: State, name: str) -> UserRequest | None:
    """Remove and return a user request, or None if not present."""
    return state.user_requests.pop(name, None)


def get_user_request(state: State, name: str) -> UserRequest | None:
    """Look up a user request by name."""
    return state.user_requests.get(name)


def find_requirers(state: State, dep_name: str) -> list[tuple[str, str]]:
    """All user_requests that name `dep_name` in their `requirements` dict.

    Returns (user_pkg_name, constraint_string) pairs. The constraint
    string is the raw value from `requirements` (e.g. `">=1.5"` or `""`).
    """
    return [
        (user_name, req.requirements[dep_name])
        for user_name, req in state.user_requests.items()
        if dep_name in req.requirements
    ]


def is_satisfied_by(state: State, dep_name: str, sysext_version: str) -> list[str]:
    """User-request names whose requirement on `dep_name` is met by `sysext_version`."""
    matched: list[str] = []
    for user_name, req in state.user_requests.items():
        if dep_name not in req.requirements:
            continue
        constraint = parse_constraint(_spec_for(dep_name, req.requirements[dep_name]))
        if satisfies(sysext_version, constraint):
            matched.append(user_name)
    return matched


def find_unsatisfied_requirements(state: State) -> dict[str, list[str]]:
    """Internal consistency check: requirements not met by any sysext in state.

    Host packages are explicitly NOT considered here — this is a state-only
    audit. Real install-time satisfaction is a separate call site that also
    checks the host.
    """
    unsatisfied: dict[str, list[str]] = {}
    for user_name, req in state.user_requests.items():
        missing: list[str] = []
        for dep_name, constraint_str in req.requirements.items():
            if not _state_satisfies_dep(state, dep_name, constraint_str):
                missing.append(dep_name)
        if missing:
            unsatisfied[user_name] = missing
    return unsatisfied


def _spec_for(name: str, constraint_str: str) -> str:
    return f"{name}{constraint_str}" if constraint_str else name


def _state_satisfies_dep(state: State, dep_name: str, constraint_str: str) -> bool:
    constraint = parse_constraint(_spec_for(dep_name, constraint_str))
    for record in find_providing_sysexts(state, dep_name):
        version_to_check = _provider_version(record, dep_name)
        if version_to_check is None:
            # Unpinned provides — only satisfies unversioned constraints.
            if constraint.operator is None:
                return True
            continue
        if satisfies(version_to_check, constraint):
            return True
    return False


def _provider_version(record: SysextRecord, dep_name: str) -> str | None:
    """Version that `record` exposes for `dep_name`. None means 'unpinned provides'."""
    if record.name == dep_name:
        return record.version
    pinned = record.provides.get(dep_name)
    if pinned is None:
        return None
    return pinned if pinned else None


def get_explicit(state: State) -> list[SysextRecord]:
    """SysextRecords the user explicitly requested.

    Resolves each `UserRequest` to the matching `(name, installed_version)`
    sysext record. Requests without a backing record are skipped silently
    — that pathology is surfaced by `audit_integrity` instead. Output is
    sorted by `installed_at` for stable rendering.
    """
    matched: list[SysextRecord] = []
    for req in state.user_requests.values():
        record = get_sysext(state, req.name, req.installed_version)
        if record is not None:
            matched.append(record)
    matched.sort(key=lambda r: r.installed_at)
    return matched


def get_implicit(
    state: State,
    dep_resolver: Callable[[str], list[str]] | None = None,
) -> list[SysextRecord]:
    """SysextRecords reachable from user_requests as transitive deps.

    Primary source: each record's `depends` field, read from RAM. The
    optional `dep_resolver` is consulted only when a reached record has
    `depends == []` (legacy entries persisted before the field
    existed). Resolver results are never written back — this function
    is strictly read-only.

    Walks `provides` aliases via `find_providing_sysexts`, so a dep on
    `zlib` resolves to a record that lists `zlib` in `provides`.
    Records that are themselves user-requested are excluded (they
    belong to explicit, not implicit).
    """
    explicit_names = set(state.user_requests.keys())
    visited: set[str] = set()
    worklist: list[str] = list(explicit_names)
    implicit: list[SysextRecord] = []

    while worklist:
        name = worklist.pop()
        for record in find_providing_sysexts(state, name):
            key = sysext_key(record.name, record.version)
            if key in visited:
                continue
            visited.add(key)
            if record.name not in explicit_names:
                implicit.append(record)
            children = record.depends
            if not children and dep_resolver is not None:
                children = dep_resolver(record.name)
            worklist.extend(children)
    return implicit


def get_orphans(
    state: State,
    dep_resolver: Callable[[str], list[str]] | None = None,
) -> list[SysextRecord]:
    """SysextRecords not reachable as explicit or implicit — cleanup candidates."""
    explicit = {sysext_key(r.name, r.version) for r in get_explicit(state)}
    implicit = {sysext_key(r.name, r.version) for r in get_implicit(state, dep_resolver)}
    return [
        record
        for key, record in state.sysexts.items()
        if key not in explicit and key not in implicit
    ]


def audit_integrity(state: State, sysexts_dir: Path) -> IntegrityReport:
    """Reconcile state.sysexts with the actual contents of `sysexts_dir`."""
    registered = {r.raw_filename: r for r in state.sysexts.values()}
    missing_files = [
        r for r in state.sysexts.values() if not (sysexts_dir / r.raw_filename).exists()
    ]
    if not sysexts_dir.exists():
        return IntegrityReport(missing_files=missing_files, unregistered_files=[])
    unregistered_files = [
        path for path in sorted(sysexts_dir.glob("*.raw")) if path.name not in registered
    ]
    return IntegrityReport(missing_files=missing_files, unregistered_files=unregistered_files)


def compute_file_sha256(path: Path) -> str:
    """Stream-hash a file with sha256."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(64 * 1024):
            h.update(chunk)
    return h.hexdigest()


def verify_sysext_integrity(record: SysextRecord, output_dir: Path) -> bool:
    """True if the on-disk `.raw` for `record` matches its recorded sha256.

    Returns False on missing files or read errors — the only signal we
    need is 'safe to reuse this image as-is'.
    """
    path = output_dir / record.raw_filename
    if not path.exists():
        return False
    try:
        return compute_file_sha256(path) == record.sha256
    except OSError as e:
        logger.warning("Integrity check failed for %s: %s", path, e)
        return False


def find_abi_drift(state: State, current_snapshot: dict[str, str]) -> list[AbiDrift]:
    """Report sysexts whose recorded base versions differ from `current_snapshot`."""
    drifts: list[AbiDrift] = []
    for key, record in state.sysexts.items():
        snapshot = state.snapshots.get(record.snapshot_id)
        if snapshot is None:
            logger.warning(
                "Sysext %s references unknown snapshot %s; skipping drift check",
                key,
                record.snapshot_id,
            )
            continue
        differences: dict[str, tuple[str, str]] = {}
        missing: list[str] = []
        for pkg, recorded_version in snapshot.packages.items():
            current_version = current_snapshot.get(pkg)
            if current_version is None:
                missing.append(pkg)
                continue
            if vercmp(current_version, recorded_version) != 0:
                differences[pkg] = (recorded_version, current_version)
        if differences or missing:
            drifts.append(
                AbiDrift(
                    sysext_key=key,
                    differences=differences,
                    missing_in_current=missing,
                )
            )
    return drifts


def _state_to_json(state: State) -> dict[str, Any]:
    return {
        "version": _SCHEMA_VERSION,
        "sysexts": {k: _sysext_to_json(v) for k, v in state.sysexts.items()},
        "user_requests": {k: _user_request_to_json(v) for k, v in state.user_requests.items()},
        "snapshots": {k: _snapshot_to_json(v) for k, v in state.snapshots.items()},
    }


def _state_from_json(data: Any) -> State:
    if not isinstance(data, dict):
        raise StateError(f"state root must be an object, got {type(data).__name__}")
    version = data.get("version")
    if version != _SCHEMA_VERSION:
        raise StateError(f"unsupported schema version: {version!r} (expected {_SCHEMA_VERSION})")
    try:
        sysexts = {k: _sysext_from_json(v) for k, v in data.get("sysexts", {}).items()}
        user_requests = {
            k: _user_request_from_json(v) for k, v in data.get("user_requests", {}).items()
        }
        snapshots = {k: _snapshot_from_json(v) for k, v in data.get("snapshots", {}).items()}
    except (KeyError, TypeError, ValueError) as e:
        raise StateError(f"invalid record: {e}") from e
    return State(sysexts=sysexts, user_requests=user_requests, snapshots=snapshots)


def _sysext_to_json(record: SysextRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "version": record.version,
        "raw_filename": record.raw_filename,
        "fs_format": record.fs_format,
        "sha256": record.sha256,
        "installed_at": record.installed_at.isoformat(),
        "snapshot_id": record.snapshot_id,
        "provides": dict(record.provides),
        "depends": list(record.depends),
    }


def _sysext_from_json(data: Any) -> SysextRecord:
    if not isinstance(data, dict):
        raise StateError(f"sysext record must be an object, got {type(data).__name__}")
    fs_format = data["fs_format"]
    if fs_format not in _VALID_FS_FORMATS:
        raise StateError(f"invalid fs_format: {fs_format!r}")
    provides = data.get("provides", {})
    if not isinstance(provides, dict):
        raise StateError(f"provides must be an object, got {type(provides).__name__}")
    # `depends` is additive; absent on records written before the field existed,
    # so default to [] rather than bumping _SCHEMA_VERSION and breaking installs.
    depends = data.get("depends", [])
    if not isinstance(depends, list):
        raise StateError(f"depends must be a list, got {type(depends).__name__}")
    return SysextRecord(
        name=data["name"],
        version=data["version"],
        raw_filename=data["raw_filename"],
        fs_format=fs_format,
        sha256=data["sha256"],
        installed_at=datetime.fromisoformat(data["installed_at"]),
        snapshot_id=data["snapshot_id"],
        provides={str(k): str(v) for k, v in provides.items()},
        depends=[str(d) for d in depends],
    )


def _user_request_to_json(request: UserRequest) -> dict[str, Any]:
    return {
        "name": request.name,
        "installed_version": request.installed_version,
        "requested_at": request.requested_at.isoformat(),
        "requirements": dict(request.requirements),
    }


def _user_request_from_json(data: Any) -> UserRequest:
    if not isinstance(data, dict):
        raise StateError(f"user_request must be an object, got {type(data).__name__}")
    requirements = data.get("requirements", {})
    if not isinstance(requirements, dict):
        raise StateError(f"requirements must be an object, got {type(requirements).__name__}")
    return UserRequest(
        name=data["name"],
        installed_version=data["installed_version"],
        requested_at=datetime.fromisoformat(data["requested_at"]),
        requirements={str(k): str(v) for k, v in requirements.items()},
    )


def _snapshot_to_json(snapshot: BaseSnapshot) -> dict[str, Any]:
    return {"id": snapshot.id, "packages": dict(snapshot.packages)}


def _snapshot_from_json(data: Any) -> BaseSnapshot:
    if not isinstance(data, dict):
        raise StateError(f"snapshot must be an object, got {type(data).__name__}")
    packages = data.get("packages", {})
    if not isinstance(packages, dict):
        raise StateError(f"snapshot packages must be an object, got {type(packages).__name__}")
    return BaseSnapshot(
        id=data["id"],
        packages={str(k): str(v) for k, v in packages.items()},
    )
