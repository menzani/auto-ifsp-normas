import logging
import re
import threading
import time
from datetime import datetime, timezone

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


# ── Cotação USD/BRL ────────────────────────────────────────────────────────────

_exchange_cache: dict = {"rate": None, "fetched_at": 0.0, "fetched_at_str": None}
_exchange_lock = threading.Lock()
_EXCHANGE_TTL = 3600  # 1 hora


def _get_usd_brl() -> dict | None:
    """
    Busca cotação USD/BRL com cache de 1h em memória.
    Em caso de falha, usa o último valor persistido no S3 e sinaliza desatualização.
    Retorna {"rate": float, "fetched_at": str, "is_stale": bool} ou None.
    """
    now = time.time()
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")

    with _exchange_lock:
        if _exchange_cache["rate"] and now - _exchange_cache["fetched_at"] < _EXCHANGE_TTL:
            return {
                "rate": _exchange_cache["rate"],
                "fetched_at": _exchange_cache["fetched_at_str"],
                "is_stale": False,
            }

    try:
        r = httpx.get("https://open.er-api.com/v6/latest/USD", timeout=5.0)
        rate = float(r.json()["rates"]["BRL"])
        with _exchange_lock:
            _exchange_cache["rate"] = rate
            _exchange_cache["fetched_at"] = now
            _exchange_cache["fetched_at_str"] = now_str
        storage.save_exchange_rate(rate, now_str)
        return {"rate": rate, "fetched_at": now_str, "is_stale": False}
    except Exception:
        _log.warning("Falha ao buscar cotação USD/BRL — usando valor persistido")

    # Fallback: cache em memória
    with _exchange_lock:
        if _exchange_cache["rate"]:
            return {
                "rate": _exchange_cache["rate"],
                "fetched_at": _exchange_cache["fetched_at_str"],
                "is_stale": True,
            }

    # Fallback: S3
    saved = storage.load_exchange_rate()
    if saved:
        return {"rate": saved["rate"], "fetched_at": saved["fetched_at"], "is_stale": True}

    return None


# ── AWS Cost Explorer ──────────────────────────────────────────────────────────

_ce_cache: dict = {"data": None, "fetched_at": 0.0}
_ce_lock = threading.Lock()
_CE_TTL = 86400  # 24 horas — dados do Cost Explorer têm delay de ~24h


def _get_bedrock_actual_costs(start_year: int, start_month: int) -> dict | None:
    """
    Retorna custo real faturado do Amazon Bedrock via AWS Cost Explorer.
    Resultado: {"costs": {"YYYY-MM": float}, "has_permission": bool}
    Cache de 24h em memória.

    Requer permissão IAM: ce:GetCostAndUsage no role da EC2.
    Dados disponíveis com ~24h de atraso.
    """
    now = time.time()
    with _ce_lock:
        if _ce_cache["data"] is not None and now - _ce_cache["fetched_at"] < _CE_TTL:
            return _ce_cache["data"]

    try:
        import boto3
        from botocore.exceptions import ClientError

        ce = boto3.client("ce", region_name="us-east-1")

        start = f"{start_year:04d}-{start_month:02d}-01"
        today = datetime.now(timezone.utc)
        if today.month == 12:
            end = f"{today.year + 1:04d}-01-01"
        else:
            end = f"{today.year:04d}-{today.month + 1:02d}-01"

        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
            Metrics=["UnblendedCost"],
        )

        costs = {}
        for item in response["ResultsByTime"]:
            month_key = item["TimePeriod"]["Start"][:7]  # YYYY-MM
            costs[month_key] = float(item["Total"]["UnblendedCost"]["Amount"])

        result = {"costs": costs, "has_permission": True}
        with _ce_lock:
            _ce_cache["data"] = result
            _ce_cache["fetched_at"] = now
        return result

    except Exception as exc:
        code = getattr(getattr(exc, "response", None), "get", lambda *a: None)
        is_auth_error = "AccessDenied" in str(exc) or "UnauthorizedOperation" in str(exc)
        _log.warning("Cost Explorer indisponível: %s", exc)
        result = {"costs": {}, "has_permission": not is_auth_error}
        with _ce_lock:
            if _ce_cache["data"] is None:
                _ce_cache["data"] = result
                _ce_cache["fetched_at"] = now
            return _ce_cache["data"]


# ── Helpers compartilhados entre GET e POST ────────────────────────────────────

def _build_custo_context(request: Request, user: dict, extra: dict | None = None) -> dict:
    monthly = audit.bedrock_usage_by_month()
    pricing = storage.load_pricing()

    years_map: dict[int, dict] = {}
    for m in monthly:
        y = m["year"]
        if y not in years_map:
            years_map[y] = {
                "year": y, "months": [],
                "input_tokens": 0, "output_tokens": 0, "legacy_tokens": 0,
                "count_processar": 0, "count_revogar": 0,
            }
        years_map[y]["months"].append(m)
        years_map[y]["input_tokens"] += m["input_tokens"]
        years_map[y]["output_tokens"] += m["output_tokens"]
        years_map[y]["legacy_tokens"] += m["legacy_tokens"]
        years_map[y]["count_processar"] += m["count_processar"]
        years_map[y]["count_revogar"] += m["count_revogar"]

    years = sorted(years_map.values(), key=lambda y: y["year"], reverse=True)

    # Determina o mês inicial para o Cost Explorer
    available = audit.list_available_months()
    start_year, start_month = available[0] if available else (
        datetime.now(timezone.utc).year, datetime.now(timezone.utc).month
    )
    ce_result = _get_bedrock_actual_costs(start_year, start_month)

    ctx = {
        "request": request,
        "user": user,
        "years": years,
        "price_input": pricing["input_per_1m_usd"],
        "price_output": pricing["output_per_1m_usd"],
        "pricing_meta": pricing,
        "model_id": settings.bedrock_model_id,
        "exchange": _get_usd_brl(),
        "actual_costs": ce_result.get("costs", {}) if ce_result else {},
        "ce_has_permission": ce_result.get("has_permission", False) if ce_result else False,
    }
    if extra:
        ctx.update(extra)
    return ctx


# ── Rotas ──────────────────────────────────────────────────────────────────────

@router.get("/custo", response_class=HTMLResponse)
def admin_custo(request: Request, user=Depends(require_admin)):
    return templates.TemplateResponse("admin_custo.html", _build_custo_context(request, user))


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
    return templates.TemplateResponse(
        "admin_custo.html",
        _build_custo_context(request, user, {"pricing_saved": True}),
    )


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
