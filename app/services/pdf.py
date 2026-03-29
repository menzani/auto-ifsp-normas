"""
Extração de PDF para Markdown usando PyMuPDF. (rev. 2026-03-29)

Detecção de headings em duas camadas determinísticas, antes de qualquer IA:
  1. Visual (get_text("dict")): linhas em negrito ou fonte maior que o corpo
  2. Keyword (regex): TÍTULO/CAPÍTULO/SEÇÃO com tolerância a typos e variações
Documentos sem nenhum heading detectado são marcados para o modo de sugestão da IA.
"""
import re
from collections.abc import Generator

import fitz  # PyMuPDF

from app.config import get_settings

settings = get_settings()

# Ligaduras Unicode explícitas (U+FB00–FB06) — glifos tipográficos que PDFs mal
# codificados expõem como caracteres literais em vez de sequências de letras.
_LIGATURE_MAP = {
    '\uFB00': 'ff',
    '\uFB01': 'fi',
    '\uFB02': 'fl',
    '\uFB03': 'ffi',
    '\uFB04': 'ffl',
    '\uFB05': 'st',
    '\uFB06': 'st',
}

# Padrões de linhas de assinatura digital em documentos governamentais brasileiros
_SIGNATURE_LINE_RE = re.compile(
    r"assinado\s+(de\s+forma\s+)?digitalmente|"
    r"assinado\s+eletronicamente|"
    r"icp[.\s\-]?brasil|"
    r"código\s+(verificador|de\s+autenticação|crc)\s*:|"
    r"autenticidade\s+deste\s+documento",
    re.IGNORECASE,
)

# Rodapés de paginação variáveis (ex: "Página 48 de 65") — removidos antes da etapa de IA
_PAGE_FOOTER_RE = re.compile(r"^\s*página\s+\d+\s+de\s+\d+\s*$", re.IGNORECASE)

# Headings estruturais detectáveis deterministicamente por palavras-chave.
# Tolerância a typos comuns: sem acento (CAPITULO), sem espaço antes do numeral
# (CAPÍTULOI), abreviação (CAP. IV), prefixo numérico (4. CAPÍTULO IV),
# separador com dois-pontos (CAPÍTULO I: nome).
_TITLE_HEADING_RE = re.compile(
    r'^\s*(?:\d+\.\s*)?'
    r'(T[IÍ]TULO\s+[IVXLCDM]+(?:\s*[-—–:]\s*.+)?)'
    r'\s*$',
    re.IGNORECASE,
)
_CHAPTER_HEADING_RE = re.compile(
    r'^\s*(?:\d+\.\s*)?'
    r'(CAP[IÍ]TULO\s*[IVXLCDM]+(?:\s*[-—–:]\s*.+)?'
    r'|CAP\.\s+[IVXLCDM]+(?:\s*[-—–:]\s*.+)?)'
    r'\s*$',
    re.IGNORECASE,
)
_SECTION_HEADING_RE = re.compile(
    r'^\s*(?:\d+\.\s*)?'
    r'(SE[CÇ][AÃ]O\s+[IVXLCDM]+(?:\s*[-—–:]\s*.+)?)'
    r'\s*$',
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


def _extract_page_text(page) -> str:
    """
    Extrai texto de uma página com detecção de headings visuais via get_text("dict").

    Linhas com ≥80 % dos caracteres em negrito, ou com fonte ≥15 % maior que o tamanho
    modal do corpo da página, e com no máximo 12 palavras, recebem prefixo ## como
    heading visual — independente de seguirem palavras-chave formais como CAPÍTULO.

    O terço superior da página (timbre institucional) é excluído da detecção para evitar
    falsos positivos. Fallback para get_text("text") se o modo dict falhar.
    """
    try:
        data = page.get_text("dict")
    except Exception:
        return page.get_text("text")

    page_height = data.get("height", 842)
    top_zone = page_height * 0.15
    blocks = [b for b in data.get("blocks", []) if b.get("type") == 0]
    if not blocks:
        return ""

    # Tamanho modal de fonte do corpo — ponderado por volume de caracteres para
    # que o texto corrido domine sobre títulos isolados.
    from collections import Counter
    size_counter: Counter = Counter()
    for block in blocks:
        if block.get("bbox", (0,))[1] < top_zone:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "").strip()
                sz = round(span.get("size", 0))
                if txt and sz > 4:
                    size_counter[sz] += len(txt)

    body_size = size_counter.most_common(1)[0][0] if size_counter else 0
    size_threshold = body_size * 1.15 if body_size else float("inf")

    lines_out = []
    for block in blocks:
        in_top_zone = block.get("bbox", (0,))[1] < top_zone
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            line_text = "".join(s.get("text", "") for s in spans).strip()
            if not line_text:
                continue
            if in_top_zone or line_text.lstrip().startswith("#"):
                lines_out.append(line_text)
                continue

            char_total = sum(len(s.get("text", "").strip()) for s in spans)
            if not char_total:
                lines_out.append(line_text)
                continue

            bold_chars = sum(
                len(s.get("text", "").strip())
                for s in spans
                if s.get("flags", 0) & 16 and s.get("text", "").strip()
            )
            avg_size = sum(
                s.get("size", 0) * len(s.get("text", "").strip())
                for s in spans if s.get("text", "").strip()
            ) / char_total

            is_large = avg_size >= size_threshold
            is_mostly_bold = bold_chars / char_total >= 0.8
            is_short = len(line_text.split()) <= 12

            if is_short and (is_large or is_mostly_bold):
                lines_out.append(f"## {line_text}")
            else:
                lines_out.append(line_text)
        lines_out.append("")  # linha em branco entre blocos

    return "\n".join(lines_out)


