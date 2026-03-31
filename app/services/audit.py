"""
Log de auditoria de ações.

MOCK_S3=true  → armazenado em data/audit-YYYY-MM.jsonl (local)
MOCK_S3=false → armazenado em s3://<bucket>/meta/audit-YYYY-MM.jsonl

Arquivos são rotacionados mensalmente para evitar que reads + rewrites S3
cresçam linearmente com o volume total do log.
Leituras (`recent()`) consultam o mês atual e o anterior para cobrir a virada de mês.
Arquivo legado meta/audit.jsonl (sem sufixo de mês) é lido como fallback de migração.
"""
import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.config import get_settings

settings = get_settings()

_lock = threading.Lock()

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

# Chave/arquivo legado (gravado antes da rotação mensal) — lido como fallback
_LEGACY_S3_KEY = "meta/audit.jsonl"
_LEGACY_LOCAL_FILE = Path("data/audit.jsonl")


def _s3_key_for(dt: datetime) -> str:
    return f"meta/audit-{dt.strftime('%Y-%m')}.jsonl"


def _local_file_for(dt: datetime) -> Path:
    return Path(f"data/audit-{dt.strftime('%Y-%m')}.jsonl")


def _read_lines_for_month(dt: datetime) -> list[str]:
    if settings.mock_s3:
        f = _local_file_for(dt)
        if not f.exists():
            return []
        return f.read_text(encoding="utf-8").splitlines()

    from botocore.exceptions import ClientError
    s3 = _get_s3_client()
    try:
        obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=_s3_key_for(dt))
        return obj["Body"].read().decode("utf-8").splitlines()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return []
        raise


def _read_legacy_lines() -> list[str]:
    """Lê o arquivo de log anterior à rotação mensal (migração)."""
    if settings.mock_s3:
        if not _LEGACY_LOCAL_FILE.exists():
            return []
        return _LEGACY_LOCAL_FILE.read_text(encoding="utf-8").splitlines()

    from botocore.exceptions import ClientError
    s3 = _get_s3_client()
    try:
        obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=_LEGACY_S3_KEY)
        return obj["Body"].read().decode("utf-8").splitlines()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return []
        raise


def _append_line(line: str) -> None:
    with _lock:
        _append_line_locked(line)


def _append_line_locked(line: str) -> None:
    now = datetime.now(timezone.utc)

    if settings.mock_s3:
        f = _local_file_for(now)
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return

    # S3 não suporta append nativo — lê o arquivo do mês atual, adiciona e reescreve.
    # Arquivo mensal cresce no máximo ~30× menos que um único arquivo anual.
    s3 = _get_s3_client()
    key = _s3_key_for(now)
    existing_lines = _read_lines_for_month(now)
    existing_lines.append(line)
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body="\n".join(existing_lines).encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def log(user_email: str, action: str, details: str, level: str = "info", extra: dict | None = None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user": user_email,
        "action": action,
        "details": details,
    }
    if level != "info":
        entry["level"] = level  # omitido em entradas normais para manter compatibilidade retroativa
    if extra:
        entry["extra"] = extra
    _append_line(json.dumps(entry, ensure_ascii=False))


def recent(limit: int = 200) -> list[dict]:
    """
    Retorna as entradas mais recentes do log, em ordem decrescente de timestamp.
    Lê o mês atual, o anterior e o arquivo legado (pré-rotação).
    """
    now = datetime.now(timezone.utc)
    prev_month = now.replace(day=1) - timedelta(days=1)

    all_lines = (
        _read_lines_for_month(now)
        + _read_lines_for_month(prev_month)
        + _read_legacy_lines()
    )

    entries = []
    seen: set[str] = set()
    for line in all_lines:
        line = line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        try:
            entry = json.loads(line)
            try:
                dt = datetime.fromisoformat(entry["ts"])
                entry["ts_display"] = dt.strftime("%d/%m/%Y %H:%M")
            except Exception:
                entry["ts_display"] = entry.get("ts", "")
            entries.append(entry)
        except json.JSONDecodeError:
            continue

    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:limit]
