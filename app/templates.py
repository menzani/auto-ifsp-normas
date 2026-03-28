from pathlib import Path
from fastapi import Request
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _get_flashes(request: Request) -> list[tuple[str, str]]:
    """Retorna e limpa mensagens flash da sessão."""
    return request.session.pop("_flashes", [])


templates.env.globals["get_flashes"] = _get_flashes
