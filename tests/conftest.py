"""
Fixtures compartilhadas para o test suite.

IMPORTANTE: as variáveis de ambiente DEVEM ser definidas ANTES de qualquer
import de módulos da app, pois vários usam `settings = get_settings()` no
nível do módulo — e get_settings() é @lru_cache.
"""
import os

# ── Definir variáveis de ambiente antes de qualquer import da app ────────────
os.environ["MOCK_AUTH"] = "true"
os.environ["MOCK_BOOKSTACK"] = "true"
os.environ["MOCK_S3"] = "true"
os.environ["SESSION_SECRET_KEY"] = "test-secret-key-for-automated-tests"

import pytest

from app.config import Settings, get_settings

# Recria Settings SEM ler .env (evita extras como lambda_reserved_concurrency
# que causam ValidationError). Os valores vêm apenas das env vars acima.
get_settings.cache_clear()
_test_settings = Settings(_env_file="")  # type: ignore[call-arg]


def _get_test_settings() -> Settings:
    return _test_settings


# Substitui get_settings globalmente para todos os módulos que já importaram
import app.config
app.config.get_settings = _get_test_settings  # type: ignore[assignment]

# Módulos que fazem `settings = get_settings()` no nível do módulo já executaram
# antes deste ponto (na collection do pytest). Precisamos garantir que o import
# aconteça APÓS o monkey-patch acima. Como o conftest roda antes dos testes mas
# DEPOIS da collection, os módulos já estão importados. Então patcheamos o
# atributo `settings` diretamente nos módulos que o definem no nível do módulo.
import app.services.storage as _storage_mod
import app.services.audit as _audit_mod
import app.services.auth as _auth_mod
import app.services.bedrock as _bedrock_mod
import app.services.pdf as _pdf_mod
import app.services.users as _users_mod
import app.services.processor as _processor_mod

for _mod in [_storage_mod, _audit_mod, _auth_mod, _bedrock_mod, _pdf_mod, _users_mod, _processor_mod]:
    if hasattr(_mod, "settings"):
        _mod.settings = _test_settings  # type: ignore[attr-defined]

# Também patch nos módulos de rotas que importam settings
try:
    import app.routes.auth as _routes_auth
    import app.routes.upload as _routes_upload
    import app.main as _main_mod
    for _mod in [_routes_auth, _routes_upload, _main_mod]:
        if hasattr(_mod, "settings"):
            _mod.settings = _test_settings  # type: ignore[attr-defined]
except Exception:
    pass


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Redireciona LOCAL_DATA e caches para um diretório temporário por teste.

    Garante isolamento total: cada teste lê/escreve num diretório limpo,
    sem interferir nos demais ou no ./data/ real do projeto.
    """
    monkeypatch.setattr(_storage_mod, "LOCAL_DATA", tmp_path)
    monkeypatch.setattr(_users_mod, "_LOCAL_STORE", tmp_path / "users.json")

    def _patched_local_file_for(dt):
        return tmp_path / f"audit-{dt.strftime('%Y-%m')}.jsonl"

    monkeypatch.setattr(_audit_mod, "_local_file_for", _patched_local_file_for)

    # Limpa caches em memória entre testes
    _storage_mod._book_meta_cache.clear()
    _storage_mod._revoked_registry_cache.clear()
    _audit_mod._budget_status_cache.clear()
    _audit_mod._monthly_usage_cache.clear()

    yield tmp_path
