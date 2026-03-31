#!/usr/bin/env python3
"""
Migração única: popula registry/book_meta.json com os dados de uploaded_by
de todos os livros já existentes no Bookstack.

Execução (a partir da raiz do projeto):
    python scripts/migrate_book_meta.py [--dry-run]

Requer MOCK_BOOKSTACK=false e credenciais do Bookstack configuradas no .env.
Livros já presentes no registro não são sobrescritos.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from concurrent.futures import ThreadPoolExecutor

from app.config import get_settings
from app.services import storage
from app.services.bookstack import _api_get_all, _api_get, _parse_tags

settings = get_settings()

DRY_RUN = "--dry-run" in sys.argv


def main() -> None:
    if settings.mock_bookstack:
        print("MOCK_BOOKSTACK=true — sem acesso real ao Bookstack. Saindo.")
        sys.exit(0)

    print("Buscando lista de livros no Bookstack...")
    books = _api_get_all("/books")
    print(f"  {len(books)} livro(s) encontrado(s).")

    existing = storage.get_book_meta_registry()
    print(f"  {len(existing)} entrada(s) já no registro local.\n")

    to_fetch = [b for b in books if str(b["id"]) not in existing]
    already = len(books) - len(to_fetch)

    if not to_fetch:
        print("Nenhum livro novo para migrar. Registro já está completo.")
        return

    print(f"Buscando tags de {len(to_fetch)} livro(s) (paralelo, 10 workers)...")

    def fetch_meta(book: dict) -> tuple[int, str, str]:
        bid = book["id"]
        try:
            detail = _api_get(f"/books/{bid}")
            uploaded_by = _parse_tags(detail).get("uploaded_by", "—")
            return bid, uploaded_by, "ok"
        except Exception as exc:
            return bid, "—", f"erro: {exc}"

    results: list[tuple[int, str, str]] = []
    with ThreadPoolExecutor(max_workers=min(10, len(to_fetch))) as executor:
        results = list(executor.map(fetch_meta, to_fetch))

    ok = [(bid, ub) for bid, ub, status in results if status == "ok"]
    errors = [(bid, status) for bid, _, status in results if status != "ok"]

    print(f"\n{'SIMULAÇÃO — ' if DRY_RUN else ''}Resultado:")
    for bid, uploaded_by in ok:
        print(f"  [{bid}] → {uploaded_by}")
    for bid, status in errors:
        print(f"  [{bid}] FALHOU — {status}", file=sys.stderr)

    print(f"\nResumo: {already} já existentes | {len(ok)} a gravar | {len(errors)} erro(s).")

    if DRY_RUN:
        print("\nModo --dry-run: nenhuma alteração gravada.")
        return

    for bid, uploaded_by in ok:
        storage.register_book_meta(bid, uploaded_by)

    print(f"\nRegistro atualizado em registry/book_meta.json.")


if __name__ == "__main__":
    main()
