"""Tests for the assistant module's token verification (RS256 via the
tribe-auth JWKS) and the JWKS client itself.

Authentication is RS256-only: the legacy HS256 shared-secret path was
removed together with the engine's (tribe-engine stage 6).

The ``assistant`` extra (fastapi/pyjwt) is optional — tests skip without it.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

jwt = pytest.importorskip("jwt")

from tribe_agent.core.config import AppConfig, CommunityConfig
from tribe_agent.core.jwks import JwksProvider

# ── JWKS provider ──────────────────────────────────────────────────────


def _rsa_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    public_jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return private_pem, json.dumps(public_jwk)


def _provider(jwks: dict[str, Any], **kwargs: Any) -> JwksProvider:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=jwks))
    return JwksProvider("http://auth.test/.well-known/jwks.json", transport=transport, **kwargs)


async def test_jwks_fetch_and_cache() -> None:
    _, public_jwk = _rsa_keypair()
    provider = _provider({"keys": [json.loads(public_jwk)]})

    assert await provider.get_signing_key("k1") is not None

    # cached: a failing transport must not matter until TTL expires
    provider._transport = httpx.MockTransport(lambda request: httpx.Response(500))
    assert await provider.get_signing_key("k1") is not None


async def test_jwks_refresh_on_unknown_kid() -> None:
    _, public_jwk = _rsa_keypair()
    provider = _provider({"keys": [json.loads(public_jwk)]})
    await provider.get_signing_key("k1")

    calls = {"n": 0}

    def rotated(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        jwk = json.loads(public_jwk)
        jwk["kid"] = "k2"
        return httpx.Response(200, json={"keys": [jwk]})

    provider._transport = httpx.MockTransport(rotated)
    assert await provider.get_signing_key("k2") is not None
    assert calls["n"] >= 1


async def test_jwks_stale_cache_triggers_refresh() -> None:
    _, public_jwk = _rsa_keypair()
    provider = _provider({"keys": [json.loads(public_jwk)]}, cache_seconds=10)
    await provider.get_signing_key("k1")

    provider._fetched_at = time.monotonic() - 11
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"keys": [json.loads(public_jwk)]})

    provider._transport = httpx.MockTransport(counting)
    await provider.get_signing_key("k1")
    assert calls["n"] >= 1


async def test_jwks_fetch_failure_returns_none() -> None:
    provider = JwksProvider(
        "http://auth.test/jwks.json",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    assert await provider.get_signing_key("k1") is None


async def test_jwks_malformed_and_non_rsa_keys_skipped() -> None:
    _, public_jwk = _rsa_keypair()
    jwks = {
        "keys": [
            {"kty": "EC", "kid": "x"},          # non-RSA
            json.loads(public_jwk),              # valid
        ]
    }
    provider = _provider(jwks)
    assert await provider.get_signing_key("k1") is not None
    assert await provider.get_signing_key("x") is None


async def test_jwks_single_key_without_kid() -> None:
    _, public_jwk = _rsa_keypair()
    jwk = json.loads(public_jwk)
    jwk.pop("kid", None)
    provider = _provider({"keys": [jwk]})
    assert await provider.get_signing_key(None) is not None


# ── assistant module auth ──────────────────────────────────────────────


def _issue_rs256(private_pem: str, kid: str = "k1", **claims: Any) -> str:
    payload = {"sub": "u1", "type": "access",
               "exp": datetime.now(UTC) + timedelta(minutes=5)}
    payload.update(claims)
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture()
def assistant_app(monkeypatch):
    pytest.importorskip("fastapi")
    from tribe_agent.modules import assistant as assistant_module

    def _build(community: CommunityConfig, jwks: dict[str, Any] | None = None):
        config = AppConfig(community=community)
        if jwks is not None:
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=jwks))

            class _FakeProvider(JwksProvider):
                def __init__(self, url: str, cache_seconds: int = 300, **kwargs: Any) -> None:
                    super().__init__(url, cache_seconds, transport=transport)

            monkeypatch.setattr("tribe_agent.core.jwks.JwksProvider", _FakeProvider)
        return assistant_module.make_app(config)

    return _build


def _auth(app, token: str) -> httpx.Response:
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        return client.post(
            "/assistant/session", json={}, headers={"Authorization": f"Bearer {token}"}
        )


# HS256 is not supported anymore (tribe-engine stage 6 removed the shared
# secret) — such tokens must be rejected outright.
def test_hs256_token_rejected(assistant_app) -> None:
    _, public_jwk = _rsa_keypair()
    app = assistant_app(
        CommunityConfig(auth_url="http://auth.test"),
        jwks={"keys": [json.loads(public_jwk)]},
    )
    resp = _auth(app, jwt.encode(
        {"sub": "u1", "type": "access",
         "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "legacy-secret-at-least-32-bytes!!", algorithm="HS256",
    ))
    assert resp.status_code == 401


# RS256 path via tribe-auth JWKS
def test_rs256_token_via_jwks_accepted(assistant_app) -> None:
    private_pem, public_jwk = _rsa_keypair()
    app = assistant_app(
        CommunityConfig(auth_url="http://auth.test", auth_client_id="tribe"),
        jwks={"keys": [json.loads(public_jwk)]},
    )
    resp = _auth(app, _issue_rs256(private_pem, aud="tribe"))
    assert resp.status_code == 502  # auth passed; opencode backend unreachable


def test_rs256_foreign_aud_rejected(assistant_app) -> None:
    private_pem, public_jwk = _rsa_keypair()
    app = assistant_app(
        CommunityConfig(auth_url="http://auth.test", auth_client_id="tribe"),
        jwks={"keys": [json.loads(public_jwk)]},
    )
    resp = _auth(app, _issue_rs256(private_pem, aud="another-app"))
    assert resp.status_code == 401


def test_rs256_forged_token_rejected(assistant_app) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    _, public_jwk = _rsa_keypair()
    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app = assistant_app(
        CommunityConfig(auth_url="http://auth.test"),
        jwks={"keys": [json.loads(public_jwk)]},
    )
    resp = _auth(app, _issue_rs256(forged_key, kid="k1"))
    assert resp.status_code == 401


def test_rs256_unreachable_jwks_rejected(assistant_app) -> None:
    pytest.importorskip("fastapi")
    from tribe_agent.modules import assistant as assistant_module

    private_pem, _ = _rsa_keypair()
    config = AppConfig(community=CommunityConfig(auth_url="http://auth.test"))
    app = assistant_module.make_app(config)  # no transport mock: real fetch fails
    resp = _auth(app, _issue_rs256(private_pem))
    assert resp.status_code == 401


def test_rs256_token_without_jwks_config_rejected(assistant_app) -> None:
    """No auth service configured → RS256 tokens have no key to verify against."""
    private_pem, _ = _rsa_keypair()
    app = assistant_app(CommunityConfig())
    resp = _auth(app, _issue_rs256(private_pem))
    assert resp.status_code == 401


# common auth rules
def test_refresh_token_type_rejected(assistant_app) -> None:
    private_pem, public_jwk = _rsa_keypair()
    app = assistant_app(
        CommunityConfig(auth_url="http://auth.test"),
        jwks={"keys": [json.loads(public_jwk)]},
    )
    resp = _auth(app, _issue_rs256(private_pem, type="refresh"))
    assert resp.status_code == 401


def test_missing_and_garbage_tokens_rejected(assistant_app) -> None:
    _, public_jwk = _rsa_keypair()
    app = assistant_app(
        CommunityConfig(auth_url="http://auth.test"),
        jwks={"keys": [json.loads(public_jwk)]},
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/assistant/session", json={}).status_code == 401
        resp = client.post(
            "/assistant/session", json={},
            headers={"Authorization": "Bearer garbage"},
        )
        assert resp.status_code == 401
