"""Manifest and pointer validation: canonical bytes, digests and shard closure.

A published generation is three nested integrity claims, and this module is where
each of them becomes executable instead of assumed:

* the **pointer** ``current.json`` names exactly one generation and carries the
  digest of the manifest that generation is made of;
* the **manifest** lists every shard of that generation - path, role, SHA-256,
  byte size and record count - and its own identity is ``sha256`` over its
  canonical bytes, so nothing in the document has to hash itself;
* each **shard** is pinned by the manifest entry that names it.

The canonical byte rule is consumed, not re-invented. ``docs/11-GRAPH-DATA-CONTRACT.md``
section 5 fixes UTF-8 without a BOM, LF newlines, lexicographically sorted object
keys, compact separators, one trailing LF for a metadata document, integer counts
only, optional absent fields omitted rather than emitted as ``null``, and Unicode
preserved exactly as supplied. ``conformance/fixtures/canonical/`` pins those bytes
and ``tools/canonical_json.py`` is the specs-side reference serializer this module
has to agree with byte for byte, so a Rust and a Python serializer can be compared
against the same digests. The structural shape enforced here - required fields,
closed objects, the file role enum, the portable relative path pattern and the
1..16777216 byte range - is the one ``contracts/schemas/project-manifest.schema.json``
and ``contracts/schemas/pointer.schema.json`` already declare. Only ``json``,
``hashlib`` and ``re`` are used, so the reader works offline and in a
snapshot-only gateway.

Order matters in several places, and each choice is deliberate:

* a document's schema major is checked before its byte form, so a document that is
  both canonical and from an unknown major reports the version rather than a
  formatting complaint;
* a pointer's field set is checked before its digest pair, and the two digests are
  required to be equal because V2 fixes ``generation_id = manifest_sha256``;
* a shard's declared byte length is checked before its digest, so a truncated
  shard is reported as truncated rather than as a hash failure;
* a shard's digest is checked before it is parsed, so untrusted bytes are never
  handed to the JSON parser before they are known to be the published ones.

The record count a shard declares follows the shipped ``examples/snapshots``
generations: a JSON array shard holds ``len(array)`` records and a single JSON
object shard holds exactly one. That is the only rule the shipped bytes support.
A future role whose shard is an object *map* would need a canonical count rule
from ``axiom-specs`` before it could be read here, and this module refuses a shard
that is neither an array nor an object instead of guessing a count.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom_mcp.registry import CURRENT_POINTER_NAME, GENERATIONS_DIRNAME

SUPPORTED_SCHEMA_MAJOR = 1
MANIFEST_SCHEMA_VERSION = 1
POINTER_SCHEMA_VERSION = 1
MAX_SHARD_BYTES = 16_777_216

MANIFEST_FILENAME = "manifest.json"
POINTER_FILENAME = CURRENT_POINTER_NAME

CANONICAL_SEPARATORS = (",", ":")
UTF8_BOM = b"\xef\xbb\xbf"

FILE_ROLES: tuple[str, ...] = (
    "nodes",
    "edges",
    "symbol_index",
    "outgoing_index",
    "incoming_index",
    "architecture_summary",
    "coverage",
)

COVERAGE_STATUSES: tuple[str, ...] = ("complete_for_profile", "partial", "unsupported")

MANIFEST_REQUIRED: tuple[str, ...] = (
    "schema_version",
    "solution_id",
    "project_id",
    "analysis_profile",
    "generator_version",
    "analyzer_set_hash",
    "source_fingerprint",
    "config_fingerprint",
    "dependency_fingerprint",
    "coverage",
    "files",
)

MANIFEST_ENTRY_KEYS: tuple[str, ...] = ("path", "role", "sha256", "bytes", "records")

MANIFEST_FINGERPRINTS: tuple[str, ...] = (
    "analyzer_set_hash",
    "source_fingerprint",
    "config_fingerprint",
    "dependency_fingerprint",
)

COVERAGE_KEYS: tuple[str, ...] = (
    "status",
    "input_files",
    "processed_files",
    "unresolved_references",
    "unsupported_patterns",
)

POINTER_REQUIRED: tuple[str, ...] = ("schema_version", "generation_id", "manifest_sha256")

# The reference evaluator in tools/catalog_contract.py refuses a manifest that
# carries its own hash, because generation_id is defined over the bytes and a
# self-reference would make the hash circular.
SELF_HASH_FIELDS: tuple[str, ...] = (
    "manifest_sha256",
    "generation_id",
    "manifest_id",
    "self_sha256",
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

# The portable relative path rule, byte-identical to the pattern
# contracts/schemas/project-manifest.schema.json applies to a manifest file entry
# and to the request-side rule in axiom_mcp.registry. tests/test_manifest.py
# asserts this pattern equals the registry's, so the two cannot drift apart.
PORTABLE_RELATIVE_RE = re.compile(r"^(?!/)(?!.*\\)(?!.*(?:^|/)\.\.(?:/|$))(?![A-Za-z]:).+$")


class ManifestError(ValueError):
    """A pointer, manifest or shard document the reader must refuse."""


class CanonicalSerializationError(ManifestError):
    """A value that canonical metadata cannot represent (a float or a non-string key)."""


class ManifestUnreadable(ManifestError):
    """Bytes that are missing, unreadable, not UTF-8, BOM-prefixed or not JSON."""


class NotCanonicalBytes(ManifestError):
    """Bytes that are valid JSON but not the exact canonical form of their value."""


class UnsupportedSchemaMajor(ManifestError):
    """A document whose ``schema_version`` major this reader does not implement."""


class ManifestInvalid(ManifestError):
    """A structurally valid document that breaks a declared contract rule."""


class DuplicateManifestEntry(ManifestError):
    """Two manifest entries that collide on file path or on role."""


class DigestMismatch(ManifestError):
    """A claimed digest that does not cover the bytes it claims to cover."""


class ByteSizeMismatch(ManifestError):
    """A shard whose length disagrees with the manifest entry that pins it."""


class RecordCountMismatch(ManifestError):
    """A shard whose record count disagrees with the manifest entry that pins it."""


def is_sha256(value: Any) -> bool:
    """Return True for a lowercase 64 character hexadecimal digest."""
    return isinstance(value, str) and bool(SHA256_RE.match(value))


def sha256_hex(data: bytes | bytearray) -> str:
    """Return the lowercase SHA-256 of ``data``."""
    return hashlib.sha256(bytes(data)).hexdigest()


def canonicalization_reasons(value: Any) -> list[str]:
    """Return the canonical-serialization violations in ``value`` (empty means accept).

    Transcribed from the specs-side reference evaluator ``tools/canonical_json.py``:
    floats are not part of canonical V2 metadata and an object key must be a string.
    """
    reasons: list[str] = []
    if isinstance(value, float):
        reasons.append("float_not_allowed_in_canonical_metadata")
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                reasons.append("object_key_is_not_a_string")
            reasons.extend(canonicalization_reasons(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            reasons.extend(canonicalization_reasons(child))
    return reasons


def canonical_text(value: Any) -> str:
    """Canonical metadata text: compact, lexicographic keys, one trailing LF."""
    reasons = canonicalization_reasons(value)
    if reasons:
        raise CanonicalSerializationError("canonical serialization rejected: " + ",".join(reasons))
    rendered = json.dumps(
        value, sort_keys=True, separators=CANONICAL_SEPARATORS, ensure_ascii=False
    )
    return rendered + "\n"


def canonical_bytes(value: Any) -> bytes:
    """Canonical metadata bytes without a BOM; the sequence identity digests cover."""
    return canonical_text(value).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """SHA-256 over the canonical bytes of ``value``."""
    return sha256_hex(canonical_bytes(value))


def generation_id(manifest: Mapping[str, Any]) -> str:
    """Declared generation identity: sha256 over the canonical manifest bytes."""
    return canonical_sha256(manifest)


def is_canonical_bytes(raw: bytes | bytearray) -> bool:
    """Return True when ``raw`` is exactly the canonical form of its own parsed value."""
    try:
        document = _parse_json(_decode_utf8(raw, what="document"), what="document")
    except ManifestError:
        return False
    if canonicalization_reasons(document):
        return False
    return canonical_bytes(document) == bytes(raw)


def _decode_utf8(raw: bytes | bytearray, *, what: str) -> str:
    if not isinstance(raw, (bytes, bytearray)):
        raise ManifestUnreadable(f"{what} is not a byte string")
    data = bytes(raw)
    if data.startswith(UTF8_BOM):
        raise ManifestUnreadable(f"{what} starts with a UTF-8 byte order mark")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestUnreadable(f"{what} is not valid UTF-8: {exc.reason}") from exc


def _parse_json(text: str, *, what: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestUnreadable(f"{what} is not valid JSON: {exc.msg}") from exc


def _require_object(document: Any, *, what: str) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise ManifestInvalid(f"{what} is not a JSON object")
    return document


def _require_exact_keys(
    document: Mapping[str, Any], expected: tuple[str, ...], *, what: str
) -> None:
    keys = set(document)
    if keys != set(expected):
        missing = sorted(set(expected) - keys)
        unknown = sorted(keys - set(expected))
        raise ManifestInvalid(
            f"{what} fields do not match the contract; missing={missing} unknown={unknown}"
        )


def _require_schema_major(document: Mapping[str, Any], *, what: str) -> int:
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ManifestInvalid(f"{what} schema_version is not an integer")
    if version != SUPPORTED_SCHEMA_MAJOR:
        raise UnsupportedSchemaMajor(
            f"{what} schema major {version} is not supported by this reader "
            f"(supported major {SUPPORTED_SCHEMA_MAJOR})"
        )
    return version


def _require_canonical(raw: bytes | bytearray, document: Any, *, what: str) -> None:
    try:
        rendered = canonical_bytes(document)
    except CanonicalSerializationError as exc:
        raise NotCanonicalBytes(f"{what} cannot be canonical: {exc}") from exc
    if rendered != bytes(raw):
        raise NotCanonicalBytes(
            f"{what} is not in canonical form: it is not the compact, lexicographic, "
            "single-trailing-LF encoding of its own value"
        )


def _sha256_field(document: Mapping[str, Any], name: str, *, what: str) -> str:
    value = document.get(name)
    if not is_sha256(value):
        raise ManifestInvalid(f"{what} {name} is not a lowercase sha256 digest")
    return value


def _identifier_field(document: Mapping[str, Any], name: str, *, what: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not IDENTIFIER_RE.match(value):
        raise ManifestInvalid(f"{what} {name} is not a portable identifier")
    return value


def _text_field(document: Mapping[str, Any], name: str, *, what: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ManifestInvalid(f"{what} {name} must be a non-empty string")
    return value


def _integer_field(
    document: Mapping[str, Any], name: str, *, what: str, minimum: int, maximum: int | None = None
) -> int:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestInvalid(f"{what} {name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise ManifestInvalid(f"{what} {name} is outside the allowed range: {value}")
    return value


def _portable_path(value: Any, *, what: str) -> str:
    if not isinstance(value, str) or not PORTABLE_RELATIVE_RE.match(value):
        raise ManifestInvalid(f"{what} is not a portable relative path: {value!r}")
    return value


@dataclass(frozen=True)
class Pointer:
    """A validated ``current.json``: one generation, one manifest digest.

    ``generation_id`` and ``manifest_sha256`` are equal in V2, which is what makes
    the generation directory name and the manifest digest the same string.
    """

    generation_id: str
    manifest_sha256: str
    schema_version: int
    raw: bytes

    @property
    def directory_name(self) -> str:
        """The ``generations/<name>/`` directory this pointer selects."""
        return self.generation_id

    def as_document(self) -> dict[str, Any]:
        """Re-render the pointer as the plain document it was loaded from."""
        return {
            "schema_version": self.schema_version,
            "generation_id": self.generation_id,
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True)
class ManifestEntry:
    """One pinned shard: where it is, what it holds and how big it is."""

    path: str
    role: str
    sha256: str
    bytes: int
    records: int

    def as_document(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "records": self.records,
        }


@dataclass(frozen=True)
class ClosureReport:
    """The outcome of verifying every shard a manifest declares."""

    entries: int
    bytes: int
    records: int
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    """A validated project manifest plus the exact bytes its identity covers."""

    schema_version: int
    solution_id: str
    project_id: str
    analysis_profile: str
    generator_version: str
    analyzer_set_hash: str
    source_fingerprint: str
    config_fingerprint: str
    dependency_fingerprint: str
    coverage: Mapping[str, Any]
    files: tuple[ManifestEntry, ...]
    data: bytes

    @property
    def generation_id(self) -> str:
        """``sha256`` over the canonical manifest bytes, never a self-reference."""
        return sha256_hex(self.data)

    @property
    def byte_size(self) -> int:
        return len(self.data)

    @property
    def total_bytes(self) -> int:
        return sum(entry.bytes for entry in self.files)

    @property
    def total_records(self) -> int:
        return sum(entry.records for entry in self.files)

    @property
    def coverage_status(self) -> str:
        return str(self.coverage["status"])

    def roles(self) -> tuple[str, ...]:
        return tuple(entry.role for entry in self.files)

    def json_document(self) -> dict[str, Any]:
        """The manifest as a plain document, re-parsed from its own bytes."""
        return json.loads(self.data.decode("utf-8"))

    def entry(self, path: str) -> ManifestEntry:
        for entry in self.files:
            if entry.path == path:
                return entry
        raise ManifestInvalid(f"manifest has no entry for path {path!r}")

    def entries_for_role(self, role: str) -> tuple[ManifestEntry, ...]:
        return tuple(entry for entry in self.files if entry.role == role)

    def verify_shard(self, entry: ManifestEntry, data: bytes | bytearray) -> Any:
        """Verify one shard against its manifest entry and return the parsed value.

        The order is length, then digest, then parse, then record count: a truncated
        shard is reported as truncated, a substituted shard as a digest failure, and
        unparsable bytes are only reported after their bytes are already known to be
        the published ones.
        """
        payload = bytes(data)
        label = f"shard {entry.path}"
        if len(payload) != entry.bytes:
            raise ByteSizeMismatch(
                f"{label} is {len(payload)} bytes but the manifest declares {entry.bytes}"
            )
        digest = sha256_hex(payload)
        if digest != entry.sha256:
            raise DigestMismatch(f"{label} sha256 {digest} does not match manifest {entry.sha256}")
        document = _parse_json(_decode_utf8(payload, what=label), what=label)
        if isinstance(document, list):
            records = len(document)
        elif isinstance(document, Mapping):
            records = 1
        else:
            raise ManifestInvalid(f"{label} is neither a JSON array nor a JSON object")
        if records != entry.records:
            raise RecordCountMismatch(
                f"{label} holds {records} records but the manifest declares {entry.records}"
            )
        return document

    def verify_closure(self, read: Callable[[str], bytes]) -> ClosureReport:
        """Verify every declared shard through ``read``, refusing the first failure.

        ``read`` receives a manifest-relative path and returns the bytes of that
        shard. It is the caller's job to make that read safe; this method makes the
        closure rule - every declared shard present, sized, hashed and counted -
        executable in one place.
        """
        paths: list[str] = []
        total_bytes = 0
        total_records = 0
        for entry in sorted(self.files, key=lambda item: item.path):
            try:
                data = read(entry.path)
            except OSError as exc:
                raise ManifestUnreadable(
                    f"shard {entry.path} is unreadable: {type(exc).__name__}"
                ) from exc
            self.verify_shard(entry, data)
            paths.append(entry.path)
            total_bytes += entry.bytes
            total_records += entry.records
        return ClosureReport(
            entries=len(paths), bytes=total_bytes, records=total_records, paths=tuple(paths)
        )

    def verify_pointer(self, pointer: Pointer) -> None:
        """Close the pointer against this manifest's bytes."""
        digest = self.generation_id
        if pointer.generation_id != digest:
            raise DigestMismatch(
                f"pointer generation_id {pointer.generation_id} does not match the "
                f"canonical manifest bytes {digest}"
            )
        if pointer.manifest_sha256 != digest:
            raise DigestMismatch(
                f"pointer manifest_sha256 {pointer.manifest_sha256} does not match the "
                f"canonical manifest bytes {digest}"
            )
        if pointer.schema_version != self.schema_version:
            raise ManifestInvalid(
                f"pointer schema_version {pointer.schema_version} does not match the "
                f"manifest schema_version {self.schema_version}"
            )


