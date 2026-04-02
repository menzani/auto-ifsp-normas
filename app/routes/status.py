from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse

from app.constants import JOB_ID_PATTERN
from app.services.auth import get_current_user, check_csrf_header
from app.services import storage
from app.templates import templates

router = APIRouter(prefix="/status", tags=["status"])

_INTERNAL_FIELDS = {"pdf_key", "owner"}
_INTERNAL_RESULT_FIELDS = {"bedrock_usage"}


def _public_job(job: dict) -> dict:
    """Remove campos internos que não devem ser expostos ao cliente via template."""
    filtered = {k: v for k, v in job.items() if k not in _INTERNAL_FIELDS}
    if "result" in filtered and isinstance(filtered["result"], dict):
        filtered["result"] = {k: v for k, v in filtered["result"].items() if k not in _INTERNAL_RESULT_FIELDS}
    return filtered


def _load_and_authorize_job(job_id: str, user: dict) -> dict:
    """Carrega o job e verifica acesso (owner ou revisor/admin)."""
    job = storage.load_status(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    owner = job.get("owner")
    if user.get("role") not in ("revisor", "admin") and (not owner or owner != user["email"]):
        raise HTTPException(403, "Acesso negado.")
    return job


@router.get("/{job_id}", response_class=HTMLResponse)
def job_status(
    request: Request,
    job_id: str = Path(..., pattern=JOB_ID_PATTERN),
    user=Depends(get_current_user),
):
    job = _load_and_authorize_job(job_id, user)
    return templates.TemplateResponse(
        "partials/progress.html",
        {"request": request, "job": _public_job(job)},
    )


@router.post("/{job_id}/cancel", response_class=HTMLResponse)
def cancel_job(
    request: Request,
    job_id: str = Path(..., pattern=JOB_ID_PATTERN),
    user=Depends(get_current_user),
    _csrf=Depends(check_csrf_header),
):
    job = _load_and_authorize_job(job_id, user)
    if job.get("status") == "processing":
        job = {**job, "status": "cancelled"}
        storage.save_status(job_id, job)
    return templates.TemplateResponse(
        "partials/progress.html",
        {"request": request, "job": _public_job(job)},
    )
