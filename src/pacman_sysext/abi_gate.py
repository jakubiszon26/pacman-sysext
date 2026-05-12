"""Pre-flight ABI Gatekeeper.

Classifies each dependency in the resolved tree against the host state
and rejects installs that would shadow a native host library with a
different version. Default-Deny on shadowing: if the host already has a
package, the sysext is forbidden from bundling a different version of
it; the only exemption is `SAFE_SHADOW_PREFIXES` (fonts, icon themes
and similar cosmetic packages where ABI doesn't apply).

The gate is a pure function: callers gather host state via `pacman.py`
and pass it in. Tests mock at the data layer, not at subprocess.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Literal

from pacman_sysext.pacman import parse_pkg_filename
from pacman_sysext.version import vercmp

logger = logging.getLogger(__name__)

Bucket = Literal["bundle", "skip", "shadow", "block"]


# Cosmetic packages allowed to shadow the host with a different version.
# A version drift in a font or icon theme cannot mismatch ABI, so we let
# them through with a warning instead of aborting the whole install.
# Matched with `fnmatch.fnmatchcase` against the package name. Patterns
# are deliberately tight — a single `adobe-source-*` would also catch a
# hypothetical `adobe-source-libsomething` library, weakening default-deny.
SAFE_SHADOW_PREFIXES: tuple[str, ...] = (
    "font-*",
    "ttf-*",
    "otf-*",
    "noto-*",
    "*-fonts",
    "*-icons",
    "*-icon-theme",
    "hicolor-icon-theme",
    "adobe-source-*-fonts",
)


@dataclass(frozen=True)
class ClassifiedDep:
    """One dependency after gate classification."""

    name: str
    resolved_version: str
    host_version: str | None
    bucket: Bucket
    filename: str


@dataclass(frozen=True)
class GateReport:
    """Outcome of `classify()` for one install transaction.

    Buckets:
        bundles: package goes into a sysext (new to the host or the
            user-requested target).
        skips: host already provides exactly this version — do not build.
        shadows: version differs from host but the package is in the
            safe-shadow exemption list; bundle with a loud warning.
        blocks: version differs from host and the package is ABI-relevant
            — abort unless the caller passes the override flag.
    """

    target_pkg: str
    bundles: list[ClassifiedDep] = field(default_factory=list)
    skips: list[ClassifiedDep] = field(default_factory=list)
    shadows: list[ClassifiedDep] = field(default_factory=list)
    blocks: list[ClassifiedDep] = field(default_factory=list)


def _is_safe_shadow(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SAFE_SHADOW_PREFIXES)


def classify(
    resolved_filenames: list[str],
    target_pkg: str,
    host_packages: dict[str, str],
    host_provided: set[str],
) -> GateReport:
    """Sort each resolved dep into bundle / skip / shadow / block.

    Args:
        resolved_filenames: filenames pacman would download for the
            target (output of `pacman -Sw --print`).
        target_pkg: name of the user-requested package. Always lands in
            `bundles` regardless of host — the user explicitly asked for
            the sysext form of this package.
        host_packages: name → version map of packages actually installed
            on the host (from `pacman -Q`). Does NOT include provides
            aliases — those come through `host_provided`.
        host_provided: names the host satisfies in any form (real package
            or `provides` alias), typically
            `set(names) - find_unsatisfied(names)`.

    Decision per dep (target_pkg short-circuits to `bundle`):
        - host_packages[name] missing AND name in host_provided -> skip
          (host satisfies via a provides alias; we can't compare versions
          without `pacman -Qi`, so refuse to shadow on the safe side).
        - host_packages[name] missing AND name not in host_provided -> bundle
          (new functionality, host doesn't ship it).
        - host_packages[name] == resolved version -> skip.
        - host_packages[name] != resolved version -> shadow if name matches
          SAFE_SHADOW_PREFIXES, else block.

    Older-than-host is treated identically to newer-than-host: any
    difference on a shadowable package is a mismatch.
    """
    bundles: list[ClassifiedDep] = []
    skips: list[ClassifiedDep] = []
    shadows: list[ClassifiedDep] = []
    blocks: list[ClassifiedDep] = []

    for filename in resolved_filenames:
        name, resolved_version = parse_pkg_filename(filename)
        host_version = host_packages.get(name)

        if name == target_pkg:
            bundles.append(ClassifiedDep(name, resolved_version, host_version, "bundle", filename))
            continue

        if host_version is None:
            if name in host_provided:
                skips.append(ClassifiedDep(name, resolved_version, None, "skip", filename))
            else:
                bundles.append(ClassifiedDep(name, resolved_version, None, "bundle", filename))
            continue

        if vercmp(resolved_version, host_version) == 0:
            skips.append(ClassifiedDep(name, resolved_version, host_version, "skip", filename))
            continue

        if _is_safe_shadow(name):
            shadows.append(ClassifiedDep(name, resolved_version, host_version, "shadow", filename))
        else:
            blocks.append(ClassifiedDep(name, resolved_version, host_version, "block", filename))

    return GateReport(
        target_pkg=target_pkg,
        bundles=bundles,
        skips=skips,
        shadows=shadows,
        blocks=blocks,
    )
