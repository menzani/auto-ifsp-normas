from pathlib import Path
from fastapi import Request
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _get_flashes(request: Request) -> list[tuple[str, str]]:
    """Retorna e limpa mensagens flash da sessão."""
    return request.session.pop("_flashes", [])


def _get_budget_alert(request: Request) -> dict:
    """Retorna status do budget para o alerta global (apenas admins, >= 70%)."""
    user = request.session.get("user")
    if not user or user.get("role") != "admin":
        return {}
    from app.services.audit import daily_budget_status
    status = daily_budget_status()
    if status.get("active") and status.get("pct", 0) >= 70:
        return status
    return {}


templates.env.globals["get_flashes"] = _get_flashes
templates.env.globals["get_budget_alert"] = _get_budget_alert
