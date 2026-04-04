"""Testes para app/services/pdf.py — funções puras de processamento de texto."""
from app.services.pdf import (
    _merge_broken_paragraphs,
    _bold_article_identifiers,
    _remove_signature_artifacts,
    detect_structural_anomalies,
    _roman_to_int,
    _int_to_roman,
)


# ── _roman_to_int / _int_to_roman ──────────────────────────────────────────


class TestRomanNumerals:
    def test_basic_values(self):
        assert _roman_to_int("I") == 1
        assert _roman_to_int("V") == 5
        assert _roman_to_int("X") == 10

    def test_subtractive_notation(self):
        assert _roman_to_int("IV") == 4
        assert _roman_to_int("IX") == 9
        assert _roman_to_int("XL") == 40

    def test_complex_values(self):
        assert _roman_to_int("XIV") == 14
        assert _roman_to_int("XXIII") == 23

    def test_case_insensitive(self):
        assert _roman_to_int("iv") == 4
        assert _roman_to_int("xiv") == 14

    def test_roundtrip(self):
        for n in range(1, 40):
            assert _roman_to_int(_int_to_roman(n)) == n

    def test_int_to_roman_known(self):
        assert _int_to_roman(1) == "I"
        assert _int_to_roman(4) == "IV"
        assert _int_to_roman(9) == "IX"
        assert _int_to_roman(14) == "XIV"
        assert _int_to_roman(2024) == "MMXXIV"


# ── _merge_broken_paragraphs ───────────────────────────────────────────────


class TestMergeBrokenParagraphs:
    def test_merges_when_no_terminal_punct_and_lowercase_next(self):
        text = "o servidor deverá\n\ncumprir o prazo"
        result = _merge_broken_paragraphs(text)
        assert result == "o servidor deverá cumprir o prazo"

    def test_no_merge_when_ends_with_period(self):
        text = "fim do artigo.\n\nInício do próximo"
        result = _merge_broken_paragraphs(text)
        assert "fim do artigo." in result
        assert "Início do próximo" in result
        assert "\n\n" in result

    def test_no_merge_when_next_starts_uppercase(self):
        text = "texto sem ponto\n\nNovo parágrafo"
        result = _merge_broken_paragraphs(text)
        assert "\n\n" in result

    def test_no_merge_when_next_is_list_item(self):
        text = "itens a seguir\n\n- primeiro item"
        result = _merge_broken_paragraphs(text)
        assert "\n\n" in result

    def test_no_merge_with_roman_list(self):
        text = "conforme disposto\n\nI) primeiro inciso"
        result = _merge_broken_paragraphs(text)
        assert "\n\n" in result

    def test_chained_merge(self):
        text = "início da\n\nfrase que\n\ncontinua aqui."
        result = _merge_broken_paragraphs(text)
        assert result == "início da frase que continua aqui."

    def test_empty_text(self):
        assert _merge_broken_paragraphs("") == ""

    def test_single_paragraph(self):
        text = "Apenas um parágrafo."
        assert _merge_broken_paragraphs(text) == text

    def test_no_merge_when_ends_with_colon(self):
        text = "conforme a seguir:\n\nalinea a"
        result = _merge_broken_paragraphs(text)
        assert "\n\n" in result


# ── _bold_article_identifiers ──────────────────────────────────────────────


class TestBoldArticleIdentifiers:
    def test_art_numbered(self):
        result = _bold_article_identifiers("Art. 1º Esta resolução...")
        assert result.startswith("**Art. 1º**")

    def test_paragrafo_unico(self):
        result = _bold_article_identifiers("Parágrafo único O disposto...")
        assert result.startswith("**Parágrafo único**")

    def test_section_symbol(self):
        result = _bold_article_identifiers("§ 2º Para os efeitos...")
        assert result.startswith("**§ 2º**")

    def test_already_bold_not_doubled(self):
        text = "**Art. 1º** Esta resolução..."
        result = _bold_article_identifiers(text)
        assert result == text  # sem alteração

    def test_mid_line_not_affected(self):
        text = "Conforme o Art. 1º desta resolução"
        result = _bold_article_identifiers(text)
        # Art. 1º não está no início da linha, não deve ser negritado
        assert "**Art. 1º**" not in result

    def test_multiline(self):
        text = "Art. 1º Primeiro.\n\nArt. 2º Segundo."
        result = _bold_article_identifiers(text)
        assert "**Art. 1º**" in result
        assert "**Art. 2º**" in result

    def test_indented_article(self):
        text = "  Art. 3º Com recuo."
        result = _bold_article_identifiers(text)
        assert "**Art. 3º**" in result


