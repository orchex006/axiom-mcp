"""The solution catalog: one pinned generation vector per query, never a latest lookup.

``docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md`` fixes two rules this module makes executable.
SNP-03 says one query uses one exact generation vector and must not re-read ``current.json``
per shard until it lands on a different generation. SNP-06 says the catalog carries
``(project_id, generation_id, source_fingerprint)`` for every member and that a missing
project must be shown as partial rather than papered over with an empty graph. A third rule
comes from ``docs/14-MULTI-PROJECT-SOLUTIONS.md`` section 4: a member is pinned by its exact
generation, and ``tools/catalog_contract.py`` rejects a member resolved by
``name``/``project_name``/``path`` alone or one that omits ``generation_id``.

So a member is read one way only. The catalog pointer is read once, the member's
``generation_id`` names one immutable directory, and the manifest inside that directory must
hash to that same id and carry the same ``source_fingerprint``. Nothing here consults a
project's ``current.json``: selecting a project's newest generation would silently answer a
question the caller did not ask, and would break SNP-03 because two members of one query
could then come from two different catalog generations.

``docs/14-MULTI-PROJECT-SOLUTIONS.md`` section 4 also separates availability from validity: a
member whose generation is absent or corrupt does not fail the whole read, it is recorded as
missing with a reason, and the vector is reported ``partial`` so a caller under
``require_complete_solution`` can reject it explicitly instead of receiving a partial answer
that looks complete.

Canonical bytes, the sha256 pattern and the pointer closure are consumed from
:mod:`axiom_mcp.manifest` rather than re-implemented, so a catalog and a manifest cannot
disagree about what ``generation_id`` covers. Only ``json``, ``hashlib`` and the standard
library are used; the reader works with no daemon running.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom_mcp import manifest as manifest_module
from axiom_mcp.manifest import Manifest, ManifestError
from axiom_mcp.registry import LIVE_LANE, CatalogLocation, SnapshotLocation, UnknownBinding

CATALOG_FILENAME = "manifest.json"

CATALOG_SCHEMA_VERSION = 1

CATALOG_REQUIRED: tuple[str, ...] = (
    "schema_version",
    "solution_id",
    "analysis_profile",
    "projects",
    "coverage",
)

CATALOG_MEMBER_KEYS: tuple[str, ...] = (
    "project_id",
    "generation_id",
    "source_fingerprint",
)

# tools/catalog_contract.py refuses these on a member: a member selected by name, project name,
# path or directory alone has no pinned generation to read, so it would have to be matched to
# a newest generation - exactly the fallback this module exists to prohibit.
NAME_ONLY_KEYS: tuple[str, ...] = ("name", "project_name", "path", "directory")

COVERAGE_STATUSES: tuple[str, ...] = ("complete_for_profile", "partial", "unsupported")

AVAILABLE = "available"
MISSING = "missing"


class CatalogError(ValueError):
    """A catalog document, a member or a member's generation the reader must refuse."""


class CatalogInvalid(CatalogError):
    """A catalog document that is not a valid catalog."""


class CatalogUnreadable(CatalogError):
    """A catalog document that could not be read from disk."""


class UnsupportedCatalogMajor(CatalogError):
    """A catalog document whose schema major this reader does not support."""


class CatalogMemberMissing(CatalogError):
    """A pinned member generation that is absent, unreadable or not the pinned one."""

    def __init__(self, project_id: str, message: str) -> None:
        self.project_id = project_id
        super().__init__(f"catalog member {project_id!r}: {message}")


def _require_object(document: Any, *, what: str) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise CatalogInvalid(f"{what} is not a JSON object")
    return document


def _require_exact_keys(
    document: Mapping[str, Any], expected: tuple[str, ...], *, what: str
) -> None:
    keys = set(document)
    if keys != set(expected):
        missing = sorted(set(expected) - keys)
        unknown = sorted(keys - set(expected))
        raise CatalogInvalid(
            f"{what} fields do not match the contract; missing={missing} unknown={unknown}"
        )


