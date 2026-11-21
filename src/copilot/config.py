"""Application settings and the chat-model factory.

Settings come from environment variables (and `.env` in local dev). Provider SDKs such as
langchain-deepseek and LangSmith read their keys straight from `os.environ`, so `load_env()`
exports `.env` into the process environment as well.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = PROJECT_ROOT / "scenarios"
RUNBOOKS_DIR = PROJECT_ROOT / "runbooks"

LLMProvider = Literal["deepseek", "google_genai"]

DEFAULT_MODELS: dict[str, str] = {
    "deepseek": "deepseek-flash",
    "google_genai": "gemini-2.5-flash",
}


def load_env() -> None:
    """Export `.env` into os.environ without overriding variables that are already set."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COPILOT_", extra="ignore")

    # --- LLM -------------------------------------------------------------------------------
    llm_provider: LLMProvider = "deepseek"
    # Empty means "use the provider's default from DEFAULT_MODELS".
    llm_model: str = ""
    llm_temperature: float = 0.0
    # DeepSeek "thinking" for the tool-using agent loops. Structured-output calls always run with
    # thinking off, because DeepSeek's thinking mode rejects a forced tool_choice.
    llm_agent_thinking: bool = True

    # --- Graph budgets ----------------------------------------------------------------------
    recursion_limit: int = 80
    triage_max_tool_rounds: int = 3
    hypothesis_max_tool_rounds: int = 3
    max_hypotheses: int = 3
    max_diagnosis_attempts: int = 2

    # --- Postgres ---------------------------------------------------------------------------
    # The app connects as a restricted role (no UPDATE/DELETE on the audit table).
    database_url: str = "postgresql://copilot_app:copilot_app@localhost:5432/copilot"
    # Migrations run as the owner role.
    admin_database_url: str = "postgresql://copilot:copilot@localhost:5432/copilot"

    # --- Auth -------------------------------------------------------------------------------
    # "keycloak" verifies JWTs. "dev" trusts a fixed local identity: tests and offline dev only.
    auth_mode: Literal["keycloak", "dev"] = "keycloak"
    # Public issuer (what the browser sees, and what appears in the token's `iss`).
    keycloak_issuer: str = "http://localhost:8080/realms/incident-copilot"
    # Where the API fetches signing keys; differs from the issuer inside docker-compose.
    keycloak_jwks_url: str = ""
    keycloak_ui_client_id: str = "copilot-ui"
    jwt_audience: str = "copilot-api"
    approver_role: str = "sre-approver"
    responder_role: str = "sre"
    # Four-eyes rule: the person who started an incident cannot approve its actions.
    require_distinct_approver: bool = True

    # Dev identity (auth_mode=dev only).
    dev_user_id: str = "dev-user"
    dev_username: str = "dev"
    dev_tenant: str = "acme"
    dev_roles: str = "sre,sre-approver"

    # --- Tool backend ----------------------------------------------------------------------
    # mock: synthetic scenarios. mcp: kind cluster through MCP tool servers (user identity forwarded).
    tool_backend: Literal["mock", "mcp"] = "mock"
    mcp_observability_url: str = "http://localhost:30081/mcp"
    mcp_k8s_read_url: str = "http://localhost:30082/mcp"
    mcp_k8s_write_url: str = "http://localhost:30083/mcp"
    mcp_timeout_s: float = 30.0
    # Post-action verification: keep checking until healthy for `verify_stable_s` or `verify_timeout_s`
    # passes. Unset means "instant" for mock (snapshots change immediately) and real waits for mcp.
    verify_timeout_s: float | None = None
    verify_stable_s: float | None = None
    verify_interval_s: float = 5.0

    # --- Tracing -----------------------------------------------------------------------------
    # OTLP/HTTP endpoint (e.g. http://localhost:4318). Empty disables OpenTelemetry export.
    otel_endpoint: str = ""
    otel_service_name: str = "incident-copilot-api"

    # --- Risk policy ------------------------------------------------------------------------
    # Write tools that may run without a human when the namespace is not protected.
    risk_low_tools: str = "restart_deployment"
    # fnmatch patterns; every write action in a matching namespace needs approval.
    risk_protected_namespaces: str = "*-prod,kube-system"

    @property
    def resolved_model(self) -> str:
        return self.llm_model or DEFAULT_MODELS[self.llm_provider]

    @property
    def verify_window(self) -> tuple[float, float]:
        """(timeout, stable) seconds for post-action verification."""
        live = self.tool_backend == "mcp"
        timeout = self.verify_timeout_s if self.verify_timeout_s is not None else (240.0 if live else 0.0)
        stable = self.verify_stable_s if self.verify_stable_s is not None else (45.0 if live else 0.0)
        return timeout, stable

    @property
    def jwks_url(self) -> str:
        return self.keycloak_jwks_url or f"{self.keycloak_issuer}/protocol/openid-connect/certs"


@lru_cache
def get_settings() -> Settings:
    load_env()
    return Settings()


@dataclass(frozen=True)
class Models:
    """The two chat models the graph uses.

    agent:      tool-calling investigation loops (thinking allowed).
    structured: structured outputs and the final report (thinking off; forced tool_choice works).
    """

    agent: BaseChatModel
    structured: BaseChatModel


def make_chat_model(settings: Settings | None = None, *, thinking: bool | None = None) -> BaseChatModel:
    """Build the chat model selected by COPILOT_LLM_PROVIDER / COPILOT_LLM_MODEL."""
    settings = settings or get_settings()
    kwargs: dict[str, Any] = {"temperature": settings.llm_temperature}
    if settings.llm_provider == "deepseek" and thinking is not None:
        kwargs["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
    return init_chat_model(settings.resolved_model, model_provider=settings.llm_provider, **kwargs)


def make_models(settings: Settings | None = None) -> Models:
    settings = settings or get_settings()
    return Models(
        agent=make_chat_model(settings, thinking=settings.llm_agent_thinking),
        structured=make_chat_model(settings, thinking=False),
    )


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """asyncio.run with a selector loop on Windows (psycopg's async mode needs it)."""
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)
