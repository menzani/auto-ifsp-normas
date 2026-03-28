"""
Geração de FAQ via Amazon Bedrock (Claude Haiku).

Documentação da API:
  https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html
"""
import json

import boto3

from app.config import get_settings

settings = get_settings()

# Normativos longos são truncados antes de enviar ao modelo.
# 80 000 caracteres ≈ 20 000 tokens — bem abaixo do limite do Haiku (200k tokens).
_MAX_INPUT_CHARS = 80_000


def generate_faq(markdown_text: str, title: str) -> str:
    """
    Gera FAQ em Markdown a partir do texto extraído do normativo.
    Retorna uma string Markdown pronta para ser salva como página no Bookstack.
    """
    text = markdown_text[:_MAX_INPUT_CHARS]
    if len(markdown_text) > _MAX_INPUT_CHARS:
        text += "\n\n*[Texto truncado — documento excede o limite de processamento.]*"

    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "messages": [
            {"role": "user", "content": _build_prompt(title, text)},
        ],
    }

    response = client.invoke_model(
        modelId=settings.bedrock_model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )

    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


def generate_revocation_summary(markdown_text: str, title: str) -> str:
    """
    Gera um resumo estruturado de um normativo para a página de revogados.
    Retorna Markdown com tipo, número, data de publicação e objetivo.
    """
    text = markdown_text[:_MAX_INPUT_CHARS]
    if len(markdown_text) > _MAX_INPUT_CHARS:
        text += "\n\n*[Texto truncado — documento excede o limite de processamento.]*"

    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "messages": [
            {"role": "user", "content": _build_revocation_prompt(title, text)},
        ],
    }

    response = client.invoke_model(
        modelId=settings.bedrock_model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )

    result = json.loads(response["body"].read())
    raw = result["content"][0]["text"]
    # Garante linha em branco entre campos **Field:** para renderização correta em Markdown
    import re as _re
    raw = _re.sub(r'(\*\*[^:\n]+:\*\*[^\n]+)\n(\*\*)', r'\1\n\n\2', raw)
    return raw


def generate_section_titles(markdown_text: str, title: str) -> list[str]:
    """
    Identifica os títulos das seções/capítulos principais do normativo.
    Retorna lista de strings com os títulos EXATAMENTE como aparecem no texto.
    """
    text = markdown_text[:_MAX_INPUT_CHARS]
    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": _build_sections_prompt(title, text)},
        ],
    }

    response = client.invoke_model(
        modelId=settings.bedrock_model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )

    result = json.loads(response["body"].read())
    output = result["content"][0]["text"]
    return [line.strip() for line in output.splitlines() if line.strip()]


def _build_revocation_prompt(title: str, text: str) -> str:
    return f"""Você é um assistente especializado em normativos institucionais do IFSP \
(Instituto Federal de Educação, Ciência e Tecnologia de São Paulo).

Com base no normativo transcrito abaixo, extraia as seguintes informações e formate em Markdown.
Separe cada campo com uma linha em branco.

**Tipo:** (ex: Portaria, Resolução, Instrução Normativa, Edital, Deliberação, etc.)

**Número:** (ex: nº 42/2023)

**Data de publicação:** (no formato DD/MM/AAAA — se não encontrada, escreva "Não informada")

**Objetivo:** (um parágrafo curto e objetivo descrevendo a finalidade do normativo)

Responda APENAS com o bloco de informações acima, sem introdução, sem comentários adicionais.

**Normativo:** {title}

---

{text}

---

Extraia as informações agora:"""


def _build_sections_prompt(title: str, text: str) -> str:
    return f"""Você é um assistente especializado em normativos institucionais do IFSP \
(Instituto Federal de Educação, Ciência e Tecnologia de São Paulo).

Analise o normativo abaixo e liste os títulos das seções ou capítulos principais, um por linha.
Copie os títulos EXATAMENTE como aparecem no texto (não modifique, não traduza, não resuma).
Retorne APENAS os títulos, sem numeração adicional, sem explicações, sem comentários.

Exemplo de resposta:
CAPÍTULO I - DAS DISPOSIÇÕES GERAIS
CAPÍTULO II - DO ÂMBITO DE APLICAÇÃO
CAPÍTULO III - DOS PRINCÍPIOS

**Normativo:** {title}

---

{text}

---

Títulos das seções/capítulos principais:"""


def _build_prompt(title: str, text: str) -> str:
    return f"""Você é um assistente especializado em normativos institucionais do IFSP \
(Instituto Federal de Educação, Ciência e Tecnologia de São Paulo).

Com base no normativo transcrito abaixo, gere um FAQ (Perguntas Frequentes) em Markdown.

**Diretrizes:**
- Crie entre 5 e 10 perguntas que servidores, alunos ou gestores fariam sobre este documento
- As respostas devem ser diretas, em linguagem simples e acessível, sem jargão jurídico desnecessário
- Não invente informações que não estejam no texto; se algo não estiver claro, diga isso na resposta
- Formato obrigatório para cada item:

**Pergunta?**

Resposta objetiva.

---

**Normativo:** {title}

---

{text}

---

Gere o FAQ agora, sem introdução ou comentários adicionais:"""
