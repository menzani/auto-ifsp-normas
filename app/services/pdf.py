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
_SIGNATURE_LINE_RE = re.compile(
    r"assinado\s+(de\s+forma\s+)?digitalmente|"
    r"assinado\s+eletronicamente|"
    r"icp[.\s\-]?brasil|"
    r"código\s+(verificador|de\s+autenticação|crc)\s*:|"
    r"autenticidade\s+deste\s+documento",
    re.IGNORECASE,
)

# Marcadores de início de bloco de assinatura eletrônica SUAP/Gov.br
# Tudo a partir dessa linha até o fim do documento é removido.
_SIGNATURE_BLOCK_RE = re.compile(
    r"emitido\s+pelo\s+suap\s+em\s+\d|"           # "Este documento foi emitido pelo SUAP em 09/11/2025"
    r"para\s+comprovar\s+sua\s+autenticidade|"     # "Para comprovar sua autenticidade..."
    r"faça\s+a\s+leitura\s+do\s+qrcode|"          # "faça a leitura do QRCode"
    r"acesse\s+https?://suap\.|"                   # "acesse https://suap.ifsp.edu.br/..."
    r"\bcd\d\b.{0,30}\bifsp\b",                   # "REITOR(A) - CD1 - IFSP"
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
    pages_text = []
    for page_num, total, text in extract_pages(pdf_bytes):
        pages_text.append(_page_to_text(page_num, text))
        if on_progress:
            on_progress(page_num, total)

    pages_text = _remove_running_headers(pages_text)
    full_text = "\n\n---\n\n".join(pages_text)
    return _remove_signature_artifacts(full_text)


def _remove_running_headers(pages: list[str]) -> list[str]:
    """
    Remove cabeçalhos de página recorrentes (ex: timbre institucional) que aparecem
    como texto simples no topo de muitas páginas.

    Estratégia: linhas que aparecem nas primeiras 5 linhas de ≥ 40% das páginas
    (mínimo 3 páginas) são consideradas cabeçalhos de rodapé/cabeçalho corrente e
    removidas de todas as páginas.
    A comparação é insensível a maiúsculas e ignora variações de espaço para tolerar
    pequenas inconsistências de OCR entre páginas.
    """
    if len(pages) < 3:
        return pages

    _HEAD_LINES = 5
    threshold = max(3, int(len(pages) * 0.4))

    import unicodedata

    def _norm(s: str) -> str:
        """Normaliza para comparação: sem acentos, maiúsculas, espaços colapsados, sem pontuação."""
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # remove diacríticos
        s = re.sub(r"[^\w\s]", " ", s)  # remove pontuação
        return re.sub(r"\s+", " ", s.strip()).upper()

    # Conta ocorrências de cada linha normalizada nas primeiras linhas de cada página
    from collections import Counter
    counts: Counter = Counter()
    for page in pages:
        seen_in_page: set[str] = set()
        for line in page.splitlines()[:_HEAD_LINES]:
            norm = _norm(line)
            if len(norm) > 5 and norm not in seen_in_page:
                counts[norm] += 1
                seen_in_page.add(norm)

    running = {norm for norm, cnt in counts.items() if cnt >= threshold}
    if not running:
        return pages

    result = []
    for page in pages:
        lines = page.splitlines()
        # Remove apenas nas primeiras linhas de cada página para não apagar
        # conteúdo legítimo que coincida com o cabeçalho mais adiante no texto.
        filtered_head = []
        for line in lines[:_HEAD_LINES]:
            if _norm(line) not in running:
                filtered_head.append(line)
        result.append("\n".join(filtered_head + lines[_HEAD_LINES:]))
    return result


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
    Remove blocos e linhas de assinatura eletrônica de PDFs governamentais brasileiros.

    Dois modos:
    - Bloco: ao encontrar marcador de início (SUAP, QRCode, CDn-IFSP), descarta
      tudo a partir dali até o próximo separador de página '---' ou fim do texto.
    - Linha: remove linhas isoladas com padrões de assinatura digital (ICP-Brasil, etc.).
    """
    pages = text.split("\n\n---\n\n")
    cleaned_pages = []
    for page in pages:
        lines = page.splitlines()
        result = []
        skip = False
        for line in lines:
            if not skip and _SIGNATURE_BLOCK_RE.search(line):
                skip = True  # descarta esta linha e todas as seguintes na página
            if skip:
                continue
            if not _SIGNATURE_LINE_RE.search(line):
                result.append(line)
        cleaned_pages.append("\n".join(result))
    return "\n\n---\n\n".join(cleaned_pages)
