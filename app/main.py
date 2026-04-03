import logging
import threading
import time
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

# ── Filtro de insistência — loga quando mesmo IP/usuário acumula 5+ erros,
#    ou quando mesmo usuário tenta de 3+ IPs distintos na janela ──────────────
_denial_counts: dict[str, list[float]] = {}  # "ip:email" → timestamps
_denial_ips: dict[str, dict[str, float]] = {}  # email → {ip: last_ts}
_denial_lock = threading.Lock()
_DENIAL_THRESHOLD = 5
_DENIAL_IP_THRESHOLD = 3
_DENIAL_WINDOW = 300.0  # 5 minutos


def _track_denial(request: Request, status_code: int) -> None:
    """Registra tentativa negada e loga se atingir limiar de insistência."""
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    user = request.session.get("user", {})
    email = user.get("email", "anon")
    key = f"{ip}:{email}"

    now = time.monotonic()
    with _denial_lock:
        # ── Insistência por IP:usuário (5+ tentativas do mesmo par) ──────
        timestamps = _denial_counts.get(key, [])
        timestamps = [t for t in timestamps if now - t < _DENIAL_WINDOW]
        timestamps.append(now)
        _denial_counts[key] = timestamps

        if len(timestamps) == _DENIAL_THRESHOLD:
            from app.services import audit
            audit.log(
                email,
                "acesso_insistente",
                f"{_DENIAL_THRESHOLD}+ tentativas negadas ({status_code}) de {ip} em {request.url.path}",
                level="warn",
            )
            _log.warning("Insistência detectada: %s (%d× em %s)", key, len(timestamps), request.url.path)

        # ── Rotação de IP (mesmo usuário de 3+ IPs distintos) ────────────
        if email != "anon":
            ip_map = _denial_ips.get(email, {})
            # Limpar IPs fora da janela
            ip_map = {i: t for i, t in ip_map.items() if now - t < _DENIAL_WINDOW}
            ip_map[ip] = now
            _denial_ips[email] = ip_map

            if len(ip_map) == _DENIAL_IP_THRESHOLD:
                from app.services import audit
                ips = ", ".join(sorted(ip_map.keys()))
                audit.log(
                    email,
                    "rotacao_ip",
                    f"Tentativas negadas de {len(ip_map)} IPs distintos em {int(_DENIAL_WINDOW)}s: {ips}",
                    level="warn",
                )
                _log.warning("Rotação de IP detectada: %s de %d IPs", email, len(ip_map))

    # Limpeza periódica de chaves expiradas
    if len(_denial_counts) + len(_denial_ips) > 1000:
        with _denial_lock:
            stale = [k for k, ts in _denial_counts.items() if not ts or now - ts[-1] > _DENIAL_WINDOW]
            for k in stale:
                del _denial_counts[k]
            stale_ips = [e for e, m in _denial_ips.items() if not m or max(m.values()) < now - _DENIAL_WINDOW]
            for e in stale_ips:
                del _denial_ips[e]


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

    # ── Startup: aquece cache do overview Bookstack ─────────────────────
    # A primeira consulta à /review seria lenta sem cache. Ao aquecer em
    # background no startup, o primeiro usuário já recebe resposta rápida.
    if not settings.mock_bookstack:
        def _warm_cache():
            try:
                from app.services.bookstack import _build_overview_fresh
                _build_overview_fresh()
                _log.info("Cache do overview Bookstack aquecido no startup")
            except Exception:
                _log.warning("Falha ao aquecer cache do overview (Bookstack pode estar indisponível)")
        threading.Thread(target=_warm_cache, daemon=True).start()

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
    if exc.status_code in (403, 429):
        _track_denial(request, exc.status_code)
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

