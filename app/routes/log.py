from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.services.auth import get_current_user
from app.services import audit
from app.templates import templates

router = APIRouter(prefix="/log", tags=["log"])


@router.get("", response_class=HTMLResponse)
def log_page(request: Request, user=Depends(get_current_user)):
    entries = audit.recent(200)
    return templates.TemplateResponse("log.html", {
        "request": request,
        "entries": entries,
    })
