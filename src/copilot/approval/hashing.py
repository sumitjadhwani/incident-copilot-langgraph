"""Canonical hashing for actions and audit rows."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(data: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 kept as-is."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def action_hash(action_payload: dict[str, Any], *, thread_id: str, tenant_id: str) -> str:
    """Bind an approval to one exact action in one thread of one tenant.

    Changing any argument, or replaying the approval on another thread or tenant, changes the hash.
    """
    return sha256_hex(canonical_json({"action": action_payload, "thread_id": thread_id, "tenant_id": tenant_id}))
