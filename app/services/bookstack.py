"""
Cliente da API do Bookstack.

MOCK_BOOKSTACK=true  → retorna dados simulados, sem chamadas externas.
MOCK_BOOKSTACK=false → conecta ao normas.ifsp.edu.br via API token.

Referência: https://demo.bookstackapp.com/api/docs
"""
import logging
import threading
import time
from datetime import datetime

_log = logging.getLogger(__name__)

import httpx

from app.config import get_settings

settings = get_settings()

# ── Dados de mock ─────────────────────────────────────────────────────────────

MOCK_SHELVES = [
    {"id": 1, "name": "Resoluções"},
    {"id": 2, "name": "Portarias"},
    {"id": 3, "name": "Instruções Normativas"},
    {"id": 4, "name": "Editais"},
]

MOCK_DRAFTS = [
    {
        "book_id": 101,
        "title": "Resolução IFSP nº 99/2025",
        "shelf_name": "Resoluções",
        "uploaded_by": "servidor@ifsp.edu.br",
        "created_at": "27/03/2025 14:32",
        "bookstack_url": "https://normas.ifsp.edu.br/books/resolucao-ifsp-99-2025",
    }
]

# ── Cache em memória ──────────────────────────────────────────────────────────

_CACHE_TTL = 300        # segundos — rascunhos; cache invalidado em toda mutação, então TTL alto é seguro
_SHELF_MAP_TTL = 300    # segundos — mapa de prateleiras (muda raramente)

_drafts_cache: dict = {}    # {"data": [...], "ts": float}
_shelf_map_cache: dict = {} # {"data": {...}, "ts": float}
_overview_cache: dict = {}  # {"data": {...}, "ts": float}

_UNSET = object()
_public_role_id = _UNSET   # cache: _UNSET = não buscado, None = não encontrado, int = ID

_cache_lock = threading.Lock()


def _invalidate_drafts_cache() -> None:
    with _cache_lock:
        _drafts_cache.clear()
        _overview_cache.clear()


def _invalidate_shelf_map_cache() -> None:
    with _cache_lock:
        _shelf_map_cache.clear()
        _overview_cache.clear()


def _get_public_role_id() -> int | None:
    """Busca o ID do papel 'Public' (acesso anônimo) uma vez e cacheia."""
    global _public_role_id
    with _cache_lock:
        if _public_role_id is not _UNSET:
            return _public_role_id  # type: ignore[return-value]
    try:
        roles = _api_get("/roles", params={"count": 100})["data"]
        role_id = None
        for role in roles:
            if role.get("display_name", "").lower() == "public":
                role_id = role["id"]
                break
    except Exception:
        role_id = None
    with _cache_lock:
        _public_role_id = role_id
    return role_id


def _restrict_book_to_authenticated(book_id: int) -> None:
    """
    Tenta negar acesso de leitura ao papel Public para o livro (rascunho em staging).
    Se a API de permissões não estiver disponível na versão do Bookstack, ignora silenciosamente —
    a prateleira de staging já provê a proteção principal contra listagem pública.
    """
    role_id = _get_public_role_id()
    if role_id is None:
        return
    try:
        _api_put(f"/books/{book_id}/permissions", {
            "override_role_permissions": [
                {"role_id": role_id, "view": False, "create": False, "update": False, "delete": False}
            ]
        })
    except Exception:
        pass  # API de permissões indisponível nesta versão do Bookstack


def _reset_book_permissions(book_id: int) -> None:
    """Remove restrições explícitas, voltando ao padrão da prateleira/sistema."""
    try:
        _api_put(f"/books/{book_id}/permissions", {
            "override_role_permissions": []
        })
    except Exception:
        pass  # API de permissões indisponível nesta versão do Bookstack


# ── Interface pública ─────────────────────────────────────────────────────────

def get_shelves() -> list[dict]:
    """Lista todas as prateleiras disponíveis. Usa o cache do shelf map quando disponível."""
    if settings.mock_bookstack:
        return MOCK_SHELVES
    with _cache_lock:
        if _shelf_map_cache and "shelves" in _shelf_map_cache and (
            time.monotonic() - _shelf_map_cache.get("ts", 0) < _SHELF_MAP_TTL
        ):
            return _shelf_map_cache["shelves"]
    return _api_get_all("/shelves")


