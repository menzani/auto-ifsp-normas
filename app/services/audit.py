"""
Log de auditoria de ações.

MOCK_S3=true  → armazenado em data/audit.jsonl (local)
MOCK_S3=false → armazenado em s3://<bucket>/meta/audit.jsonl
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

settings = get_settings()

_LOCAL_FILE = Path("data/audit.jsonl")
_S3_KEY = "meta/audit.jsonl"
_lock = threading.Lock()


def _read_lines() -> list[str]:
    if settings.mock_s3:
        if not _LOCAL_FILE.exists():
            return []
        return _LOCAL_FILE.read_text(encoding="utf-8").splitlines()

    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3", region_name=settings.aws_region)
    try:
        obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=_S3_KEY)
        return obj["Body"].read().decode("utf-8").splitlines()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket"):
            return []
        raise


def _append_line(line: str) -> None:
    with _lock:
        _append_line_locked(line)


def _append_line_locked(line: str) -> None:
    if settings.mock_s3:
        _LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOCAL_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return

    # S3 não suporta append nativo — lê, adiciona e reescreve
    import boto3
    s3 = boto3.client("s3", region_name=settings.aws_region)
    existing = "\n".join(_read_lines())
    updated = (existing + "\n" + line).lstrip("\n")
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=_S3_KEY,
        Body=updated.encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def log(user_email: str, action: str, details: str) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user": user_email,
        "action": action,
        "details": details,
    }
    _append_line(json.dumps(entry, ensure_ascii=False))


def recent(limit: int = 200) -> list[dict]:
    lines = _read_lines()
    entries = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
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
        if len(entries) >= limit:
            break
    return entries
