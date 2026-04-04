"""Testes para app/services/audit.py — HMAC, logging e agregações de tokens."""
import json

from app.services import audit


# ── HMAC ───────────────────────────────────────────────────────────────────


class TestHmac:
    def test_compute_is_deterministic(self):
        h1 = audit._compute_hmac("payload")
        h2 = audit._compute_hmac("payload")
        assert h1 == h2

    def test_different_payload_different_hmac(self):
        assert audit._compute_hmac("a") != audit._compute_hmac("b")

    def test_verify_valid_entry(self):
        entry = {"ts": "2025-01-01T00:00:00", "user": "u@test.com", "action": "upload"}
        payload = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        entry["_hmac"] = audit._compute_hmac(payload)
        assert audit._verify_hmac(entry) is True

    def test_verify_tampered_entry(self):
        entry = {"ts": "2025-01-01T00:00:00", "user": "u@test.com", "action": "upload"}
        payload = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        entry["_hmac"] = audit._compute_hmac(payload)
        entry["action"] = "revogar"  # tampered
        assert audit._verify_hmac(entry) is False

    def test_verify_entry_without_hmac(self):
        entry = {"ts": "2025-01-01", "user": "u", "action": "x"}
        assert audit._verify_hmac(entry) is False


# ── _sum_tokens_from_extra ─────────────────────────────────────────────────


class TestSumTokensFromExtra:
    def test_complete_format(self):
        extra = {
            "extraction_input_tokens": 1000,
            "extraction_output_tokens": 200,
            "faq_input_tokens": 500,
            "faq_output_tokens": 100,
        }
        assert audit._sum_tokens_from_extra(extra) == 1800

    def test_combined_format(self):
        extra = {"input_tokens": 3000, "output_tokens": 600}
        assert audit._sum_tokens_from_extra(extra) == 3600

    def test_legacy_format(self):
        extra = {"tokens": 5000}
        assert audit._sum_tokens_from_extra(extra) == 5000

    def test_empty_dict(self):
        assert audit._sum_tokens_from_extra({}) == 0


# ── log + recent roundtrip ─────────────────────────────────────────────────


class TestLogAndRecent:
    def test_log_and_read_back(self):
        audit.log("user@ifsp.edu.br", "upload", "Portaria 42")
        entries = audit.recent(limit=10)
        assert len(entries) >= 1
        entry = entries[0]
        assert entry["user"] == "user@ifsp.edu.br"
        assert entry["action"] == "upload"
        assert "_hmac" in entry
        assert entry.get("_verified") is True

    def test_log_with_extra(self):
        audit.log("u@test.com", "processar", "Resolução 10", extra={"input_tokens": 500})
        entries = audit.recent(limit=10)
        processar = [e for e in entries if e["action"] == "processar"]
        assert len(processar) >= 1
        assert processar[0]["extra"]["input_tokens"] == 500

    def test_log_warn_level(self):
        audit.log("u@test.com", "session_anomaly", "IP divergente", level="warn")
        entries = audit.recent(limit=10)
        warns = [e for e in entries if e.get("level") == "warn"]
        assert len(warns) >= 1

    def test_deduplication(self):
        """Entradas duplicadas (mesma linha JSON) devem ser deduplicadas."""
        audit.log("dup@test.com", "upload", "Dup")
        # recent() já faz deduplicação internamente
        entries = audit.recent(limit=100)
        dup_entries = [e for e in entries if e["user"] == "dup@test.com"]
        assert len(dup_entries) == 1


# ── daily_budget_status ────────────────────────────────────────────────────


class TestDailyBudgetStatus:
    def test_no_limit_returns_inactive(self):
        # Budget padrão: daily_limit=0
        status = audit.daily_budget_status()
        assert status["active"] is False
        assert status["exhausted"] is False

    def test_with_limit_and_no_usage(self):
        from app.services import storage
        storage.save_budget(100_000, "admin@test.com")
        audit.invalidate_budget_status_cache()
        status = audit.daily_budget_status()
        assert status["active"] is True
        assert status["usage"] == 0
        assert status["pct"] == 0.0
        assert status["exhausted"] is False
