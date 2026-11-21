"""JWT verification (Keycloak) and mapping verified claims to an `Actor`.

Checks: RS256 signature against the realm's JWKS, issuer, audience, expiry, and the presence of
`sub`. The tenant comes from the `tenant` claim (a Keycloak attribute mapper), with the
`/tenants/<name>` group path as a fallback. Nothing about identity is ever taken from the request
body or from the model.
"""

from __future__ import annotations

import asyncio
from typing import Any

import jwt

from copilot.config import Settings
from copilot.context import Actor
from copilot.errors import Unauthorized

ALGORITHMS = ["RS256"]


class JWTVerifier:
    def __init__(self, *, issuer: str, audience: str, jwks_url: str | None = None, key: Any = None) -> None:
        if not (jwks_url or key):
            raise ValueError("JWTVerifier needs a jwks_url or a static key")
        self.issuer = issuer
        self.audience = audience
        self._key = key
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300, timeout=5) if jwks_url else None

    @classmethod
    def from_settings(cls, settings: Settings) -> JWTVerifier:
        return cls(issuer=settings.keycloak_issuer, audience=settings.jwt_audience, jwks_url=settings.jwks_url)

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            if self._key is not None:
                key = self._key
            else:
                assert self._jwks is not None
                signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
                key = signing_key.key
            return jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWKClientError as exc:
            raise Unauthorized(f"cannot fetch signing keys: {exc}") from exc
        except jwt.InvalidTokenError as exc:
            raise Unauthorized(f"invalid token: {exc}") from exc


def tenant_from_claims(claims: dict[str, Any]) -> str | None:
    tenant = claims.get("tenant")
    if isinstance(tenant, list):
        tenant = tenant[0] if len(tenant) == 1 else None
    if isinstance(tenant, str) and tenant:
        return tenant
    tenants = [g.split("/")[2] for g in claims.get("groups", []) if g.startswith("/tenants/") and g.count("/") == 2]
    return tenants[0] if len(tenants) == 1 else None


def actor_from_claims(claims: dict[str, Any], token: str | None = None) -> Actor:
    tenant = tenant_from_claims(claims)
    if not tenant:
        raise Unauthorized("token carries no (single) tenant")
    roles = frozenset((claims.get("realm_access") or {}).get("roles", []))
    return Actor(
        user_id=claims["sub"],
        username=claims.get("preferred_username") or claims["sub"],
        tenant_id=tenant,
        roles=roles,
        groups=tuple(claims.get("groups", [])),
        access_token=token,
    )
