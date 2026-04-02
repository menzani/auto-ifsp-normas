import hashlib
import html
import secrets
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.services.auth import get_current_user, check_csrf_header
from app.services import storage, audit
from app.services.processor import run_in_background
from app.templates import templates

settings = get_settings()
router = APIRouter(tags=["upload"])

PDF_MAGIC = b"%PDF"
MAX_BYTES = settings.max_upload_size_mb * 1024 * 1024


_UPLOAD_ROLES = ("operador", "revisor", "admin")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(get_current_user)):
    if user.get("role") not in _UPLOAD_ROLES:
        return RedirectResponse("/review", status_code=302)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "max_size_mb": settings.max_upload_size_mb,
        "bookstack_url": settings.bookstack_base_url,
        "budget": audit.daily_budget_status(),
    })


@router.post("/upload", response_class=HTMLResponse)
async def upload_pdf(
    request: Request,
    user=Depends(get_current_user),
    pdf_file: UploadFile = File(...),
    title: str = Form(..., min_length=3, max_length=255),
    _csrf=Depends(check_csrf_header),
):
    if user.get("role") not in _UPLOAD_ROLES:
        raise HTTPException(403, "Acesso restrito a operadores, revisores e administradores.")

    # ── Rate limit simples por usuário ───────────────────────────────────
    _check_rate_limit(user["sub"])

    # ── Limite diário de tokens Bedrock ──────────────────────────────────
    budget_status = audit.daily_budget_status()
    if budget_status["exhausted"]:
        return HTMLResponse(
            '<div class="br-message danger" role="alert">'
            '<div class="icon"><i class="fas fa-times-circle" aria-hidden="true"></i></div>'
            '<div class="content">O limite diário de tokens Bedrock foi atingido. '
            'Tente novamente amanhã ou solicite ao administrador que aumente o limite.</div>'
            '</div>',
            status_code=200,
        )

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

    # ── Checksum e detecção de duplicado ─────────────────────────────────
    checksum = hashlib.sha256(content).hexdigest()
    existing = storage.find_pdf_by_checksum(checksum)
    if existing:
        return HTMLResponse(
            f'<div class="br-message warning" role="alert">'
            f'<div class="icon"><i class="fas fa-exclamation-triangle" aria-hidden="true"></i></div>'
            f'<div class="content">Este arquivo já foi enviado anteriormente'
            f' como <strong>{html.escape(existing["title"])}</strong>'
            f' em {html.escape(existing["uploaded_at"])}.</div>'
            f'</div>',
            status_code=200,
        )

    # ── Armazena e dispara processamento ─────────────────────────────────
    job_id = secrets.token_urlsafe(16)
    pdf_key = storage.save_pdf(job_id, content)
    storage.register_pdf_checksum(checksum, job_id, title.strip(), user["email"])
    initial_status = {
        "id": job_id,
        "status": "processing",
        "current_step": 1,
        "total_steps": 5,
        "current_step_label": "Iniciando processamento...",
        "progress_pct": 0,
        "owner": user["email"],
        "pdf_key": pdf_key,
    }
    storage.save_status(job_id, initial_status)

    started = run_in_background(
        job_id=job_id,
        pdf_key=pdf_key,
        title=title.strip(),
        uploaded_by=user["email"],
        checksum=checksum,
    )
    if not started:
        storage.delete_pdf(pdf_key)
        storage.unregister_pdf_checksum_by_job_id(job_id)
        storage.save_status(job_id, {**initial_status, "status": "error",
                                     "error": "Servidor ocupado. Aguarde e tente novamente."})
        return HTMLResponse(
            '<div class="br-message danger" role="alert">'
            '<div class="icon"><i class="fas fa-times-circle" aria-hidden="true"></i></div>'
            '<div class="content">O servidor está processando o número máximo de documentos simultâneos. '
            'Aguarde alguns minutos e tente novamente.</div>'
            '</div>',
            status_code=200,
        )

    audit.log(user["email"], "upload", title.strip(), extra={"checksum": checksum[:12]})

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


# Rate limit em memória — reinicia com o processo, adequado para instância única EC2
_rate_limit: dict[str, list] = {}

def _check_rate_limit(user_sub: str) -> None:
    now = time.time()
    window = 3600  # 1 hora
    limit = settings.max_uploads_per_user_per_hour

    # Remove entradas de usuários cujos timestamps já expiraram todos.
    # Previne acúmulo indefinido do dict ao longo de meses de operação.
    stale = [s for s, ts in _rate_limit.items() if not any(now - t < window for t in ts)]
    for s in stale:
        del _rate_limit[s]

    timestamps = _rate_limit.get(user_sub, [])
    timestamps = [t for t in timestamps if now - t < window]

    if len(timestamps) >= limit:
        raise HTTPException(429, f"Limite de {limit} envios por hora atingido.")

    timestamps.append(now)
    _rate_limit[user_sub] = timestamps
