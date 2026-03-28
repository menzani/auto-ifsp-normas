from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.services.auth import get_current_user
from app.services import storage
from app.templates import templates

router = APIRouter(prefix="/status", tags=["status"])


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_status(job_id: str, request: Request, user=Depends(get_current_user)):
    job = storage.load_status(job_id)
    if job is None:
        raise HTTPException(404, "Job não encontrado.")
    return templates.TemplateResponse(
        "partials/progress.html",
        {"request": request, "job": job},
    )
