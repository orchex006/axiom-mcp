"""V2-016 - wire paths and native bindings are normalized independently (CP-02, CP-03).

Two different things are called *a path* in this gateway, and validating one kind with the other
kind's rule is how a request string turns into an arbitrary file read:

* A **wire path** is what a request or a published manifest names: a repository-relative UTF-8
  reference that uses ``/`` separators. ``contracts/cross-platform-v2.md`` CP-03 fixes what such a
  reference may contain. It is validated *lexically*, with no filesystem access at all.
* A **native binding** is an absolute path this machine owns - ``AXIOM_HOME``, a registered
  ``repo_root`` or a lane root. It is normalized *natively*, with the host's own separator,
  drive/UNC syntax and symbolic-link resolution, and with no portability rule applied to it.

Neither function accepts the other kind. :func:`parse_wire_path` never sees a native root;
:func:`bind_native_root` never applies the portable-profile rule, so a binding that happens to
contain a Windows-reserved component, a ``:`` or a ``?`` is bound rather than refused. A wire path
is never resolved against the working directory either, because a relative reference is refused
before it can be joined to anything.

:func:`resolve_wire_path` is the only place the two meet, and it always runs the same four steps
in the same order: validate the reference, bind and resolve the root, join the reference's own
validated segments, then require the *resolved* result to sit inside the *resolved* root. The
containment check compares resolved paths, never string prefixes, because ``/<root>`` is a prefix
of ``/<root>extra``. Steps one to three open no file, so a traversal, drive-qualified or UNC
injection is refused before any read is attempted.

What is deliberately not here: path *identity*. Storing a folded or decomposed spelling would
silently rename a source path, which CP-03 forbids, so this module preserves the caller's spelling
byte for byte and leaves case-only and NFC/NFD hazard detection to the graphd path-key layer that
owns it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

WIRE_SEPARATOR: Final[str] = "/"

# The CP-03 "default portable profile" refuses Windows-reserved device names, including the
# reserved extension variants ("CON.json") and the superscript-digit variants, as a *wire*
# component. It is never applied to a native binding: normalizing one kind with the other kind's
# rule is exactly what this module exists to prevent.
RESERVED_WIRE_COMPONENTS: Final[frozenset[str]] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(10)),
        *(f"lpt{index}" for index in range(10)),
        "com\u00b9",
        "com\u00b2",
        "com\u00b3",
        "lpt\u00b9",
        "lpt\u00b2",
        "lpt\u00b3",
    }
)

# Characters the default portable profile refuses inside a wire component: the characters Windows
# cannot store in a name, plus ":" which is both the drive separator and alternate-data-stream
# syntax. ":" is refused here rather than being special-cased, so "a/b.txt:stream" is one refusal.
UNPORTABLE_WIRE_CHARACTERS: Final[frozenset[str]] = frozenset('<>:"|?*')

_DRIVE_PREFIX: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:")
_TRAILING_DOT_OR_SPACE: Final[tuple[str, ...]] = (".", " ")


class PathError(ValueError):
    """A path this gateway refuses before it can become a file access."""


class WirePathRejected(PathError):
    """A caller- or manifest-supplied reference that is not a portable wire path."""


class NativeBindingRejected(PathError):
    """A native root that is not an absolute path of this host, or is not a directory."""


class PathEscape(PathError):
    """A reference that resolves outside the native root it was bound to."""


class PathUnresolvable(PathError):
    """A native root or reference the filesystem refused to resolve."""


def _is_control(character: str) -> bool:
    """True for C0 controls, DEL and C1 controls, which no wire path may carry."""
    code = ord(character)
    return code < 0x20 or 0x7F <= code <= 0x9F


@dataclass(frozen=True)
class WirePath:
    """A validated wire reference, keeping the exact spelling the caller supplied."""

    spelling: str
    parts: tuple[str, ...]

    def as_posix(self) -> str:
        """The reference as it was supplied, which is already the wire spelling."""
        return self.spelling

    def __str__(self) -> str:
        return self.spelling


def _require_wire_component(component: str, reference: str) -> None:
    if component == "":
        raise WirePathRejected(f"a wire path must not hold an empty segment: {reference!r}")
    if component == ".":
        raise WirePathRejected(f"a wire path must not hold a '.' segment: {reference!r}")
    if component == "..":
        raise WirePathRejected(f"a wire path must not hold a '..' segment: {reference!r}")
    for character in component:
        if character in UNPORTABLE_WIRE_CHARACTERS:
            raise WirePathRejected(
                f"a wire path component must not contain {character!r}: {reference!r}"
            )
    if component.endswith(_TRAILING_DOT_OR_SPACE):
        raise WirePathRejected(
            f"a wire path component must not end with a dot or a space: {reference!r}"
        )
    stem = component.partition(".")[0]
    if stem.casefold() in RESERVED_WIRE_COMPONENTS:
        raise WirePathRejected(
            f"a wire path component must not be the reserved name {stem!r}: {reference!r}"
        )


def parse_wire_path(reference: object) -> WirePath:
    """Validate a wire reference lexically and return it with its exact spelling.

    Refused, with no filesystem access at all: a non-string, an empty string, any control
    character, any backslash (so a backslash-separated or UNC/device spelling cannot pass as a
    relative path), a leading ``/``, a drive-qualified prefix, an empty or ``.`` or ``..``
    segment, an unportable character, a trailing dot or space and a Windows-reserved component.

    A ``pathlib.Path`` is refused on purpose: a wire path is text from the wire, and passing a
    native path object to it would blur the two normalizations this module keeps apart.
    """
    if not isinstance(reference, str):
        raise WirePathRejected(f"a wire path must be a string, got {type(reference).__name__}")
    if reference == "":
        raise WirePathRejected("a wire path must not be empty")
    for character in reference:
        if _is_control(character):
            raise WirePathRejected(
                f"a wire path must not contain a control character: {reference!r}"
            )
    if "\\" in reference:
        raise WirePathRejected(
            f"a wire path must separate with '/', not a backslash: {reference!r}"
        )
    if reference.startswith(WIRE_SEPARATOR):
        raise WirePathRejected(f"a wire path must be relative, not absolute or UNC: {reference!r}")
    if _DRIVE_PREFIX.match(reference):
        raise WirePathRejected(f"a wire path must not be drive-qualified: {reference!r}")
    parts = reference.split(WIRE_SEPARATOR)
    for component in parts:
        _require_wire_component(component, reference)
    return WirePath(spelling=reference, parts=tuple(parts))


def _native_text(root: object) -> str:
    if isinstance(root, str):
        text = root
    elif isinstance(root, os.PathLike):
        value = os.fspath(root)
        if isinstance(value, bytes):
            raise NativeBindingRejected("a native root must be a text path, not bytes")
        text = value
    else:
        raise NativeBindingRejected(
            f"a native root must be a string or Path, got {type(root).__name__}"
        )
    if text == "":
        raise NativeBindingRejected("a native root must not be empty")
    return text


def bind_native_root(root: object) -> Path:
    """Bind a native root: require this host's own absolute syntax, then resolve it.

    The portable-profile rule is *not* applied here. A native root may contain a reserved name, a
    ``:``, a ``?`` or a ``..`` and reach this function unchanged; ``..`` and symbolic links are
    then normalized by the host through :meth:`pathlib.Path.resolve`, because a binding is
    normalized rather than judged. A relative root, an empty root, a non-path value, a bytes path
    and a root that exists but is not a directory are refused.
    """
    text = _native_text(root)
    try:
        candidate = Path(text)
        absolute = candidate.is_absolute()
    except (OSError, ValueError) as exc:
        raise NativeBindingRejected(f"a native root must be a usable path: {exc}") from exc
    if not absolute:
        raise NativeBindingRejected(
            f"a native root must be an absolute path of this host, got {text!r}"
        )
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathUnresolvable(f"the native root {text!r} could not be resolved: {exc}") from exc
    if resolved.exists() and not resolved.is_dir():
        raise NativeBindingRejected(
            f"the native root {text!r} exists and is not a directory; a lane root must be a "
            "directory so containment can be checked against it"
        )
    return resolved


def _resolve(candidate: Path, reference: str) -> Path:
    try:
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathUnresolvable(
            f"the wire path {reference!r} could not be resolved natively: {exc}"
        ) from exc


def _contained(resolved_root: Path, resolved_candidate: Path) -> bool:
    return resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents


def resolve_wire_path(root: object, reference: object) -> Path:
    """Resolve a wire reference inside a native root, refusing an escape before any read.

    The reference is validated first and the containment check runs last, on resolved paths, so
    an intermediate symbolic link that leaves the root is refused even though the reference itself
    was a portable, relative string. Resolution is not an open: a reference to a file that does not
    exist returns a path, and the caller's own bounded read reports the real error.
    """
    wire = parse_wire_path(reference)
    native = bind_native_root(root)
    resolved = _resolve(native.joinpath(*wire.parts), wire.spelling)
    if not _contained(native, resolved):
        raise PathEscape(
            f"wire path {wire.spelling!r} resolves to {resolved} outside the bound root {native}; "
            "containment is checked on resolved native paths, never on a string prefix"
        )
    return resolved


__all__ = [
    "RESERVED_WIRE_COMPONENTS",
    "UNPORTABLE_WIRE_CHARACTERS",
    "WIRE_SEPARATOR",
    "NativeBindingRejected",
    "PathError",
    "PathEscape",
    "PathUnresolvable",
    "WirePath",
    "WirePathRejected",
    "bind_native_root",
    "parse_wire_path",
    "resolve_wire_path",
]
