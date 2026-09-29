"""Motor Google Gemini (SDK google-genai, plano gratuito do AI Studio)."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta
from typing import Iterator

import httpx
from google import genai
from google.genai import errors, types

from ..common import RESTART, Restart, TranslationReport
from ..prompts import SYSTEM_PROMPT

NAME = "gemini"
LABEL = "Gemini (grátis)"
KEY_ENV = "GEMINI_API_KEY"
KEY_HELP = "Crie a chave grátis em aistudio.google.com/apikey"
DEFAULT_MODEL = "gemini-3.5-flash"
MAX_OUTPUT_TOKENS = 65536
PRICE_PER_MTOK = None  # plano gratuito

# Cada modelo tem a sua própria cota diária gratuita. Quando a do modelo principal
# acaba, o app segue com estes, na ordem (só os que a sua chave enxerga). Para
# usar outros, ou desligar, defina GEMINI_FALLBACK_MODELS no .env (vazio = desliga).
DEFAULT_FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]

# Quantas vezes esperar e repetir quando o Gemini responde "limite por minuto"
# (429) ou "servidor sobrecarregado" (5xx / queda de conexão).
MAX_RATE_LIMIT_RETRIES = 6
MAX_SERVER_RETRIES = 4

# Textos acadêmicos de teologia tratam de violência, sexualidade, guerra etc.
# Os filtros ficam só no nível "alto" para não barrar discussão legítima.
_SAFETY = [
    types.SafetySetting(category=category, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH)
    for category in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]

_FINISH_WARNINGS = {
    types.FinishReason.MAX_TOKENS: "a resposta atingiu o limite de tamanho e foi cortada. Traduza esse trecho separadamente.",
    types.FinishReason.SAFETY: "o filtro de segurança do Gemini interrompeu este trecho.",
    types.FinishReason.RECITATION: (
        "o Gemini interrompeu este trecho por considerá-lo reprodução de texto já publicado "
        "(RECITATION), mesmo após uma nova tentativa. Refaça esse capítulo com o Claude."
    ),
    types.FinishReason.PROHIBITED_CONTENT: "o Gemini bloqueou este trecho (conteúdo proibido pela política do Google).",
    types.FinishReason.BLOCKLIST: "o Gemini bloqueou este trecho (termo em lista de bloqueio).",
    types.FinishReason.SPII: "o Gemini bloqueou este trecho (dados pessoais sensíveis).",
    types.FinishReason.OTHER: "o Gemini interrompeu este trecho sem informar o motivo.",
}

# Acrescentado ao pedido na nova tentativa após um corte por RECITATION. A causa
# típica é uma citação (bíblica ou de outro autor) sair idêntica a uma
# tradução publicada; a correção é traduzi-la com redação própria.
_RECITATION_HINT = (
    "Observação: uma tentativa anterior de traduzir este trecho foi interrompida porque "
    "parte da resposta reproduzia literalmente um texto já publicado. Refaça a tradução "
    "completa do trecho com redação própria: traduza as citações bíblicas e de outros "
    "autores diretamente da versão citada pelo autor, sem copiar literalmente nenhuma "
    "tradução publicada em português."
)


class DailyQuotaExceeded(Exception):
    """Cota diária do plano gratuito esgotada em todos os modelos disponíveis."""

    def __init__(self, detail: str = ""):
        super().__init__(detail)
        self.detail = detail


API_ERRORS = (errors.APIError, DailyQuotaExceeded, httpx.TransportError, ConnectionError)


def get_model() -> str:
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def make_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)


def document_content(pdf_bytes: bytes, instructions: str) -> list:
    """Pedido com o PDF anexado (lido pelo modelo como imagem) + instruções."""
    return [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), instructions]


def _thinking_config(model: str, effort: str) -> types.ThinkingConfig | None:
    """Qualidade (Rápida / Alta / Máxima) -> profundidade de raciocínio.

    Gemini 2.5 usa `thinking_budget` (tokens); Gemini 3.x usa `thinking_level`."""
    if model.startswith("gemini-2.5"):
        budget = {"medium": 1024, "high": -1, "xhigh": 24576}[effort]  # -1 = dinâmico
        return types.ThinkingConfig(thinking_budget=budget)
    if model.startswith("gemini-3"):
        level = {"medium": types.ThinkingLevel.LOW, "high": None, "xhigh": types.ThinkingLevel.HIGH}[effort]
        return types.ThinkingConfig(thinking_level=level) if level else None
    return None


def _config(model: str, effort: str) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        safety_settings=_SAFETY,
        thinking_config=_thinking_config(model, effort),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _model_chain() -> list[str]:
    """Modelo principal seguido dos alternativos."""
    primary = get_model()
    raw = os.getenv("GEMINI_FALLBACK_MODELS")
    extra = DEFAULT_FALLBACK_MODELS if raw is None else [m.strip() for m in raw.split(",") if m.strip()]
    return [primary] + [m for m in extra if m != primary]


# Modelos sem cota até o instante indicado (epoch): a cota diária zera à meia-noite
# do Pacífico (horário do Google); 404 vale por 1 hora.
_UNAVAILABLE: dict[str, float] = {}


def _next_quota_reset() -> float:
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return reset.timestamp()
    except Exception:  # sem banco de fusos (Windows sem tzdata)
        return time.time() + 6 * 3600


def _available(models: list[str]) -> list[str]:
    now = time.time()
    return [m for m in models if _UNAVAILABLE.get(m, 0) <= now]


def _quota_info(exc: errors.APIError) -> tuple[bool, str]:
    """Lê o erro 429: (é limite diário?, descrição do limite atingido).

    O Google lista o limite estourado em `violations` (quotaId e quotaValue)."""
    payload = exc.details if isinstance(exc.details, dict) else {}
    details = (payload.get("error") or payload).get("details") or []
    parts, daily = [], False
    for item in details if isinstance(details, list) else []:
        for v in item.get("violations", []) if isinstance(item, dict) else []:
            quota_id = str(v.get("quotaId", ""))
            daily = daily or "PerDay" in quota_id
            name = re.sub(r"-(Free|Paid)Tier.*$", "", quota_id.replace("PerProjectPerModel", "").replace("PerModel", ""))
            value = v.get("quotaValue")
            parts.append(f"{name} = {value}" if value else name)
    if not parts:  # formato inesperado: cai no texto bruto
        daily = "PerDay" in str(exc.details)
    return daily, "; ".join(dict.fromkeys(parts))


def _retry_delay(exc: errors.APIError, attempt: int) -> float:
    """Espera sugerida pelo servidor (RetryInfo "37s") ou recuo exponencial."""
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", str(exc.details))
    return float(m.group(1)) + 1 if m else min(15 * 2**attempt, 120)


def _with_hint(content: str | list, hint: str) -> str | list:
    return f"{content}\n\n{hint}" if isinstance(content, str) else [*content, hint]


def stream(
    client: genai.Client,
    content: str | list,
    effort: str,
    report: TranslationReport,
    label: str,
) -> Iterator[str | Restart]:
    """Envia um pedido de tradução e devolve o texto em streaming.

    - Limite por minuto (429) e servidor instável (5xx, queda de conexão):
      espera e repete sozinho. Se já tinha chegado texto, emite RESTART antes.
    - Corte por RECITATION: repete uma vez pedindo redação própria nas citações.
    - Cota diária esgotada: levanta DailyQuotaExceeded.
    - Bloqueios e cortes por tamanho viram avisos em `report.warnings`.
    """
    requested = get_model()
    chain = _model_chain()
    skip: set[str] = set()  # modelos já descartados neste pedido

    def next_model() -> str | None:
        return next((m for m in _available(chain) if m not in skip), None)

    model = next_model()
    if model is None:
        raise DailyQuotaExceeded("todos os modelos estão sem cota hoje: " + ", ".join(chain))
    config = _config(model, effort)
    rate_attempts = server_attempts = 0
    recitation_retried = False
    daily_info = ""

    def switch(reason: str) -> str | None:
        """Passa ao próximo modelo (recalcula a configuração); None se não houver."""
        nonlocal model, config, rate_attempts, server_attempts
        new = next_model()
        if new is None:
            return None
        report.say(f"{reason} Continuando com {new}…")
        model, config = new, _config(new, effort)
        rate_attempts = server_attempts = 0
        return new

    while True:
        started = False
        last = None
        try:
            for chunk in client.models.generate_content_stream(model=model, contents=content, config=config):
                last = chunk
                text = chunk.text  # None em pedaços só de raciocínio ou metadados
                if text:
                    started = True
                    yield text
        except errors.ClientError as exc:
            if exc.code == 404:  # modelo inexistente para esta chave
                _UNAVAILABLE[model] = time.time() + 3600
                skip.add(model)
                if switch(f"O modelo {model} não está disponível."):
                    if started:
                        yield RESTART
                    continue
                raise
            if exc.code != 429:
                raise
            daily, info = _quota_info(exc)
            if daily:
                _UNAVAILABLE[model] = _next_quota_reset()
                skip.add(model)
                daily_info = f"{model}: {info}" if info else model
                if switch(f"A cota diária do {model} acabou."):
                    if started:
                        yield RESTART
                    continue
                raise DailyQuotaExceeded(daily_info) from exc
            if rate_attempts >= MAX_RATE_LIMIT_RETRIES:
                raise
            wait = _retry_delay(exc, rate_attempts)
            rate_attempts += 1
            report.say(f"Limite de pedidos por minuto do Gemini atingido. Aguardando {wait:.0f}s…")
            if started:
                yield RESTART
            time.sleep(wait)
            continue
        except (errors.ServerError, httpx.TransportError, ConnectionError):
            if server_attempts >= MAX_SERVER_RETRIES:
                # Este modelo segue instável: tenta outro, sem descartá-lo de vez.
                skip.add(model)
                if switch(f"O {model} continua instável."):
                    if started:
                        yield RESTART
                    continue
                raise
            wait = min(10 * 2**server_attempts, 90)
            server_attempts += 1
            report.say(f"Servidor do Gemini ocupado ou instável. Tentando de novo em {wait}s…")
            if started:
                yield RESTART
            time.sleep(wait)
            continue

        if last is not None and last.usage_metadata:
            usage = last.usage_metadata
            report.input_tokens += usage.prompt_token_count or 0
            report.output_tokens += (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)

        reason = last.candidates[0].finish_reason if last is not None and last.candidates else None
        if reason == types.FinishReason.RECITATION and not recitation_retried:
            recitation_retried = True
            report.say("O Gemini interrompeu o trecho (RECITATION). Refazendo com redação própria nas citações…")
            if started:
                yield RESTART
            content = _with_hint(content, _RECITATION_HINT)
            continue
        break

    report.say("")
    if model != requested:
        report.warnings.append(
            f"{label}: traduzido com o modelo {model}, porque a cota do {requested} não estava disponível."
        )
    if last is None:
        report.warnings.append(f"{label}: o Gemini não devolveu resposta.")
        return
    feedback = last.prompt_feedback
    if feedback and feedback.block_reason:
        report.warnings.append(f"{label}: o Gemini bloqueou o pedido ({feedback.block_reason.name}).")
    elif reason in _FINISH_WARNINGS:
        report.warnings.append(f"{label}: {_FINISH_WARNINGS[reason]}")


def describe_error(exc: Exception) -> str:
    """Mensagem amigável para os erros mais comuns da API do Gemini."""
    if isinstance(exc, DailyQuotaExceeded):
        detail = f" (limite atingido: {exc.detail})" if exc.detail else ""
        return (f"A cota diária gratuita do Gemini acabou{detail}. Ela zera todo dia à meia-noite "
                "do horário do Pacífico (4h ou 5h da manhã no Brasil). Volte depois e toque em "
                "Continuar, ative o faturamento no Google AI Studio para limites bem maiores, ou use o Claude.")
    if isinstance(exc, errors.ClientError):
        msg = exc.message or ""
        if exc.code == 400 and "API key" in msg:
            return "Chave do Gemini inválida. Confira o valor de GEMINI_API_KEY."
        if exc.code in (401, 403):
            return "A chave do Gemini não tem permissão para usar este modelo."
        if exc.code == 404:
            return f"O modelo \"{get_model()}\" não foi encontrado. Ajuste GEMINI_MODEL no .env."
        if exc.code == 429:
            return "Limite de uso do Gemini atingido. Aguarde alguns minutos e tente de novo."
        return f"O Gemini recusou o pedido ({exc.code}): {msg}"
    if isinstance(exc, errors.ServerError):
        return f"O servidor do Gemini continua instável ({exc.code}). Tente de novo mais tarde."
    if isinstance(exc, (httpx.TransportError, ConnectionError)):
        return "Sem conexão com a API do Gemini. Verifique a internet."
    return f"Erro inesperado do Gemini: {exc}"
