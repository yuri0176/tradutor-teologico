"""Motor Anthropic Claude (SDK anthropic, pago por uso)."""

from __future__ import annotations

import base64
import os
from typing import Iterator

import anthropic

from ..common import Restart, TranslationReport
from ..prompts import SYSTEM_PROMPT

NAME = "claude"
LABEL = "Claude (pago)"
KEY_ENV = "ANTHROPIC_API_KEY"
KEY_HELP = "Crie a chave em console.anthropic.com (cobrança por uso, à parte da assinatura do Claude)"
DEFAULT_MODEL = "claude-opus-5-5"
MAX_OUTPUT_TOKENS = 64000

# Preço por milhão de tokens (entrada, saída) em US$, para a estimativa exibida
# antes de traduzir um livro. Confira os valores atuais em anthropic.com/pricing.
_PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Modelos que aceitam o fallback automático em caso de recusa dos
# classificadores de segurança (beta server-side-fallback-2026-07-01).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

API_ERRORS = (anthropic.APIError,)


def get_model() -> str:
    return os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def price_per_mtok() -> tuple[float, float] | None:
    return _PRICES.get(get_model())


def make_client(api_key: str) -> anthropic.Anthropic:
    # O SDK já repete sozinho erros de conexão, 429 e 5xx (inclusive "overloaded").
    return anthropic.Anthropic(api_key=api_key, max_retries=4)


def document_content(pdf_bytes: bytes, instructions: str) -> list[dict]:
    """Pedido com o PDF anexado (lido pelo modelo como imagem) + instruções."""
    return [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": instructions},
    ]


def _request_kwargs(model: str, effort: str) -> dict:
    kwargs: dict = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        # System prompt fixo e marcado para cache: os trechos seguintes de um
        # mesmo texto o reaproveitam a custo reduzido.
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
    }
    if "haiku" not in model:
        kwargs["output_config"] = {"effort": effort}
    if model in _FALLBACK_MODELS:
        kwargs["betas"] = [_FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def stream(
    client: anthropic.Anthropic,
    content: str | list,
    effort: str,
    report: TranslationReport,
    label: str,
) -> Iterator[str | Restart]:
    """Envia um pedido de tradução e devolve o texto em streaming.

    Recusas e cortes por limite de tamanho viram avisos em `report.warnings`."""
    kwargs = _request_kwargs(get_model(), effort)
    messages = [{"role": "user", "content": content}]
    stream_ctx = (
        client.beta.messages.stream(messages=messages, **kwargs)
        if "betas" in kwargs
        else client.messages.stream(messages=messages, **kwargs)
    )
    with stream_ctx as s:
        for text in s.text_stream:
            yield text
        final = s.get_final_message()

    report.model = get_model()
    usage = final.usage
    report.input_tokens += (
        (usage.input_tokens or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    )
    report.output_tokens += usage.output_tokens or 0

    if final.stop_reason == "refusal":
        report.warnings.append(f"{label}: o Claude recusou este trecho, que ficou sem tradução ou incompleto.")
    elif final.stop_reason == "max_tokens":
        report.warnings.append(
            f"{label}: a resposta atingiu o limite de tamanho e foi cortada. Traduza esse trecho separadamente."
        )


def describe_error(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "Chave do Claude inválida. Confira o valor de ANTHROPIC_API_KEY."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "A chave do Claude não tem permissão para usar este modelo."
    if isinstance(exc, anthropic.NotFoundError):
        return f"O modelo \"{get_model()}\" não foi encontrado. Ajuste ANTHROPIC_MODEL no .env."
    if isinstance(exc, anthropic.RateLimitError):
        return "Limite de uso do Claude atingido. Aguarde um minuto e tente de novo."
    if isinstance(exc, anthropic.BadRequestError):
        msg = exc.message or ""
        if "credit" in msg.lower() or "billing" in msg.lower():
            return "Sua conta da Anthropic está sem créditos. Adicione créditos em console.anthropic.com."
        return f"O Claude recusou o pedido: {msg}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Erro da API do Claude ({exc.status_code}). Tente novamente em instantes."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Sem conexão com a API da Anthropic. Verifique a internet."
    return f"Erro inesperado do Claude: {exc}"
