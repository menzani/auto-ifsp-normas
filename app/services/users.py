"""
Gerenciamento de usuários e papéis.

MOCK_S3=true  → armazenado em data/users.json (local)
MOCK_S3=false → armazenado em s3://<bucket>/meta/users.json

Papéis válidos:
  servidor  — pode enviar normativos (padrão)
  revisor   — pode enviar + revisar/publicar/remover rascunhos
  admin     — acesso total + gerenciar usuários + ver logs
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

settings = get_settings()

VALID_ROLES = ("servidor", "revisor", "admin")

_LOCAL_STORE = Path("data/users.json")
_S3_KEY = "meta/users.json"


def _load() -> dict:
    if settings.mock_s3:
        if not _LOCAL_STORE.exists():
            return {}
        try:
            return json.loads(_LOCAL_STORE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3", region_name=settings.aws_region)
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

    import boto3
    s3 = boto3.client("s3", region_name=settings.aws_region)
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=_S3_KEY,
        Body=content,
        ContentType="application/json",
    )


def get_role(email: str) -> str:
    return _load().get(email, {}).get("role", "servidor")


def set_role(email: str, role: str) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"Papel inválido: {role}")
    data = _load()
    if email not in data:
        data[email] = {}
    data[email]["role"] = role
    _save(data)


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
    return data[email]["role"]


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
