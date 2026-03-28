"""
Cliente da API do Bookstack.

MOCK_BOOKSTACK=true  → retorna dados simulados, sem chamadas externas.
MOCK_BOOKSTACK=false → conecta ao normas.ifsp.edu.br via API token.

Referência: https://demo.bookstackapp.com/api/docs
"""
import logging
import time
from datetime import datetime

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

_CACHE_TTL = 120        # segundos — rascunhos (muda com frequência)
_SHELF_MAP_TTL = 300    # segundos — mapa de prateleiras (muda raramente)

_drafts_cache: dict = {}    # {"data": [...], "ts": float}
_shelf_map_cache: dict = {} # {"data": {...}, "ts": float}

_UNSET = object()
_public_role_id = _UNSET   # cache: _UNSET = não buscado, None = não encontrado, int = ID


def _invalidate_drafts_cache() -> None:
    _drafts_cache.clear()


def _invalidate_shelf_map_cache() -> None:
    _shelf_map_cache.clear()


def _get_public_role_id() -> int | None:
    """Busca o ID do papel 'Public' (acesso anônimo) uma vez e cacheia."""
    global _public_role_id
    if _public_role_id is not _UNSET:
        return _public_role_id  # type: ignore[return-value]
    try:
        roles = _api_get("/roles", params={"count": 100})["data"]
        for role in roles:
            if role.get("display_name", "").lower() == "public":
                _public_role_id = role["id"]
                return _public_role_id
    except Exception:
        pass
    _public_role_id = None
    return None


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
    if _shelf_map_cache and "shelves" in _shelf_map_cache and (
        time.monotonic() - _shelf_map_cache.get("ts", 0) < _SHELF_MAP_TTL
    ):
        return _shelf_map_cache["shelves"]
    return _api_get("/shelves", params={"count": 500})["data"]


