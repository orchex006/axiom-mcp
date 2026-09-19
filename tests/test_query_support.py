"""Shared builders for the bounded query engine tests.

These helpers build *pinned generation documents* - the exact JSON a ``nodes/*`` or ``edges/*``
shard holds - so a query test can state a graph in a few lines and still exercise the real parsers
in :mod:`axiom_mcp.query.model`. They are deliberately not fixtures on disk: a traversal bound or
a cycle is easier to read where it is asserted, and the vendored ``auth-api`` generation is still
used by the integration tests that need real shard bytes.
"""

from __future__ import annotations

import hashlib
from typing import Any

DEFAULT_PROJECT = "auth-api"
DEFAULT_FILE = "src/Auth.Api/AuthController.cs"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generation_id(seed: str) -> str:
    return digest(f"generation:{seed}")


def node_document(
    name: str,
    *,
    project_id: str = DEFAULT_PROJECT,
    kind: str = "Method",
    qualified_name: str | None = None,
    language: str = "csharp",
    file: str = DEFAULT_FILE,
    start_line: int = 1,
    end_line: int = 2,
    identity_quality: str = "semantic_key",
    attributes: dict[str, Any] | None = None,
    node_id: str | None = None,
) -> dict[str, Any]:
    qualified = qualified_name if qualified_name is not None else name
    return {
        "id": node_id or digest(f"{project_id}:{qualified}"),
        "project_id": project_id,
        "kind": kind,
        "name": name,
        "qualified_name": qualified,
        "language": language,
        "source": {"file": file, "start_line": start_line, "end_line": end_line},
        "identity_quality": identity_quality,
        "attributes": dict(attributes or {}),
    }


def edge_document(
    source_id: str,
    *,
    target_id: str | None = None,
    target_project_id: str = DEFAULT_PROJECT,
    kind: str = "CALLS",
    resolution: str = "exact_static",
    unresolved_target: str | None = None,
    analyzer_id: str = "csharp-roslyn",
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": digest(
            f"edge:{source_id}:{target_id or unresolved_target}:{kind}:{resolution}:{analyzer_id}"
        ),
        "source_id": source_id,
        "target_project_id": target_project_id,
        "kind": kind,
        "resolution": resolution,
        "evidence": [
            {
                "source": {"file": DEFAULT_FILE, "start_line": 3, "end_line": 4},
                "rule_id": "method-invocation",
            }
        ],
        "analyzer_id": analyzer_id,
    }
    if target_id is not None:
        document["target_id"] = target_id
    else:
        document["unresolved_target"] = unresolved_target or "Unknown.Target"
    return document
