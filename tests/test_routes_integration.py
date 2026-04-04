"""Testes de integração — rotas HTTP via TestClient (Starlette)."""
import pytest
from starlette.testclient import TestClient

from app.main import app
from app.services import storage


@pytest.fixture()
def client():
    """TestClient sem follow_redirects para inspecionar redirects."""
    return TestClient(app, raise_server_exceptions=False)


def _mock_login(client: TestClient, email: str = "operador@test.com", name: str = "Operador Teste"):
    """Faz login mock e retorna o client com sessão autenticada."""
    resp = client.post("/auth/mock", data={"email": email, "name": name}, follow_redirects=False)
    assert resp.status_code == 302
    return client


# ── Auth ───────────────────────────────────────────────────────────────────


class TestAuthRoutes:
    def test_login_page_renders(self, client):
        resp = client.get("/auth/login")
        assert resp.status_code == 200
        assert "Entrar" in resp.text or "mock" in resp.text.lower()

    def test_unauthenticated_redirect_to_login(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["location"]

    def test_mock_login_redirects_to_dashboard(self, client):
        resp = client.post("/auth/mock", data={"email": "op@test.com", "name": "Op"}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_logout_clears_session(self, client):
        _mock_login(client)
        resp = client.get("/auth/logout", follow_redirects=False)
        assert resp.status_code == 302
        # Após logout, acessar / deve redirecionar para login
        resp2 = client.get("/", follow_redirects=False)
        assert resp2.status_code == 302
        assert "/auth/login" in resp2.headers["location"]


# ── Dashboard ──────────────────────────────────────────────────────────────


class TestDashboard:
    def test_operador_sees_upload_form(self, client):
        _mock_login(client, "operador@test.com", "Op")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "upload" in resp.text.lower()

    def test_servidor_redirected_to_review(self, client):
        _mock_login(client, "servidor@test.com", "Serv")
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/review" in resp.headers["location"]


# ── Upload validation ──────────────────────────────────────────────────────


class TestUploadValidation:
    def _get_csrf(self, client: TestClient) -> str:
        """Extrai CSRF token da sessão após login."""
        resp = client.get("/")
        # O CSRF token é gerado pelo get_current_user via _ensure_csrf_token
        # Precisamos extraí-lo dos cookies da sessão
        # Approach: fazer um GET autenticado, a sessão terá o token
        # O TestClient mantém cookies automaticamente
        # Vamos extrair do HTML ou inspecionar internamente
        # Mais simples: o token está na sessão. Podemos extrair do meta tag no HTML.
        import re
        match = re.search(r'csrfToken["\s:=]+["\']([a-f0-9]+)["\']', resp.text)
        if match:
            return match.group(1)
        # Fallback: tenta var JS
        match = re.search(r'csrf.*?["\']([\da-f]{64})["\']', resp.text, re.IGNORECASE)
        return match.group(1) if match else ""

    def test_non_pdf_rejected(self, client):
        _mock_login(client, "operador@test.com", "Op")
        csrf = self._get_csrf(client)
        resp = client.post(
            "/upload",
            files={"pdf_file": ("doc.txt", b"hello world", "text/plain")},
            data={"title": "Teste"},
            headers={"x-csrf-token": csrf},
        )
        assert resp.status_code == 200
        assert "extensão" in resp.text.lower() or "inválido" in resp.text.lower()

    def test_valid_pdf_starts_processing(self, client, monkeypatch):
        _mock_login(client, "operador@test.com", "Op")
        csrf = self._get_csrf(client)

        # Mock run_in_background para não disparar pipeline real
        from app.services import processor
        monkeypatch.setattr(processor, "run_in_background", lambda **kw: True)

        pdf_content = b"%PDF-1.4 fake content for test"
        resp = client.post(
            "/upload",
            files={"pdf_file": ("norma.pdf", pdf_content, "application/pdf")},
            data={"title": "Portaria de Teste"},
            headers={"x-csrf-token": csrf},
        )
        assert resp.status_code == 200


# ── Status polling ─────────────────────────────────────────────────────────


class TestStatusRoutes:
    def test_unknown_job_returns_404(self, client):
        _mock_login(client, "operador@test.com", "Op")
        resp = client.get("/status/aaaBBBcccDDDeeeFFF123")
        assert resp.status_code == 404

    def test_owner_can_see_own_job(self, client):
        _mock_login(client, "operador@test.com", "Op")
        storage.save_status("test_job_01", {
            "id": "test_job_01",
            "status": "processing",
            "owner": "operador@test.com",
            "current_step": 1,
            "total_steps": 5,
            "current_step_label": "Extraindo...",
            "progress_pct": 10,
        })
        resp = client.get("/status/test_job_01")
        assert resp.status_code == 200

    def test_other_user_gets_403(self, client):
        _mock_login(client, "servidor@test.com", "Outro")
        storage.save_status("test_job_02", {
            "id": "test_job_02",
            "status": "processing",
            "owner": "dono@test.com",
            "current_step": 1,
            "total_steps": 5,
            "current_step_label": "...",
            "progress_pct": 0,
        })
        resp = client.get("/status/test_job_02")
        assert resp.status_code == 403