def _text_field(document: Mapping[str, Any], name: str, *, what: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise CatalogInvalid(f"{what} {name} must be a non-empty string")
    return value


def _identifier_field(document: Mapping[str, Any], name: str, *, what: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not manifest_module.IDENTIFIER_RE.match(value):
        raise CatalogInvalid(f"{what} {name} is not a portable identifier")
    return value


def _sha256_field(document: Mapping[str, Any], name: str, *, what: str) -> str:
    value = document.get(name)
    if not manifest_module.is_sha256(value):
        raise CatalogInvalid(f"{what} {name} is not a lowercase sha256 digest")
    return str(value)


@dataclass(frozen=True)
class CatalogMember:
    """One project the catalog pins: an exact generation and its source fingerprint."""

    project_id: str
    generation_id: str
    source_fingerprint: str

    def as_document(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "generation_id": self.generation_id,
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True)
class Catalog:
    """A validated catalog document plus the exact bytes its identity covers."""

    schema_version: int
    solution_id: str
    analysis_profile: str
    coverage: str
    members: tuple[CatalogMember, ...]
    data: bytes

    @property
    def generation_id(self) -> str:
        """``sha256`` over the canonical catalog bytes, never a self-reference."""
        return manifest_module.sha256_hex(self.data)

    @property
    def catalog_generation_id(self) -> str:
        """The read-model commit point a query pins: this catalog byte's own digest."""
        return self.generation_id

    def member(self, project_id: str) -> CatalogMember:
        for member in self.members:
            if member.project_id == project_id:
                return member
        raise CatalogInvalid(f"catalog {self.solution_id!r} has no member {project_id!r}")

    def vector(self) -> tuple[CatalogMember, ...]:
        """The pinned generation vector, in a stable order."""
        return tuple(sorted(self.members, key=lambda member: member.project_id))

    def json_document(self) -> dict[str, Any]:
        return json.loads(self.data.decode("utf-8"))


def load_catalog_bytes(raw: bytes | bytearray, *, what: str = CATALOG_FILENAME) -> Catalog:
    """Validate one catalog document from its exact bytes.

    The schema major is checked before the byte form, so a document that is both canonical
    and from an unknown major reports the version rather than a formatting complaint; a
    member's pinned ``generation_id`` is checked before its fingerprint, because a member
    without a generation is the fallback case the contract prohibits outright.
    """
    payload = bytes(raw)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogInvalid(f"{what} is not UTF-8: {exc.reason}") from exc
    try:
        document = _require_object(json.loads(text), what=what)
    except json.JSONDecodeError as exc:
        raise CatalogInvalid(f"{what} is not valid JSON: {exc.msg}") from exc

    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise CatalogInvalid(f"{what} schema_version is not an integer")
    if version != CATALOG_SCHEMA_VERSION:
        raise UnsupportedCatalogMajor(
            f"{what} schema major {version} is not supported by this reader "
            f"(supported major {CATALOG_SCHEMA_VERSION})"
        )
    if not manifest_module.is_canonical_bytes(payload):
        raise CatalogInvalid(
            f"{what} is not in canonical form: it is not the compact, lexicographic, "
            "single-trailing-LF encoding of its own value"
        )

    _require_exact_keys(document, CATALOG_REQUIRED, what=what)
    solution_id = _identifier_field(document, "solution_id", what=what)
    analysis_profile = _text_field(document, "analysis_profile", what=what)

    coverage = document.get("coverage")
    if coverage not in COVERAGE_STATUSES:
        raise CatalogInvalid(f"{what} coverage is not a supported status: {coverage!r}")

    projects = document.get("projects")
    if not isinstance(projects, list) or not projects:
        raise CatalogInvalid(f"{what} projects is not a non-empty list")

    members: list[CatalogMember] = []
    seen: set[str] = set()
    for index, item in enumerate(projects):
        label = f"{what} project member {index}"
        entry = _require_object(item, what=label)
        for name_only in NAME_ONLY_KEYS:
            if name_only in entry:
                raise CatalogInvalid(
                    f"{label} resolves the member by {name_only!r}; name-only member "
                    "resolution is prohibited because it has no pinned generation to read"
                )
        _require_exact_keys(entry, CATALOG_MEMBER_KEYS, what=label)
        project_id = _identifier_field(entry, "project_id", what=label)
        if project_id in seen:
            raise CatalogInvalid(f"{what} declares duplicate member {project_id!r}")
        seen.add(project_id)
        members.append(
            CatalogMember(
                project_id=project_id,
                generation_id=_sha256_field(entry, "generation_id", what=label),
                source_fingerprint=_sha256_field(entry, "source_fingerprint", what=label),
            )
        )

    return Catalog(
        schema_version=CATALOG_SCHEMA_VERSION,
        solution_id=solution_id,
        analysis_profile=analysis_profile,
        coverage=str(coverage),
        members=tuple(members),
        data=payload,
    )


def load_catalog(path: Path) -> Catalog:
    """Load and validate a catalog document from disk."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise CatalogUnreadable(f"{Path(path).name} is unreadable: {type(exc).__name__}") from exc
    return load_catalog_bytes(raw, what=Path(path).name)


@dataclass(frozen=True)
class MemberPin:
    """One member of a pinned vector and the manifest that generation resolved to."""

    member: CatalogMember
    status: str
    generation_dir: Path | None = None
    manifest: Manifest | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.status == AVAILABLE

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "project_id": self.member.project_id,
            "generation_id": self.member.generation_id,
            "source_fingerprint": self.member.source_fingerprint,
            "status": self.status,
        }
        if self.reason is not None:
            document["reason"] = self.reason
        return document


@dataclass(frozen=True)
class CatalogVector:
    """The one generation vector a query runs against.

    The vector is complete when every member resolved to the generation the catalog pinned.
    A missing member is reported, never replaced: ``docs/14-MULTI-PROJECT-SOLUTIONS.md``
    section 6 requires ``PROJECT_UNAVAILABLE`` with partial coverage instead of an empty graph.
    """

    catalog_generation_id: str
    solution_id: str
    coverage: str
    pins: tuple[MemberPin, ...]

    @property
    def available(self) -> tuple[MemberPin, ...]:
        return tuple(pin for pin in self.pins if pin.available)

    @property
    def missing(self) -> tuple[MemberPin, ...]:
        return tuple(pin for pin in self.pins if not pin.available)

    @property
    def is_complete(self) -> bool:
        return not self.missing

    @property
    def effective_coverage(self) -> str:
        """``partial`` whenever a member is missing, whatever the catalog claimed."""
        if self.missing and self.coverage != "unsupported":
            return "partial"
        return self.coverage

    def generations(self) -> tuple[tuple[str, str], ...]:
        """``(project_id, generation_id)`` for the members actually usable."""
        return tuple((pin.member.project_id, pin.member.generation_id) for pin in self.available)

    def require_complete(self) -> None:
        """Fail when the vector is partial; the caller's ``require_complete_solution`` gate."""
        if self.missing:
            names = ", ".join(pin.member.project_id for pin in self.missing)
            raise CatalogMemberMissing(
                names,
                f"the pinned catalog vector is partial and require_complete_solution was "
                f"requested; unavailable members: {names}",
            )


MemberOpener = Callable[[CatalogMember], tuple[Manifest, Path]]


def pin_catalog(catalog: Catalog, *, open_member: MemberOpener) -> CatalogVector:
    """Resolve every member of one catalog generation to its exact pinned generation.

    ``open_member`` receives the pinned member and returns the manifest and generation
    directory that member names. It must not consult a project's ``current.json``: this
    function's whole purpose is that the same catalog bytes always select the same
    generation directory, in every process and on every later request.

    A member whose ``open_member`` raises :class:`CatalogMemberMissing` is recorded as
    missing with its reason instead of aborting the read, so a caller can distinguish
    ``partial`` from a failed query. Any other failure propagates: a programming error or an
    unreadable catalog pointer is not a partial vector.
    """
    pins: list[MemberPin] = []
    for member in catalog.vector():
        try:
            found, directory = open_member(member)
        except CatalogMemberMissing as exc:
            pins.append(MemberPin(member=member, status=MISSING, reason=str(exc)))
            continue
        pins.append(
            MemberPin(
                member=member,
                status=AVAILABLE,
                generation_dir=directory,
                manifest=found,
            )
        )
    return CatalogVector(
        catalog_generation_id=catalog.catalog_generation_id,
        solution_id=catalog.solution_id,
        coverage=catalog.coverage,
        pins=tuple(pins),
    )


def load_solution_catalog(location: CatalogLocation) -> Catalog:
    """Load the catalog generation the lane's pointer pins.

    The catalog pointer is the read-model commit point (SNP-07), so it is read once here and
    its generation vector is what the whole query uses. Nothing in this module reads a
    project lane pointer, and the pointer is not re-read per member: re-reading it is how two
    members of one answer end up from two catalog generations (SNP-03).
    """
    if not isinstance(location, CatalogLocation):
        raise CatalogInvalid("a resolved catalog location is required to load a catalog")
    catalog, _ = load_pinned_catalog_generation(location)
    return catalog


def load_pinned_catalog_generation(location: CatalogLocation) -> tuple[Catalog, Path]:
    """Load the catalog generation the lane pointer names, and its directory.

    The generation directory name must equal the digest of the catalog bytes inside it, so a
    renamed directory - or a catalog swapped under a directory that kept its name - is refused
    rather than read as the generation the pointer selected.
    """
    pointer = manifest_module.load_pointer(location.pointer)
    directory = location.generation_root(pointer.directory_name)
    catalog = load_catalog(directory / CATALOG_FILENAME)
    if directory.name != catalog.generation_id:
        raise CatalogInvalid(
            f"catalog generation directory {directory.name} does not match the catalog bytes "
            f"{catalog.generation_id}"
        )
    if pointer.generation_id != catalog.generation_id:
        raise CatalogInvalid(
            f"catalog pointer generation_id {pointer.generation_id} does not match the "
            f"canonical catalog bytes {catalog.generation_id}"
        )
    return catalog, directory


def project_member_opener(
    location_for: Callable[[str], SnapshotLocation],
) -> MemberOpener:
    """Build the member opener a live read uses: pin the member's exact generation.

    ``location_for(project_id)`` returns the project's lane location resolved from the trusted
    local bindings. The returned opener reads ``generations/<generation_id>/manifest.json``
    only - never ``current.json`` - and reports a mismatch as a missing member, because a
    generation directory whose contents hash to a different id is not the pinned generation.
    """

    def opener(member: CatalogMember) -> tuple[Manifest, Path]:
        try:
            location = location_for(member.project_id)
        except UnknownBinding as exc:
            raise CatalogMemberMissing(member.project_id, str(exc)) from exc
        directory = location.generation_root(member.generation_id)
        try:
            raw = manifest_module.read_bytes(directory / manifest_module.MANIFEST_FILENAME)
        except ManifestError as exc:
            raise CatalogMemberMissing(member.project_id, str(exc)) from exc
        found = manifest_module.load_manifest_bytes(
            raw, what=f"manifest {member.generation_id[:12]}"
        )
        if found.generation_id != member.generation_id:
            raise CatalogMemberMissing(
                member.project_id,
                f"generation directory {member.generation_id[:12]} holds manifest bytes "
                f"{found.generation_id[:12]}, not the pinned generation",
            )
        if found.source_fingerprint != member.source_fingerprint:
            raise CatalogMemberMissing(
                member.project_id,
                f"generation {member.generation_id[:12]} declares source fingerprint "
                f"{found.source_fingerprint[:12]}, not the pinned "
                f"{member.source_fingerprint[:12]}",
            )
        return found, directory

    return opener


__all__ = [
    "AVAILABLE",
    "CATALOG_FILENAME",
    "CATALOG_MEMBER_KEYS",
    "CATALOG_REQUIRED",
    "CATALOG_SCHEMA_VERSION",
    "COVERAGE_STATUSES",
    "Catalog",
    "CatalogError",
    "CatalogInvalid",
    "CatalogMember",
    "CatalogMemberMissing",
    "CatalogUnreadable",
    "CatalogVector",
    "LIVE_LANE",
    "MISSING",
    "NAME_ONLY_KEYS",
    "MemberPin",
    "UnsupportedCatalogMajor",
    "load_catalog",
    "load_catalog_bytes",
    "load_pinned_catalog_generation",
    "load_solution_catalog",
    "pin_catalog",
    "project_member_opener",
]
