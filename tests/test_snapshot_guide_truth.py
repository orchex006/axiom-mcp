"""W9-4: the snapshot guide's limits must match the modules this repository ships.

``docs/guides/snapshots.md`` is only trustworthy because it states which parts of the read path
exist as code and which are still specification. That statement is therefore checked against the
modules rather than trusted: the guide is read from the working tree and compared with the
platform backends' own declarations and the shared ``guard.protocol`` constants. The negative leg
rewrites the guide the way it read before the correction, so the match above is shown to be
sensitive rather than vacuous - the shape ``tests/test_guard_protocol.py`` uses for the consumed
guard contract.

The backends are read as source declarations rather than imported, because ``locks_posix`` needs
``fcntl`` and ``locks_windows`` needs ``msvcrt``: a doc-truth check must run on every platform
instead of skipping on the platform whose paragraph it protects.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from axiom_mcp.guard import protocol  # noqa: E402

GUIDE = REPO_ROOT / "docs" / "guides" / "snapshots.md"
GUARD_DIR = REPO_ROOT / "src" / "axiom_mcp" / "guard"
PACKAGE_DIR = REPO_ROOT / "src" / "axiom_mcp"

BACKENDS = {
    "posix": GUARD_DIR / "locks_posix.py",
    "windows": GUARD_DIR / "locks_windows.py",
}

# Every module a reader relying on this guide is pointed at, by the name the guide uses.
FILES = {
    "locks_posix.py": GUARD_DIR / "locks_posix.py",
    "locks_windows.py": GUARD_DIR / "locks_windows.py",
    "engine.py": GUARD_DIR / "engine.py",
    "protocol.py": GUARD_DIR / "protocol.py",
    "adapter.py": GUARD_DIR / "adapter.py",
    "read_session.py": PACKAGE_DIR / "read_session.py",
    "cache.py": PACKAGE_DIR / "cache.py",
    "manifest.py": PACKAGE_DIR / "manifest.py",
    "registry.py": PACKAGE_DIR / "registry.py",
}

# A backend declares what it is; the guide has to name the same value. The comparison is
# normalized because the guide writes "byte range" where the declaration writes "byte-range".
DECLARATION = re.compile(r'^(backend_name|primitive|scope) = "([^"]+)"$', re.MULTILINE)

# Wording that was true at an earlier revision and is not any more: each one denies a module
# that is in the tree and tested, so the guide must not carry it.
STALE_CLAIMS = (
    "Windows backend is **not present",
    "`locks_posix.py` only",
    "No reader-session or cache runtime exists",
)


def guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def normalized(text: str) -> str:
    """Case- and separator-insensitive form, so prose and identifiers can be compared."""
    return re.sub(r"[-_\s]+", " ", text).lower()


def declarations(path: pathlib.Path) -> dict[str, str]:
    """The module-level declarations a backend makes about itself."""
    return dict(DECLARATION.findall(path.read_text(encoding="utf-8")))


def reasons(text: str) -> list[str]:
    """Every way the guide's limits disagree with what ships and what the guards declare."""
    problems: list[str] = []
    for name, path in FILES.items():
        if name not in text:
            problems.append(f"the guide does not name the shipped module {name!r}")
        if not path.is_file():
            problems.append(f"the guide names {name!r}, which is not in the tree")
    for name, path in BACKENDS.items():
        if not path.is_file():
            problems.append(f"the {name} backend module is missing from the tree")
            continue
        declared = declarations(path)
        for key in ("primitive", "scope"):
            value = declared.get(key)
            if not value:
                problems.append(f"the {name} backend declares no {key}")
            elif normalized(value) not in normalized(text):
                problems.append(f"the guide does not name the {name} {key} {value!r}")
    offset, length = protocol.WINDOWS_BYTE_RANGE
    if f"offset {offset} length {length}" not in text:
        problems.append("the guide does not state the declared Windows byte range")
    for stale in STALE_CLAIMS:
        if stale in text:
            problems.append(f"the guide still carries the stale claim: {stale!r}")
    return problems


def test_the_snapshot_guide_limits_match_the_shipped_modules() -> None:
    """AC: the guard and reader limits the guide records are the ones that ship."""
    assert reasons(guide_text()) == []


def test_a_reintroduced_stale_claim_is_rejected() -> None:
    """Negative: the checker rejects the wording the guide carried before the correction."""
    stale = guide_text().replace(
        "Windows backend is present at this revision",
        "Windows backend is **not present at this revision, and `src/axiom_mcp/guard/` ships\n"
        "  `locks_posix.py` only",
    )
    assert stale != guide_text()
    problems = reasons(stale)
    assert any("stale claim" in problem for problem in problems), problems
