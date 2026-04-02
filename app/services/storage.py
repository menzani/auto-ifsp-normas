"""
Camada de armazenamento.
MOCK_S3=true  → salva em ./data/ localmente.
MOCK_S3=false → usa AWS S3.
"""
import json
import threading
from pathlib import Path

from app.config import get_settings

settings = get_settings()

LOCAL_DATA = Path("data")

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


def _local_path(key: str) -> Path:
    p = (LOCAL_DATA / key).resolve()
    try:
        p.relative_to(LOCAL_DATA.resolve())
    except ValueError:
        raise ValueError(f"Chave de armazenamento inválida: {key!r}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ── Helpers genéricos JSON (mock/S3) ────────────────────────────────────────


def _load_json(key: str, default=None):
    """Lê um JSON do armazenamento. Retorna *default* se a chave não existir."""
    if settings.mock_s3:
        p = _local_path(key)
        if not p.exists():
            return default() if callable(default) else default
        try:
            return json.loads(p.read_text())
        except Exception:
            return default() if callable(default) else default

    from botocore.exceptions import ClientError
    try:
        obj = _get_s3_client().get_object(Bucket=settings.s3_bucket_name, Key=key)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return default() if callable(default) else default
        raise


def _save_json(key: str, data) -> None:
    """Grava um objeto como JSON no armazenamento."""
    content = json.dumps(data, ensure_ascii=False, indent=2).encode()
    if settings.mock_s3:
        _local_path(key).write_bytes(content)
        return

    _get_s3_client().put_object(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=content,
        ContentType="application/json",
    )


# ── PDF ──────────────────────────────────────────────────────────────────────


def save_pdf(job_id: str, content: bytes) -> str:
    """Salva o PDF e retorna a chave de armazenamento."""
    key = f"pdfs/{job_id}.pdf"
    if settings.mock_s3:
        _local_path(key).write_bytes(content)
        return key

    s3 = _get_s3_client()
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=content,
        ContentType="application/pdf",
    )
    return key


def get_pdf(key: str) -> bytes:
    """Lê o PDF do armazenamento."""
    if settings.mock_s3:
        return _local_path(key).read_bytes()

    s3 = _get_s3_client()
    obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=key)
    return obj["Body"].read()


def delete_pdf(key: str) -> None:
    """Remove o PDF do armazenamento. No modo mock, apaga o arquivo local se existir."""
    if settings.mock_s3:
        p = LOCAL_DATA / key
        if p.exists():
            p.unlink()
        return

    s3 = _get_s3_client()
    s3.delete_object(Bucket=settings.s3_bucket_name, Key=key)


# ── Status de jobs ───────────────────────────────────────────────────────────


def save_status(job_id: str, status_data: dict) -> None:
    """Persiste o status do job como JSON."""
    _save_json(f"status/{job_id}.json", status_data)


def load_status(job_id: str) -> dict | None:
    """Lê o status do job. Retorna None se não encontrado."""
    return _load_json(f"status/{job_id}.json")


def list_processing_jobs() -> list[dict]:
    """
    Lista todos os jobs com status 'processing'.
    Usado no startup para detectar jobs órfãos (processo reiniciado enquanto processava).
    """
    if settings.mock_s3:
        status_dir = LOCAL_DATA / "status"
        if not status_dir.exists():
            return []
        jobs = []
        for p in status_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text())
                if data.get("status") == "processing":
                    jobs.append(data)
            except Exception:
                pass
        return jobs

    s3 = _get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    jobs = []
    for page in paginator.paginate(Bucket=settings.s3_bucket_name, Prefix="status/"):
        for obj in page.get("Contents", []):
            try:
                resp = s3.get_object(Bucket=settings.s3_bucket_name, Key=obj["Key"])
                data = json.loads(resp["Body"].read())
                if data.get("status") == "processing":
                    jobs.append(data)
            except Exception:
                pass
    return jobs


# ── URLs ─────────────────────────────────────────────────────────────────────


def get_presigned_url(key: str) -> str:
    """Gera URL presigned do S3 com expiração configurável (padrão: 1 hora)."""
    return _get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket_name, "Key": key},
        ExpiresIn=settings.s3_presigned_url_expiry,
    )


def get_download_url(key: str) -> str:
    """
    Gera URL de download.
    - Mock: path relativo ao diretório estático local.
    - S3: endpoint da aplicação com rate limit por IP (nginx → /pdf/{job_id}).
      O endpoint gera uma presigned URL na hora do acesso, evitando exposição direta do S3.
    """
    if settings.mock_s3:
        return f"/static/data/{key}"

    job_id = key.removeprefix("pdfs/").removesuffix(".pdf")
    return f"{settings.app_base_url}/pdf/{job_id}"


# ── Registro de checksums ───────────────────────────────────────────────────

_CHECKSUMS_KEY = "registry/pdf_checksums.json"
_checksums_lock = threading.Lock()


def find_pdf_by_checksum(checksum: str) -> dict | None:
    """Retorna os metadados do upload anterior com o mesmo checksum, ou None."""
    with _checksums_lock:
        return _load_json(_CHECKSUMS_KEY, dict).get(checksum)


def register_pdf_checksum(checksum: str, job_id: str, title: str, uploaded_by: str) -> None:
    """Registra o checksum SHA-256 de um PDF recém-enviado."""
    from datetime import datetime, timezone
    with _checksums_lock:
        registry = _load_json(_CHECKSUMS_KEY, dict)
        registry[checksum] = {
            "job_id": job_id,
            "title": title,
            "uploaded_by": uploaded_by,
            "uploaded_at": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M"),
        }
        _save_json(_CHECKSUMS_KEY, registry)