def has_structure(text: str) -> bool:
    """
    Retorna True se o texto já contém headings Markdown detectados deterministicamente.
    Falso indica documento plano — aciona modo de sugestão de estrutura pela IA.
    """
    return bool(re.search(r'^#{1,3}\s+\S', text, re.MULTILINE))


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
    ocr_count = 0
    for i, page in enumerate(doc, start=1):
        text = _extract_page_text(page)
        if len(text.strip()) < 50 and not settings.mock_s3:
            if ocr_count < settings.max_ocr_pages_per_pdf:
                # Página sem texto detectado — tenta OCR via Textract
                from app.services.textract import ocr_page_image
                image_bytes = page.get_pixmap(dpi=200).tobytes("png")
                ocr_text = ocr_page_image(image_bytes)
                if ocr_text.strip():
                    text = ocr_text
                ocr_count += 1
            else:
                text = f"*[Página {i} sem texto — limite de OCR atingido ({settings.max_ocr_pages_per_pdf} páginas)]*"
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
    full_text = _remove_signature_artifacts(full_text)
    full_text = _fix_ligature_artifacts(full_text)
    return _detect_headings(full_text)


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
    _TAIL_LINES = 3
    threshold = max(3, int(len(pages) * 0.4))

    import unicodedata

    def _norm(s: str) -> str:
        """Normaliza para comparação: sem acentos, maiúsculas, espaços colapsados, sem pontuação."""
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # remove diacríticos
        s = re.sub(r"[^\w\s]", " ", s)  # remove pontuação
        return re.sub(r"\s+", " ", s.strip()).upper()

    # Conta ocorrências de cada linha normalizada nas primeiras e últimas linhas de cada página
    from collections import Counter
    counts: Counter = Counter()
    for page in pages:
        seen_in_page: set[str] = set()
        all_lines = page.splitlines()
        candidate_lines = all_lines[:_HEAD_LINES] + all_lines[-_TAIL_LINES:]
        for line in candidate_lines:
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
        # Remove apenas nas primeiras e últimas linhas de cada página para não apagar
        # conteúdo legítimo que coincida com o cabeçalho/rodapé mais adiante no texto.
        filtered_head = [line for line in lines[:_HEAD_LINES] if _norm(line) not in running]
        middle = lines[_HEAD_LINES:max(_HEAD_LINES, len(lines) - _TAIL_LINES)]
        filtered_tail = [line for line in lines[-_TAIL_LINES:] if _norm(line) not in running]
        # Evita duplicação quando a página é curta demais para ter head e tail distintos
        if len(lines) <= _HEAD_LINES + _TAIL_LINES:
            result.append("\n".join(filtered_head))
        else:
            result.append("\n".join(filtered_head + middle + filtered_tail))
    return result


def _page_to_text(page_num: int, raw_text: str) -> str:
    """Limpeza básica do texto bruto de uma página."""
    text = raw_text.strip()
    if not text:
        return f"*[Página {page_num} sem conteúdo de texto]*"

    lines = [line.strip() for line in text.splitlines() if not _PAGE_FOOTER_RE.match(line.strip())]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return cleaned


