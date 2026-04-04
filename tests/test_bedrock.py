"""Testes para app/services/bedrock.py — sanitização, retry e builders de prompt."""
from unittest.mock import patch, MagicMock

import pytest
from botocore.exceptions import ClientError

from app.services.bedrock import (
    _sanitize_for_prompt,
    _with_retry,
    _build_prompt,
    _build_revocation_prompt,
    _build_multimodal_prompt,
)


# ── _sanitize_for_prompt ───────────────────────────────────────────────────


class TestSanitizeForPrompt:
    def test_escapes_opening_tag(self):
        assert "<_documento" in _sanitize_for_prompt("<documento>")

    def test_escapes_closing_tag(self):
        assert "</_documento" in _sanitize_for_prompt("</documento>")

    def test_normal_text_unchanged(self):
        text = "Art. 1º Esta resolução dispõe sobre normativos."
        assert _sanitize_for_prompt(text) == text

    def test_case_insensitive(self):
        assert "<_documento" in _sanitize_for_prompt("<DOCUMENTO>")
        assert "</_documento" in _sanitize_for_prompt("</Documento>")

    def test_mixed_content(self):
        text = "Texto <documento>injetado</documento> aqui."
        result = _sanitize_for_prompt(text)
        assert "<documento>" not in result
        assert "</documento>" not in result


# ── _with_retry ────────────────────────────────────────────────────────────


def _make_client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "InvokeModel")


class TestWithRetry:
    @patch("app.services.bedrock.time.sleep")
    def test_success_first_attempt(self, mock_sleep):
        result = _with_retry(lambda: "ok")
        assert result == "ok"
        mock_sleep.assert_not_called()

    @patch("app.services.bedrock.time.sleep")
    def test_retries_on_throttling(self, mock_sleep):
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _make_client_error("ThrottlingException")
            return "recovered"

        result = _with_retry(fn)
        assert result == "recovered"
        assert mock_sleep.call_count == 2

    @patch("app.services.bedrock.time.sleep")
    def test_non_retryable_error_raises_immediately(self, mock_sleep):
        def fn():
            raise _make_client_error("ValidationException")

        with pytest.raises(ClientError) as exc_info:
            _with_retry(fn)
        assert exc_info.value.response["Error"]["Code"] == "ValidationException"
        mock_sleep.assert_not_called()

    @patch("app.services.bedrock.time.sleep")
    def test_max_attempts_exceeded(self, mock_sleep):
        def fn():
            raise _make_client_error("ThrottlingException")

        with pytest.raises(ClientError):
            _with_retry(fn)
        assert mock_sleep.call_count == 2  # 3 tentativas, 2 sleeps entre elas


# ── Prompt builders ────────────────────────────────────────────────────────


class TestBuildPrompt:
    def test_contains_title_and_text(self):
        prompt = _build_prompt("Portaria 42", "Texto do normativo")
        assert "Portaria 42" in prompt
        assert "Texto do normativo" in prompt

    def test_contains_documento_tags(self):
        prompt = _build_prompt("T", "Conteúdo")
        assert "<documento>" in prompt
        assert "</documento>" in prompt


class TestBuildRevocationPrompt:
    def test_contains_title_and_text(self):
        prompt = _build_revocation_prompt("Resolução 10", "Texto completo")
        assert "Resolução 10" in prompt
        assert "Texto completo" in prompt

    def test_asks_for_structured_fields(self):
        prompt = _build_revocation_prompt("T", "C")
        assert "**Tipo:**" in prompt
        assert "**Número:**" in prompt
        assert "**Data de publicação:**" in prompt
        assert "**Objetivo:**" in prompt


class TestBuildMultimodalPrompt:
    def test_page_count_in_prompt(self):
        prompt = _build_multimodal_prompt(4)
        assert "4 página(s)" in prompt

    def test_continuation_adds_note(self):
        prompt = _build_multimodal_prompt(2, is_continuation=True)
        assert "ATENÇÃO" in prompt
        assert "meio de um artigo" in prompt

    def test_no_continuation_by_default(self):
        prompt = _build_multimodal_prompt(2, is_continuation=False)
        assert "ATENÇÃO" not in prompt
