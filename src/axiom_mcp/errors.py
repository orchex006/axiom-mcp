"""Structured MCP error mapping plus redaction at the response boundary.

Two different things are called "an error" in MCP, and conflating them is how a
caller ends up retrying a malformed request forever or treating an unavailable
snapshot as a protocol bug:

* a **protocol error** is a JSON-RPC failure - the request never became a
  meaningful call, so it is reported in ``error`` with a JSON-RPC code and the
  canonical Axiom code carried in ``error.data.code``;
* a **tool-result error** is a call that ran and failed for a domain reason -
  the snapshot expired, the project is missing, the daemon is unavailable - so
  it is reported as a tool result with ``isError: true`` and a structured body,
  which is what lets a host show a real diagnosis instead of a transport fault.

Stack traces never cross this boundary. :class:`AxiomError` records the *type*
of a cause, never a formatted traceback, and every rendered message and detail
value is redacted first. Redaction follows the canonical policy in
``axiom-specs/contracts/redaction-policy.md``: the class table, the placeholder
``<redacted:{category}>`` and the allowlisted metadata keys are transcribed from
that document rather than invented here, and ``tests/test_errors.py`` verifies
this module against the policy's executable reference evaluator case by case, so
a drift is a test failure instead of a silent divergence.

The envelope is the one the canonical control contract fixes -
``{code, message, retryable, details, request_id}`` - and the codes are the ones
``contracts/schemas/query-response.schema.json`` enumerates.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

REDACTION_PLACEHOLDER = "<redacted:{category}>"
BACKSLASH = chr(92)
FORWARD_SLASH = chr(47)

# Transcribed from contracts/redaction-policy.md section 2. Order is report
# order, not priority: every class is reported.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("cloud_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("provider_token", re.compile(r"\b(?:gh[pousr]|glpat|xox[baprs])_[A-Za-z0-9_-]{16,}\b")),
    ("provider_token", re.compile(r"\bsk-[A-Za-z0-9]{24,}\b")),
    ("bearer_token", re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b")),
    (
        "password_assignment",
        re.compile(
            r"\b(?:password|passwd|pwd|secret|token|api[_-]?key)\b[ \t]*[:=][ \t]*[^\s,;]{6,}",
            re.I,
        ),
    ),
    ("credential_url", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]{1,64}:[^/\s@]{1,64}@", re.I)),
    (
        "odbc_connection_string",
        re.compile(
            r"\b(?:Data Source|Server|Initial Catalog)\b[ \t]*=[^;\n]+;[\s\S]{0,200}?"
            r"\b(?:Password|Pwd)\b[ \t]*=[^;\s]+",
            re.I,
        ),
    ),
    (
        "windows_absolute_path",
        re.compile(r"(?<![\w$])[A-Za-z]:[\\/]+(?:[^\\/\s\"'<>|]+[\\/]+)*[^\\/\s\"'<>|]*"),
    ),
    ("unc_path", re.compile(r"\\\\+[A-Za-z0-9._$-]{1,64}\\\\+[^\\/\s]{1,64}")),
    (
        "posix_absolute_path",
        re.compile(
            r"(?<![\w:/.-])/(?:home|Users|root|usr|etc|var|opt|private|mnt|Volumes|tmp|srv)"
            r"(?:/[^\s\"',;:)\]}]*)?"
        ),
    ),
)

PROHIBITED_CLASSES: tuple[str, ...] = tuple(dict.fromkeys(category for category, _ in PATTERNS))

ABSOLUTE_PATH_CLASSES = frozenset({"windows_absolute_path", "posix_absolute_path", "unc_path"})

# Transcribed from contracts/redaction-policy.md section 3.
ALLOWED_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "solution_id",
        "project_id",
        "repo",
        "path",
        "language",
        "symbol_kind",
        "symbol_name",
        "line_start",
        "line_end",
        "content_sha256",
        "source_fingerprint",
        "generation_id",
        "bytes",
        "nodes_count",
        "edges_count",
        "coverage",
        "analysis_profile",
        "timestamp_utc",
        "error_code",
        "request_id",
        "redaction_notice",
    }
)

# Canonical codes: contracts/schemas/query-response.schema.json queryError plus
# PROJECT_UNAVAILABLE, SNAPSHOT_UNAVAILABLE and SNAPSHOT_CORRUPT, which the read
# protocol names in docs/12-SNAPSHOT-READ-WRITE-PROTOCOL.md section 5 and
# docs/14-MULTI-PROJECT-SOLUTIONS.md.
CANONICAL_CODES: frozenset[str] = frozenset(
    {
        "UNSUPPORTED_OPERATION",
        "LIMIT_EXCEEDED",
        "VALIDATION_ERROR",
        "UNAUTHENTICATED",
        "FORBIDDEN",
        "NOT_FOUND",
        "CONFLICT",
        "INCOMPATIBLE_INPUT",
        "RATE_LIMITED",
        "SNAPSHOT_EXPIRED",
        "SNAPSHOT_UNAVAILABLE",
        "SNAPSHOT_CORRUPT",
        "DAEMON_UNAVAILABLE",
        "PROJECT_UNAVAILABLE",
        "NOT_READY",
        "INTERNAL_ERROR",
    }
)

SURFACE_PROTOCOL = "protocol"
SURFACE_TOOL_RESULT = "tool_result"

# A code that describes the request itself is a protocol error; a code that
# describes the queried subject is a tool-result error the host can act on.
PROTOCOL_SURFACE_CODES: frozenset[str] = frozenset(
    {
        "VALIDATION_ERROR",
        "INCOMPATIBLE_INPUT",
        "UNAUTHENTICATED",
        "FORBIDDEN",
        "UNSUPPORTED_OPERATION",
    }
)

RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "RATE_LIMITED",
        "NOT_READY",
        "DAEMON_UNAVAILABLE",
        "PROJECT_UNAVAILABLE",
        "SNAPSHOT_UNAVAILABLE",
    }
)

# Canonical code -> HTTP status, from contracts/control-api-v1.md. Only the codes
# that contract names are listed; the rest keep their surface and are not given
# an invented status.
CODE_TO_HTTP_STATUS: dict[str, int] = {
    "VALIDATION_ERROR": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "INCOMPATIBLE_INPUT": 422,
    "RATE_LIMITED": 429,
    "NOT_READY": 503,
    "DAEMON_UNAVAILABLE": 503,
    "PROJECT_UNAVAILABLE": 503,
    "SNAPSHOT_UNAVAILABLE": 503,
    "SNAPSHOT_EXPIRED": 410,
}

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# Canonical code -> JSON-RPC code used when the failure is a protocol error.
CODE_TO_JSONRPC: dict[str, int] = {
    "VALIDATION_ERROR": JSONRPC_INVALID_PARAMS,
    "INCOMPATIBLE_INPUT": JSONRPC_INVALID_PARAMS,
    "UNSUPPORTED_OPERATION": JSONRPC_METHOD_NOT_FOUND,
    "UNAUTHENTICATED": JSONRPC_INVALID_REQUEST,
    "FORBIDDEN": JSONRPC_INVALID_REQUEST,
    "NOT_FOUND": JSONRPC_METHOD_NOT_FOUND,
    "INTERNAL_ERROR": JSONRPC_INTERNAL_ERROR,
}

DEFAULT_MAX_DETAIL_DEPTH = 6
DEFAULT_MAX_DETAIL_ITEMS = 64
DEFAULT_MAX_DETAIL_STRING = 2048

TRACEBACK_MARKERS: tuple[str, ...] = ("Traceback (most recent call last)", 'File "')

__all__ = [
    "ALLOWED_METADATA_KEYS",
    "AxiomError",
    "CANONICAL_CODES",
    "CODE_TO_HTTP_STATUS",
    "CODE_TO_JSONRPC",
    "DEFAULT_MAX_DETAIL_DEPTH",
    "DEFAULT_MAX_DETAIL_ITEMS",
    "DEFAULT_MAX_DETAIL_STRING",
    "PROHIBITED_CLASSES",
    "PROTOCOL_SURFACE_CODES",
    "REDACTION_PLACEHOLDER",
    "RETRYABLE_CODES",
    "SURFACE_PROTOCOL",
    "SURFACE_TOOL_RESULT",
    "categories",
    "contains_traceback",
    "default_surface",
    "error_payload",
    "find_violations",
    "from_exception",
    "is_clean",
    "metadata_violations",
    "protocol_error",
    "redact_details",
    "redact_text",
    "render",
    "repository_relative_path",
    "to_http_status",
    "tool_result_error",
]


# --- redaction, matching the canonical reference evaluator -----------------


def find_violations(text: str) -> list[dict[str, Any]]:
    """Return ``{"category", "span"}`` records. Never returns the matched bytes."""
    violations: list[dict[str, Any]] = []
    for category, pattern in PATTERNS:
        for match in pattern.finditer(text):
            violations.append({"category": category, "span": [match.start(), match.end()]})
    violations.sort(key=lambda row: (row["span"][0], row["span"][1]))
    return violations


def categories(text: str) -> set[str]:
    return {row["category"] for row in find_violations(text)}


def is_clean(text: str) -> bool:
    return not find_violations(text)


def repository_relative_path(raw: str, root: str | None) -> str | None:
    """Map an absolute path inside ``root`` to its repository-relative form."""
    if root is None:
        return None
    root_norm = str(root).replace(BACKSLASH, FORWARD_SLASH).rstrip(FORWARD_SLASH)
    candidate = raw.replace(BACKSLASH, FORWARD_SLASH)
    if not root_norm:
        return None
    lowered_root = root_norm.lower()
    lowered_candidate = candidate.lower()
    if lowered_candidate == lowered_root:
        return "."
    prefix = lowered_root + FORWARD_SLASH
    if lowered_candidate.startswith(prefix):
        return candidate[len(root_norm) + 1 :]
    return None


def redact_text(text: str, root: str | None = None) -> str:
    """Replace every prohibited span; an in-root absolute path becomes relative."""
    rows = find_violations(text)
    out = text
    for row in sorted(rows, key=lambda r: r["span"][0], reverse=True):
        start, end = row["span"]
        replacement = None
        if row["category"] in ABSOLUTE_PATH_CLASSES:
            relative = repository_relative_path(text[start:end], root)
            if relative is not None:
                replacement = relative
        if replacement is None:
            replacement = REDACTION_PLACEHOLDER.format(category=row["category"])
        out = out[:start] + replacement + out[end:]
    return out


def metadata_violations(value: Any, path: str = "$") -> list[dict[str, Any]]:
    """Apply the metadata allowlist and the text scan to a query metadata object."""
    problems: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in ALLOWED_METADATA_KEYS:
                problems.append(
                    {"category": "metadata_key_not_allowlisted", "path": f"{path}.{key}"}
                )
                continue
            problems.extend(metadata_violations(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problems.extend(metadata_violations(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        for row in find_violations(value):
            problems.append({"category": row["category"], "path": path, "span": row["span"]})
    elif value is None or isinstance(value, (bool, int, float)):
        pass
    else:
        problems.append({"category": "metadata_value_not_scalar", "path": path})
    return problems


def redact_details(
    value: Any,
    root: str | None = None,
    *,
    depth: int = 0,
    max_depth: int = DEFAULT_MAX_DETAIL_DEPTH,
    max_items: int = DEFAULT_MAX_DETAIL_ITEMS,
    max_string: int = DEFAULT_MAX_DETAIL_STRING,
) -> Any:
    """Redact a detail tree, bounded in depth, width and string length.

    Redaction happens before truncation, so a bound can never expose a fragment
    of a value that the policy removes.
    """
    if depth >= max_depth:
        return REDACTION_PLACEHOLDER.format(category="detail_depth_exceeded")
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                out["_truncated"] = True
                break
            safe_key = redact_text(str(key), root)
            out[safe_key] = redact_details(
                item,
                root,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )
        return out
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        redacted = [
            redact_details(
                item,
                root,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            redacted.append(REDACTION_PLACEHOLDER.format(category="detail_items_truncated"))
        return redacted
    if isinstance(value, str):
        return redact_text(value, root)[:max_string]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return REDACTION_PLACEHOLDER.format(category="detail_value_not_serializable")


def contains_traceback(text: str) -> bool:
    """Return True when ``text`` looks like a formatted stack trace."""
    return any(marker in text for marker in TRACEBACK_MARKERS)


# --- canonical error model -------------------------------------------------


def default_surface(code: str) -> str:
    """Return the surface a canonical code belongs to unless overridden."""
    return SURFACE_PROTOCOL if code in PROTOCOL_SURFACE_CODES else SURFACE_TOOL_RESULT


def to_http_status(code: str) -> int:
    """Return the canonical HTTP status for a code, defaulting to 500."""
    return CODE_TO_HTTP_STATUS.get(code, 500)


class AxiomError(Exception):
    """A structured failure carrying a canonical code and a safe surface.

    ``message`` and every value in ``details`` are redacted on construction, so a
    caller cannot accidentally hand an unredacted structure to a renderer. A
    cause is recorded as its type name only: this class has no field that can
    hold a formatted traceback.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        surface: str | None = None,
        root: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        if code not in CANONICAL_CODES:
            raise ValueError(f"unregistered canonical error code: {code}")
        if surface is not None and surface not in {SURFACE_PROTOCOL, SURFACE_TOOL_RESULT}:
            raise ValueError(f"unknown error surface: {surface}")
        redacted_message = redact_text(str(message), root).strip()
        self.code = code
        self.message = redacted_message or code
        self.retryable = (code in RETRYABLE_CODES) if retryable is None else retryable
        self.details: dict[str, Any] = dict(redact_details(details, root) or {})
        self.request_id = request_id
        self.surface = surface if surface is not None else default_surface(code)
        self.root = root
        self.cause_type = type(cause).__name__ if cause is not None else None
        super().__init__(f"{code}: {self.message}")

    @property
    def status(self) -> int:
        return to_http_status(self.code)

    def as_dict(self, *, request_id: str | None = None) -> dict[str, Any]:
        """Render the canonical envelope, redacted and traceback-free."""
        return error_payload(self, request_id=request_id)


