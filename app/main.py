from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mangum import Mangum
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routes import auth, upload, status, review, log, admin

settings = get_settings()

app = FastAPI(
    title="IFSP Normas",
    docs_url=None,
    redoc_url=None,
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Redireciona para login em vez de retornar JSON para erros de auth."""
    if exc.status_code == 401:
        return RedirectResponse(url="/auth/login", status_code=302)
    if exc.status_code == 403:
        from app.templates import templates
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "status_code": 403, "detail": exc.detail},
            status_code=403,
        )
    from app.templates import templates
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "status_code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )

# Sessão segura (cookie HttpOnly + assinado com SECRET_KEY)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    session_cookie="ifsp_session",
    max_age=7200,        # 2 horas
    https_only=settings.https_only,
    same_site="lax",
)

# Assets estáticos
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Rotas
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(status.router)
app.include_router(review.router)
app.include_router(log.router)
app.include_router(admin.router)

# Entry point Lambda
handler = Mangum(app, lifespan="off")
