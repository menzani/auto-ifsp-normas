from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.services import storage

settings = get_settings()
router = APIRouter(tags=["pdf"])

_JOB_ID_PATTERN = r"^[a-zA-Z0-9_-]{10,50}$"


@router.get("/pdf/{job_id}")
async def download_pdf(job_id: str = Path(..., pattern=_JOB_ID_PATTERN)):
    """
    Redireciona para download do PDF via URL presigned do S3.
    Sem autenticação — documentos publicados são públicos.
    Rate limiting por IP aplicado no nginx (evita custo de flood no S3).
    """
    if settings.mock_s3:
        return RedirectResponse(f"/static/data/pdfs/{job_id}.pdf", status_code=302)

    key = f"pdfs/{job_id}.pdf"
    try:
        url = storage.get_presigned_url(key)
    except Exception:
        raise HTTPException(404, "PDF não encontrado.")
    return RedirectResponse(url, status_code=302)
