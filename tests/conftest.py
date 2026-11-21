from __future__ import annotations

import asyncio
import sys


def pytest_asyncio_loop_factories(config, item):
    """psycopg's async mode cannot run on Windows' default Proactor loop."""
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return None
