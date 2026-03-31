"""
Pipeline de processamento síncrono de um normativo.

Roda em background via threading.Thread (daemon=True).

Etapas:
  1. Extração via visão (Claude Vision — lotes de páginas)
  2. Verificando extração
  3. Gerando FAQ com IA
  4. Publicando rascunho no Bookstack
  5. Concluído
"""
import logging
import threading
import time

from app.config import get_settings
from app.services import audit, bookstack as bs, storage
from app.services.bedrock import generate_faq
from app.services.pdf import pdf_to_markdown_multimodal, detect_structural_anomalies

_log = logging.getLogger(__name__)

class _JobCancelled(Exception):
    pass


def _raise_if_cancelled(job_id: str) -> None:
    status = storage.load_status(job_id) or {}
    if status.get("status") == "cancelled":
        raise _JobCancelled()


STEPS = [
    (1, "Extraindo texto do PDF"),
    (2, "Verificando extração"),
    (3, "Gerando FAQ com IA"),
    (4, "Publicando rascunho no Bookstack"),
    (5, "Concluído"),
]
TOTAL_STEPS = len(STEPS)


def _set_step(job_id: str, step: int, extra: dict | None = None):
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


def _set_done(job_id: str, result: dict):
    storage.save_status(job_id, {
        "id": job_id,
        "status": "done",
        "current_step": TOTAL_STEPS,
        "total_steps": TOTAL_STEPS,
        "current_step_label": "Concluído",
        "progress_pct": 100,
        "result": result,
    })


def _set_error(job_id: str, message: str):
    storage.save_status(job_id, {
        "id": job_id,
        "status": "error",
        "error": message,
        "current_step": 0,
        "total_steps": TOTAL_STEPS,
        "current_step_label": "Erro",
        "progress_pct": 0,
    })


def run(job_id: str, pdf_key: str, title: str, uploaded_by: str, checksum: str = ""):
    """Executa o pipeline completo. Bloqueia até concluir."""
    bookstack_book_id: int | None = None
    _private = {"owner": uploaded_by, "pdf_key": pdf_key}
    start_time = time.monotonic()
    try:
        # ── Etapa 1: Extração ────────────────────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 1, _private)
        pdf_bytes = storage.get_pdf(pdf_key)

        base_status = storage.load_status(job_id) or {"id": job_id}

        def on_multimodal_progress(current, total):
            pct = int(current / total * 40)  # 0–40 %
            storage.save_status(job_id, base_status | {
                "current_step_label": f"Extraindo via visão — lote {current}/{total}",
                "progress_pct": pct,
            })

        markdown_text, structure_usage = pdf_to_markdown_multimodal(
            pdf_bytes, on_progress=on_multimodal_progress
        )
        extraction_check = _verify_extraction(markdown_text)
        anomalies = detect_structural_anomalies(markdown_text)

        # ── Etapa 2: Verificação da extração ────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 2, _private)

        # ── Etapa 3: FAQ com IA ──────────────────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 3, _private)
        faq_markdown, faq_usage = generate_faq(markdown_text, title)

        # ── Etapa 4: Bookstack ───────────────────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 4, _private)
        download_url = storage.get_download_url(pdf_key)
        book_url, bookstack_book_id = bs.create_normativo(
            title=title,
            full_text_markdown=markdown_text,
            faq_markdown=faq_markdown,
            download_url=download_url,
            uploaded_by=uploaded_by,
            pdf_key=pdf_key,
            anomalies=anomalies,
        )

        # ── Etapa 5: Concluído ───────────────────────────────────────────
        _raise_if_cancelled(job_id)
        elapsed = round(time.monotonic() - start_time)
        bedrock_usage = {
            "model": get_settings().bedrock_model_id,
            "structure_input_tokens": structure_usage["input_tokens"],
            "structure_output_tokens": structure_usage["output_tokens"],
            "faq_input_tokens": faq_usage["input_tokens"],
            "faq_output_tokens": faq_usage["output_tokens"],
            "total_input_tokens": structure_usage["input_tokens"] + faq_usage["input_tokens"],
            "total_output_tokens": structure_usage["output_tokens"] + faq_usage["output_tokens"],
        }
        _set_done(job_id, {
            "book_url": book_url,
            "extraction_check": extraction_check,
            "processing_time_seconds": elapsed,
            "bedrock_usage": bedrock_usage,
        })
        extra: dict = {
            "tempo_s": elapsed,
            "input_tokens": bedrock_usage["total_input_tokens"],
            "output_tokens": bedrock_usage["total_output_tokens"],
        }
        if checksum:
            extra["checksum"] = checksum[:12]
        audit.log(uploaded_by, "processar", title, extra=extra)

    except _JobCancelled:
        storage.delete_pdf(pdf_key)
        if checksum:
            storage.unregister_pdf_checksum_by_job_id(job_id)
        if bookstack_book_id:
            bs.delete_book_from_bookstack(bookstack_book_id)
        audit.log(uploaded_by, "cancelar", title)
    except ValueError as exc:
        # Erro de validação do documento (ex: excesso de páginas) — não é falha do sistema.
        storage.delete_pdf(pdf_key)
        if checksum:
            storage.unregister_pdf_checksum_by_job_id(job_id)
        _set_error(job_id, str(exc))
    except Exception:
        _log.exception("Erro no pipeline de upload job=%s", job_id)
        if checksum:
            storage.unregister_pdf_checksum_by_job_id(job_id)
        _set_error(job_id, "Erro interno no processamento. Tente novamente ou contate o administrador.")
        audit.log(uploaded_by, "erro_pipeline", f"Falha interna no processamento de '{title}' (job {job_id})", level="warn")
        raise


def _verify_extraction(markdown_text: str) -> dict:
    """
    Confere a qualidade do texto extraído do PDF.
    Retorna estatísticas e um aviso (warning) se detectar problemas.
    """
    pages = markdown_text.split("\n\n---\n\n")
    total_pages = len(pages)
    chars_per_page = [len(p.strip()) for p in pages]
    total_chars = sum(chars_per_page)
    avg_chars = total_chars // total_pages if total_pages else 0
    blank_pages = sum(1 for c in chars_per_page if c < 50)

    warning = None
    if blank_pages == total_pages:
        warning = (
            f"Nenhum texto foi extraído ({total_pages} página(s) sem conteúdo). "
            "O documento pode ser uma imagem digitalizada sem OCR."
        )
    elif blank_pages > total_pages * 0.5:
        warning = (
            f"{blank_pages} de {total_pages} página(s) sem texto detectado. "
            "O documento pode conter imagens ou layout complexo — verifique o rascunho."
        )
    elif avg_chars < 100 and total_pages > 1:
        warning = (
            f"Baixa densidade de texto (média de {avg_chars} caracteres/página). "
            "Verifique se o conteúdo foi extraído corretamente."
        )

    return {
        "pages": total_pages,
        "total_chars": total_chars,
        "avg_chars_per_page": avg_chars,
        "blank_pages": blank_pages,
        "warning": warning,
    }


_semaphore = threading.Semaphore(3)  # máx 3 processamentos simultâneos


def run_in_background(job_id: str, pdf_key: str, title: str, uploaded_by: str, checksum: str = "") -> bool:
    """Dispara o pipeline numa thread separada. Retorna False se o limite de jobs simultâneos for atingido."""
    if not _semaphore.acquire(blocking=False):
        return False

    def _target():
        try:
            run(job_id, pdf_key, title, uploaded_by, checksum)
        finally:
            _semaphore.release()

    threading.Thread(target=_target, daemon=True).start()
    return True


