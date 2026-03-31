import logging
import re
import threading
import time

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services.auth import require_admin, check_csrf_form
from app.services import users as user_store
from app.services import audit, storage
from app.templates import templates

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()
_log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Cache em memória da cotação USD/BRL — revalidado a cada hora
_exchange_cache: dict = {"rate": None, "fetched_at": 0.0}
_exchange_lock = threading.Lock()
_EXCHANGE_TTL = 3600  # segundos


def _get_usd_brl() -> float | None:
    """Busca cotação USD/BRL com cache de 1h. Fonte: Open Exchange Rates (gratuita, sem chave)."""
    now = time.time()
    with _exchange_lock:
        if _exchange_cache["rate"] and now - _exchange_cache["fetched_at"] < _EXCHANGE_TTL:
            return _exchange_cache["rate"]
    try:
        r = httpx.get(
            "https://open.er-api.com/v6/latest/USD",
            timeout=5.0,
        )
        rate = float(r.json()["rates"]["BRL"])
        with _exchange_lock:
            _exchange_cache["rate"] = rate
            _exchange_cache["fetched_at"] = now
        return rate
    except Exception:
        _log.warning("Falha ao buscar cotação USD/BRL")
        with _exchange_lock:
            return _exchange_cache["rate"]  # último valor válido ou None


@router.get("/custo", response_class=HTMLResponse)
def admin_custo(request: Request, user=Depends(require_admin)):
    monthly = audit.bedrock_usage_by_month()
    pricing = storage.load_pricing()

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
        "price_input": pricing["input_per_1m_usd"],
        "price_output": pricing["output_per_1m_usd"],
        "pricing_meta": pricing,
        "model_id": settings.bedrock_model_id,
        "usd_brl": _get_usd_brl(),
    })


@router.post("/custo/pricing", response_class=HTMLResponse)
def update_pricing(
    request: Request,
    user=Depends(require_admin),
    input_per_1m: float = Form(..., gt=0),
    output_per_1m: float = Form(..., gt=0),
    csrf_token: str = Form(""),
):
    check_csrf_form(request, csrf_token)
    storage.save_pricing(input_per_1m, output_per_1m, user["email"])
    audit.log(user["email"], "alterar_preco_bedrock",
              f"entrada={input_per_1m}/1M saída={output_per_1m}/1M")

    # Recarrega a página com os novos preços
    monthly = audit.bedrock_usage_by_month()
    pricing = storage.load_pricing()

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
        "price_input": pricing["input_per_1m_usd"],
        "price_output": pricing["output_per_1m_usd"],
        "pricing_meta": pricing,
        "model_id": settings.bedrock_model_id,
        "usd_brl": _get_usd_brl(),
        "pricing_saved": True,
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
