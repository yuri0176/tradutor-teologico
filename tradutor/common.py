"""Tipos compartilhados entre os motores de tradução e o resto do app."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TranslationOptions:
    source_language: str = "auto"
    variant: str = "português do Brasil"
    bible_format: str = "ponto"
    translator_notes: bool = False
    gloss_terms: bool = False
    effort: str = "high"
    engine: str = "gemini"  # "gemini" | "claude"
    workers: int = 1  # capítulos traduzidos ao mesmo tempo (modo livro)


@dataclass
class TranslationReport:
    """Preenchido durante o streaming; lido pela interface ao final."""

    parts: int = 0
    warnings: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    waited: float = 0.0  # segundos parados esperando limite de uso ou servidor ocupado
    model: str = ""  # modelo que de fato traduziu
    # Chamado com uma mensagem quando o app precisa esperar ou refazer um
    # trecho; com "" quando a espera termina.
    notify: Callable[[str], None] | None = None

    def say(self, msg: str) -> None:
        if self.notify:
            self.notify(msg)


class Restart:
    """Sinal emitido no meio do streaming: descarte o texto já recebido deste
    trecho, porque ele vai ser gerado de novo desde o início."""


RESTART = Restart()
