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
from datetime import datetime
from secrets import token_urlsafe

from app.services import bookstack as bs

_log = logging.getLogger(__name__)
from app.services import audit, storage
from app.services.bedrock import generate_revocation_summary

class _JobCancelled(Exception):
    pass


def _raise_if_cancelled(job_id: str) -> None:
    status = storage.load_status(job_id) or {}
    if status.get("status") == "cancelled":
        raise _JobCancelled()


STEPS = [
    (1, "Buscando normativo no Bookstack"),
    (2, "Gerando resumo com IA"),
    (3, "Criando entrada em Revogadas"),
    (4, "Removendo normativo original"),
    (5, "Concluído"),
]
TOTAL_STEPS = len(STEPS)


def _set_step(job_id: str, step: int, extra: dict | None = None) -> None:
    label = STEPS[step - 1][1]
    pct = int((step - 1) / TOTAL_STEPS * 100)
    data = {
        "id": job_id,
        "status": "processing",
        "current_step": step,
        "total_steps": TOTAL_STEPS,
        "current_step_label": label,
        "progress_pct": pct,
    }
    if extra:
        data.update(extra)
    storage.save_status(job_id, data)


def _set_done(job_id: str, result: dict) -> None:
    storage.save_status(job_id, {
        "id": job_id,
        "status": "done",
        "current_step": TOTAL_STEPS,
        "total_steps": TOTAL_STEPS,
        "current_step_label": "Concluído",
        "progress_pct": 100,
        "result": result,
    })


def _set_error(job_id: str, message: str) -> None:
    storage.save_status(job_id, {
        "id": job_id,
        "status": "error",
        "error": message,
        "current_step": 0,
        "total_steps": TOTAL_STEPS,
        "current_step_label": "Erro",
        "progress_pct": 0,
    })


def _extract_field(text: str, field: str) -> str:
    """Extrai o valor de um campo **Field:** do markdown gerado pela IA."""
    m = re.search(rf"\*\*{re.escape(field)}:\*\*\s*(.+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def run(job_id: str, book_id: int, revoked_by: str) -> None:
    """Executa o pipeline completo de revogação. Bloqueia até concluir."""
    revoked_book_id: int | None = None
    # owner deve persistir em todos os saves — _set_step reescreve o dict completo.
    _private = {"owner": revoked_by}
    try:
        # ── Etapa 1: Buscar normativo ─────────────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 1, _private)
        info = bs.get_book_for_revocation(book_id)
        title = info["title"]
        pdf_key = info["pdf_key"]
        uploaded_by = info["uploaded_by"]
        page_markdown = info["page_markdown"]
        pdf_url = storage.get_download_url(pdf_key) if pdf_key else ""

        # ── Etapa 2: Resumo com IA ────────────────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 2, _private)
        summary_markdown = generate_revocation_summary(page_markdown, title)

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
        _raise_if_cancelled(job_id)
        _set_step(job_id, 3, _private)
        revoked_book_url, revoked_book_id = bs.create_revoked_book_entry(
            title=revoked_book_title,
            summary_markdown=summary_markdown,
            pdf_url=pdf_url,
            uploaded_by=uploaded_by,
            tipo=tipo,
        )

        # ── Etapa 4: Remover normativo original ───────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 4, _private)
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
            "revoked_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "bookstack_url": revoked_book_url,
            "bookstack_book_id": revoked_book_id,
        })
        audit.log(revoked_by, "revogar", title)

        _set_done(job_id, {
            "title": title,
            "revoked_book_url": revoked_book_url,
            "pdf_url": pdf_url,
        })

    except _JobCancelled:
        if revoked_book_id:
            bs.delete_book_from_bookstack(revoked_book_id)
        audit.log(revoked_by, "cancelar_revogacao", title or f"book_id={book_id}")
    except Exception as exc:
        _log.exception("Erro no pipeline de revogação job=%s", job_id)
        _set_error(job_id, "Erro interno na revogação. Tente novamente ou contate o administrador.")
        raise


def run_in_background(job_id: str, book_id: int, revoked_by: str) -> None:
    """Dispara o pipeline numa thread separada (dev local)."""
    t = threading.Thread(
        target=run,
        args=(job_id, book_id, revoked_by),
        daemon=True,
    )
    t.start()
