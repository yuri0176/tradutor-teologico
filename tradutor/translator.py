"""Tradução independente de motor: divisão de textos longos, streaming e
estimativas. O motor (Gemini ou Claude) vem de `options.engine`."""

from __future__ import annotations

import re
from typing import Iterator

from .common import RESTART, Restart, TranslationOptions, TranslationReport
from .engines import all_api_errors, get_engine
from .prompts import build_user_message

__all__ = [
    "API_ERRORS",
    "CONTEXT_TAIL_CHARS",
    "RESTART",
    "Restart",
    "TranslationOptions",
    "TranslationReport",
    "estimate_tokens",
    "get_engine",
    "split_into_chunks",
    "translate_pdf_document",
    "translate_text",
]

# Tamanho de cada trecho enviado à API (em caracteres). Um artigo curto cabe
# em um trecho só; textos maiores são divididos em parágrafos inteiros para
# que o usuário acompanhe o progresso e nenhuma resposta estoure o limite.
CHUNK_CHARS = 18000
CONTEXT_TAIL_CHARS = 1500

# Erros de qualquer motor que a interface trata com engine.describe_error().
API_ERRORS = all_api_errors()


def estimate_tokens(chars: int, requests: int) -> tuple[int, int]:
    """Estimativa grosseira: (tokens de entrada, tokens de saída).

    ~4 caracteres por token; a tradução em português sai ~25% mais longa; cada
    pedido carrega ~3,5 mil tokens de instruções. Não inclui o raciocínio
    interno do modelo."""
    return chars // 4 + requests * 3500, int(chars / 4 * 1.25)


def split_into_chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Divide o texto em trechos de até `limit` caracteres, sem cortar parágrafos
    (ou, se um parágrafo sozinho passar do limite, sem cortar frases)."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            pieces.append(para)
            continue
        sentence_buf = ""
        for sentence in re.split(r"(?<=[.!?;:])\s+", para):
            if sentence_buf and len(sentence_buf) + len(sentence) + 1 > limit:
                pieces.append(sentence_buf)
                sentence_buf = sentence
            else:
                sentence_buf = f"{sentence_buf} {sentence}".strip()
        if sentence_buf:
            pieces.append(sentence_buf)

    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        if buf and len(buf) + len(piece) + 2 > limit:
            chunks.append(buf)
            buf = piece
        else:
            buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        chunks.append(buf)
    return chunks


def _collect(pieces: Iterator[str | Restart], done: str) -> Iterator[str]:
    """Acumula o streaming de um trecho e devolve "texto pronto até agora".

    Um RESTART descarta o que já tinha chegado deste trecho (o motor vai
    gerá-lo de novo)."""
    current: list[str] = []
    for piece in pieces:
        if isinstance(piece, Restart):
            current.clear()
        else:
            current.append(piece)
        yield done + "".join(current)


def translate_text(client, text: str, options: TranslationOptions, report: TranslationReport) -> Iterator[str]:
    """Traduz trecho por trecho. Cada valor gerado é o texto traduzido completo
    até aquele momento (a interface só precisa exibir o último)."""
    engine = get_engine(options.engine)
    chunks = split_into_chunks(text)
    report.parts = len(chunks)
    done = ""
    previous_translation = ""

    for i, chunk in enumerate(chunks, start=1):
        message = build_user_message(
            chunk,
            source_language=options.source_language,
            variant=options.variant,
            bible_format=options.bible_format,
            translator_notes=options.translator_notes,
            gloss_terms=options.gloss_terms,
            part=i,
            total_parts=len(chunks),
            previous_tail=previous_translation[-CONTEXT_TAIL_CHARS:] or None,
        )
        prefix = done + ("\n\n" if done else "")
        snapshot = prefix
        for snapshot in _collect(engine.stream(client, message, options.effort, report, f"Trecho {i}"), prefix):
            yield snapshot
        previous_translation = snapshot[len(prefix):]
        done = snapshot


def translate_pdf_document(
    client, pdf_bytes: bytes, options: TranslationOptions, report: TranslationReport
) -> Iterator[str]:
    """Para PDFs sem camada de texto (digitalizados): envia o próprio PDF ao
    modelo, que lê as páginas como imagem."""
    engine = get_engine(options.engine)
    report.parts = 1
    instructions = build_user_message(
        None,
        source_language=options.source_language,
        variant=options.variant,
        bible_format=options.bible_format,
        translator_notes=options.translator_notes,
        gloss_terms=options.gloss_terms,
    )
    content = engine.document_content(pdf_bytes, instructions)
    yield from _collect(engine.stream(client, content, options.effort, report, "PDF"), "")
