"""Extração de texto de PDFs curtos."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_PAGES = 40
# Abaixo disso por página, o PDF provavelmente é digitalizado (imagem sem texto).
MIN_CHARS_PER_PAGE = 80


class PdfError(Exception):
    """Erro apresentável ao usuário."""


@dataclass
class PdfContent:
    text: str
    pages: int
    likely_scanned: bool


def clean_text(text: str) -> str:
    # Junta palavras hifenizadas no fim da linha: "justifi-\ncação" -> "justificação".
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Espaços repetidos e linhas em branco excessivas.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text(data: bytes) -> PdfContent:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # senha real exigida
                raise PdfError("O PDF está protegido por senha.") from exc
        pages = len(reader.pages)
    except PdfReadError as exc:
        raise PdfError("Não foi possível ler o PDF. O arquivo pode estar corrompido.") from exc

    if pages == 0:
        raise PdfError("O PDF não tem páginas.")
    if pages > MAX_PAGES:
        raise PdfError(
            f"O PDF tem {pages} páginas. O limite é {MAX_PAGES}: envie um artigo "
            "curto ou cole o trecho desejado no campo de texto."
        )

    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    text = clean_text("\n\n".join(parts))
    return PdfContent(
        text=text,
        pages=pages,
        likely_scanned=len(text) < MIN_CHARS_PER_PAGE * pages,
    )