def create_normativo(
    title: str,
    full_text_markdown: str,
    faq_markdown: str,
    download_url: str,
    uploaded_by: str,
    pdf_key: str = "",
    anomalies: list[str] | None = None,
    structure_mode: str = "validate",
) -> str:
    """
    Cria um livro (normativo) com capítulos em rascunho na prateleira de staging.

    Estrutura criada:
      1. Perguntas Frequentes  (capítulo com 1 página: FAQ + link de download)
      2. Texto Completo        (capítulo com 1 página: texto integral com headings de navegação)

    A prateleira definitiva é escolhida pelo revisor na hora da publicação.
    Retorna (url, book_id) do livro criado no Bookstack.
    """
    if settings.mock_bookstack:
        return f"{settings.bookstack_base_url}/books/mock-{title[:30].lower().replace(' ', '-')}", 0

    # 1. Cria o livro com tags de rastreamento
    book = _api_post("/books", {
        "name": title,
        "description": f"Enviado por {uploaded_by}",
        "tags": [
            {"name": "uploaded_by", "value": uploaded_by},
            {"name": "s3_pdf_key", "value": pdf_key},
        ],
    })
    book_id = book["id"]

    # 2. Registra metadados locais (evita N+1 na tela de revisão)
    from app.services import storage as _storage
    _storage.register_book_meta(book_id, uploaded_by)

    # 3. Coloca na prateleira de staging e restringe acesso ao papel Public.
    #    O livro ficará invisível ao público até ser aprovado na revisão.
    if settings.bookstack_staging_shelf_id:
        _add_book_to_shelf(settings.bookstack_staging_shelf_id, book_id)
    _restrict_book_to_authenticated(book_id)

    # 4. Capítulo "1. Perguntas Frequentes"
    faq_chapter = _api_post("/chapters", {
        "book_id": book_id,
        "name": "1. Perguntas Frequentes",
        "description": "Seção que resume e simplifica a compreensão da política para amplo acesso.",
    })
    _api_post("/pages", {
        "chapter_id": faq_chapter["id"],
        "name": "Perguntas Frequentes",
        "markdown": faq_markdown,
        "draft": True,
    })

    # 5. Capítulo "2. Texto Completo" — uma página por capítulo do normativo (quando detectado)
    text_chapter = _api_post("/chapters", {
        "book_id": book_id,
        "name": "2. Texto Completo",
        "description": "Reprodução do texto completo para simplificação de busca e consultas específicas.",
    })
    if anomalies or structure_mode == "suggest":
        _api_post("/pages", {
            "chapter_id": text_chapter["id"],
            "name": "Avisos sobre o documento",
            "markdown": _build_anomaly_page(anomalies or [], structure_mode),
            "draft": True,
        })
    for page_name, page_content in _split_into_chapter_pages(full_text_markdown):
        _api_post("/pages", {
            "chapter_id": text_chapter["id"],
            "name": page_name,
            "markdown": page_content,
            "draft": True,
        })

    # 6. Capítulo "3. Download" — link permanente para o PDF original
    if download_url:
        dl_chapter = _api_post("/chapters", {
            "book_id": book_id,
            "name": "3. Download",
            "description": "Link permanente para o PDF original do normativo.",
        })
        _api_post("/pages", {
            "chapter_id": dl_chapter["id"],
            "name": "Download do PDF",
            "markdown": f"## Download\n\n[Baixar PDF original]({download_url})",
            "draft": True,
        })

    _invalidate_drafts_cache()
    _invalidate_shelf_map_cache()
    return f"{settings.bookstack_base_url}/books/{book['slug']}", book_id


