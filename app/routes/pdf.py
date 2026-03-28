from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.constants import JOB_ID_PATTERN
from app.services import storage
from app.services.auth import get_current_user

settings = get_settings()
router = APIRouter(tags=["pdf"])


@router.get("/pdf/{job_id}")
async def download_pdf(
    request: Request,
    job_id: str = Path(..., pattern=JOB_ID_PATTERN),
):
    """
    Redireciona para download do PDF via URL presigned do S3.

    Documentos concluídos (status=done) são acessíveis sem autenticação —
    o link fica embutido na página do Bookstack e pode ser compartilhado.
    Rascunhos e jobs em processamento exigem autenticação e ownership.
    Rate limiting por IP aplicado no nginx (evita custo de flood no S3).
    """
    job = storage.load_status(job_id)

    if job is None or job.get("status") == "error":
        raise HTTPException(404, "PDF não encontrado.")

    # Rascunhos e jobs em andamento: apenas o dono ou revisores/admins
    if job.get("status") != "done":
        user = get_current_user(request)
        owner = job.get("owner")
        if owner and user["email"] != owner and user.get("role") not in ("revisor", "admin"):
            raise HTTPException(403, "Acesso negado.")

    if settings.mock_s3:
        return RedirectResponse(f"/static/data/pdfs/{job_id}.pdf", status_code=302)

    key = f"pdfs/{job_id}.pdf"
    try:
        url = storage.get_presigned_url(key)
    except Exception:
        raise HTTPException(404, "PDF não encontrado.")
    return RedirectResponse(url, status_code=302)
