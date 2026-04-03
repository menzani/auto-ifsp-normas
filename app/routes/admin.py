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


_PRICING_URL = "https://aws.amazon.com/bedrock/pricing/"
_EXCHANGE_URL = "https://www.google.com/finance/quote/USD-BRL"


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

def _usd(inp: int, out: int, price_input: float, price_output: float, leg: int = 0) -> float:
    """Calcula custo USD a partir de tokens. leg = tokens no formato legado (split 50/50)."""
    leg_in = leg // 2
    leg_out = leg - leg_in
    return ((inp + leg_in) / 1_000_000 * price_input +
            (out + leg_out) / 1_000_000 * price_output)


def _formula(in_tok: int, out_tok: int, pi: float, po: float) -> dict:
    """Monta dict com os componentes da fórmula de custo para exibição."""
    in_cost = in_tok / 1_000_000 * pi
    out_cost = out_tok / 1_000_000 * po
    return {
        "in_tok": in_tok, "out_tok": out_tok,
        "in_cost": in_cost, "out_cost": out_cost,
        "total": in_cost + out_cost,
    }


def _resolve_pricing() -> dict:
    """
    Resolve preços a partir de configuração manual (S3) ou default.
    Retorna dict unificado para o contexto do template.
    """
    manual = storage.load_pricing()
    has_manual = "updated_by" in manual
    return {
        "input_per_1m_usd": manual["input_per_1m_usd"],
        "output_per_1m_usd": manual["output_per_1m_usd"],
        "source": "manual" if has_manual else "default",
        "fetched_at": manual.get("updated_at"),
        "updated_by": manual.get("updated_by"),
    }


def _build_custo_context(request: Request, user: dict, extra: dict | None = None) -> dict:
    monthly = audit.bedrock_usage_by_month()
    pricing = _resolve_pricing()
    pi = pricing["input_per_1m_usd"]
    po = pricing["output_per_1m_usd"]

    # Determina o mês inicial para o Cost Explorer
    available = audit.list_available_months()
    start_year, start_month = available[0] if available else (
        datetime.now(timezone.utc).year, datetime.now(timezone.utc).month
    )
    ce_result = _get_bedrock_actual_costs(start_year, start_month)
    actual_costs = ce_result.get("costs", {}) if ce_result else {}

    # Enriquece cada mês com custos pré-calculados e fórmulas
    months_enriched = []
    for m in monthly:
        leg = m["legacy_tokens"]
        extraction_est = _usd(m["extraction_input"], m["extraction_output"], pi, po)
        faq_est        = _usd(m["faq_input"],        m["faq_output"],        pi, po)
        revocation_est = _usd(m["revocation_input"], m["revocation_output"], pi, po)
        combined_est   = _usd(m["combined_input"],   m["combined_output"],   pi, po, leg)
        total_est = extraction_est + faq_est + revocation_est + combined_est
        m_key = f"{m['year']:04d}-{m['month']:02d}"
        months_enriched.append({
            **m,
            "extraction_est_usd": extraction_est,
            "faq_est_usd":        faq_est,
            "revocation_est_usd": revocation_est,
            "combined_est_usd":   combined_est,
            "total_est_usd":      total_est,
            "actual_usd":         actual_costs.get(m_key),
            "formulas": {
                "extraction": _formula(m["extraction_input"], m["extraction_output"], pi, po),
                "faq":        _formula(m["faq_input"],        m["faq_output"],        pi, po),
                "revocation": _formula(m["revocation_input"], m["revocation_output"], pi, po),
            },
        })

    # Agrupa por ano
    years_map: dict[int, dict] = {}
    for m in months_enriched:
        y = m["year"]
        if y not in years_map:
            years_map[y] = {
                "year": y, "months": [],
                "count_processar": 0, "count_revogar": 0,
                "extraction_est_usd": 0.0, "faq_est_usd": 0.0,
                "revocation_est_usd": 0.0, "combined_est_usd": 0.0,
                "total_est_usd": 0.0, "actual_usd": 0.0,
                "has_actual": False, "has_split": False,
            }
        yr = years_map[y]
        yr["months"].append(m)
        yr["count_processar"]    += m["count_processar"]
        yr["count_revogar"]      += m["count_revogar"]
        yr["extraction_est_usd"] += m["extraction_est_usd"]
        yr["faq_est_usd"]        += m["faq_est_usd"]
        yr["revocation_est_usd"] += m["revocation_est_usd"]
        yr["combined_est_usd"]   += m["combined_est_usd"]
        yr["total_est_usd"]      += m["total_est_usd"]
        if m["actual_usd"] is not None:
            yr["actual_usd"] += m["actual_usd"]
            yr["has_actual"] = True
        if m["has_split"]:
            yr["has_split"] = True

    for yr in years_map.values():
        if not yr["has_actual"]:
            yr["actual_usd"] = None

    years = sorted(years_map.values(), key=lambda y: y["year"], reverse=True)

    # Totais globais
    grand: dict = {
        "count_processar":    sum(y["count_processar"]    for y in years),
        "count_revogar":      sum(y["count_revogar"]      for y in years),
        "extraction_est_usd": sum(y["extraction_est_usd"] for y in years),
        "faq_est_usd":        sum(y["faq_est_usd"]        for y in years),
        "revocation_est_usd": sum(y["revocation_est_usd"] for y in years),
        "combined_est_usd":   sum(y["combined_est_usd"]   for y in years),
        "total_est_usd":      sum(y["total_est_usd"]      for y in years),
        "actual_usd": (
            sum(y["actual_usd"] for y in years if y["actual_usd"] is not None)
            if any(y["actual_usd"] is not None for y in years) else None
        ),
        "has_split": any(y["has_split"] for y in years),
    }

    ctx = {
        "request": request,
        "user": user,
        "years": years,
        "grand": grand,
        "pricing": pricing,
        "model_id": settings.bedrock_model_id,
        "exchange": _get_usd_brl(),
        "ce_has_permission": ce_result.get("has_permission", False) if ce_result else False,
        "pricing_url": _PRICING_URL,
        "exchange_url": _EXCHANGE_URL,
        "budget": storage.load_budget(),
        "budget_status": audit.daily_budget_status(),
        "usage_months": list(reversed(available)),
        "usage_by_user": _get_usage_by_user(available),
        "usage_selected": _format_month(available),
    }
    if extra:
        ctx.update(extra)
    return ctx


