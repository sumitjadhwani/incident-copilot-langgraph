"""Long-term memory on the LangGraph Store.

Namespaces are always prefixed with the tenant, so one tenant can never read another's memory:

    ("tenants", <tenant_id>, "incidents")  key = thread_id   past incident summaries
    ("tenants", <tenant_id>, "runbooks")   key = category    team runbooks

The tenant comes from graph state set by trusted code, never from the model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from copilot.config import RUNBOOKS_DIR


def incidents_ns(tenant_id: str) -> tuple[str, ...]:
    return ("tenants", tenant_id, "incidents")


def runbooks_ns(tenant_id: str) -> tuple[str, ...]:
    return ("tenants", tenant_id, "runbooks")


async def recall_similar(store: Any, tenant_id: str | None, service: str, limit: int = 3) -> list[dict[str, Any]]:
    if store is None or not tenant_id:
        return []
    items = await store.asearch(incidents_ns(tenant_id), filter={"service": service}, limit=limit)
    return [item.value for item in items]


async def remember_incident(store: Any, tenant_id: str | None, thread_id: str, record: dict[str, Any]) -> None:
    if store is None or not tenant_id:
        return
    await store.aput(incidents_ns(tenant_id), thread_id, {**record, "recorded_at": datetime.now(UTC).isoformat()})


async def get_runbook(store: Any, tenant_id: str | None, category: str | None) -> str | None:
    if store is None or not tenant_id or not category:
        return None
    item = await store.aget(runbooks_ns(tenant_id), category)
    return item.value.get("text") if item else None


def load_runbook_files() -> dict[str, str]:
    """Read runbooks/*.md. The first line must be `category: <category>`."""
    runbooks: dict[str, str] = {}
    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        first, _, body = path.read_text(encoding="utf-8").partition("\n")
        if not first.startswith("category:"):
            raise ValueError(f"{path.name}: first line must be 'category: <name>'")
        runbooks[first.split(":", 1)[1].strip()] = body.strip()
    return runbooks


async def seed_runbooks(store: Any, tenant_ids: list[str]) -> int:
    count = 0
    for category, text in load_runbook_files().items():
        for tenant_id in tenant_ids:
            await store.aput(runbooks_ns(tenant_id), category, {"category": category, "text": text})
            count += 1
    return count