def get_draft_books() -> list[dict]:
    """
    Lista livros com páginas em rascunho (para a tela de revisão).
    Resultado cacheado por _CACHE_TTL segundos.
    """
    if settings.mock_bookstack:
        return MOCK_DRAFTS

    with _cache_lock:
        if _drafts_cache and (time.monotonic() - _drafts_cache["ts"] < _CACHE_TTL):
            return _drafts_cache["data"]

    result = _fetch_draft_books()
    with _cache_lock:
        _drafts_cache["data"] = result
        _drafts_cache["ts"] = time.monotonic()
    return result


def get_all_books_overview() -> dict:
    """
    Retorna {"drafts": [...], "published": [...], "invalid": [...], "shelves": [...]}.
    Usado na tela de revisão (GET) para exibir os 3 grupos de normativos.

    A fonte da verdade para rascunhos é a prateleira de staging:
    livros que ainda estão nela não foram aprovados e são tratados como draft.
    Livros fora da staging com tag status:invalido são inválidos; os demais, publicados.
    """
    if settings.mock_bookstack:
        return {"drafts": MOCK_DRAFTS, "published": [], "invalid": [], "shelves": MOCK_SHELVES}

    with _cache_lock:
        if _overview_cache and (time.monotonic() - _overview_cache.get("ts", 0) < _CACHE_TTL):
            return _overview_cache["data"]

    from concurrent.futures import ThreadPoolExecutor

    # Busca lista de livros e dados de prateleiras em paralelo.
    # _build_shelf_data() retorna tudo que precisamos sobre prateleiras numa chamada cacheada.
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_books = executor.submit(_api_get_all, "/books")
        f_shelf = executor.submit(_build_shelf_data)
        all_books_list = f_books.result()
        shelf_map, all_shelves, staging_book_ids, revoked_shelf_book_ids = f_shelf.result()

    all_books = {b["id"]: b for b in all_books_list}

    # A listagem /books não inclui tags — uploaded_by vem do registro local (S3/data),
    # gravado no momento do upload. Isso elimina N+1 chamadas individuais à API do Bookstack.
    from app.services import storage as _storage
    _book_meta = _storage.get_book_meta_registry()
    uploaded_by_map: dict[int, str] = {
        bid: _book_meta.get(str(bid), {}).get("uploaded_by", "—")
        for bid in all_books
    }

    drafts = []
    published = []

    for bid, book in all_books.items():
        if bid in revoked_shelf_book_ids:
            continue  # gerenciado pela prateleira Revogadas, não aparece em publicados

        bookstack_url = f"{settings.bookstack_base_url}/books/{book['slug']}"
        created_at = _format_datetime(book.get("created_at", ""))

        if bid in staging_book_ids:
            drafts.append({
                "book_id": bid,
                "title": book["name"],
                "shelf_name": "—",  # escolhida pelo revisor na hora da publicação
                "uploaded_by": uploaded_by_map.get(bid, "—"),
                "created_at": created_at,
                "bookstack_url": bookstack_url,
            })
        else:
            published.append({
                "book_id": bid,
                "title": book["name"],
                "shelf_name": shelf_map.get(bid, "—"),
                "uploaded_by": uploaded_by_map.get(bid, "—"),
                "bookstack_url": bookstack_url,
            })

    result = {"drafts": drafts, "published": published, "invalid": [], "shelves": all_shelves}
    with _cache_lock:
        _overview_cache["data"] = result
        _overview_cache["ts"] = time.monotonic()
    return result