_MONTH_NAMES_PT = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


def _format_month(available: list[tuple[int, int]]) -> str:
    if not available:
        now = datetime.now(timezone.utc)
        return f"{_MONTH_NAMES_PT[now.month - 1]} {now.year}"
    y, m = available[-1]
    return f"{_MONTH_NAMES_PT[m - 1]} {y}"


def _get_usage_by_user(available: list[tuple[int, int]]) -> list[dict]:
    if not available:
        now = datetime.now(timezone.utc)
        return audit.token_usage_by_user(now.year, now.month)
    y, m = available[-1]
    return audit.token_usage_by_user(y, m)


# ── Rotas ──────────────────────────────────────────────────────────────────────

@router.get("/custo", response_class=HTMLResponse)
def admin_custo(request: Request, user=Depends(require_admin)):
    return templates.TemplateResponse("admin_custo.html", _build_custo_context(request, user))


@router.get("/custo/usage-by-user", response_class=HTMLResponse)
def usage_by_user_partial(
    request: Request,
    year: int = 0,
    month: int = 0,
    user=Depends(require_admin),
):
    now = datetime.now(timezone.utc)
    if year == 0 or month == 0 or not (1 <= month <= 12) or not (2000 <= year <= 2100):
        year, month = now.year, now.month
    data = audit.token_usage_by_user(year, month)
    label = f"{_MONTH_NAMES_PT[month - 1]} {year}"
    return templates.TemplateResponse("partials/usage_by_user.html", {
        "request": request,
        "usage_by_user": data,
        "usage_selected": label,
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
    return templates.TemplateResponse(
        "admin_custo.html",
        _build_custo_context(request, user, {"pricing_saved": True}),
    )


@router.post("/custo/exchange", response_class=HTMLResponse)
def update_exchange(
    request: Request,
    user=Depends(require_admin),
    rate: float = Form(..., gt=0),
    csrf_token: str = Form(""),
):
    check_csrf_form(request, csrf_token)
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")
    # Salva no S3 e atualiza o cache em memória
    storage.save_exchange_rate(rate, now_str)
    with _exchange_lock:
        _exchange_cache["rate"] = rate
        _exchange_cache["fetched_at"] = time.time()
        _exchange_cache["fetched_at_str"] = now_str
    audit.log(user["email"], "alterar_cotacao", f"1 USD = R$ {rate:.4f}")
    return templates.TemplateResponse(
        "admin_custo.html",
        _build_custo_context(request, user, {"exchange_saved": True}),
    )


@router.post("/custo/budget", response_class=HTMLResponse)
def update_budget(
    request: Request,
    user=Depends(require_admin),
    daily_limit: int = Form(..., ge=0, le=10_000_000_000),
    csrf_token: str = Form(""),
):
    check_csrf_form(request, csrf_token)
    storage.save_budget(daily_limit, user["email"])
    audit.invalidate_budget_status_cache()
    audit.log(user["email"], "alterar_limite_diario",
              f"limite={daily_limit} tokens" if daily_limit > 0 else "limite=ilimitado")
    return templates.TemplateResponse(
        "admin_custo.html",
        _build_custo_context(request, user, {"budget_saved": True}),
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
    if email == user["email"]:
        raise HTTPException(400, "Não é permitido alterar o próprio papel.")
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
