from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.services.auth import get_current_user
from app.services import audit
from app.templates import templates

router = APIRouter(prefix="/log", tags=["log"])

_PAGE_SIZE = 20


@router.get("", response_class=HTMLResponse)
def log_page(request: Request, user=Depends(get_current_user)):
    is_admin = user.get("role") == "admin"
    show_technical = is_admin and request.query_params.get("tecnico") == "1"

    now = datetime.now(timezone.utc)
    try:
        year = int(request.query_params.get("year", now.year))
        month = int(request.query_params.get("month", now.month))
        page = max(1, int(request.query_params.get("page", 1)))
    except (ValueError, TypeError):
        year, month, page = now.year, now.month, 1

    month = max(1, min(12, month))

    all_entries = audit.read_month(year, month)
    entries = all_entries if show_technical else [
        e for e in all_entries if e.get("level", "info") == "info"
    ]

    total = len(entries)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, total_pages)
    start = (page - 1) * _PAGE_SIZE
    page_entries = entries[start:start + _PAGE_SIZE]

    # Meses disponíveis — garante que o mês atual sempre aparece na lista
    available = audit.list_available_months()
    current = (now.year, now.month)
    if current not in available:
        available = sorted(available + [current])

    available_months = [
        (y, m, f"{audit._MONTH_NAMES_PT[m - 1]}/{y}")
        for y, m in reversed(available)
    ]

    return templates.TemplateResponse("log.html", {
        "request": request,
        "user": user,
        "entries": page_entries,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "year": year,
        "month": month,
        "available_months": available_months,
        "is_admin": is_admin,
        "show_technical": show_technical,
    })
