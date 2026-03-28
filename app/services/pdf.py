"""
Extração de PDF para Markdown usando PyMuPDF.

A conversão acontece página a página, permitindo atualizar o progresso
a cada página processada.
"""
import re
from collections import Counter
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
    Converte PDF para Markdown.
    on_progress(current, total): callback opcional chamado a cada página.
    """
    pages_md = []
    for page_num, total, text in extract_pages(pdf_bytes):
        pages_md.append(_page_to_markdown(page_num, text))
        if on_progress:
            on_progress(page_num, total)

    full_text = "\n\n---\n\n".join(pages_md)
    full_text = _remove_repeated_page_headers(full_text)
    full_text = _deduplicate_headings(full_text)
    full_text = _remove_signature_artifacts(full_text)
    return full_text


def _remove_repeated_page_headers(text: str) -> str:
    """
    Remove blocos de cabeçalho que se repetem no início de múltiplas páginas
    (ex: timbre institucional em cada folha do PDF).
    Mantém apenas a primeira ocorrência no documento.
    """
    pages = text.split("\n\n---\n\n")
    if len(pages) <= 1:
        return text

    def leading_headings(page_text: str) -> list[str]:
        """Headings iniciais da página, antes do primeiro conteúdo real."""
        result = []
        for line in page_text.splitlines():
            s = line.strip()
            if not s:
                continue
            if line.startswith("#"):
                result.append(line.lstrip("#").strip())
            else:
                break
        return result

    all_leading = [h for p in pages for h in leading_headings(p)]
    counts = Counter(all_leading)
    repeated = {h for h, c in counts.items() if c >= 2}

    if not repeated:
        return text

    seen: set[str] = set()
    result_pages = []
    for page in pages:
        lines = page.splitlines()
        filtered = []
        for line in lines:
            if line.startswith("#"):
                norm = line.lstrip("#").strip()
                if norm in repeated:
                    if norm not in seen:
                        seen.add(norm)
                        filtered.append(line)  # primeira ocorrência: mantém
                    continue  # ocorrências posteriores: descarta
            filtered.append(line)
        result_pages.append("\n".join(filtered))

    return "\n\n---\n\n".join(result_pages)


def _deduplicate_headings(text: str) -> str:
    """
    Remove headings consecutivos idênticos remanescentes após
    _remove_repeated_page_headers.
    """
    lines = text.splitlines()
    result = []
    prev_heading: str | None = None
    for line in lines:
        is_heading = line.startswith("#")
        if is_heading:
            normalized = line.lstrip("#").strip()
            if normalized == prev_heading:
                continue
            prev_heading = normalized
        else:
            if line.strip():
                prev_heading = None
        result.append(line)
    return "\n".join(result)


def _remove_signature_artifacts(text: str) -> str:
    """
    Remove linhas de assinatura digital típicas de PDFs governamentais brasileiros
    (ex: "Assinado digitalmente por ...", referências ICP-Brasil, códigos de autenticação).
    """
    lines = text.splitlines()
    return "\n".join(line for line in lines if not _SIGNATURE_RE.search(line))


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
    Heurística: linha curta, sem ponto final, toda em maiúsculas
    ou que começa com padrão de artigo/capítulo/seção.
    Linhas muito curtas (≤ 2 palavras) e maiúsculas são excluídas para evitar
    falsos positivos em atribuições como "O REITOR", "O DIRETOR".
    """
    if len(line) > 120:
        return False
    if re.match(r"^(Art\.|Artigo|Cap[íi]tulo|Se[cç][aã]o|§)\s", line, re.IGNORECASE):
        return True
    if line.isupper() and not line.endswith("."):
        words = line.split()
        # Exige ao menos 3 palavras para evitar atribuições curtas (ex: "O REITOR")
        if len(words) >= 3:
            return True
        # Aceita palavras-chave estruturais reconhecidas mesmo com 1-2 palavras
        if re.match(r"^(RESOLVE|CONSIDERANDO|EMENTA|ANEXO|SUMÁRIO|PREFÁCIO)\b", line):
            return True
    return False
