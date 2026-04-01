"""
Extração de PDF para Markdown usando PyMuPDF e Claude Vision via Amazon Bedrock.
"""
import re

import fitz  # PyMuPDF

from app.config import get_settings

settings = get_settings()

# Padrões de linhas de assinatura digital em documentos governamentais brasileiros
_SIGNATURE_LINE_RE = re.compile(
    r"assinado\s+(de\s+forma\s+)?digitalmente|"
    r"assinado\s+eletronicamente|"
    r"icp[.\s\-]?brasil|"
    r"código\s+(verificador|de\s+autenticação|crc)\s*:|"
    r"autenticidade\s+deste\s+documento",
    re.IGNORECASE,
)

# Identificadores de artigo e parágrafo que devem ser negritados no início de linha.
# Captura apenas o marcador (Art. 1º, § 2º, Parágrafo único…), não o texto do artigo.
_ARTICLE_ID_RE = re.compile(
    r'^(?P<indent>[ \t]*)'
    r'(?P<id>'
    r'Art(?:igo)?\.?\s*\d+[ºo°]?\.?'
    r'|§\s*(?:\d+[ºo°]?|[Úú]nico)'
    r'|Par[aá]grafo\s+(?:\d+[ºo°]?|[Úú]nico)'
    r'|Par\.\s*\d+[ºo°]?'
    r')(?=[\s\.,;:]|$)',
    re.IGNORECASE | re.MULTILINE,
)


_TERMINAL_PUNCT_RE = re.compile(r'[.!?:;"")\]»]\s*$')
_LIST_START_RE = re.compile(r'^\s*([a-zA-Z]\)|[IVXivx]+\)|\d+\.|\s*[-*•])\s')


def _merge_broken_paragraphs(text: str) -> str:
    """Mescla parágrafos cortados no meio de uma frase pela quebra de página.

    Heurística: se um parágrafo termina sem pontuação terminal e o seguinte
    começa com letra minúscula sem ser um marcador de lista, une os dois.
    """
    paragraphs = re.split(r'\n{2,}', text)
    result: list[str] = []
    i = 0
    while i < len(paragraphs):
        para = paragraphs[i]
        stripped_end = para.rstrip()
        while i + 1 < len(paragraphs):
            nxt = paragraphs[i + 1]
            nxt_lstripped = nxt.lstrip()
            if (stripped_end
                    and not _TERMINAL_PUNCT_RE.search(stripped_end)
                    and nxt_lstripped
                    and nxt_lstripped[0].islower()
                    and not _LIST_START_RE.match(nxt)):
                i += 1
                para = stripped_end + ' ' + nxt_lstripped
                stripped_end = para.rstrip()
            else:
                break
        result.append(para)
        i += 1
    return '\n\n'.join(result)


def _bold_article_identifiers(text: str) -> str:
    """Garante negrito nos identificadores de artigo e parágrafo no início de cada linha."""
    def _replace(m: re.Match) -> str:
        pos = m.start('id')
        if pos >= 2 and text[pos - 2:pos] == '**':
            return m.group(0)  # já está negritado
        return m.group('indent') + '**' + m.group('id') + '**'
    return _ARTICLE_ID_RE.sub(_replace, text)

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


def pdf_to_markdown_multimodal(pdf_bytes: bytes, on_progress=None) -> tuple[str, dict]:
    """
    Extrai e estrutura o PDF enviando cada lote de páginas como imagens para Claude Vision via Bedrock.
    Substitui tanto a extração por PyMuPDF quanto a etapa de estruturação por IA.
    Retorna (markdown_estruturado, uso_de_tokens_acumulado).
    on_progress(batch_atual, total_batches): callback opcional por lote processado.
    """
    from app.services.bedrock import extract_pages_multimodal

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Não foi possível abrir o PDF: {exc}") from exc

    total_pages = len(doc)
    if total_pages > settings.max_pdf_pages:
        doc.close()
        raise ValueError(
            f"O PDF tem {total_pages} páginas, excedendo o limite de {settings.max_pdf_pages}. "
            "Divida o documento ou envie apenas as seções necessárias."
        )
    batch_size = settings.multimodal_batch_pages

    page_images: list[bytes] = []
    for page in doc:
        pix = page.get_pixmap(dpi=settings.multimodal_dpi)
        page_images.append(pix.tobytes("png"))
    doc.close()

    batches = [page_images[i:i + batch_size] for i in range(0, total_pages, batch_size)]
    total_batches = len(batches)
    parts: list[str] = []
    total_usage = {"input_tokens": 0, "output_tokens": 0}

    for batch_idx, batch in enumerate(batches):
        start_page = batch_idx * batch_size + 1
        text, usage = extract_pages_multimodal(batch, start_page, is_continuation=batch_idx > 0)
        parts.append(text.strip())
        total_usage["input_tokens"] += usage["input_tokens"]
        total_usage["output_tokens"] += usage["output_tokens"]
        if on_progress:
            on_progress(batch_idx + 1, total_batches)

    full_text = "\n\n---\n\n".join(parts)
    full_text = _remove_signature_artifacts(full_text)
    full_text = full_text.replace("\n\n---\n\n", "\n\n")  # remove batch separators — renderiam como <hr> no Bookstack
    full_text = _merge_broken_paragraphs(full_text)
    full_text = _bold_article_identifiers(full_text)
    return full_text, total_usage




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


def _merge_chapter_titles(lines: list[str]) -> list[str]:
    """
    Mescla o numeral de capítulo/título/seção com o nome quando estão em headings separados.

    Em documentos jurídicos brasileiros é comum o PDF dispor o heading em duas linhas:
      ## CAPÍTULO II           ← só o numeral
      (linhas em branco)
      ## DOS CONCEITOS         ← só o nome (iniciado com preposição articulada)
    →   ## CAPÍTULO II — DOS CONCEITOS

    Só age quando os dois headings têm o mesmo nível (##) e o segundo começa com
    DA/DO/DAS/DOS/DE, que é o padrão das denominações de capítulo em normativos.
    """
    _numeral_only_re = re.compile(
        r'^(#{1,3})\s+((?:CAP[IÍ]TULO|T[IÍ]TULO|SE[CÇ][AÃ]O)\s+[IVXLCDM\d]+)\s*$',
        re.IGNORECASE,
    )
    _preposition_heading_re = re.compile(
        r'^(#{1,3})\s+((?:DA|DO|DAS|DOS|DE)\s+\S.*)$',
        re.IGNORECASE,
    )
    merged = []
    i = 0
    while i < len(lines):
        m = _numeral_only_re.match(lines[i])
        if m:
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                m2 = _preposition_heading_re.match(lines[j])
                if m2 and m.group(1) == m2.group(1):
                    merged.append(f"{m.group(1)} {m.group(2)} — {m2.group(2)}")
                    i = j + 1
                    continue
        merged.append(lines[i])
        i += 1
    return merged




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
