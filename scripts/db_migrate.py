"""Create/upgrade the database: app role, LangGraph checkpointer + store tables, audit schema.

uv run python scripts/db_migrate.py            # migrate and seed runbooks for the demo tenants
uv run python scripts/db_migrate.py --verify   # also verify the audit hash chain
"""

from __future__ import annotations

import argparse

from copilot.config import get_settings, run_async
from copilot.db import migrate, persistence
from copilot.memory.store import seed_runbooks

DEMO_TENANTS = ["acme", "globex"]


async def main(verify: bool) -> None:
    settings = get_settings()
    await migrate(settings.admin_database_url, settings.database_url)
    print("migrations applied")
    async with persistence(settings) as p:
        count = await seed_runbooks(p.store, DEMO_TENANTS)
        print(f"seeded {count} runbook entries for tenants {DEMO_TENANTS}")
        if verify:
            report = await p.audit.verify_chain()
            print(
                f"audit chain ok={report.ok} rows={report.rows_checked}"
                + (f" first_bad_id={report.first_bad_id} ({report.reason})" if not report.ok else "")
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verify", action="store_true")
    run_async(main(parser.parse_args().verify))
