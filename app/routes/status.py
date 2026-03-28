from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse

from app.services.auth import get_current_user
from app.services import storage
from app.templates import templates

router = APIRouter(prefix="/status", tags=["status"])

_JOB_ID_PATTERN = r"^[a-zA-Z0-9_-]{10,60}$"

_INTERNAL_FIELDS = {"pdf_key", "owner"}


def _public_job(job: dict) -> dict:
    """Remove campos internos que não devem ser expostos ao cliente via template."""
    return {k: v for k, v in job.items() if k not in _INTERNAL_FIELDS}


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_status(
    request: Request,
    job_id: str = Path(..., pattern=_JOB_ID_PATTERN),
    user=Depends(get_current_user),
):
    job = storage.load_status(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    if job.get("owner") and job["owner"] != user["email"] and user.get("role") not in ("revisor", "admin"):
        raise HTTPException(403, "Acesso negado.")
    return templates.TemplateResponse(
        "partials/progress.html",
        {"request": request, "job": _public_job(job)},
    )


@router.post("/{job_id}/cancel", response_class=HTMLResponse)
async def cancel_job(
    request: Request,
    job_id: str = Path(..., pattern=_JOB_ID_PATTERN),
    user=Depends(get_current_user),
):
    job = storage.load_status(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    if job.get("owner") and job["owner"] != user["email"] and user.get("role") not in ("revisor", "admin"):
        raise HTTPException(403, "Acesso negado.")
    if job.get("status") == "processing":
        storage.save_status(job_id, {**job, "status": "cancelled"})
        job = storage.load_status(job_id)
    return templates.TemplateResponse(
        "partials/progress.html",
        {"request": request, "job": _public_job(job)},
    )
