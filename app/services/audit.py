"""
Log de auditoria de ações.

MOCK_S3=true  → armazenado em data/audit-YYYY-MM.jsonl (local)
MOCK_S3=false → armazenado em s3://<bucket>/meta/audit-YYYY-MM.jsonl

Arquivos são rotacionados mensalmente para evitar que reads + rewrites S3
cresçam linearmente com o volume total do log.
Leituras (`recent()`) consultam o mês atual e o anterior para cobrir a virada de mês.
"""
import json
import re
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.config import get_settings
from app.services.storage import _get_s3_client

settings = get_settings()

_lock = threading.Lock()

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
        entry["level"] = level  # omitido quando "info" para manter o JSON conciso
    if extra:
        entry["extra"] = extra
    _append_line(json.dumps(entry, ensure_ascii=False))


_MONTH_NAMES_PT = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


def _parse_lines(lines: list[str]) -> list[dict]:
    """Parsa linhas NDJSON, deduplica e retorna lista ordenada por timestamp decrescente."""
    entries = []
    seen: set[str] = set()
    for line in lines:
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
    return entries


def recent(limit: int = 200) -> list[dict]:
    """Retorna as entradas mais recentes do log, em ordem decrescente de timestamp."""
    now = datetime.now(timezone.utc)
    prev_month = now.replace(day=1) - timedelta(days=1)
    all_lines = _read_lines_for_month(now) + _read_lines_for_month(prev_month)
    return _parse_lines(all_lines)[:limit]


def read_month(year: int, month: int) -> list[dict]:
    """Retorna todas as entradas de um mês específico, em ordem decrescente de timestamp."""
    dt = datetime(year, month, 1, tzinfo=timezone.utc)
    return _parse_lines(_read_lines_for_month(dt))


def list_available_months() -> list[tuple[int, int]]:
    """Retorna lista de (ano, mês) com arquivos de audit existentes, em ordem crescente."""
    _AUDIT_RE = re.compile(r"audit-(\d{4})-(\d{2})\.jsonl$")

    if settings.mock_s3:
        result = []
        for f in sorted(Path("data").glob("audit-*.jsonl")):
            m = _AUDIT_RE.search(f.name)
            if m:
                result.append((int(m.group(1)), int(m.group(2))))
        return result

    from botocore.exceptions import ClientError
    s3 = _get_s3_client()
    result = []
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=settings.s3_bucket_name, Prefix="meta/audit-"):
            for obj in page.get("Contents", []):
                m = _AUDIT_RE.search(obj["Key"])
                if m:
                    result.append((int(m.group(1)), int(m.group(2))))
    except ClientError:
        pass
    return sorted(result)


def bedrock_usage_by_month() -> list[dict]:
    """
    Agrega uso de tokens Bedrock por mês lendo todos os arquivos de audit.
    Retorna lista ordenada por (ano, mês).

    Suporta três formatos de extra em entradas "processar":
    - Completo (novo): extraction_input_tokens + faq_input_tokens + seus output equivalentes
    - Combinado: input_tokens + output_tokens (sem split extração/FAQ)
    - Legado: tokens = total (split 50/50 para cálculo de custo)

    Entradas "revogar" usam input_tokens + output_tokens (ou tokens legado).
    """
    result = []
    for year, month in list_available_months():
        entries = read_month(year, month)
        count_processar = count_revogar = 0
        extraction_input = extraction_output = 0
        faq_input = faq_output = 0
        revocation_input = revocation_output = 0
        combined_input = combined_output = 0  # processar sem split extração/FAQ
        legacy_tokens = 0  # formato muito antigo (campo "tokens")

        for e in entries:
            action = e.get("action", "")
            extra = e.get("extra") or {}
            if action not in ("processar", "revogar"):
                continue

            if action == "processar":
                count_processar += 1
                if "extraction_input_tokens" in extra:
                    extraction_input += extra.get("extraction_input_tokens", 0)
                    extraction_output += extra.get("extraction_output_tokens", 0)
                    faq_input += extra.get("faq_input_tokens", 0)
                    faq_output += extra.get("faq_output_tokens", 0)
                elif extra.get("input_tokens") or extra.get("output_tokens"):
                    combined_input += extra.get("input_tokens", 0)
                    combined_output += extra.get("output_tokens", 0)
                elif extra.get("tokens"):
                    legacy_tokens += extra["tokens"]
            else:  # revogar
                count_revogar += 1
                if extra.get("input_tokens") or extra.get("output_tokens"):
                    revocation_input += extra.get("input_tokens", 0)
                    revocation_output += extra.get("output_tokens", 0)
                elif extra.get("tokens"):
                    legacy_tokens += extra["tokens"]

        result.append({
            "year": year,
            "month": month,
            "month_name": _MONTH_NAMES_PT[month - 1],
            "count_processar": count_processar,
            "count_revogar": count_revogar,
            "extraction_input": extraction_input,
            "extraction_output": extraction_output,
            "faq_input": faq_input,
            "faq_output": faq_output,
            "revocation_input": revocation_input,
            "revocation_output": revocation_output,
            "combined_input": combined_input,   # processar sem split extração/FAQ
            "combined_output": combined_output,
            "legacy_tokens": legacy_tokens,     # formato antigo sem split entrada/saída
            "has_split": extraction_input + extraction_output + faq_input + faq_output > 0,
        })
    return result
