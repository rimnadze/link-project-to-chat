from __future__ import annotations

import pytest

from link_project_to_chat.google_chat.auth import (
    GoogleChatAuthError,
    VerifiedGoogleChatRequest,
    verify_google_chat_request,
)


def test_missing_authorization_header_rejected():
    with pytest.raises(GoogleChatAuthError):
        verify_google_chat_request(headers={}, mode="endpoint_url", audiences=["https://x.test/google-chat/events"])


def test_non_bearer_authorization_header_rejected():
    with pytest.raises(GoogleChatAuthError):
        verify_google_chat_request(
            headers={"authorization": "Basic abc"},
            mode="endpoint_url",
            audiences=["https://x.test/google-chat/events"],
        )


def test_endpoint_url_claims_are_accepted_with_injected_verifier():
    def verifier(token: str, audience: str) -> dict:
        return {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "email": "chat@system.gserviceaccount.com",
            "email_verified": True,
            "sub": "chat",
            "exp": 1770000000,
        }

    verified = verify_google_chat_request(
        headers={"authorization": "Bearer token"},
        mode="endpoint_url",
        audiences=["https://x.test/google-chat/events"],
        oidc_verifier=verifier,
    )

    assert verified == VerifiedGoogleChatRequest(
        issuer="https://accounts.google.com",
        audience="https://x.test/google-chat/events",
        subject="chat",
        email="chat@system.gserviceaccount.com",
        expires_at=1770000000,
        auth_mode="endpoint_url",
    )


def test_project_number_claims_are_accepted_with_injected_verifier():
    def verifier(token: str, audience: str) -> dict:
        return {"iss": "chat@system.gserviceaccount.com", "aud": audience, "sub": "chat", "exp": 1770000000}

    verified = verify_google_chat_request(
        headers={"authorization": "Bearer token"},
        mode="project_number",
        audiences=["123"],
        jwt_verifier=verifier,
    )

    assert verified.issuer == "chat@system.gserviceaccount.com"
    assert verified.audience == "123"
    assert verified.auth_mode == "project_number"


def test_aud_mismatch_exhausts_all_audiences_and_raises():
    def verifier(token: str, audience: str) -> dict:
        return {
            "iss": "https://accounts.google.com",
            "aud": "https://wrong.test/google-chat/events",
            "email": "chat@system.gserviceaccount.com",
            "email_verified": True,
            "sub": "chat",
            "exp": 1770000000,
        }

    with pytest.raises(GoogleChatAuthError, match="audience mismatch"):
        verify_google_chat_request(
            headers={"authorization": "Bearer token"},
            mode="endpoint_url",
            audiences=["https://x.test/google-chat/events", "https://y.test/google-chat/events"],
            oidc_verifier=verifier,
        )


def test_default_chat_jwt_verifier_uses_injected_jwks(monkeypatch):
    from link_project_to_chat.google_chat import auth as auth_mod

    sample_claims = {"iss": "chat@system.gserviceaccount.com", "aud": "123", "exp": 1770000000, "sub": "chat"}
    fetch_calls = []

    def fake_jwt_decode(token, certs, audience):
        assert token == "fake-jwt"
        assert audience == "123"
        assert isinstance(certs, dict)
        return sample_claims

    def fake_fetch_certs():
        fetch_calls.append("fetch")
        return {"kid1": "PEM-BODY"}

    monkeypatch.setattr(auth_mod, "_CHAT_CERTS_CACHE", None)
    monkeypatch.setattr(auth_mod, "_fetch_chat_certs", fake_fetch_certs)
    monkeypatch.setattr(auth_mod, "_decode_chat_jwt", fake_jwt_decode)

    verified = auth_mod.verify_google_chat_request(
        headers={"authorization": "Bearer fake-jwt"},
        mode="project_number",
        audiences=["123"],
    )

    assert verified.audience == "123"
    assert verified.auth_mode == "project_number"

    again = auth_mod.verify_google_chat_request(
        headers={"authorization": "Bearer fake-jwt"},
        mode="project_number",
        audiences=["123"],
    )
    assert again.audience == "123"
    assert fetch_calls == ["fetch"]


def test_get_chat_certs_caches_within_ttl_and_refetches_after_expiry(monkeypatch):
    from link_project_to_chat.google_chat import auth as auth_mod

    fetch_calls = []

    def fake_fetch_certs():
        fetch_calls.append("fetch")
        return {"kid1": f"PEM-{len(fetch_calls)}"}

    monkeypatch.setattr(auth_mod, "_CHAT_CERTS_CACHE", None)
    monkeypatch.setattr(auth_mod, "_fetch_chat_certs", fake_fetch_certs)

    first = auth_mod._get_chat_certs(now=0.0)
    assert first == {"kid1": "PEM-1"}
    assert fetch_calls == ["fetch"]

    # Within the TTL window — must hit cache, no new fetch.
    cached = auth_mod._get_chat_certs(now=auth_mod._CHAT_CERTS_CACHE_TTL_SECONDS - 1.0)
    assert cached == {"kid1": "PEM-1"}
    assert fetch_calls == ["fetch"]

    # One second past the TTL — must refetch.
    refreshed = auth_mod._get_chat_certs(now=auth_mod._CHAT_CERTS_CACHE_TTL_SECONDS + 1.0)
    assert refreshed == {"kid1": "PEM-2"}
    assert fetch_calls == ["fetch", "fetch"]

    # Still within the TTL of the new fetch — no more fetches.
    auth_mod._get_chat_certs(now=auth_mod._CHAT_CERTS_CACHE_TTL_SECONDS + 2.0)
    assert fetch_calls == ["fetch", "fetch"]


