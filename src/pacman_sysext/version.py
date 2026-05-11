"""Pacman version comparison and dependency-constraint parsing.

We do not reimplement pacman's version semantics (epochs, pkgrel, alpha
suffixes, etc.) — they are nontrivial and pacman already owns them. We
shell out to the `vercmp` binary for comparisons and parse only the
constraint syntax (`libfoo>=1.5`, `libfoo`, `libfoo!=2`) ourselves.
"""

import logging
import os
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_C_ENV = {**os.environ, "LC_ALL": "C"}

_CONSTRAINT_RE = re.compile(
    r"^(?P<name>[a-zA-Z0-9._+-]+?)"
    r"(?:\s*(?P<op>>=|<=|!=|>|<|=)\s*(?P<version>[a-zA-Z0-9]\S*))?"
    r"$"
)

_VALID_OPERATORS = frozenset({">=", "<=", "!=", ">", "<", "="})


class VersionError(Exception):
    """Version comparison or constraint parsing failed."""


@dataclass(frozen=True)
class VersionConstraint:
    """A pacman-style version constraint, e.g. `libfoo>=1.5`.

    `operator` is None for unconstrained specs (just `libfoo`), in which
    case `version` is also None.
    """

    name: str
    operator: str | None
    version: str | None


def vercmp(a: str, b: str) -> int:
    """Compare two pacman versions. Returns -1, 0, or 1."""
    try:
        result = subprocess.run(
            ["vercmp", a, b],
            capture_output=True,
            text=True,
            check=True,
            env=_C_ENV,
        )
    except FileNotFoundError as e:
        raise VersionError("vercmp not found - is pacman installed?") from e
    except subprocess.CalledProcessError as e:
        raise VersionError(f"vercmp failed: {e.stderr.strip()}") from e

    raw = result.stdout.strip()
    try:
        value = int(raw)
    except ValueError as e:
        raise VersionError(f"vercmp produced non-integer output: {raw!r}") from e

    if value < 0:
        return -1
    if value > 0:
        return 1
    return 0


def parse_constraint(spec: str) -> VersionConstraint:
    """Parse a pacman dependency spec like `libfoo>=1.5` or `libfoo`.

    Whitespace around the operator is allowed. Optdep description
    sections (`nodejs: for runtime`) are stripped. Raises VersionError
    on malformed input.
    """
    if not spec or not spec.strip():
        raise VersionError("empty constraint")

    cleaned = spec.strip()
    # pacman uses ": " (colon + whitespace) as the optdep description separator;
    # versions with epoch use bare ":" without a following space ("1:2.0"), so the
    # space distinguishes the two unambiguously.
    idx = cleaned.find(": ")
    if idx != -1:
        cleaned = cleaned[:idx].rstrip()
    elif cleaned.endswith(":"):
        cleaned = cleaned[:-1].rstrip()
    if not cleaned:
        raise VersionError(f"empty constraint after stripping description: {spec!r}")

    match = _CONSTRAINT_RE.match(cleaned)
    if not match:
        raise VersionError(f"malformed constraint: {spec!r}")

    return VersionConstraint(
        name=match["name"],
        operator=match["op"],
        version=match["version"],
    )


def satisfies(version: str, constraint: VersionConstraint) -> bool:
    """Return True if `version` satisfies `constraint`.

    Unconstrained constraints (no operator) are always satisfied.
    """
    if constraint.operator is None or constraint.version is None:
        return True
    if constraint.operator not in _VALID_OPERATORS:
        raise VersionError(f"unknown operator: {constraint.operator!r}")

    cmp = vercmp(version, constraint.version)
    match constraint.operator:
        case ">=":
            return cmp >= 0
        case "<=":
            return cmp <= 0
        case ">":
            return cmp > 0
        case "<":
            return cmp < 0
        case "=":
            return cmp == 0
        case "!=":
            return cmp != 0
        case _:  # pragma: no cover - guarded above
            raise VersionError(f"unknown operator: {constraint.operator!r}")


def select_best_version(
    candidates: list[str],
    constraints: list[VersionConstraint],
) -> str | None:
    """Pick the newest candidate satisfying every constraint, or None."""
    eligible = [
        c for c in candidates if all(satisfies(c, constraint) for constraint in constraints)
    ]
    if not eligible:
        return None

    best = eligible[0]
    for candidate in eligible[1:]:
        if vercmp(candidate, best) > 0:
            best = candidate
    return best
