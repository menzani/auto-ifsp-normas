import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routes import auth, upload, status, review, log, admin, pdf

settings = get_settings()

_log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(application: FastAPI):
    # ── Startup: marca jobs órfãos como erro ─────────────────────────────
    # Ao reiniciar o processo, qualquer job "processing" não tem mais uma thread
    # associada — o usuário ficaria esperando indefinidamente sem esta verificação.
    try:
        from app.services import storage
        orphaned = storage.list_processing_jobs()
        for job in orphaned:
            storage.save_status(job["id"], {
                **job,
                "status": "error",
                "error": "O processamento foi interrompido por uma reinicialização do servidor. Tente novamente.",
            })
            _log.warning("Job órfão marcado como erro no startup: %s", job["id"])
    except Exception:
        _log.exception("Erro ao verificar jobs órfãos no startup")

    try:
        from app.services.users import migrate_role_names
        n = migrate_role_names()
        if n:
            _log.info("Migração de papéis: %d usuário(s) renomeados de 'uploader' para 'operador'", n)
    except Exception:
        _log.exception("Erro na migração de papéis no startup")

    yield
    # Shutdown: nenhuma ação necessária


app = FastAPI(
    title="IFSP Normas",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
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
app.include_router(pdf.router)

