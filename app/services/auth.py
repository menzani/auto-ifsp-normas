"""
Serviço de autenticação Google OAuth 2.0.

Em modo MOCK_AUTH=true, bypassa o fluxo OAuth e autentica com um
usuário de teste selecionável, sem nenhuma chamada externa.
"""
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import HTTPException, Request, status

from app.config import get_settings

settings = get_settings()

# Sessão expira após 30 min sem nenhuma requisição ao app
_INACTIVITY_TIMEOUT = 1800

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _get_client_ip(request: Request) -> str:
    """IP real do cliente via X-Real-IP (setado pelo nginx a partir de $remote_addr)."""
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def _ua_hash(request: Request) -> str:
    """Primeiros 16 hex do SHA-256 do User-Agent — identifica o navegador sem expor a string."""
    ua = request.headers.get("user-agent", "")
    return hashlib.sha256(ua.encode()).hexdigest()[:16]


def _bootstrap_admins() -> list[str]:
    return [e.strip() for e in settings.admin_emails.split(",") if e.strip()]


def get_current_user(request: Request) -> dict[str, Any]:
    """
    Retorna o usuário da sessão com verificações de integridade:
    - Timeout de inatividade (30 min sem requisição ao app)
    - Binding de IP: rejeita sessão usada a partir de IP diferente do login
    - Binding de User-Agent: rejeita sessão usada em navegador diferente
    - Refresca papel do user store a cada request (TTL 60 s)
    """
    user = request.session.get("user")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado.",
        )

    # Inatividade — protege máquinas desacompanhadas com sessão aberta
    if time.time() - user.get("_last_active", 0) > _INACTIVITY_TIMEOUT:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão expirada por inatividade.",
        )

    # Binding de IP — detecta cookie copiado para outra máquina/rede
    current_ip = _get_client_ip(request)
    bound_ip = user.get("_bound_ip", "")
    if bound_ip and current_ip and current_ip != bound_ip:
        from app.services import audit as _audit
        _audit.log(user["email"], "session_anomaly",
                   f"IP divergente — sessão: {bound_ip}, atual: {current_ip}", level="warn")
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão inválida — faça login novamente.",
        )

    # Binding de User-Agent — detecta cookie copiado para outro navegador
    current_ua = _ua_hash(request)
    bound_ua = user.get("_bound_ua", "")
    if bound_ua and current_ua != bound_ua:
        from app.services import audit as _audit
        _audit.log(user["email"], "session_anomaly",
                   "User-Agent divergente — possível uso indevido de sessão", level="warn")
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão inválida — faça login novamente.",
        )

    # Atualiza sessão apenas quando necessário para evitar reescrita do cookie
    # em toda requisição (incluindo polling HTMX a cada 2 s).
    now = time.time()
    needs_save = False

    # _last_active com granularidade de 60 s — suficiente para janela de 30 min
    if now - user.get("_last_active", 0) > 60:
        user["_last_active"] = now
        needs_save = True

    # Refresca papel do user store (TTL 60 s no cache de users.py)
    from app.services.users import get_role
    current_role = get_role(user["email"])
    if current_role != user.get("role"):
        user["role"] = current_role
        needs_save = True

    if needs_save:
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
        "role": role,
        "_bound_ip": _get_client_ip(request),
        "_bound_ua": _ua_hash(request),
        "_last_active": time.time(),
    }


def mock_login(request: Request, email: str, name: str) -> dict[str, Any]:
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
        "role": role,
        "_bound_ip": _get_client_ip(request),
        "_bound_ua": _ua_hash(request),
        "_last_active": time.time(),
    }


def _callback_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    # Não usa X-Forwarded-Host — nginx não o define, cliente poderia injetá-lo.
    # Host é setado pelo nginx via proxy_set_header Host $host; — valor verificado.
    host = request.headers.get("host", request.url.hostname)
    return f"{proto}://{host}/auth/callback"
