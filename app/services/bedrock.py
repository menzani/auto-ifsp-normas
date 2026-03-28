"""
Geração de FAQ via Amazon Bedrock (Claude Haiku).

Documentação da API:
  https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html
"""
import json
import logging
import re

import boto3

from app.config import get_settings

settings = get_settings()

# Normativos longos são truncados antes de enviar ao modelo.
# 80 000 caracteres ≈ 20 000 tokens — bem abaixo do limite do Haiku (200k tokens).
_MAX_INPUT_CHARS = 80_000

_bedrock_client = None
_bedrock_client_lock = __import__("threading").Lock()


def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is not None:
        return _bedrock_client
    with _bedrock_client_lock:
        if _bedrock_client is None:
            _bedrock_client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    return _bedrock_client


def generate_faq(markdown_text: str, title: str) -> str:
    """
    Gera FAQ em Markdown a partir do texto extraído do normativo.
    Retorna uma string Markdown pronta para ser salva como página no Bookstack.
    """
    text = markdown_text[:_MAX_INPUT_CHARS]
    if len(markdown_text) > _MAX_INPUT_CHARS:
        text += "\n\n*[Texto truncado — documento excede o limite de processamento.]*"

    client = _get_bedrock_client()

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "messages": [
            {"role": "user", "content": _build_prompt(title, text)},
        ],
    }

    try:
        response = client.invoke_model(
            modelId=settings.bedrock_model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]
    except Exception:
        logging.getLogger(__name__).exception("Erro ao gerar FAQ via Bedrock")
        raise RuntimeError("Não foi possível gerar o FAQ. Verifique as permissões do Bedrock e tente novamente.")


def generate_revocation_summary(markdown_text: str, title: str) -> str:
    """
    Gera um resumo estruturado de um normativo para a página de revogados.
    Retorna Markdown com tipo, número, data de publicação e objetivo.
    """
    text = markdown_text[:_MAX_INPUT_CHARS]
    if len(markdown_text) > _MAX_INPUT_CHARS:
        text += "\n\n*[Texto truncado — documento excede o limite de processamento.]*"

    client = _get_bedrock_client()

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "messages": [
            {"role": "user", "content": _build_revocation_prompt(title, text)},
        ],
    }

    try:
        response = client.invoke_model(
            modelId=settings.bedrock_model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        raw = result["content"][0]["text"]
    except Exception:
        logging.getLogger(__name__).exception("Erro ao gerar resumo de revogação via Bedrock")
        raise RuntimeError("Não foi possível gerar o resumo. Verifique as permissões do Bedrock e tente novamente.")
    # Garante linha em branco entre campos **Field:** para renderização correta em Markdown
    raw = re.sub(r'(\*\*[^:\n]+:\*\*[^\n]+)\n(\*\*)', r'\1\n\n\2', raw)
    return raw


def structure_markdown(text: str, on_progress=None) -> str:
    """
    Corrige artefatos de extração de PDF e estrutura o texto em Markdown semântico
    em uma única etapa de IA.

    Artefatos corrigidos: acentos quebrados, hifenizações incorretas, espaços indevidos.
    Estrutura adicionada: headings (#, ##) apenas onde fazem sentido semântico
    (capítulos, artigos, seções) — linhas de atribuição como "O REITOR" e timbres
    institucionais não recebem heading.

    Processa em chunks de 8.000 chars. Retorna o texto original em caso de falha.
    on_progress(current, total) é chamado após cada chunk processado.
    """
    _CHUNK = 8_000
    source = text[:_MAX_INPUT_CHARS]
    chunks = [source[i:i+_CHUNK] for i in range(0, len(source), _CHUNK)]
    structured = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        is_continuation = i > 1
        structured.append(_structure_chunk(chunk, is_continuation))
        if on_progress:
            on_progress(i, total)
    if len(text) > _MAX_INPUT_CHARS:
        structured.append(text[_MAX_INPUT_CHARS:])
    return "".join(structured)


def _structure_chunk(chunk: str, is_continuation: bool) -> str:
    client = _get_bedrock_client()
    continuation_note = (
        "Este é um trecho de continuação do documento — não adicione heading de título geral.\n\n"
        if is_continuation else ""
    )
    prompt = (
        f"{continuation_note}"
        "Você recebe texto bruto extraído de um normativo institucional brasileiro (PDF governamental). "
        "Faça duas coisas simultaneamente:\n\n"
        "1. CORRIJA artefatos de extração: acentos quebrados, palavras partidas por hifenização, "
        "espaços onde deveriam haver letras acentuadas (ex: 'Poli ca' → 'Política').\n\n"
        "2. ESTRUTURE em Markdown seguindo rigorosamente esta hierarquia:\n\n"
        "   # (H1) — SOMENTE para 'TÍTULO I', 'TÍTULO II'… quando o documento\n"
        "      tiver divisão explícita em Títulos numerados.\n\n"
        "   ## (H2) — SOMENTE para 'CAPÍTULO I', 'CAPÍTULO II'…\n"
        "      Inclua sempre o nome do capítulo no mesmo heading:\n"
        "      CORRETO:   ## CAPÍTULO I — DO OBJETIVO\n"
        "      ERRADO:    ## CAPÍTULO I  (linha separada com o nome)\n\n"
        "   ### (H3) — SOMENTE para Seções dentro de capítulos:\n"
        "      ex: '### Seção I — Do Acesso', '### Seção II — Da Gestão'\n"
        "      Inclua sempre o nome da seção no mesmo heading.\n\n"
        "   SEM heading (texto normal ou lista):\n"
        "   - Artigos (Art. 1º, Art. 2º…): parágrafo de texto normal\n"
        "   - Parágrafos (§ 1º, Parágrafo único): parágrafo de texto normal\n"
        "   - Timbres: 'MINISTÉRIO DA EDUCAÇÃO', 'INSTITUTO FEDERAL…'\n"
        "   - Atribuições: 'O REITOR', 'O DIRETOR', 'A REITORA'\n"
        "   - Ementa, preâmbulo, RESOLVE:, CONSIDERANDO\n"
        "   - Assinaturas, datas, locais\n\n"
        "   LISTAS — formate como lista Markdown (um item por linha):\n"
        "   - Incisos (I -, II -, III -…): cada inciso vira um item de lista '- **I** — texto'\n"
        "     ex: '- **I** — 2FA/MFA: descrição do termo;'\n"
        "         '- **II** — Acesso Remoto: descrição;'\n"
        "   - Alíneas (a), b), c)…): sub-itens indentados '  - **a)** texto'\n"
        "   - Se incisos ou alíneas estiverem concatenados num único parágrafo,\n"
        "     separe-os — cada marcador (I -, II -, a), b)) inicia um novo item.\n\n"
        "   Preserve separadores de página (linhas '---') como estão.\n\n"
        "Retorne APENAS o texto corrigido e estruturado, sem explicações, sem comentários.\n\n"
        + chunk
    )
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 9_000,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        response = client.invoke_model(
            modelId=settings.bedrock_model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]
    except Exception:
        logging.getLogger(__name__).exception("Erro ao estruturar chunk via Bedrock")
        return chunk


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