def get_book_for_revocation(book_id: int) -> dict:
    """
    Busca dados do livro para o pipeline de revogação, sem deletar.
    Retorna {title, pdf_key, uploaded_by, page_markdown}.
    """
    if settings.mock_bookstack:
        return {
            "title": f"Normativo Mock {book_id}",
            "pdf_key": f"pdfs/mock-{book_id}.pdf",
            "uploaded_by": "mock@ifsp.edu.br",
            "page_markdown": f"# {book_id}\n\nPortaria nº 1/2025, de 01/01/2025.\n\nDispõe sobre procedimentos internos.",
        }

    book = _api_get(f"/books/{book_id}")
    tags_dict = _parse_tags(book)

    # Busca páginas de texto para alimentar a IA (exclui FAQ e links de download)
    _SKIP_PAGE_NAMES = {"Perguntas Frequentes", "FAQ", "Link de Download", "Resumo e Download", "Download do PDF"}
    pages = _api_get("/pages", params={"filter[book_id:eq]": book_id, "count": 500})["data"]
    text_pages = [p for p in pages if p.get("name", "") not in _SKIP_PAGE_NAMES]

    from concurrent.futures import ThreadPoolExecutor

    def _fetch_page_md(page: dict) -> str:
        detail = _api_get(f"/pages/{page['id']}")
        content = detail.get("markdown", "")
        return f"## {page['name']}\n\n{content}" if content else ""

    with ThreadPoolExecutor(max_workers=min(10, max(1, len(text_pages)))) as executor:
        contents = list(executor.map(_fetch_page_md, text_pages))

    page_markdown = "\n\n".join(c for c in contents if c)

    return {
        "title": book["name"],
        "pdf_key": tags_dict.get("s3_pdf_key", ""),
        "uploaded_by": tags_dict.get("uploaded_by", "—"),
        "page_markdown": page_markdown,
    }


def create_revoked_book_entry(
    title: str,
    summary_markdown: str,
    pdf_url: str,
    uploaded_by: str,
    tipo: str = "",
) -> tuple[str, int]:
    """
    Cria um livro na prateleira Revogadas com o resumo gerado pela IA e o link do PDF.
    Retorna (url, book_id) do livro criado.
    """
    if settings.mock_bookstack:
        return f"{settings.bookstack_base_url}/books/revogado-mock-{title[:20].lower().replace(' ', '-')}", 0

    tags = [
        {"name": "uploaded_by", "value": uploaded_by},
        {"name": "status", "value": "revogado"},
    ]
    if tipo:
        tags.append({"name": "tipo", "value": tipo})

    book = _api_post("/books", {
        "name": title,
        "description": f"Normativo revogado. Enviado originalmente por {uploaded_by}.",
        "tags": tags,
    })
    book_id = book["id"]

    page_content = summary_markdown
    if pdf_url:
        page_content += f"\n\n---\n\n## Download\n\n[Baixar PDF original]({pdf_url})"

    _api_post("/pages", {
        "book_id": book_id,
        "name": "Resumo e Download",
        "markdown": page_content,
        "draft": False,
    })

    if settings.bookstack_revoked_shelf_id:
        _add_book_to_shelf(settings.bookstack_revoked_shelf_id, book_id)

    _invalidate_shelf_map_cache()
    return f"{settings.bookstack_base_url}/books/{book['slug']}", book_id


