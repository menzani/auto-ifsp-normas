"""
Pipeline de revogação assíncrona de um normativo.

Etapas e suas fatias de progresso:
  1. Buscando normativo no Bookstack    (0–20 %)
  2. Gerando resumo com IA              (20–50 %)
  3. Criando entrada em Revogadas       (50–70 %)
  4. Removendo normativo original       (70–90 %)
  5. Concluído                          (100 %)
"""
import logging
import re
import threading
from datetime import datetime, timezone
from secrets import token_urlsafe

from app.config import get_settings
from app.services import audit, bookstack as bs, storage
from app.services.bedrock import generate_revocation_summary
from app.services.pipeline import (
    JobCancelled, raise_if_cancelled, set_step, set_done, set_error,
)

_log = logging.getLogger(__name__)

STEPS = [
    (1, "Buscando normativo no Bookstack"),
    (2, "Gerando resumo com IA"),
    (3, "Criando entrada em Revogadas"),
    (4, "Removendo normativo original"),
    (5, "Concluído"),
]
TOTAL_STEPS = len(STEPS)


def _extract_field(text: str, field: str) -> str:
    """Extrai o valor de um campo **Field:** do markdown gerado pela IA."""
    m = re.search(rf"\*\*{re.escape(field)}:\*\*\s*(.+?)(?:\n|$)", text[:10_000], re.IGNORECASE)
    return m.group(1).strip() if m else ""


def run(job_id: str, book_id: int, revoked_by: str) -> None:
    """Executa o pipeline completo de revogação. Bloqueia até concluir."""
    revoked_book_id: int | None = None
    title = ""  # inicializado aqui para garantir binding no except _JobCancelled
    # owner deve persistir em todos os saves — _set_step reescreve o dict completo.
    _private = {"owner": revoked_by}
    try:
        # ── Etapa 1: Buscar normativo ─────────────────────────────────────
        raise_if_cancelled(job_id)
        set_step(job_id, 1, STEPS, _private)
        info = bs.get_book_for_revocation(book_id)
        title = info["title"]
        pdf_key = info["pdf_key"]
        uploaded_by = info["uploaded_by"]
        page_markdown = info["page_markdown"]
        pdf_url = storage.get_download_url(pdf_key) if pdf_key else ""

        # ── Etapa 2: Resumo com IA ────────────────────────────────────────
        raise_if_cancelled(job_id)
        set_step(job_id, 2, STEPS, _private)
        summary_markdown, summary_usage = generate_revocation_summary(page_markdown, title)

        # Compõe o título com dados extraídos pela IA: "Tipo nº X/YYYY, de DD/MM/YYYY"
        tipo = _extract_field(summary_markdown, "Tipo")
        numero = _extract_field(summary_markdown, "Número")
        data = _extract_field(summary_markdown, "Data de publicação")

        if tipo and numero:
            base = f"{tipo} {numero}"
        elif numero:
            base = numero
        elif tipo:
            base = tipo
        else:
            base = title  # fallback se a IA não retornar dados suficientes
        revoked_book_title = f"Revogada - {base}, de {data}" if data else f"Revogada - {base}"

        # ── Etapa 3: Criar entrada em Revogadas ───────────────────────────
        raise_if_cancelled(job_id)
        set_step(job_id, 3, STEPS, _private)
        revoked_book_url, revoked_book_id = bs.create_revoked_book_entry(
            title=revoked_book_title,
            summary_markdown=summary_markdown,
            pdf_url=pdf_url,
            uploaded_by=uploaded_by,
            tipo=tipo,
        )

        # ── Etapa 4: Remover normativo original ───────────────────────────
        raise_if_cancelled(job_id)
        set_step(job_id, 4, STEPS, _private)
        bs.delete_book_from_bookstack(book_id)

        # ── Concluído: persistir no registro ──────────────────────────────
        revocation_id = token_urlsafe(12)
        storage.add_to_revoked_registry({
            "id": revocation_id,
            "title": title,
            "pdf_key": pdf_key,
            "pdf_url": pdf_url,
            "uploaded_by": uploaded_by,
            "revoked_by": revoked_by,
            "revoked_at": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M"),
            "bookstack_url": revoked_book_url,
            "bookstack_book_id": revoked_book_id,
        })
        audit.log(revoked_by, "revogar", title, extra={
            "input_tokens": summary_usage["input_tokens"],
            "output_tokens": summary_usage["output_tokens"],
        })

        set_done(job_id, {
            "title": title,
            "revoked_book_url": revoked_book_url,
            "pdf_url": pdf_url,
            "bedrock_usage": {
                "model": get_settings().bedrock_model_id,
                "total_input_tokens": summary_usage["input_tokens"],
                "total_output_tokens": summary_usage["output_tokens"],
            },
        }, TOTAL_STEPS)

    except JobCancelled:
        if revoked_book_id:
            bs.delete_book_from_bookstack(revoked_book_id)
        audit.log(revoked_by, "cancelar_revogacao", title or f"book_id={book_id}")
    except Exception as exc:
        _log.exception("Erro no pipeline de revogação job=%s", job_id)
        set_error(job_id, "Erro interno na revogação. Tente novamente ou contate o administrador.", TOTAL_STEPS)
        audit.log(revoked_by, "erro_pipeline", f"Falha interna na revogação de '{title or f'book_id={book_id}'}' (job {job_id})", level="warn")
        raise


_semaphore = threading.Semaphore(2)  # máx 2 revogações simultâneas


def run_in_background(job_id: str, book_id: int, revoked_by: str) -> bool:
    """Dispara o pipeline numa thread separada. Retorna False se o limite de jobs simultâneos for atingido."""
    if not _semaphore.acquire(blocking=False):
        return False

    def _target():
        try:
            run(job_id, book_id, revoked_by)
        finally:
            _semaphore.release()

    threading.Thread(target=_target, daemon=True).start()
    return True