def from_exception(
    exc: BaseException,
    *,
    code: str = "INTERNAL_ERROR",
    root: str | None = None,
    safe_message: str | None = None,
    retryable: bool | None = None,
    details: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> AxiomError:
    """Convert an arbitrary exception into a safe :class:`AxiomError`.

    For ``INTERNAL_ERROR`` the message is a fixed sentence unless the caller
    supplies one, because an unexpected exception's text is exactly the kind of
    string that carries a token or an absolute path. The exception type is
    recorded; the traceback is not.
    """
    if safe_message is None:
        safe_message = "internal error" if code == "INTERNAL_ERROR" else str(exc)
    merged: dict[str, Any] = dict(details or {})
    merged.setdefault("exception_type", type(exc).__name__)
    return AxiomError(
        code,
        safe_message,
        retryable=retryable,
        details=merged,
        request_id=request_id,
        root=root,
        cause=exc,
    )


def error_payload(error: AxiomError, *, request_id: str | None = None) -> dict[str, Any]:
    """Render the canonical ``{code,message,retryable,details,request_id}`` body."""
    payload: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        "details": dict(error.details),
    }
    correlation = request_id if request_id is not None else error.request_id
    if correlation is not None:
        payload["request_id"] = correlation
    return payload


def protocol_error(error: AxiomError, *, rpc_id: Any = None) -> dict[str, Any]:
    """Render a JSON-RPC error object for a protocol-surface failure.

    The JSON-RPC code stays useful to a generic client while the canonical Axiom
    code travels in ``error.data.code``, so neither layer has to guess what the
    other meant.
    """
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {
            "code": CODE_TO_JSONRPC.get(error.code, JSONRPC_INTERNAL_ERROR),
            "message": error.message,
            "data": error_payload(error),
        },
    }


def tool_result_error(error: AxiomError) -> dict[str, Any]:
    """Render a tool-result error: a real diagnosis instead of a transport fault."""
    return {
        "content": [{"type": "text", "text": f"{error.code}: {error.message}"}],
        "structuredContent": {"error": error_payload(error)},
        "isError": True,
    }


def render(error: AxiomError, *, rpc_id: Any = None) -> dict[str, Any]:
    """Render ``error`` on the surface it belongs to."""
    if error.surface == SURFACE_PROTOCOL:
        return protocol_error(error, rpc_id=rpc_id)
    return tool_result_error(error)


@dataclass(frozen=True)
class ErrorSummary:
    """A compact, safe projection used for logs and evidence."""

    code: str
    surface: str
    retryable: bool

    @classmethod
    def of(cls, error: AxiomError) -> ErrorSummary:
        return cls(code=error.code, surface=error.surface, retryable=error.retryable)
