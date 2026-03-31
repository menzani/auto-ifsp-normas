from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.services.auth import get_current_user
from app.services import audit
from app.templates import templates

router = APIRouter(prefix="/log", tags=["log"])


@router.get("", response_class=HTMLResponse)
def log_page(request: Request, user=Depends(get_current_user)):
    is_admin = user.get("role") == "admin"
    show_technical = is_admin and request.query_params.get("tecnico") == "1"

    all_entries = audit.recent(200)
    entries = all_entries if show_technical else [
        e for e in all_entries if e.get("level", "info") == "info"
    ]

    return templates.TemplateResponse("log.html", {
        "request": request,
        "user": user,
        "entries": entries,
        "is_admin": is_admin,
        "show_technical": show_technical,
    })