def create_normativo(
    title: str,
    full_text_markdown: str,
    faq_markdown: str,
    download_url: str,
    uploaded_by: str,
    pdf_key: str = "",
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

    # 2. Coloca na prateleira de staging e restringe acesso ao papel Public.
    #    O livro ficará invisível ao público até ser aprovado na revisão.
    if settings.bookstack_staging_shelf_id:
        staging = _api_get(f"/shelves/{settings.bookstack_staging_shelf_id}")
        existing_ids = [b["id"] for b in staging.get("books", [])]
        _api_put(f"/shelves/{settings.bookstack_staging_shelf_id}", {"books": existing_ids + [book_id]})
    _restrict_book_to_authenticated(book_id)

    # 3. Capítulo "1. Perguntas Frequentes"
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

    # 4. Capítulo "2. Texto Completo"
    text_chapter = _api_post("/chapters", {
        "book_id": book_id,
        "name": "2. Texto Completo",
        "description": "Reprodução do texto completo para simplificação de busca e consultas específicas.",
    })
    _api_post("/pages", {
        "chapter_id": text_chapter["id"],
        "name": "Texto Integral",
        "markdown": full_text_markdown,
        "draft": True,
    })

    # 5. Capítulo "3. Download" — link permanente para o PDF original
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

    # Verifica cache
    if _drafts_cache and (time.monotonic() - _drafts_cache["ts"] < _CACHE_TTL):
        return _drafts_cache["data"]

    result = _fetch_draft_books()
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

    from concurrent.futures import ThreadPoolExecutor

    def _fetch_all_books() -> list:
        return _api_get("/books", params={"count": 500})["data"]

    # Busca lista de livros e dados de prateleiras em paralelo.
    # _build_shelf_data() retorna tudo que precisamos sobre prateleiras numa chamada cacheada.
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_books = executor.submit(_fetch_all_books)
        f_shelf = executor.submit(_build_shelf_data)
        all_books_list = f_books.result()
        shelf_map, all_shelves, staging_book_ids, revoked_shelf_book_ids = f_shelf.result()

    all_books = {b["id"]: b for b in all_books_list}

    # Busca uploaded_by dos livros em staging em paralelo (a listagem não inclui tags).
    # Filtra apenas os IDs que realmente existem em all_books para evitar 404 em caso
    # de livros deletados diretamente no Bookstack sem remoção da prateleira (referência órfã).
    def _fetch_uploaded_by(bid: int) -> tuple[int, str]:
        try:
            detail = _api_get(f"/books/{bid}")
            tags = {t["name"]: t["value"] for t in detail.get("tags", [])}
            return bid, tags.get("uploaded_by", "—")
        except Exception:
            return bid, "—"

    staging_to_fetch = staging_book_ids & all_books.keys()
    staging_uploaded_by: dict[int, str] = {}
    if staging_to_fetch:
        with ThreadPoolExecutor(max_workers=min(10, len(staging_to_fetch))) as executor:
            staging_uploaded_by = dict(executor.map(_fetch_uploaded_by, staging_to_fetch))

    drafts = []
    published = []
    invalid = []

    for bid, book in all_books.items():
        if bid in revoked_shelf_book_ids:
            continue  # gerenciado pela prateleira Revogadas, não aparece em publicados

        tags = {t["name"]: t["value"] for t in book.get("tags", [])}
        bookstack_url = f"{settings.bookstack_base_url}/books/{book['slug']}"
        raw = book.get("created_at", "")
        try:
            created_at = datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
        except Exception:
            created_at = raw

        if bid in staging_book_ids:
            drafts.append({
                "book_id": bid,
                "title": book["name"],
                "shelf_name": "—",  # escolhida pelo revisor na hora da publicação
                "uploaded_by": staging_uploaded_by.get(bid, "—"),
                "created_at": created_at,
                "bookstack_url": bookstack_url,
            })
        elif tags.get("status") in ("invalido", "revogado"):
            invalid.append({
                "book_id": bid,
                "title": book["name"],
                "shelf_name": shelf_map.get(bid, "—"),
                "uploaded_by": tags.get("uploaded_by", "—"),
                "bookstack_url": bookstack_url,
            })
        else:
            published.append({
                "book_id": bid,
                "title": book["name"],
                "shelf_name": shelf_map.get(bid, "—"),
                "uploaded_by": tags.get("uploaded_by", "—"),
                "bookstack_url": bookstack_url,
            })

    return {"drafts": drafts, "published": published, "invalid": invalid, "shelves": all_shelves}


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
    tags_dict = {t["name"]: t["value"] for t in book.get("tags", [])}

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
        shelf = _api_get(f"/shelves/{settings.bookstack_revoked_shelf_id}")
        existing = [b["id"] for b in shelf.get("books", [])]
        if book_id not in existing:
            _api_put(f"/shelves/{settings.bookstack_revoked_shelf_id}", {"books": existing + [book_id]})

    _invalidate_shelf_map_cache()
    return f"{settings.bookstack_base_url}/books/{book['slug']}", book_id


def delete_book_from_bookstack(book_id: int) -> None:
    """Remove apenas o livro do Bookstack (sem tocar no S3). Usado no pipeline de revogação."""
    if settings.mock_bookstack:
        return

    _api_delete(f"/books/{book_id}")
    _invalidate_drafts_cache()
    _invalidate_shelf_map_cache()


def _fetch_draft_books() -> list[dict]:
    """
    Busca os livros na prateleira de staging (rascunhos aguardando revisão).
    Consistente com get_all_books_overview(): usa staging shelf como fonte da verdade.
    """
    all_books = {b["id"]: b for b in _api_get("/books", params={"count": 500})["data"]}

    staging_book_ids: set[int] = set()
    if settings.bookstack_staging_shelf_id:
        staging = _api_get(f"/shelves/{settings.bookstack_staging_shelf_id}")
        staging_book_ids = {b["id"] for b in staging.get("books", [])}

    result = []
    for bid in staging_book_ids:
        book = all_books.get(bid)
        if not book:
            continue
        tags = {t["name"]: t["value"] for t in book.get("tags", [])}
        raw = book.get("created_at", "")
        try:
            created_at = datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
        except Exception:
            created_at = raw
        result.append({
            "book_id": bid,
            "title": book["name"],
            "shelf_name": "—",
            "uploaded_by": tags.get("uploaded_by", "—"),
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

    # 1. Publica as páginas
    pages = _api_get("/pages", params={
        "filter[book_id:eq]": book_id,
        "filter[draft:eq]": "true",
    })["data"]
    for page in pages:
        _api_put(f"/pages/{page['id']}", {"draft": False})

    # 2. Remove da staging
    if settings.bookstack_staging_shelf_id:
        staging = _api_get(f"/shelves/{settings.bookstack_staging_shelf_id}")
        remaining = [b["id"] for b in staging.get("books", []) if b["id"] != book_id]
        _api_put(f"/shelves/{settings.bookstack_staging_shelf_id}", {"books": remaining})

    # 3. Adiciona à prateleira escolhida pelo revisor
    target = _api_get(f"/shelves/{shelf_id}")
    existing = [b["id"] for b in target.get("books", [])]
    if book_id not in existing:
        _api_put(f"/shelves/{shelf_id}", {"books": existing + [book_id]})

    _invalidate_drafts_cache()
    _invalidate_shelf_map_cache()


def delete_book(book_id: int) -> None:
    """Remove o livro do Bookstack e o PDF correspondente do S3."""
    if settings.mock_bookstack:
        return

    # Busca a chave S3 antes de deletar o livro
    book = _api_get(f"/books/{book_id}")
    pdf_key = next(
        (t["value"] for t in book.get("tags", []) if t["name"] == "s3_pdf_key"),
        None,
    )

    _api_delete(f"/books/{book_id}")

    if pdf_key:
        from app.services import storage
        storage.delete_pdf(pdf_key)

    _invalidate_drafts_cache()


# ── Helpers HTTP ──────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Token {settings.bookstack_token_id}:{settings.bookstack_token_secret}",
        "Content-Type": "application/json",
    }


def _api_get(path: str, params: dict | None = None) -> dict:
    r = httpx.get(
        f"{settings.bookstack_base_url}/api{path}",
        headers=_headers(),
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _api_post(path: str, body: dict) -> dict:
    r = httpx.post(
        f"{settings.bookstack_base_url}/api{path}",
        headers=_headers(),
        json=body,
        timeout=15,
    )
    if not r.is_success:
        logging.error("Bookstack API error %s %s: %s", r.status_code, path, r.text[:500])
    r.raise_for_status()
    return r.json()


def _api_put(path: str, body: dict) -> dict:
    r = httpx.put(
        f"{settings.bookstack_base_url}/api{path}",
        headers=_headers(),
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _api_delete(path: str) -> None:
    r = httpx.delete(
        f"{settings.bookstack_base_url}/api{path}",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()


def _build_shelf_data() -> tuple[dict, list, set, set]:
    """
    Retorna (book_to_shelf, shelves, staging_ids, revoked_ids).
    Cacheado por _SHELF_MAP_TTL segundos — prateleiras mudam raramente.
    Detalhes de cada prateleira buscados em paralelo.
    """
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

    shelves = _api_get("/shelves", params={"count": 500})["data"]
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

    _shelf_map_cache.update({
        "data": book_to_shelf,
        "shelves": shelves,
        "staging_ids": staging_ids,
        "revoked_ids": revoked_ids,
        "ts": time.monotonic(),
    })
    return book_to_shelf, shelves, staging_ids, revoked_ids
