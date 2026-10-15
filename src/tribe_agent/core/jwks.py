"""JWKS client: fetch and cache the auth service's public signing keys.

Used by the assistant module to verify RS256 access tokens issued by the
standalone tribe-auth service (see tribe-engine AUTH_SERVICE_PLAN.md, stage 3)
without any manual key distribution. Requires the optional ``pyjwt`` extra.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CACHE_SECONDS = 300


class JwksProvider:
    """Cached JWKS: refreshed on TTL and on unknown ``kid``."""

    def __init__(
        self,
        url: str,
        cache_seconds: int = DEFAULT_CACHE_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.cache_seconds = cache_seconds
        self._keys: dict[str, Any] = {}
        self._fetched_at: float = 0.0
        self._transport = transport  # testing hook (httpx.MockTransport)

    async def get_signing_key(self, kid: str | None) -> Any | None:
        if self._is_stale() or (kid and kid not in self._keys):
            await self.refresh()
        if kid:
            return self._keys.get(kid)
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) > self.cache_seconds

    async def refresh(self) -> None:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=5.0) as client:
                resp = await client.get(self.url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — network errors must not propagate
            logger.warning("JWKS fetch failed (%s): %s", self.url, exc)
            return

        from jwt.algorithms import RSAAlgorithm

        keys: dict[str, Any] = {}
        for jwk in data.get("keys", []):
            if jwk.get("kty") != "RSA":
                continue
            try:
                key = RSAAlgorithm.from_jwk(json.dumps(jwk))
            except Exception as exc:  # noqa: BLE001 — skip malformed keys
                logger.warning("Bad JWK (kid=%s): %s", jwk.get("kid"), exc)
                continue
            kid = jwk.get("kid")
            if kid:
                keys[kid] = key
            elif not keys:
                # tolerate single-key JWKS without kid (served under "")
                keys[""] = key
        if keys:
            self._keys = keys
            self._fetched_at = time.monotonic()