def load_pointer_bytes(raw: bytes | bytearray, *, what: str = POINTER_FILENAME) -> Pointer:
    """Validate pointer bytes: schema major, canonical form, fields and digest pair."""
    document = _require_object(_parse_json(_decode_utf8(raw, what=what), what=what), what=what)
    _require_schema_major(document, what=what)
    _require_canonical(raw, document, what=what)
    _require_exact_keys(document, POINTER_REQUIRED, what=what)
    generation = _sha256_field(document, "generation_id", what=what)
    manifest_sha256 = _sha256_field(document, "manifest_sha256", what=what)
    if generation != manifest_sha256:
        raise ManifestInvalid(
            f"{what} manifest_sha256 does not equal generation_id; V2 fixes them equal"
        )
    return Pointer(
        generation_id=generation,
        manifest_sha256=manifest_sha256,
        schema_version=SUPPORTED_SCHEMA_MAJOR,
        raw=bytes(raw),
    )


def _validate_coverage(document: Mapping[str, Any], *, what: str) -> Mapping[str, Any]:
    coverage = document.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ManifestInvalid(f"{what} coverage is not a JSON object")
    _require_exact_keys(coverage, COVERAGE_KEYS, what=f"{what} coverage")
    status = coverage.get("status")
    if status not in COVERAGE_STATUSES:
        raise ManifestInvalid(f"{what} coverage status is not supported: {status!r}")
    inputs = _integer_field(coverage, "input_files", what=f"{what} coverage", minimum=0)
    processed = _integer_field(coverage, "processed_files", what=f"{what} coverage", minimum=0)
    _integer_field(coverage, "unresolved_references", what=f"{what} coverage", minimum=0)
    if processed > inputs:
        raise ManifestInvalid(
            f"{what} coverage processed_files exceeds input_files: {processed} > {inputs}"
        )
    patterns = coverage.get("unsupported_patterns")
    if not isinstance(patterns, list):
        raise ManifestInvalid(f"{what} coverage unsupported_patterns is not a list")
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            raise ManifestInvalid(f"{what} coverage has a non-string unsupported pattern")
    return coverage