def _fix_ligature_artifacts(text: str) -> str:
    """
    Corrige artefatos de ligadura tipográfica comuns em PDFs governamentais brasileiros.

    Dois tipos de artefato:
    1. Ligaduras Unicode explícitas (U+FB00–FB06): glifos como ﬁ, ﬂ, ﬀ que PDFs mal
       codificados expõem como caracteres literais — sempre substituídos.
    2. Aspas tipográficas usadas como substitutos de ligaduras: o encoder do PDF mapeia
       combinações como 'ti' ou 'fi' para aspas curvas (U+201C, U+2019). Detectadas
       apenas quando aparecem entre letras, para não afetar aspas legítimas.
    """
    for char, replacement in _LIGATURE_MAP.items():
        text = text.replace(char, replacement)
    # U+201C (") entre letras → 'ti'  ex: Ins"tui → Institui
    text = re.sub(r'(?<=\w)\u201c(?=\w)', 'ti', text)
    # U+2019 (') entre letras → 'fi'  ex: o'cial → oficial
    text = re.sub(r'(?<=\w)\u2019(?=\w)', 'fi', text)
    return text


def _roman_to_int(s: str) -> int:
    vals = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        val = vals.get(ch, 0)
        total += val if val >= prev else -val
        prev = val
    return total


_INT_TO_ROMAN = [
    (1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
    (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
    (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I'),
]


def _int_to_roman(n: int) -> str:
    result = ''
    for value, numeral in _INT_TO_ROMAN:
        while n >= value:
            result += numeral
            n -= value
    return result


def detect_structural_anomalies(text: str) -> list[str]:
    """
    Detecta anomalias na numeração de capítulos do documento (gaps, duplicatas, inversões).
    Deve ser chamado após pdf_to_markdown (que já marcou os headings com ##).
    Retorna lista de descrições legíveis (vazia se nenhuma anomalia detectada).
    """
    chapter_re = re.compile(r'^##\s+CAP[IÍ]TULO\s+([IVXLCDM]+)', re.IGNORECASE | re.MULTILINE)
    chapters = chapter_re.findall(text)
    if len(chapters) < 2:
        return []

    nums = [_roman_to_int(r) for r in chapters]
    anomalies = []
    for i in range(1, len(nums)):
        gap = nums[i] - nums[i - 1]
        if gap > 1:
            missing = [_int_to_roman(nums[i - 1] + j) for j in range(1, gap)]
            s = 's' if len(missing) > 1 else ''
            missing_str = ', '.join(f'CAPÍTULO {r}' for r in missing)
            anomalies.append(
                f"Lacuna na numeração: após CAPÍTULO {chapters[i-1].upper()} vem "
                f"CAPÍTULO {chapters[i].upper()} — {missing_str} não exist{'em' if len(missing) > 1 else 'e'} no documento original."
            )
        elif gap == 0:
            anomalies.append(
                f"Numeração duplicada: CAPÍTULO {chapters[i].upper()} aparece mais de uma vez no documento original."
            )
        elif gap < 0:
            anomalies.append(
                f"Numeração fora de ordem: CAPÍTULO {chapters[i-1].upper()} é seguido por "
                f"CAPÍTULO {chapters[i].upper()} no documento original."
            )
    return anomalies


def _detect_headings(text: str) -> str:
    """
    Detecta e marca headings estruturais (Títulos, Capítulos, Seções) com prefixos Markdown
    antes do processamento por IA.

    Linhas que correspondem a padrões rígidos de estrutura normativa (ex: "CAPÍTULO IV — ...")
    recebem o prefixo # / ## / ### adequado. Isso garante que a numeração de capítulos
    nunca dependa da inferência do modelo de IA — evita renumeração ou omissão de capítulos.

    Só processa linhas que ainda não têm prefixo # para não duplicar headings já presentes.
    """
    lines = text.splitlines()
    result = []
    for line in lines:
        if line.lstrip().startswith('#'):
            result.append(line)
        elif _TITLE_HEADING_RE.match(line):
            result.append('# ' + line.strip())
        elif _CHAPTER_HEADING_RE.match(line):
            result.append('## ' + line.strip())
        elif _SECTION_HEADING_RE.match(line):
            result.append('### ' + line.strip())
        else:
            result.append(line)
    return '\n'.join(result)


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
