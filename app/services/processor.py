"""
Pipeline de processamento síncrono de um normativo.

Em produção (Fase 3), este código roda numa Lambda separada
acionada por evento S3. Em dev local, é chamado em background
via threading.Thread (daemon=True).

Etapas e suas fatias de progresso:
  1. Extraindo texto do PDF        (0–25 %)
  2. Gerando FAQ com IA            (25–50 %)
  3. Publicando rascunho           (50–90 %)
  4. Concluído                     (100 %)
"""
import threading

from app.services import bookstack as bs
from app.services import audit, storage
from app.services.bedrock import fix_extraction_artifacts, generate_faq
from app.services.pdf import pdf_to_markdown

class _JobCancelled(Exception):
    pass


def _raise_if_cancelled(job_id: str) -> None:
    status = storage.load_status(job_id) or {}
    if status.get("status") == "cancelled":
        raise _JobCancelled()


STEPS = [
    (1, "Extraindo texto do PDF"),
    (2, "Corrigindo artefatos de extração"),
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


def run(job_id: str, pdf_key: str, title: str, uploaded_by: str):
    """Executa o pipeline completo. Bloqueia até concluir."""
    bookstack_book_id: int | None = None
    try:
        # ── Etapa 1: Extração de PDF ─────────────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 1)
        pdf_bytes = storage.get_pdf(pdf_key)

        pages_done = [0]
        pages_total = [1]

        def on_progress(current, total):
            pages_done[0] = current
            pages_total[0] = total
            pct = int(current / total * 50)  # 0–50 %
            current_status = storage.load_status(job_id) or {}
            storage.save_status(job_id, current_status | {
                "current_step_label": f"Extraindo texto — página {current}/{total}",
                "progress_pct": pct,
            })

        markdown_text = pdf_to_markdown(pdf_bytes, on_progress=on_progress)

        # ── Conferência de qualidade da extração ─────────────────────────
        extraction_check = _verify_extraction(markdown_text)

        # ── Etapa 2: Correção de artefatos ───────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 2)

        def on_correction_progress(current, total):
            pct = int(20 + current / total * 20)  # 20–40 %
            current_status = storage.load_status(job_id) or {}
            storage.save_status(job_id, current_status | {
                "current_step_label": f"Corrigindo artefatos — parte {current}/{total}",
                "progress_pct": pct,
            })

        markdown_text = fix_extraction_artifacts(markdown_text, on_progress=on_correction_progress)

        # ── Etapa 3: FAQ com IA ──────────────────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 3)
        faq_markdown = generate_faq(markdown_text, title)

        # ── Etapa 4: Bookstack ───────────────────────────────────────────
        _raise_if_cancelled(job_id)
        _set_step(job_id, 4)
        download_url = storage.get_download_url(pdf_key)
        book_url, bookstack_book_id = bs.create_normativo(
            title=title,
            full_text_markdown=markdown_text,
            faq_markdown=faq_markdown,
            download_url=download_url,
            uploaded_by=uploaded_by,
            pdf_key=pdf_key,
        )

        # ── Etapa 5: Concluído ───────────────────────────────────────────
        _raise_if_cancelled(job_id)
        _set_done(job_id, {"book_url": book_url, "extraction_check": extraction_check})

    except _JobCancelled:
        storage.delete_pdf(pdf_key)
        if bookstack_book_id:
            bs.delete_book_from_bookstack(bookstack_book_id)
        audit.log(uploaded_by, "cancelar", title)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("Erro no pipeline de upload job=%s", job_id)
        _set_error(job_id, "Erro interno no processamento. Tente novamente ou contate o administrador.")
        raise


def _verify_extraction(markdown_text: str) -> dict:
    """
    Confere a qualidade do texto extraído do PDF.
    Páginas são separadas por '\\n\\n---\\n\\n' pelo pdf_to_markdown.
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


def run_in_background(job_id: str, pdf_key: str, title: str, uploaded_by: str):
    """Dispara o pipeline numa thread separada (dev local)."""
    t = threading.Thread(
        target=run,
        args=(job_id, pdf_key, title, uploaded_by),
        daemon=True,
    )
    t.start()


