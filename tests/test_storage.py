"""Testes para app/services/storage.py — armazenamento local (mock_s3=true)."""
import pytest

from app.services import storage


# ── Path traversal protection ──────────────────────────────────────────────


class TestLocalPath:
    def test_normal_key(self, _isolate_data_dir):
        p = storage._local_path("pdfs/abc.pdf")
        assert p.name == "abc.pdf"
        assert p.parent.name == "pdfs"

    def test_path_traversal_blocked(self):
        with pytest.raises(ValueError, match="inválida"):
            storage._local_path("../etc/passwd")

    def test_double_traversal_blocked(self):
        with pytest.raises(ValueError, match="inválida"):
            storage._local_path("foo/../../bar")


# ── JSON helpers ───────────────────────────────────────────────────────────


class TestJsonHelpers:
    def test_save_load_roundtrip(self):
        data = {"chave": "valor", "numero": 42}
        storage._save_json("test/data.json", data)
        loaded = storage._load_json("test/data.json")
        assert loaded == data

    def test_load_nonexistent_returns_default(self):
        assert storage._load_json("nao/existe.json") is None
        assert storage._load_json("nao/existe.json", dict) == {}
        assert storage._load_json("nao/existe.json", list) == []

    def test_load_callable_default(self):
        result = storage._load_json("nao/existe.json", lambda: {"a": 1})
        assert result == {"a": 1}


# ── PDF CRUD ───────────────────────────────────────────────────────────────


class TestPdfCrud:
    def test_save_and_get(self):
        content = b"%PDF-1.4 fake content"
        key = storage.save_pdf("job123", content)
        assert key == "pdfs/job123.pdf"
        assert storage.get_pdf(key) == content

    def test_delete(self):
        content = b"%PDF-1.4 test"
        key = storage.save_pdf("job_del", content)
        storage.delete_pdf(key)
        with pytest.raises(FileNotFoundError):
            storage.get_pdf(key)

    def test_delete_nonexistent_no_error(self):
        storage.delete_pdf("pdfs/fantasma.pdf")  # não deve lançar exceção


# ── Status CRUD ────────────────────────────────────────────────────────────


class TestStatusCrud:
    def test_save_and_load(self):
        data = {"id": "j1", "status": "processing", "progress_pct": 50}
        storage.save_status("j1", data)
        loaded = storage.load_status("j1")
        assert loaded == data

    def test_load_nonexistent(self):
        assert storage.load_status("inexistente") is None


# ── Checksum registration ─────────────────────────────────────────────────


class TestChecksumRegistration:
    def test_first_registration_succeeds(self):
        result = storage.register_pdf_checksum("abc123", "job1", "Título", "user@ifsp.edu.br")
        assert result is None  # sucesso

    def test_duplicate_returns_existing(self):
        storage.register_pdf_checksum("dup_hash", "job_a", "T1", "a@ifsp.edu.br")
        existing = storage.register_pdf_checksum("dup_hash", "job_b", "T2", "b@ifsp.edu.br")
        assert existing is not None
        assert existing["job_id"] == "job_a"

    def test_find_by_checksum(self):
        storage.register_pdf_checksum("find_me", "job_f", "T", "u@ifsp.edu.br")
        found = storage.find_pdf_by_checksum("find_me")
        assert found["job_id"] == "job_f"

    def test_find_unknown_checksum(self):
        assert storage.find_pdf_by_checksum("desconhecido") is None

    def test_unregister_by_job_id(self):
        storage.register_pdf_checksum("unreg_hash", "job_u", "T", "u@ifsp.edu.br")
        storage.unregister_pdf_checksum_by_job_id("job_u")
        assert storage.find_pdf_by_checksum("unreg_hash") is None

    def test_unregister_nonexistent_no_error(self):
        storage.unregister_pdf_checksum_by_job_id("fantasma")  # não deve lançar


# ── Book meta registry ─────────────────────────────────────────────────────


class TestBookMetaRegistry:
    def test_register_and_get(self):
        storage.register_book_meta(101, "user@ifsp.edu.br")
        registry = storage.get_book_meta_registry()
        assert "101" in registry
        assert registry["101"]["uploaded_by"] == "user@ifsp.edu.br"

    def test_unregister(self):
        storage.register_book_meta(202, "u@ifsp.edu.br")
        storage.unregister_book_meta(202)
        registry = storage.get_book_meta_registry()
        assert "202" not in registry


# ── Revoked registry ──────────────────────────────────────────────────────


class TestRevokedRegistry:
    def test_add_and_get(self):
        entry = {"id": "rev_001", "title": "Portaria X"}
        storage.add_to_revoked_registry(entry)
        registry = storage.get_revoked_registry()
        assert len(registry) == 1
        assert registry[0]["id"] == "rev_001"

    def test_remove(self):
        storage.add_to_revoked_registry({"id": "rev_rm", "title": "T"})
        removed = storage.remove_from_revoked_registry("rev_rm")
        assert removed is not None
        assert removed["id"] == "rev_rm"
        assert storage.get_revoked_registry() == []

    def test_remove_nonexistent(self):
        assert storage.remove_from_revoked_registry("fantasma") is None
