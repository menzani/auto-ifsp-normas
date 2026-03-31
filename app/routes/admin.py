import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services.auth import require_admin, check_csrf_form
from app.services import users as user_store
from app.services import audit
from app.templates import templates

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/custo", response_class=HTMLResponse)
def admin_custo(request: Request, user=Depends(require_admin)):
    monthly = audit.bedrock_usage_by_month()

    # Agrega por ano
    years_map: dict[int, dict] = {}
    for m in monthly:
        y = m["year"]
        if y not in years_map:
            years_map[y] = {
                "year": y,
                "months": [],
                "input_tokens": 0,
                "output_tokens": 0,
                "legacy_tokens": 0,
                "count_processar": 0,
                "count_revogar": 0,
            }
        years_map[y]["months"].append(m)
        years_map[y]["input_tokens"] += m["input_tokens"]
        years_map[y]["output_tokens"] += m["output_tokens"]
        years_map[y]["legacy_tokens"] += m["legacy_tokens"]
        years_map[y]["count_processar"] += m["count_processar"]
        years_map[y]["count_revogar"] += m["count_revogar"]

    years = sorted(years_map.values(), key=lambda y: y["year"], reverse=True)

    return templates.TemplateResponse("admin_custo.html", {
        "request": request,
        "user": user,
        "years": years,
        "price_input": settings.bedrock_price_input_per_1m,
        "price_output": settings.bedrock_price_output_per_1m,
        "model_id": settings.bedrock_model_id,
    })


@router.get("/users", response_class=HTMLResponse)
def admin_users(request: Request, user=Depends(require_admin)):
    all_users = user_store.list_users()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "all_users": all_users,
        "valid_roles": user_store.VALID_ROLES,
    })


@router.post("/users/{email:path}/role", response_class=HTMLResponse)
def update_role(
    email: str,
    request: Request,
    role: str = Form(...),
    csrf_token: str = Form(""),
    user=Depends(require_admin),
):
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(400, "Email inválido.")
    if not settings.mock_auth and not email.endswith(f"@{settings.google_allowed_domain}"):
        raise HTTPException(400, "Email deve pertencer ao domínio institucional.")
    if role not in user_store.VALID_ROLES:
        raise HTTPException(400, "Papel inválido.")
    check_csrf_form(request, csrf_token)
    user_store.set_role(email, role)
    audit.log(user["email"], "alterar_papel", f"{email} → {role}")

    all_users = user_store.list_users()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "all_users": all_users,
        "valid_roles": user_store.VALID_ROLES,
        "saved_email": email,
    })
