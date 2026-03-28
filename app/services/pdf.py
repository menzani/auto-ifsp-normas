"""
Extração de PDF para Markdown usando PyMuPDF.

A conversão acontece página a página, permitindo atualizar o progresso
a cada página processada. Nenhuma heurística de heading é aplicada aqui —
a estruturação semântica fica a cargo da etapa de IA no pipeline.
"""
import re
from collections.abc import Generator

import fitz  # PyMuPDF

# Padrões de linhas de assinatura digital em documentos governamentais brasileiros
_SIGNATURE_RE = re.compile(
    r"assinado\s+(de\s+forma\s+)?digitalmente|"
    r"assinado\s+eletronicamente|"
    r"icp[.\s\-]?brasil|"
    r"código\s+(verificador|de\s+autenticação|crc)\s*:|"
    r"autenticidade\s+deste\s+documento",
    re.IGNORECASE,
)


def extract_pages(pdf_bytes: bytes) -> Generator[tuple[int, int, str], None, None]:
    """
    Itera sobre as páginas do PDF.
    Yield: (page_number, total_pages, page_text)
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Não foi possível abrir o PDF: {exc}") from exc
    total = len(doc)
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text")
        yield i, total, text
    doc.close()


def pdf_to_markdown(pdf_bytes: bytes, on_progress=None) -> str:
    """
    Converte PDF para texto limpo (sem headings heurísticos).
    A estruturação Markdown é feita posteriormente pela etapa de IA.
    on_progress(current, total): callback opcional chamado a cada página.
    """
    pages_md = []
    for page_num, total, text in extract_pages(pdf_bytes):
        pages_md.append(_page_to_text(page_num, text))
        if on_progress:
            on_progress(page_num, total)

    full_text = "\n\n---\n\n".join(pages_md)
    return _remove_signature_artifacts(full_text)


def _page_to_text(page_num: int, raw_text: str) -> str:
    """Limpeza básica do texto bruto de uma página."""
    text = raw_text.strip()
    if not text:
        return f"*[Página {page_num} sem conteúdo de texto]*"

    lines = [line.strip() for line in text.splitlines()]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return cleaned


def _remove_signature_artifacts(text: str) -> str:
    """
    Remove linhas de assinatura digital típicas de PDFs governamentais brasileiros
    (ex: "Assinado digitalmente por ...", referências ICP-Brasil, códigos de autenticação).
    """
    lines = text.splitlines()
    return "\n".join(line for line in lines if not _SIGNATURE_RE.search(line))
