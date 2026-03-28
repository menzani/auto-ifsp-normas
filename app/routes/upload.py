import asyncio
import secrets

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services.auth import get_current_user
from app.services import storage, audit
from app.services.processor import run_in_background
from app.templates import templates

settings = get_settings()
router = APIRouter(tags=["upload"])

PDF_MAGIC = b"%PDF"
MAX_BYTES = settings.max_upload_size_mb * 1024 * 1024


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "max_size_mb": settings.max_upload_size_mb,
        "bookstack_url": settings.bookstack_base_url,
    })


@router.post("/upload", response_class=HTMLResponse)
async def upload_pdf(
    request: Request,
    user=Depends(get_current_user),
    pdf_file: UploadFile = File(...),
    title: str = Form(...),
):
    # ── Rate limit simples por usuário ───────────────────────────────────
    _check_rate_limit(user["sub"])

    # ── Lê o arquivo em memória com limite de tamanho ────────────────────
    content = await pdf_file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        return HTMLResponse(
            f'<div class="br-message danger" role="alert">'
            f'<div class="icon"><i class="fas fa-times-circle" aria-hidden="true"></i></div>'
            f'<div class="content">O arquivo excede o limite de {settings.max_upload_size_mb} MB.</div>'
            f'</div>',
            status_code=200,
        )

    # ── Validação em 3 camadas ───────────────────────────────────────────
    error_msg = _validate_pdf(pdf_file.filename or "", pdf_file.content_type or "", content)
    if error_msg:
        return HTMLResponse(
            f'<div class="br-message danger" role="alert">'
            f'<div class="icon"><i class="fas fa-times-circle" aria-hidden="true"></i></div>'
            f'<div class="content">{error_msg}</div>'
            f'</div>',
            status_code=200,
        )

    # ── Armazena e dispara processamento ─────────────────────────────────
    job_id = secrets.token_urlsafe(16)
    pdf_key = storage.save_pdf(job_id, content)
    initial_status = {
        "id": job_id,
        "status": "processing",
        "current_step": 1,
        "total_steps": 4,
        "current_step_label": "Iniciando processamento...",
        "progress_pct": 0,
        "owner": user["email"],
    }
    storage.save_status(job_id, initial_status)

    audit.log(user["email"], "upload", title.strip())
    run_in_background(
        job_id=job_id,
        pdf_key=pdf_key,
        title=title.strip(),
        uploaded_by=user["email"],
    )

    return templates.TemplateResponse(
        "partials/progress.html",
        {"request": request, "job": initial_status},
    )


def _validate_pdf(filename: str, content_type: str, content: bytes) -> str | None:
    """Valida extensão, MIME type e magic bytes. Retorna mensagem de erro ou None."""
    if not filename.lower().endswith(".pdf"):
        return "O arquivo deve ter extensão .pdf."
    if content_type not in ("application/pdf", "application/octet-stream"):
        return "Tipo de arquivo inválido. Envie um PDF."
    if not content.startswith(PDF_MAGIC):
        return "O arquivo não parece ser um PDF válido (assinatura incorreta)."
    return None


# Rate limit em memória (reinicia com o processo — suficiente para Fase 1)
_rate_limit: dict[str, list] = {}

def _check_rate_limit(user_sub: str) -> None:
    import time
    now = time.time()
    window = 3600  # 1 hora
    limit = settings.max_uploads_per_user_per_hour

    timestamps = _rate_limit.get(user_sub, [])
    timestamps = [t for t in timestamps if now - t < window]

    if len(timestamps) >= limit:
        raise HTTPException(429, f"Limite de {limit} envios por hora atingido.")

    timestamps.append(now)
    _rate_limit[user_sub] = timestamps
