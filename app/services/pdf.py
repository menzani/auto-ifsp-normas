"""
Extração de PDF para Markdown usando PyMuPDF.

A conversão acontece página a página, permitindo atualizar o progresso
a cada página processada.
"""
import re
from collections.abc import Generator
from typing import Any

import fitz  # PyMuPDF


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
    Converte PDF para Markdown.
    on_progress(current, total): callback opcional chamado a cada página.
    """
    pages_md = []
    for page_num, total, text in extract_pages(pdf_bytes):
        pages_md.append(_page_to_markdown(page_num, text))
        if on_progress:
            on_progress(page_num, total)

    return "\n\n---\n\n".join(pages_md)


def _page_to_markdown(page_num: int, raw_text: str) -> str:
    """Pós-processamento básico do texto extraído."""
    text = raw_text.strip()
    if not text:
        return f"*[Página {page_num} sem conteúdo de texto]*"

    lines = text.splitlines()
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append("")
            continue

        # Heurísticas simples para identificar títulos
        if _is_heading(stripped):
            result.append(f"## {stripped}")
        else:
            result.append(stripped)

    # Remove linhas em branco consecutivas
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(result))
    return cleaned


def _is_heading(line: str) -> bool:
    """
    Heurística simples: linha curta, sem ponto final, toda em maiúsculas
    ou que começa com padrão de artigo/capítulo.
    """
    if len(line) > 120:
        return False
    if re.match(r"^(Art\.|Artigo|Cap[íi]tulo|Se[cç][aã]o|§)\s", line, re.IGNORECASE):
        return True
    if line.isupper() and len(line) > 4 and not line.endswith("."):
        return True
    return False
