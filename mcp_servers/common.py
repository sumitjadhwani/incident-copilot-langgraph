"""Shared pieces of the MCP tool servers.

Security model:
- Every request carries the end user's Keycloak access token. The server verifies it itself (signature via
  JWKS, issuer, audience `copilot-mcp`, expiry). It never trusts identity from tool arguments.
- The verified identity is turned into Kubernetes impersonation headers:
      Impersonate-User:  kc:<username>
      Impersonate-Group: copilot:tenant:<tenant>        (always)
      Impersonate-Group: copilot:approvers:<tenant>     (write server only, and only for sre-approver)
  The server's own ServiceAccount can do nothing except impersonate those names (RBAC resourceNames), so
  Kubernetes RBAC decides what the human may do, and the API server audit log records both the service
  account and the human.
- Errors are raised as ToolError("<code>: <message>") with code in {forbidden, not_found, invalid, unavailable}
  so the agent can map them back to its own error types.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

log = logging.getLogger("mcp_servers")

ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://localhost:8080/realms/incident-copilot")
JWKS_URL = os.environ.get("KEYCLOAK_JWKS_URL", f"{ISSUER}/protocol/openid-connect/certs")
AUDIENCE = os.environ.get("MCP_AUDIENCE", "copilot-mcp")
APPROVER_ROLE = os.environ.get("APPROVER_ROLE", "sre-approver")
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def fail(code: str, message: str) -> None:
    raise ToolError(f"{code}: {message}")


def check_name(kind: str, value: str | None) -> None:
    if value is not None and (not NAME_RE.match(value) or len(value) > 63):
        fail("invalid", f"{kind} '{value}' is not a valid Kubernetes name")


# ------------------------------------------------------------------------------------------------
# Authentication
# ------------------------------------------------------------------------------------------------


class KeycloakTokenVerifier:
    """FastMCP TokenVerifier: validates Keycloak RS256 access tokens."""

    def __init__(self, issuer: str = ISSUER, audience: str = AUDIENCE, jwks_url: str = JWKS_URL, key: Any = None):
        self.issuer = issuer
        self.audience = audience
        self._key = key
        self._jwks = None if key is not None else jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300, timeout=5)

    def decode(self, token: str) -> dict[str, Any]:
        key = self._key if self._key is not None else self._jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
            leeway=30,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            import anyio

            claims = await anyio.to_thread.run_sync(self.decode, token)
        except Exception as exc:  # noqa: BLE001 - any failure means "not authenticated"
            log.warning("rejected token: %s", exc)
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("azp") or "unknown",
            scopes=[],
            expires_at=claims.get("exp"),
            resource=self.audience,
            subject=claims["sub"],
            claims=claims,
        )


@dataclass(frozen=True)
class Impersonation:
    user: str
    groups: tuple[str, ...]
    tenant: str
    username: str


def tenant_from_claims(claims: dict[str, Any]) -> str | None:
    tenant = claims.get("tenant")
    if isinstance(tenant, str) and tenant:
        return tenant
    tenants = [g.split("/")[2] for g in claims.get("groups", []) if g.startswith("/tenants/") and g.count("/") == 2]
    return tenants[0] if len(tenants) == 1 else None


def impersonation_for(claims: dict[str, Any], *, write: bool) -> Impersonation:
    username = claims.get("preferred_username")
    tenant = tenant_from_claims(claims)
    if not username or not tenant or not NAME_RE.match(username) or not NAME_RE.match(tenant):
        fail("forbidden", "token has no usable username/tenant")
    roles = set((claims.get("realm_access") or {}).get("roles", []))
    groups = [f"copilot:tenant:{tenant}"]
    if write and APPROVER_ROLE in roles:
        groups.append(f"copilot:approvers:{tenant}")
    return Impersonation(user=f"kc:{username}", groups=tuple(groups), tenant=tenant, username=username)


def current_identity(*, write: bool) -> Impersonation:
    token = get_access_token()
    if token is None or not token.claims:
        fail("forbidden", "no authenticated user")
    return impersonation_for(token.claims, write=write)


def build_server(name: str, instructions: str) -> FastMCP:
    port = int(os.environ.get("PORT", "8000"))
    public_url = os.environ.get("MCP_PUBLIC_URL", f"http://localhost:{port}")
    return FastMCP(
        name,
        instructions=instructions,
        host="0.0.0.0",
        port=port,
        stateless_http=True,
        json_response=True,
        token_verifier=KeycloakTokenVerifier(),
        auth=AuthSettings(issuer_url=ISSUER, resource_server_url=public_url, required_scopes=None),
        # Reached through a NodePort / host.docker.internal; Host-header checks would reject that.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


# ------------------------------------------------------------------------------------------------
# Kubernetes API client (in-cluster, impersonating)
# ------------------------------------------------------------------------------------------------

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def _status_message(r: httpx.Response) -> str:
    try:
        return r.json().get("message") or r.text
    except ValueError:
        return r.text[:300]


class K8sClient:
    def __init__(self) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        ca = SA_DIR / "ca.crt"
        verify: Any = ssl.create_default_context(cafile=str(ca)) if ca.exists() else True
        self._client = httpx.AsyncClient(base_url=f"https://{host}:{port}", verify=verify, timeout=15)

    @staticmethod
    def _sa_token() -> str:
        # Projected tokens rotate: read on every request.
        return (SA_DIR / "token").read_text().strip()

    async def request(
        self,
        method: str,
        path: str,
        who: Impersonation,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        content_type: str = "application/json",
        raw: bool = False,
    ) -> Any:
        headers = [
            ("Authorization", f"Bearer {self._sa_token()}"),
            ("Impersonate-User", who.user),
            *[("Impersonate-Group", g) for g in who.groups],
            ("Accept", "application/json"),
        ]
        if body is not None:
            headers.append(("Content-Type", content_type))
        try:
            r = await self._client.request(
                method, path, params=params, headers=headers, content=None if body is None else json.dumps(body)
            )
        except httpx.TransportError as exc:
            fail("unavailable", f"kubernetes API unreachable: {exc}")
        if r.status_code in (401, 403):
            fail("forbidden", _status_message(r))
        if r.status_code == 404:
            fail("not_found", _status_message(r))
        if r.status_code in (400, 409, 422):
            fail("invalid", _status_message(r))
        if r.status_code >= 500:
            fail("unavailable", f"kubernetes API {r.status_code}: {_status_message(r)}")
        return r.text if raw else r.json()
