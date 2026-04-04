"""Testes para app/services/auth.py — CSRF, session binding e helpers."""
import hashlib
import time

import pytest
from fastapi import HTTPException

from app.services.auth import (
    _ua_hash,
    _bootstrap_admins,
    check_csrf_header,
    check_csrf_form,
    get_current_user,
)


# ── Helpers para construir objetos Request fake ────────────────────────────


class FakeRequest:
    """Request mínimo para testes de auth sem precisar de TestClient."""

    def __init__(self, session: dict | None = None, headers: dict | None = None, client_host: str = "127.0.0.1"):
        self.session = session or {}
        self._headers = headers or {}

        class FakeClient:
            host = client_host
        self.client = FakeClient()

    @property
    def headers(self):
        return self._headers


# ── _ua_hash ───────────────────────────────────────────────────────────────


class TestUaHash:
    def test_consistent_for_same_ua(self):
        r = FakeRequest(headers={"user-agent": "Mozilla/5.0 Test"})
        assert _ua_hash(r) == _ua_hash(r)

    def test_different_ua_different_hash(self):
        r1 = FakeRequest(headers={"user-agent": "Chrome/120"})
        r2 = FakeRequest(headers={"user-agent": "Firefox/120"})
        assert _ua_hash(r1) != _ua_hash(r2)

    def test_length_is_16(self):
        r = FakeRequest(headers={"user-agent": "Qualquer"})
        assert len(_ua_hash(r)) == 16

    def test_missing_ua_still_works(self):
        r = FakeRequest(headers={})
        h = _ua_hash(r)
        assert len(h) == 16


# ── _bootstrap_admins ──────────────────────────────────────────────────────


class TestBootstrapAdmins:
    def test_parses_emails(self, monkeypatch):
        from app.services import auth
        monkeypatch.setattr(auth.settings, "admin_emails", "a@ifsp.edu.br, b@ifsp.edu.br")
        result = _bootstrap_admins()
        assert result == ["a@ifsp.edu.br", "b@ifsp.edu.br"]

    def test_strips_whitespace(self, monkeypatch):
        from app.services import auth
        monkeypatch.setattr(auth.settings, "admin_emails", "  c@ifsp.edu.br  ")
        assert _bootstrap_admins() == ["c@ifsp.edu.br"]

    def test_empty_string(self, monkeypatch):
        from app.services import auth
        monkeypatch.setattr(auth.settings, "admin_emails", "")
        assert _bootstrap_admins() == []


# ── CSRF validation ───────────────────────────────────────────────────────


class TestCsrfHeader:
    def test_valid_token(self):
        token = "abc123def456"
        req = FakeRequest(session={"_csrf_token": token}, headers={"x-csrf-token": token})
        check_csrf_header(req)  # não deve lançar

    def test_invalid_token_raises_403(self):
        req = FakeRequest(session={"_csrf_token": "correct"}, headers={"x-csrf-token": "wrong"})
        with pytest.raises(HTTPException) as exc_info:
            check_csrf_header(req)
        assert exc_info.value.status_code == 403

    def test_missing_session_token_raises_403(self):
        req = FakeRequest(session={}, headers={"x-csrf-token": "qualquer"})
        with pytest.raises(HTTPException) as exc_info:
            check_csrf_header(req)
        assert exc_info.value.status_code == 403


class TestCsrfForm:
    def test_valid_token(self):
        token = "form_token_abc"
        req = FakeRequest(session={"_csrf_token": token})
        check_csrf_form(req, token)  # não deve lançar

    def test_invalid_token_raises_403(self):
        req = FakeRequest(session={"_csrf_token": "certo"})
        with pytest.raises(HTTPException) as exc_info:
            check_csrf_form(req, "errado")
        assert exc_info.value.status_code == 403


# ── get_current_user ──────────────────────────────────────────────────────


class TestGetCurrentUser:
    def _make_user(self, **overrides):
        base = {
            "sub": "mock-test",
            "name": "Teste",
            "email": "test@ifsp.edu.br",
            "role": "servidor",
            "_bound_ip": "127.0.0.1",
            "_bound_ua": hashlib.sha256(b"TestAgent").hexdigest()[:16],
            "_last_active": time.time(),
        }
        base.update(overrides)
        return base

    def test_no_user_raises_401(self):
        req = FakeRequest(session={})
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(req)
        assert exc_info.value.status_code == 401

    def test_inactivity_timeout_raises_401(self):
        user = self._make_user(_last_active=time.time() - 3600)
        req = FakeRequest(session={"user": user})
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(req)
        assert exc_info.value.status_code == 401
        assert req.session == {}  # sessão limpa

    def test_ip_mismatch_raises_401(self):
        user = self._make_user(_bound_ip="10.0.0.1")
        req = FakeRequest(
            session={"user": user},
            headers={"x-real-ip": "192.168.0.1", "user-agent": "TestAgent"},
        )
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(req)
        assert exc_info.value.status_code == 401

    def test_ua_mismatch_raises_401(self):
        user = self._make_user(_bound_ua="different_ua_hash")
        req = FakeRequest(
            session={"user": user},
            headers={"user-agent": "TestAgent"},
            client_host="127.0.0.1",
        )
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(req)
        assert exc_info.value.status_code == 401

    def test_valid_session_returns_user(self):
        from app.services.users import upsert_user
        upsert_user("test@ifsp.edu.br", "Teste", [])

        ua = "TestAgent"
        ua_h = hashlib.sha256(ua.encode()).hexdigest()[:16]
        user = self._make_user(_bound_ua=ua_h)
        req = FakeRequest(
            session={"user": user},
            headers={"user-agent": ua},
            client_host="127.0.0.1",
        )
        result = get_current_user(req)
        assert result["email"] == "test@ifsp.edu.br"
