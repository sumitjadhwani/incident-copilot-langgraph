FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first (cached layer), then the project.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY scenarios ./scenarios
COPY runbooks ./runbooks
COPY infra/postgres ./infra/postgres
COPY scripts/db_migrate.py ./scripts/db_migrate.py
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

CMD ["sh", "-c", "python scripts/db_migrate.py && exec uvicorn copilot.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
