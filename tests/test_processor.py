"""Testes para app/services/processor.py — verificação de extração."""
from app.services.processor import _verify_extraction


class TestVerifyExtraction:
    def test_normal_text(self):
        pages = ["A" * 500, "B" * 600, "C" * 400]
        text = "\n\n---\n\n".join(pages)
        result = _verify_extraction(text)
        assert result["pages"] == 3
        assert result["total_chars"] == 1500
        assert result["blank_pages"] == 0
        assert result["warning"] is None

    def test_all_blank_pages(self):
        text = "\n\n---\n\n".join(["", "   ", ""])
        result = _verify_extraction(text)
        assert result["blank_pages"] == result["pages"]
        assert result["warning"] is not None
        assert "Nenhum texto" in result["warning"]

    def test_majority_blank(self):
        pages = ["A" * 500] + [""] * 5
        text = "\n\n---\n\n".join(pages)
        result = _verify_extraction(text)
        assert result["blank_pages"] > result["pages"] * 0.5
        assert result["warning"] is not None
        assert "sem texto" in result["warning"]

    def test_low_density(self):
        # Texto com >50 chars por página (não é blank) mas avg < 100
        pages = ["A" * 60] * 5
        text = "\n\n---\n\n".join(pages)
        result = _verify_extraction(text)
        assert result["blank_pages"] == 0
        assert result["avg_chars_per_page"] < 100
        assert result["warning"] is not None
        assert "Baixa densidade" in result["warning"]

    def test_single_page_no_low_density_warning(self):
        """Low density warning só aparece com mais de 1 página."""
        result = _verify_extraction("A" * 60)
        assert result["pages"] == 1
        assert result["warning"] is None