def unregister_pdf_checksum_by_job_id(job_id: str) -> None:
    """Remove do registro o checksum associado ao job_id (cancelamento, erro ou exclusão)."""
    with _checksums_lock:
        registry = _load_json(_CHECKSUMS_KEY, dict)
        to_remove = [k for k, v in registry.items() if v.get("job_id") == job_id]
        if not to_remove:
            return
        for k in to_remove:
            del registry[k]
        _save_json(_CHECKSUMS_KEY, registry)


# ── Metadados de livros Bookstack ────────────────────────────────────────────

_BOOK_META_KEY = "registry/book_meta.json"
_book_meta_lock = threading.Lock()


def get_book_meta_registry() -> dict:
    """Retorna {str(book_id): {"uploaded_by": email}} para livros conhecidos pelo sistema."""
    return _load_json(_BOOK_META_KEY, dict)


def register_book_meta(book_id: int, uploaded_by: str) -> None:
    """Registra metadados locais de um livro Bookstack recém-criado."""
    with _book_meta_lock:
        registry = _load_json(_BOOK_META_KEY, dict)
        registry[str(book_id)] = {"uploaded_by": uploaded_by}
        _save_json(_BOOK_META_KEY, registry)


def unregister_book_meta(book_id: int) -> None:
    """Remove metadados locais de um livro deletado."""
    with _book_meta_lock:
        registry = _load_json(_BOOK_META_KEY, dict)
        key = str(book_id)
        if key not in registry:
            return
        del registry[key]
        _save_json(_BOOK_META_KEY, registry)


# ── Cotação USD/BRL ──────────────────────────────────────────────────────────

_EXCHANGE_RATE_KEY = "meta/exchange_rate.json"


def load_exchange_rate() -> dict | None:
    """Carrega a última cotação USD/BRL persistida. Retorna None se nunca registrada."""
    try:
        return _load_json(_EXCHANGE_RATE_KEY)
    except Exception:
        return None


def save_exchange_rate(rate: float, fetched_at: str) -> None:
    """Persiste a cotação USD/BRL atual no S3."""
    _save_json(_EXCHANGE_RATE_KEY, {"rate": rate, "fetched_at": fetched_at})


# ── Preços Bedrock ───────────────────────────────────────────────────────────

_PRICING_KEY = "meta/pricing.json"
_pricing_lock = threading.Lock()

_DEFAULT_PRICING: dict = {
    "input_per_1m_usd": 3.00,
    "output_per_1m_usd": 15.00,
}


def load_pricing() -> dict:
    """Carrega preços do Bedrock do S3. Retorna defaults se ainda não configurado."""
    try:
        result = _load_json(_PRICING_KEY)
        return result if result is not None else dict(_DEFAULT_PRICING)
    except Exception:
        return dict(_DEFAULT_PRICING)


def save_pricing(input_per_1m: float, output_per_1m: float, updated_by: str) -> None:
    """Persiste configuração de preços do Bedrock no S3."""
    from datetime import datetime, timezone
    data = {
        "input_per_1m_usd": round(input_per_1m, 6),
        "output_per_1m_usd": round(output_per_1m, 6),
        "updated_at": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M"),
        "updated_by": updated_by,
    }
    with _pricing_lock:
        _save_json(_PRICING_KEY, data)


# ── Orçamento diário de tokens ────────────────────────────────────────────────

_BUDGET_KEY = "meta/token_budget.json"
_budget_lock = threading.Lock()

_DEFAULT_BUDGET: dict = {"daily_limit": 0}


def load_budget() -> dict:
    """Carrega limite diário de tokens. Retorna defaults se não configurado."""
    try:
        result = _load_json(_BUDGET_KEY)
        return result if result is not None else dict(_DEFAULT_BUDGET)
    except Exception:
        return dict(_DEFAULT_BUDGET)


def save_budget(daily_limit: int, updated_by: str) -> None:
    """Persiste configuração de limite diário de tokens."""
    from datetime import datetime, timezone
    data = {
        "daily_limit": daily_limit,
        "updated_at": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M"),
        "updated_by": updated_by,
    }
    with _budget_lock:
        _save_json(_BUDGET_KEY, data)


# ── Registro de revogações ───────────────────────────────────────────────────

_REVOKED_REGISTRY_KEY = "registry/revoked_books.json"
_revoked_registry_lock = threading.Lock()


def get_revoked_registry() -> list[dict]:
    """Retorna a lista de normativos revogados do registro persistente."""
    return _load_json(_REVOKED_REGISTRY_KEY, list)


def add_to_revoked_registry(entry: dict) -> None:
    """Adiciona uma entrada ao registro de revogados."""
    with _revoked_registry_lock:
        registry = _load_json(_REVOKED_REGISTRY_KEY, list)
        registry.append(entry)
        _save_json(_REVOKED_REGISTRY_KEY, registry)


def remove_from_revoked_registry(revocation_id: str) -> dict | None:
    """
    Remove uma entrada pelo id. Retorna a entrada completa removida, ou None se não encontrada.
    """
    with _revoked_registry_lock:
        registry = _load_json(_REVOKED_REGISTRY_KEY, list)
        entry = next((e for e in registry if e["id"] == revocation_id), None)
        if entry is None:
            return None
        _save_json(_REVOKED_REGISTRY_KEY, [e for e in registry if e["id"] != revocation_id])
        return entry
