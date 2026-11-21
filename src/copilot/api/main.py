"""ASGI entry point: `uvicorn copilot.api.main:app`."""

from __future__ import annotations

import logging

from copilot.api.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()