def _validate_entry(item: Any, *, index: int, what: str) -> ManifestEntry:
    label = f"{what} files[{index}]"
    if not isinstance(item, Mapping):
        raise ManifestInvalid(f"{label} is not a JSON object")
    _require_exact_keys(item, MANIFEST_ENTRY_KEYS, what=label)
    path = _portable_path(item.get("path"), what=f"{label} path")
    role = item.get("role")
    if role not in FILE_ROLES:
        raise ManifestInvalid(f"{label} role is not supported: {role!r}")
    digest = _sha256_field(item, "sha256", what=label)
    size = _integer_field(item, "bytes", what=label, minimum=1, maximum=MAX_SHARD_BYTES)
    records = _integer_field(item, "records", what=label, minimum=0)
    return ManifestEntry(path=path, role=role, sha256=digest, bytes=size, records=records)


def load_manifest_bytes(raw: bytes | bytearray, *, what: str = MANIFEST_FILENAME) -> Manifest:
    """Validate manifest bytes and return the manifest plus the bytes it is defined by.

    Refused here, each for its own reason: an unknown schema major, bytes that are not
    the canonical encoding of their own value, an unknown or missing field, a manifest
    that carries its own hash, a duplicate file path or duplicate role, an unsupported
    role, a path that is not portable and relative, a digest that is not a lowercase
    sha256, a shard size outside 1..16777216 and a coverage count that exceeds its input
    count. This is the check AC1 of C-013 asks for: altered bytes, unknown major and
    duplicate entries fail, and a canonical example passes.
    """
    document = _require_object(_parse_json(_decode_utf8(raw, what=what), what=what), what=what)
    _require_schema_major(document, what=what)
    _require_canonical(raw, document, what=what)

    for field in SELF_HASH_FIELDS:
        if field in document:
            raise ManifestInvalid(
                f"{what} contains its own hash field {field!r}; the generation identity "
                "is defined over these bytes and must not reference itself"
            )
    _require_exact_keys(document, MANIFEST_REQUIRED, what=what)

    solution_id = _identifier_field(document, "solution_id", what=what)
    project_id = _identifier_field(document, "project_id", what=what)
    analysis_profile = _text_field(document, "analysis_profile", what=what)
    generator_version = _text_field(document, "generator_version", what=what)
    fingerprints = {
        name: _sha256_field(document, name, what=what) for name in MANIFEST_FINGERPRINTS
    }
    coverage = _validate_coverage(document, what=what)

    files = document.get("files")
    if not isinstance(files, list) or not files:
        raise ManifestInvalid(f"{what} files is not a non-empty list")
    entries: list[ManifestEntry] = []
    seen_paths: set[str] = set()
    seen_roles: set[str] = set()
    for index, item in enumerate(files):
        entry = _validate_entry(item, index=index, what=what)
        if entry.path in seen_paths:
            raise DuplicateManifestEntry(f"{what} declares duplicate file path {entry.path!r}")
        if entry.role in seen_roles:
            raise DuplicateManifestEntry(f"{what} declares duplicate file role {entry.role!r}")
        seen_paths.add(entry.path)
        seen_roles.add(entry.role)
        entries.append(entry)

    return Manifest(
        schema_version=SUPPORTED_SCHEMA_MAJOR,
        solution_id=solution_id,
        project_id=project_id,
        analysis_profile=analysis_profile,
        generator_version=generator_version,
        analyzer_set_hash=fingerprints["analyzer_set_hash"],
        source_fingerprint=fingerprints["source_fingerprint"],
        config_fingerprint=fingerprints["config_fingerprint"],
        dependency_fingerprint=fingerprints["dependency_fingerprint"],
        coverage=coverage,
        files=tuple(entries),
        data=bytes(raw),
    )