# ── _remove_signature_artifacts ────────────────────────────────────────────


class TestRemoveSignatureArtifacts:
    def test_removes_signature_line(self):
        text = "Art. 1º Texto.\n\nAssinado digitalmente por FULANO"
        result = _remove_signature_artifacts(text)
        assert "Assinado digitalmente" not in result
        assert "Art. 1º" in result

    def test_removes_icp_brasil_line(self):
        text = "Conteúdo.\nICP-Brasil\nMais conteúdo."
        result = _remove_signature_artifacts(text)
        assert "ICP-Brasil" not in result
        assert "Conteúdo." in result

    def test_removes_suap_block(self):
        text = (
            "Art. 1º Texto válido.\n"
            "emitido pelo SUAP em 09/11/2025, às 14:30\n"
            "Para comprovar autenticidade...\n"
            "código verificador: 123456"
        )
        result = _remove_signature_artifacts(text)
        assert "SUAP" not in result
        assert "Art. 1º" in result

    def test_block_removal_stops_at_page_separator(self):
        text = (
            "Página 1.\n"
            "emitido pelo SUAP em 01/01/2025\n"
            "lixo da assinatura"
            "\n\n---\n\n"
            "Página 2 — conteúdo válido."
        )
        result = _remove_signature_artifacts(text)
        assert "Página 2" in result
        assert "SUAP" not in result

    def test_clean_text_passes_through(self):
        text = "Art. 1º Esta resolução dispõe sobre..."
        assert _remove_signature_artifacts(text) == text

    def test_codigo_verificador_removed(self):
        text = "Texto.\nCódigo verificador: ABC123\nMais texto."
        result = _remove_signature_artifacts(text)
        assert "verificador" not in result


# ── detect_structural_anomalies ────────────────────────────────────────────


class TestDetectStructuralAnomalies:
    def test_clean_sequence(self):
        text = "## CAPÍTULO I\n\n## CAPÍTULO II\n\n## CAPÍTULO III"
        assert detect_structural_anomalies(text) == []

    def test_gap_detected(self):
        text = "## CAPÍTULO I\n\n## CAPÍTULO III"
        anomalies = detect_structural_anomalies(text)
        assert len(anomalies) == 1
        assert "Lacuna" in anomalies[0]
        assert "CAPÍTULO II" in anomalies[0]

    def test_duplicate_detected(self):
        text = "## CAPÍTULO II\n\n## CAPÍTULO II"
        anomalies = detect_structural_anomalies(text)
        assert len(anomalies) == 1
        assert "duplicada" in anomalies[0].lower()

    def test_out_of_order_detected(self):
        text = "## CAPÍTULO III\n\n## CAPÍTULO II"
        anomalies = detect_structural_anomalies(text)
        assert len(anomalies) == 1
        assert "fora de ordem" in anomalies[0].lower()

    def test_single_chapter_no_anomaly(self):
        text = "## CAPÍTULO I\n\nConteúdo."
        assert detect_structural_anomalies(text) == []

    def test_no_chapters(self):
        text = "Texto sem capítulos."
        assert detect_structural_anomalies(text) == []

    def test_large_gap(self):
        text = "## CAPÍTULO I\n\n## CAPÍTULO V"
        anomalies = detect_structural_anomalies(text)
        assert len(anomalies) == 1
        assert "II" in anomalies[0]
        assert "III" in anomalies[0]
        assert "IV" in anomalies[0]
