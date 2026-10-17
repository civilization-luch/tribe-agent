"""Tests for the doctor command's token diagnostics.

The bot token is a long-lived RS256 access token minted by tribe-auth for a
service account (there is no refresh flow for it), so doctor must surface
its remaining lifetime instead of failing silently when it expires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

jwt = pytest.importorskip("jwt")

from tribe_agent.cli import TOKEN_EXPIRY_WARNING_DAYS, _token_expiry


def _token(expires_in: timedelta) -> str:
    payload = {
        "sub": "01KVA3GT70JN6KR7E4X375VP7Y",
        "aud": "tribe",
        "iss": "https://auth.tribe.partnr.icu",
        "type": "access",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + expires_in).timestamp()),
    }
    return jwt.encode(payload, "unused-secret-at-least-32-bytes-long!!", algorithm="HS256")


def test_token_expiry_reads_exp_without_verifying() -> None:
    token = _token(timedelta(days=365))
    exp = _token_expiry(token)
    assert exp is not None
    assert exp.tzinfo is not None
    assert (exp - datetime.now(UTC)).days >= 364


def test_token_expiry_of_garbage_is_none() -> None:
    assert _token_expiry("not-a-jwt") is None
    assert _token_expiry("") is None


def test_token_expiry_survives_missing_pyjwt(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "jwt":
            raise ImportError("pyjwt missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert _token_expiry(_token(timedelta(days=1))) is None


def test_warning_threshold_is_reasonable() -> None:
    assert 0 < TOKEN_EXPIRY_WARNING_DAYS <= 90


def test_expired_token_reports_negative_days() -> None:
    exp = _token_expiry(_token(timedelta(days=-1)))
    assert exp is not None
    assert (exp - datetime.now(UTC)).days < 0