def read_bytes(path: Path) -> bytes:
    """Read ``path``, reporting a failure as the file name only, never as a full path."""
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise ManifestUnreadable(f"{Path(path).name} is unreadable: {type(exc).__name__}") from exc


def load_pointer(path: Path) -> Pointer:
    """Load and validate a pointer document from disk."""
    return load_pointer_bytes(read_bytes(path), what=Path(path).name)


def load_generation(pointer_path: Path) -> tuple[Pointer, Manifest, Path]:
    """Load a pointer, the generation directory it names and the manifest inside it.

    The generation directory is ``<pointer-directory>/generations/<generation_id>``, and
    its name must equal the digest of the manifest bytes, so a directory that was renamed
    - or a manifest swapped under a directory that kept its name - is refused rather than
    read as if it were the generation the pointer selected.
    """
    pointer = load_pointer(pointer_path)
    directory = Path(pointer_path).parent / GENERATIONS_DIRNAME / pointer.directory_name
    manifest = load_manifest_bytes(
        read_bytes(directory / MANIFEST_FILENAME), what=f"manifest {directory.name}"
    )
    if directory.name != manifest.generation_id:
        raise DigestMismatch(
            f"generation directory {directory.name} does not match the manifest bytes "
            f"{manifest.generation_id}"
        )
    manifest.verify_pointer(pointer)
    return pointer, manifest, directory


