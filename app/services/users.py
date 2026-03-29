"""
Gerenciamento de usuários e papéis.

MOCK_S3=true  → armazenado em data/users.json (local)
MOCK_S3=false → armazenado em s3://<bucket>/meta/users.json

Papéis válidos:
  servidor  — visualização geral (revisão somente leitura, log); padrão
  uploader  — servidor + pode enviar normativos
  revisor   — uploader + pode publicar/revogar normativos
  admin     — revisor + pode excluir rascunhos/revogados, gerenciar usuários
"""
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

settings = get_settings()

VALID_ROLES = ("servidor", "uploader", "revisor", "admin")

_s3_client = None
_s3_client_lock = threading.Lock()


def _get_s3_client():
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    with _s3_client_lock:
        if _s3_client is None:
            import boto3
            _s3_client = boto3.client("s3", region_name=settings.aws_region)
    return _s3_client

_LOCAL_STORE = Path("data/users.json")
_S3_KEY = "meta/users.json"

# Cache em memória para papéis — evita leitura S3 a cada request autenticado.
# Invalidado individualmente ao alterar ou criar um usuário.
_ROLE_TTL = 60.0  # segundos
_role_cache: dict[str, tuple[str, float]] = {}  # email → (role, timestamp)
_role_cache_lock = threading.Lock()


def _role_cache_get(email: str) -> str | None:
    with _role_cache_lock:
        entry = _role_cache.get(email)
    if entry and (time.monotonic() - entry[1]) < _ROLE_TTL:
        return entry[0]
    return None


def _role_cache_set(email: str, role: str) -> None:
    with _role_cache_lock:
        _role_cache[email] = (role, time.monotonic())


def _role_cache_invalidate(email: str) -> None:
    with _role_cache_lock:
        _role_cache.pop(email, None)


def _load() -> dict:
    if settings.mock_s3:
        if not _LOCAL_STORE.exists():
            return {}
        try:
            return json.loads(_LOCAL_STORE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    from botocore.exceptions import ClientError
    s3 = _get_s3_client()
    try:
        obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=_S3_KEY)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return {}
        raise


def _save(data: dict) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    if settings.mock_s3:
        _LOCAL_STORE.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_STORE.write_bytes(content)
        return

    s3 = _get_s3_client()
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=_S3_KEY,
        Body=content,
        ContentType="application/json",
    )


def get_role(email: str) -> str:
    cached = _role_cache_get(email)
    if cached is not None:
        return cached
    role = _load().get(email, {}).get("role", "servidor")
    _role_cache_set(email, role)
    return role


def set_role(email: str, role: str) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"Papel inválido: {role}")
    data = _load()
    if email not in data:
        data[email] = {}
    data[email]["role"] = role
    _save(data)
    _role_cache_invalidate(email)


def upsert_user(email: str, name: str, bootstrap_admins: list[str]) -> str:
    """
    Cria ou atualiza o usuário. Retorna o papel atual.
    Na primeira vez, atribui "admin" se o email constar em bootstrap_admins,
    caso contrário "servidor".
    Emails em bootstrap_admins sempre mantêm papel admin.
    """
    data = _load()
    now = datetime.now(timezone.utc).isoformat()
    if email not in data:
        role = "admin" if email in bootstrap_admins else "servidor"
        data[email] = {"role": role, "name": name, "first_login": now}
    else:
        data[email]["name"] = name
        if email in bootstrap_admins and data[email].get("role") != "admin":
            data[email]["role"] = "admin"
    data[email]["last_login"] = now
    _save(data)
    role = data[email]["role"]
    _role_cache_set(email, role)
    return role


def list_users() -> list[dict]:
    data = _load()
    users = []
    for email, info in data.items():
        last_login_raw = info.get("last_login", "")
        try:
            last_login = datetime.fromisoformat(last_login_raw).strftime("%d/%m/%Y %H:%M")
        except Exception:
            last_login = last_login_raw
        users.append({
            "email": email,
            "name": info.get("name", ""),
            "role": info.get("role", "servidor"),
            "last_login": last_login,
        })
    users.sort(key=lambda u: u.get("last_login", ""), reverse=True)
    return users
