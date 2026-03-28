from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.services.auth import (
    build_google_auth_url,
    exchange_code_for_user,
    mock_login,
)
from app.templates import templates

settings = get_settings()
router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "mock_auth": settings.mock_auth,
    })


@router.get("/google")
async def login_google(request: Request):
    """Inicia fluxo OAuth real. Em modo mock, redireciona para seleção de usuário."""
    if settings.mock_auth:
        return RedirectResponse("/auth/login", status_code=302)

    url = build_google_auth_url(request)
    return RedirectResponse(url, status_code=302)


@router.post("/mock")
async def login_mock(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
):
    """Login de teste (somente MOCK_AUTH=true)."""
    if not settings.mock_auth:
        return RedirectResponse("/auth/login", status_code=302)

    user = mock_login(email, name)
    request.session["user"] = user
    return RedirectResponse("/", status_code=302)


@router.get("/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse("/auth/login?erro=acesso_negado", status_code=302)

    user = await exchange_code_for_user(request, code, state)
    request.session["user"] = user
    return RedirectResponse("/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=302)