def delete_book_from_bookstack(book_id: int) -> None:
    """Remove apenas o livro do Bookstack (sem tocar no S3). Usado no pipeline de revogação."""
    if settings.mock_bookstack:
        return

    try:
        _api_delete(f"/books/{book_id}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        _log.warning("delete_book_from_bookstack: livro %s não encontrado (já removido?)", book_id)

    from app.services import storage as _storage
    _storage.unregister_book_meta(book_id)
    _invalidate_drafts_cache()
    _invalidate_shelf_map_cache()


def _fetch_draft_books() -> list[dict]:
    """
    Busca os livros na prateleira de staging (rascunhos aguardando revisão).
    Consistente com get_all_books_overview(): usa staging shelf como fonte da verdade.
    uploaded_by vem do registro local — a listagem /books não inclui tags.
    """
    all_books = {b["id"]: b for b in _api_get_all("/books")}

    staging_book_ids: set[int] = set()
    if settings.bookstack_staging_shelf_id:
        staging = _api_get(f"/shelves/{settings.bookstack_staging_shelf_id}")
        staging_book_ids = {b["id"] for b in staging.get("books", [])}

    from app.services import storage as _storage
    book_meta = _storage.get_book_meta_registry()

    result = []
    for bid in staging_book_ids:
        book = all_books.get(bid)
        if not book:
            continue
        created_at = _format_datetime(book.get("created_at", ""))
        result.append({
            "book_id": bid,
            "title": book["name"],
            "shelf_name": "—",
            "uploaded_by": book_meta.get(str(bid), {}).get("uploaded_by", "—"),
            "created_at": created_at,
            "bookstack_url": f"{settings.bookstack_base_url}/books/{book['slug']}",
        })
    return result


def publish_book(book_id: int, shelf_id: int) -> None:
    """Publica as páginas em rascunho e move o livro da staging para a prateleira escolhida."""
    if settings.mock_bookstack:
        return

    # 0. Remove restrições de acesso (livro passa a ser público)
    _reset_book_permissions(book_id)

    # 1. Publica as páginas em paralelo
    pages = _api_get("/pages", params={
        "filter[book_id:eq]": book_id,
        "filter[draft:eq]": "true",
    })["data"]
    if pages:
        from concurrent.futures import ThreadPoolExecutor
        def _publish_page(page: dict) -> None:
            _api_put(f"/pages/{page['id']}", {"draft": False})
        with ThreadPoolExecutor(max_workers=min(5, len(pages))) as executor:
            list(executor.map(_publish_page, pages))

    # 2. Remove da staging
    if settings.bookstack_staging_shelf_id:
        _remove_book_from_shelf(settings.bookstack_staging_shelf_id, book_id)

    # 3. Adiciona à prateleira escolhida pelo revisor
    _add_book_to_shelf(shelf_id, book_id)

    _invalidate_drafts_cache()
    _invalidate_shelf_map_cache()


def get_published_book_title(book_id: int) -> str | None:
    """
    Retorna o título do livro se estiver publicado (fora da staging e revogadas).
    Usa o cache de prateleiras para a verificação e uma única chamada à API para o título.
    Retorna None se o livro estiver em staging/revogadas ou não for encontrado.
    """
    if settings.mock_bookstack:
        return f"Normativo Mock {book_id}"
    _, _, staging_ids, revoked_ids = _build_shelf_data()
    if book_id in staging_ids or book_id in revoked_ids:
        return None
    try:
        return _api_get(f"/books/{book_id}")["name"]
    except Exception:
        return None


def move_book(book_id: int, new_shelf_id: int) -> None:
    """Move um livro publicado de uma prateleira para outra."""
    if settings.mock_bookstack:
        return

    book_to_shelf_name, all_shelves, _, _ = _build_shelf_data()
    forbidden = {settings.bookstack_staging_shelf_id, settings.bookstack_revoked_shelf_id}

    # Localiza a prateleira atual via cache — evita iterar sobre todas as prateleiras com chamadas HTTP
    current_shelf_name = book_to_shelf_name.get(book_id)
    name_to_id = {s["name"]: s["id"] for s in all_shelves}
    current_shelf_id = name_to_id.get(current_shelf_name) if current_shelf_name else None

    if current_shelf_id and current_shelf_id not in forbidden and current_shelf_id != new_shelf_id:
        _remove_book_from_shelf(current_shelf_id, book_id)

    # Adiciona à nova prateleira
    _add_book_to_shelf(new_shelf_id, book_id)

    _invalidate_shelf_map_cache()


def delete_book(book_id: int) -> None:
    """Remove o livro do Bookstack e o PDF correspondente do S3."""
    if settings.mock_bookstack:
        return

    # Busca a chave S3 antes de deletar o livro.
    # Se o livro já foi removido manualmente do Bookstack, ignora o 404 e prossegue.
    pdf_key = None
    try:
        book = _api_get(f"/books/{book_id}")
        pdf_key = _parse_tags(book).get("s3_pdf_key")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        _log.warning("delete_book: livro %s não encontrado no Bookstack (já removido?)", book_id)

    try:
        _api_delete(f"/books/{book_id}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise

    from app.services import storage
    storage.unregister_book_meta(book_id)
    if pdf_key:
        storage.delete_pdf(pdf_key)
        job_id = pdf_key.removeprefix("pdfs/").removesuffix(".pdf")
        storage.unregister_pdf_checksum_by_job_id(job_id)

    _invalidate_drafts_cache()


# ── Helpers internos ──────────────────────────────────────────────────────────

import re as _re


def _parse_tags(obj: dict) -> dict:
    """Converte lista de tags {name, value} de um livro/recurso em dicionário."""
    return {t["name"]: t["value"] for t in obj.get("tags", [])}


def _format_datetime(raw: str) -> str:
    """Converte string ISO8601 para formato DD/MM/YYYY HH:MM."""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return raw


def _add_book_to_shelf(shelf_id: int, book_id: int) -> None:
    """Adiciona um livro a uma prateleira se ainda não estiver presente."""
    shelf = _api_get(f"/shelves/{shelf_id}")
    books = [b["id"] for b in shelf.get("books", [])]
    if book_id not in books:
        _api_put(f"/shelves/{shelf_id}", {"books": books + [book_id]})


def _remove_book_from_shelf(shelf_id: int, book_id: int) -> None:
    """Remove um livro de uma prateleira."""
    shelf = _api_get(f"/shelves/{shelf_id}")
    remaining = [b["id"] for b in shelf.get("books", []) if b["id"] != book_id]
    _api_put(f"/shelves/{shelf_id}", {"books": remaining})

def _build_anomaly_page(anomalies: list[str], structure_mode: str = "validate") -> str:
    """Gera o conteúdo Markdown da página 'Avisos sobre o documento'."""
    parts = []

    if structure_mode == "multimodal":
        parts.append(
            "## Extraído via visão computacional\n\n"
            "O texto foi extraído diretamente das imagens das páginas do PDF pelo modelo Claude Vision, "
            "sem processamento intermediário por PyMuPDF. A estrutura de headings foi detectada visualmente pelo modelo."
        )

    if structure_mode == "suggest":
        parts.append(
            "## Estrutura sugerida pela IA\n\n"
            "Este documento não continha formatação de seções detectável no PDF original "
            "(sem negrito, sem variação de fonte, sem palavras-chave como CAPÍTULO ou TÍTULO). "
            "A estrutura de headings foi sugerida pelo modelo de IA com base no contexto temático do texto. "
            "**Revise e ajuste os headings antes de publicar.**"
        )

    if anomalies:
        items = '\n'.join(f'- {a}' for a in anomalies)
        parts.append(
            "## Inconsistências no documento original\n\n"
            "As seguintes inconsistências foram detectadas automaticamente no PDF original. "
            "O texto foi reproduzido fielmente — nenhuma correção foi aplicada.\n\n"
            f"{items}"
        )

    return "\n\n".join(parts)


# Divide apenas em Títulos (H1) e Capítulos (H2) — Seções (H3) ficam dentro da página do capítulo.
# Detecta explicitamente palavras-chave estruturais para não depender do nível de heading que a IA usou.
_CHAPTER_SPLIT_RE = _re.compile(
    r"^#{1,3}\s+((?:TÍTULO|CAPÍTULO|CHAPTER|ANEXO)\b.*)$",
    _re.IGNORECASE | _re.MULTILINE,
)


def _split_into_chapter_pages(markdown: str) -> list[tuple[str, str]]:
    """
    Divide o markdown em páginas por Título/Capítulo do normativo.

    Seções (Seção I, Seção II…) ficam dentro da página do capítulo ao qual pertencem.
    Retorna lista de (nome_da_página, conteúdo_markdown).
    Fallback: uma única página "Texto Integral" se não houver capítulos detectados.
    Conteúdo anterior ao primeiro capítulo (ementa/preâmbulo) vira página "Preâmbulo"
    se for substancial (> 100 chars).
    """
    lines = markdown.splitlines()
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in lines:
        m = _CHAPTER_SPLIT_RE.match(line)
        if m:
            if current_lines or current_heading is not None:
                sections.append((current_heading, current_lines))
            current_heading = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines or current_heading is not None:
        sections.append((current_heading, current_lines))

    if not sections or all(h is None for h, _ in sections):
        return [("Texto Integral", markdown)]

    pages: list[tuple[str, str]] = []
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if heading is None:
            if len(body) > 100:
                pages.append(("Preâmbulo", body))
        else:
            content = f"## {heading}\n\n{body}" if body else f"## {heading}"
            pages.append((heading, content))

    return pages if pages else [("Texto Integral", markdown)]


# ── Helpers HTTP ──────────────────────────────────────────────────────────────

# httpx.Client é thread-safe e mantém pool de conexões TCP/TLS entre chamadas.
# Evita a sobrecarga de handshake por requisição, especialmente nas operações paralelas
# com ThreadPoolExecutor (ex: _build_shelf_data, _fetch_uploaded_by).
_http_client = httpx.Client(
    base_url=f"{settings.bookstack_base_url}/api",
    headers={
        "Authorization": f"Token {settings.bookstack_token_id}:{settings.bookstack_token_secret}",
        "Content-Type": "application/json",
    },
    timeout=15,
)


def _api_get_all(path: str, extra_params: dict | None = None) -> list:
    """Percorre todas as páginas de um endpoint paginado e retorna a lista completa."""
    page_size = 100
    params: dict = {"count": page_size, "offset": 0, **(extra_params or {})}
    results: list = []
    while True:
        batch = _api_get(path, params=params)["data"]
        results.extend(batch)
        if len(batch) < page_size:
            break
        params = {**params, "offset": params["offset"] + len(batch)}
    return results


def _api_get(path: str, params: dict | None = None) -> dict:
    r = _http_client.get(path, params=params)
    r.raise_for_status()
    return r.json()


def _api_post(path: str, body: dict) -> dict:
    r = _http_client.post(path, json=body)
    if not r.is_success:
        logging.error("Bookstack API error %s %s: %s", r.status_code, path, r.text[:500])
    r.raise_for_status()
    return r.json()


def _api_put(path: str, body: dict) -> dict:
    r = _http_client.put(path, json=body)
    if not r.is_success:
        _log.error("Bookstack API error %s %s: %s", r.status_code, path, r.text[:500])
    r.raise_for_status()
    return r.json()


def _api_delete(path: str) -> None:
    r = _http_client.delete(path)
    if not r.is_success:
        _log.error("Bookstack API error %s %s: %s", r.status_code, path, r.text[:500])
    r.raise_for_status()


def _build_shelf_data() -> tuple[dict, list, set, set]:
    """
    Retorna (book_to_shelf, shelves, staging_ids, revoked_ids).
    Cacheado por _SHELF_MAP_TTL segundos — prateleiras mudam raramente.
    Detalhes de cada prateleira buscados em paralelo.
    """
    with _cache_lock:
        if _shelf_map_cache and "shelves" in _shelf_map_cache and (
            time.monotonic() - _shelf_map_cache["ts"] < _SHELF_MAP_TTL
        ):
            return (
                _shelf_map_cache["data"],
                _shelf_map_cache["shelves"],
                _shelf_map_cache["staging_ids"],
                _shelf_map_cache["revoked_ids"],
            )

    from concurrent.futures import ThreadPoolExecutor

    shelves = _api_get_all("/shelves")
    book_to_shelf: dict[int, str] = {}
    staging_ids: set[int] = set()
    revoked_ids: set[int] = set()

    def _fetch_shelf(shelf: dict) -> tuple[int, str, list]:
        detail = _api_get(f"/shelves/{shelf['id']}")
        return shelf["id"], shelf["name"], detail.get("books", [])

    with ThreadPoolExecutor(max_workers=min(10, max(1, len(shelves)))) as executor:
        for shelf_id, shelf_name, books in executor.map(_fetch_shelf, shelves):
            for book in books:
                bid = book["id"]
                book_to_shelf[bid] = shelf_name
                if shelf_id == settings.bookstack_staging_shelf_id:
                    staging_ids.add(bid)
                if shelf_id == settings.bookstack_revoked_shelf_id:
                    revoked_ids.add(bid)

    with _cache_lock:
        _shelf_map_cache.update({
            "data": book_to_shelf,
            "shelves": shelves,
            "staging_ids": staging_ids,
            "revoked_ids": revoked_ids,
            "ts": time.monotonic(),
        })
    return book_to_shelf, shelves, staging_ids, revoked_ids