__all__ = [
    "CANONICAL_SEPARATORS",
    "COVERAGE_KEYS",
    "COVERAGE_STATUSES",
    "ClosureReport",
    "DuplicateManifestEntry",
    "ByteSizeMismatch",
    "CanonicalSerializationError",
    "DigestMismatch",
    "FILE_ROLES",
    "IDENTIFIER_RE",
    "MANIFEST_ENTRY_KEYS",
    "MANIFEST_FILENAME",
    "MANIFEST_REQUIRED",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_SHARD_BYTES",
    "Manifest",
    "ManifestEntry",
    "ManifestError",
    "ManifestInvalid",
    "ManifestUnreadable",
    "NotCanonicalBytes",
    "POINTER_FILENAME",
    "POINTER_REQUIRED",
    "POINTER_SCHEMA_VERSION",
    "PORTABLE_RELATIVE_RE",
    "Pointer",
    "RecordCountMismatch",
    "SHA256_RE",
    "SUPPORTED_SCHEMA_MAJOR",
    "UnsupportedSchemaMajor",
    "canonical_bytes",
    "canonical_sha256",
    "canonical_text",
    "canonicalization_reasons",
    "generation_id",
    "is_canonical_bytes",
    "is_sha256",
    "load_generation",
    "load_manifest_bytes",
    "load_pointer",
    "load_pointer_bytes",
    "read_bytes",
    "sha256_hex",
]