def test_decode_chat_jwt_passes_certs_and_audience_to_google_auth(monkeypatch):
    from google.auth import jwt as google_jwt

    from link_project_to_chat.google_chat import auth as auth_mod

    calls = []
    claims = {"iss": "chat@system.gserviceaccount.com", "aud": "aud"}

    def fake_decode(*args, **kwargs):
        calls.append((args, kwargs))
        return claims

    monkeypatch.setattr(google_jwt, "decode", fake_decode)

    assert auth_mod._decode_chat_jwt("token", {"kid": "pem"}, "aud") is claims
    assert calls == [(("token",), {"certs": {"kid": "pem"}, "audience": "aud"})]


# Widened signer / issuer set (Workspace add-on support) ---------------------


def test_endpoint_url_accepts_workspace_addon_signer_when_widened():
    """When the caller widens `accepted_signer_emails` to include the
    gsuiteaddons signer (which `GoogleChatTransport.verify_request` does
    whenever `config.project_number` is set), a token signed by that identity
    must verify successfully."""
    addon_signer = "service-292758438544@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"

    def verifier(token: str, audience: str) -> dict:
        return {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "email": addon_signer,
            "email_verified": True,
            "sub": "addon",
            "exp": 1770000000,
        }

    verified = verify_google_chat_request(
        headers={"authorization": "Bearer token"},
        mode="endpoint_url",
        audiences=["https://x.test/google-chat/events"],
        oidc_verifier=verifier,
        accepted_signer_emails=frozenset({"chat@system.gserviceaccount.com", addon_signer}),
    )

    assert verified.email == addon_signer
    assert verified.auth_mode == "endpoint_url"


def test_endpoint_url_rejects_unrelated_signer_even_when_set_widened(caplog):
    """Widening must be exact — a signer not in the accepted set still fails,
    and the WARNING log surfaces the offender for debugging."""

    def verifier(token: str, audience: str) -> dict:
        return {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "email": "attacker@evil.test",
            "email_verified": True,
            "sub": "attacker",
            "exp": 1770000000,
        }

    with caplog.at_level("WARNING", logger="link_project_to_chat.google_chat.auth"):
        with pytest.raises(GoogleChatAuthError):
            verify_google_chat_request(
                headers={"authorization": "Bearer token"},
                mode="endpoint_url",
                audiences=["https://x.test/google-chat/events"],
                oidc_verifier=verifier,
                accepted_signer_emails=frozenset(
                    {"chat@system.gserviceaccount.com",
                     "service-1@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"}
                ),
            )

    assert any("signer email mismatch" in rec.message for rec in caplog.records)


def test_project_number_accepts_widened_issuer_for_addon():
    """In project_number mode, the JWT issuer differs for Workspace add-on apps.
    The widened `accepted_issuers` set lets the verifier accept that path."""
    addon_signer = "service-292758438544@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"

    def jwt_verifier(token: str, audience: str) -> dict:
        return {"iss": addon_signer, "aud": audience, "sub": "addon", "exp": 1770000000}

    verified = verify_google_chat_request(
        headers={"authorization": "Bearer token"},
        mode="project_number",
        audiences=["292758438544"],
        jwt_verifier=jwt_verifier,
        accepted_issuers=frozenset({"chat@system.gserviceaccount.com", addon_signer}),
    )

    assert verified.issuer == addon_signer
    assert verified.auth_mode == "project_number"


def test_endpoint_url_audience_mismatch_logs_claims(caplog):
    """The new claim-logging path must surface the actual audience Google sent
    so future drift is debuggable from the bot log alone (no need for tracers)."""

    def verifier(token: str, audience: str) -> dict:
        return {
            "iss": "https://accounts.google.com",
            "aud": "https://wrong.example/events",
            "email": "chat@system.gserviceaccount.com",
            "email_verified": True,
            "sub": "chat",
            "exp": 1770000000,
        }

    with caplog.at_level("WARNING", logger="link_project_to_chat.google_chat.auth"):
        with pytest.raises(GoogleChatAuthError):
            verify_google_chat_request(
                headers={"authorization": "Bearer token"},
                mode="endpoint_url",
                audiences=["https://x.test/google-chat/events"],
                oidc_verifier=verifier,
            )

    matched = [rec for rec in caplog.records if "audience mismatch" in rec.message]
    assert matched, "expected an audience-mismatch WARNING"
    assert "https://wrong.example/events" in matched[0].getMessage()
