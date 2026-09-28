"""Motores de tradução disponíveis. Todos expõem a mesma interface:

NAME, LABEL, KEY_ENV, KEY_HELP, API_ERRORS, get_model(), make_client(api_key),
document_content(pdf_bytes, instructions), stream(client, content, effort,
report, label) e describe_error(exc).
"""

from __future__ import annotations

from types import ModuleType

from . import claude, gemini

ENGINES: dict[str, ModuleType] = {gemini.NAME: gemini, claude.NAME: claude}


def get_engine(name: str) -> ModuleType:
    return ENGINES.get(name, gemini)


def all_api_errors() -> tuple[type[BaseException], ...]:
    return tuple(err for engine in ENGINES.values() for err in engine.API_ERRORS)
