"""
OCR de páginas de imagem via AWS Textract.

Usado como fallback em extract_pages() quando PyMuPDF não detecta texto
(páginas escaneadas sem camada de texto).

Permissão IAM necessária na EC2 Role: textract:DetectDocumentText
"""
import logging

import boto3

from app.config import get_settings

settings = get_settings()

_textract_client = None
_textract_client_lock = __import__("threading").Lock()


def _get_textract_client():
    global _textract_client
    if _textract_client is not None:
        return _textract_client
    with _textract_client_lock:
        if _textract_client is None:
            _textract_client = boto3.client("textract", region_name=settings.aws_region)
    return _textract_client


def ocr_page_image(image_bytes: bytes) -> str:
    """
    Extrai texto de uma imagem de página via AWS Textract DetectDocumentText.
    Retorna string vazia em caso de falha — o chamador decide o fallback.
    """
    try:
        client = _get_textract_client()
        response = client.detect_document_text(Document={"Bytes": image_bytes})
        lines = [
            block["Text"]
            for block in response.get("Blocks", [])
            if block["BlockType"] == "LINE"
        ]
        return "\n".join(lines)
    except Exception:
        logging.getLogger(__name__).exception("Erro ao processar OCR via Textract")
        return ""
