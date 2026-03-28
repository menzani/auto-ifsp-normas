"""
Serviço de autenticação Google OAuth 2.0.

Em modo MOCK_AUTH=true, bypassa o fluxo OAuth e autentica com um
usuário de teste selecionável, sem nenhuma chamada externa.
"""
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import HTTPException, Request, status

from app.config import get_settings

settings = get_settings()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _bootstrap_admins() -> list[str]:
    return [e.strip() for e in settings.admin_emails.split(",") if e.strip()]


def get_current_user(request: Request) -> dict[str, Any]:
    """
    Retorna o usuário da sessão.
    Refresca o papel do user store a cada request para que
    mudanças feitas pelo admin entrem em vigor imediatamente.
    """
    user = request.session.get("user")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado.",
        )

    from app.services.users import get_role
    current_role = get_role(user["email"])
    if current_role != user.get("role"):
        user["role"] = current_role
        request.session["user"] = user

    return user


def require_admin(request: Request) -> dict[str, Any]:
    """Exige papel de admin."""
    user = get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso restrito a administradores.",
        )
    return user



def build_google_auth_url(request: Request) -> str:
    """Gera a URL de redirecionamento para o Google OAuth."""
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _callback_url(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "hd": settings.google_allowed_domain,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_user(request: Request, code: str, state: str) -> dict[str, Any]:
    """
    Troca o código OAuth pelo token e retorna os dados do usuário.
    Valida state (CSRF) e hosted domain (hd).
    """
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or not secrets.compare_digest(expected_state, state):
        raise HTTPException(status_code=400, detail="Estado OAuth inválido (possível CSRF).")

    async with AsyncOAuth2Client(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=_callback_url(request),
    ) as client:
        token = await client.fetch_token(
            GOOGLE_TOKEN_URL,
            grant_type="authorization_code",
            code=code,
        )

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        resp.raise_for_status()
        userinfo = resp.json()

    if userinfo.get("hd") != settings.google_allowed_domain:
        raise HTTPException(
            status_code=403,
            detail=f"Acesso restrito a contas @{settings.google_allowed_domain}.",
        )

    from app.services.users import upsert_user
    role = upsert_user(userinfo["email"], userinfo.get("name", ""), _bootstrap_admins())

    return {
        "sub": userinfo["sub"],
        "name": userinfo.get("name", ""),
        "email": userinfo.get("email", ""),
        "picture": userinfo.get("picture", ""),
        "hd": userinfo.get("hd", ""),
        "role": role,
    }


def mock_login(email: str, name: str) -> dict[str, Any]:
    """Cria sessão de teste sem OAuth. Papel vem do user store.
    Para facilitar testes, o prefixo do email define o papel automaticamente
    se ainda não tiver sido definido manualmente (ex: revisor@... → revisor).
    """
    from app.services.users import upsert_user, set_role, VALID_ROLES
    role = upsert_user(email, name, _bootstrap_admins())
    prefix = email.split("@")[0].lower()
    if prefix in VALID_ROLES and role != prefix:
        set_role(email, prefix)
        role = prefix
    return {
        "sub": f"mock-{email}",
        "name": name,
        "email": email,
        "picture": "",
        "hd": settings.google_allowed_domain,
        "role": role,
    }


def _callback_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    # Não usa X-Forwarded-Host — nginx não o define, cliente poderia injetá-lo.
    # Host é setado pelo nginx via proxy_set_header Host $host; — valor verificado.
    host = request.headers.get("host", request.url.hostname)
    return f"{proto}://{host}/auth/callback"
