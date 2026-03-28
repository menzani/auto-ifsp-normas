"""
Camada de armazenamento.
MOCK_S3=true  → salva em ./data/ localmente.
MOCK_S3=false → usa AWS S3.
"""
import json
import shutil
from pathlib import Path

from app.config import get_settings

settings = get_settings()

LOCAL_DATA = Path("data")


def _local_path(key: str) -> Path:
    p = LOCAL_DATA / key
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def save_pdf(job_id: str, content: bytes) -> str:
    """Salva o PDF e retorna a chave de armazenamento."""
    key = f"pdfs/{job_id}.pdf"
    if settings.mock_s3:
        _local_path(key).write_bytes(content)
        return key

    import boto3
    s3 = boto3.client("s3", region_name=settings.aws_region)
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

    import boto3
    s3 = boto3.client("s3", region_name=settings.aws_region)
    obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=key)
    return obj["Body"].read()


def save_status(job_id: str, status_data: dict) -> None:
    """Persiste o status do job como JSON."""
    key = f"status/{job_id}.json"
    content = json.dumps(status_data, ensure_ascii=False, indent=2).encode()
    if settings.mock_s3:
        _local_path(key).write_bytes(content)
        return

    import boto3
    s3 = boto3.client("s3", region_name=settings.aws_region)
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=content,
        ContentType="application/json",
    )


def load_status(job_id: str) -> dict | None:
    """Lê o status do job. Retorna None se não encontrado."""
    key = f"status/{job_id}.json"
    if settings.mock_s3:
        p = _local_path(key)
        if not p.exists():
            return None
        return json.loads(p.read_text())

    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3", region_name=settings.aws_region)
    try:
        obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=key)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


def get_download_url(key: str) -> str:
    """Gera URL de download (local: path relativo; S3: URL pública permanente)."""
    if settings.mock_s3:
        return f"/static/data/{key}"

    return (
        f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{key}"
    )


_REVOKED_REGISTRY_KEY = "registry/revoked_books.json"


def get_revoked_registry() -> list[dict]:
    """Retorna a lista de normativos revogados do registro persistente."""
    if settings.mock_s3:
        p = _local_path(_REVOKED_REGISTRY_KEY)
        if not p.exists():
            return []
        return json.loads(p.read_text())

    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3", region_name=settings.aws_region)
    try:
        obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=_REVOKED_REGISTRY_KEY)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return []
        raise


def add_to_revoked_registry(entry: dict) -> None:
    """Adiciona uma entrada ao registro de revogados."""
    registry = get_revoked_registry()
    registry.append(entry)
    _save_revoked_registry(registry)


def remove_from_revoked_registry(revocation_id: str) -> str | None:
    """
    Remove uma entrada pelo id. Retorna o pdf_key da entrada removida, ou None se não encontrada.
    """
    registry = get_revoked_registry()
    entry = next((e for e in registry if e["id"] == revocation_id), None)
    if entry is None:
        return None
    _save_revoked_registry([e for e in registry if e["id"] != revocation_id])
    return entry.get("pdf_key")


def _save_revoked_registry(registry: list[dict]) -> None:
    content = json.dumps(registry, ensure_ascii=False, indent=2).encode()
    if settings.mock_s3:
        _local_path(_REVOKED_REGISTRY_KEY).write_bytes(content)
        return

    import boto3
    s3 = boto3.client("s3", region_name=settings.aws_region)
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=_REVOKED_REGISTRY_KEY,
        Body=content,
        ContentType="application/json",
    )
