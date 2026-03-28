"""
Pipeline de processamento síncrono de um normativo.

Em produção (Fase 3), este código roda numa Lambda separada
acionada por evento S3. Em dev local, é chamado em background
via threading.Thread (daemon=True).

Etapas e suas fatias de progresso:
  1. Extraindo texto do PDF        (0–25 %)
  2. Gerando FAQ com IA            (25–50 %)
  3. Analisando estrutura          (50–75 %)
  4. Publicando rascunho           (75–90 %)
  5. Concluído                     (100 %)
"""
import re
import threading

from app.services import bookstack as bs
from app.services import storage
from app.services.bedrock import generate_faq, generate_section_titles
from app.services.pdf import pdf_to_markdown

STEPS = [
    (1, "Extraindo texto do PDF"),
    (2, "Gerando FAQ com IA"),
    (3, "Analisando estrutura do documento"),
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
    try:
        # ── Etapa 1: Extração de PDF ─────────────────────────────────────
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

        # ── Etapa 2: FAQ com IA ──────────────────────────────────────────
        _set_step(job_id, 2)
        faq_markdown = generate_faq(markdown_text, title)

        # ── Etapa 3: Estrutura do documento ──────────────────────────────
        _set_step(job_id, 3)
        titles = generate_section_titles(markdown_text, title)
        sections = _split_into_sections(markdown_text, titles)

        # ── Etapa 4: Bookstack ───────────────────────────────────────────
        _set_step(job_id, 4)
        download_url = storage.get_download_url(pdf_key)
        book_url = bs.create_normativo(
            title=title,
            sections=sections,
            faq_markdown=faq_markdown,
            download_url=download_url,
            uploaded_by=uploaded_by,
            pdf_key=pdf_key,
        )

        # ── Concluído ────────────────────────────────────────────────────
        _set_done(job_id, {"book_url": book_url})

    except Exception as exc:
        _set_error(job_id, str(exc))
        raise


def _split_into_sections(markdown_text: str, titles: list[str]) -> list[dict]:
    """
    Divide o texto extraído do PDF nas seções identificadas pela IA.
    Cada título é buscado no texto; o conteúdo vai até o próximo título.
    Se nenhum título for encontrado, retorna a página com o texto completo.
    """
    if not titles:
        return [{"title": "Texto Completo", "content": markdown_text}]

    # Mapeia posição de cada título no texto
    positions: list[tuple[int, str]] = []
    for t in titles:
        pos = markdown_text.find(t)
        if pos != -1:
            positions.append((pos, t))
    positions.sort()

    if not positions:
        return [{"title": "Texto Completo", "content": markdown_text}]

    sections = []
    for i, (start_pos, section_title) in enumerate(positions):
        end_pos = positions[i + 1][0] if i + 1 < len(positions) else len(markdown_text)
        content = markdown_text[start_pos:end_pos].strip()
        sections.append({"title": section_title, "content": content})
    return sections


def run_in_background(job_id: str, pdf_key: str, title: str, uploaded_by: str):
    """Dispara o pipeline numa thread separada (dev local)."""
    t = threading.Thread(
        target=run,
        args=(job_id, pdf_key, title, uploaded_by),
        daemon=True,
    )
    t.start()


