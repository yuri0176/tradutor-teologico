"""Tradutor Teológico Acadêmico — interface Streamlit (mobile-first).

Execute com:  streamlit run app.py
"""

from __future__ import annotations

import os

import streamlit as st
from dotenv import load_dotenv

from tradutor import book_ui
from tradutor.pdf_utils import PdfError, extract_text
from tradutor.engines import ENGINES
from tradutor.translator import (
    API_ERRORS,
    TranslationOptions,
    TranslationReport,
    get_engine,
    split_into_chunks,
    translate_pdf_document,
    translate_text,
)
from tradutor.ui import MOBILE_CSS, copy_button

load_dotenv()

st.set_page_config(
    page_title="Tradutor Teológico",
    page_icon="📜",
    layout="centered",
    initial_sidebar_state="collapsed",
)
st.markdown(MOBILE_CSS, unsafe_allow_html=True)

LANGUAGES = {
    "Detectar automaticamente": "auto",
    "Inglês": "inglês",
    "Espanhol": "espanhol",
    "Alemão": "alemão",
    "Francês": "francês",
    "Italiano": "italiano",
    "Holandês": "holandês",
    "Latim": "latim",
}
QUALITY = {
    "Alta (recomendada)": "high",
    "Máxima (mais lenta)": "xhigh",
    "Rápida": "medium",
}


def configured_key(engine) -> str | None:
    """Chave do .env / variável de ambiente ou dos secrets do Streamlit."""
    key = os.getenv(engine.KEY_ENV)
    if not key:
        try:
            key = st.secrets.get(engine.KEY_ENV)
        except Exception:  # sem secrets.toml
            key = None
    return str(key).strip() if key else None


def resolve_api_key(engine) -> str | None:
    """Ordem: .env / secrets → campo na barra lateral."""
    return configured_key(engine) or st.session_state.get(f"key_{engine.NAME}", "").strip() or None


# ---------------------------------------------------------------- barra lateral
with st.sidebar:
    st.header("⚙️ Configuração")
    names = list(ENGINES)
    # Padrão: o motor que já tem chave configurada (Gemini, se os dois tiverem).
    default = next((n for n in names if configured_key(ENGINES[n])), names[0])
    engine_name = st.radio(
        "Motor de tradução",
        names,
        index=names.index(default),
        format_func=lambda n: ENGINES[n].LABEL,
        help="Gemini: grátis, com limites diários. Claude: pago por uso, costuma ser mais "
        "preciso em textos difíceis.",
    )
    engine = get_engine(engine_name)
    if configured_key(engine):
        st.success(f"Chave do {engine.LABEL} carregada do .env / secrets.")
    else:
        st.text_input(
            f"Chave da API — {engine.LABEL}",
            type="password",
            key=f"key_{engine.NAME}",
            help=f"{engine.KEY_HELP}. Usada só nesta sessão; para não digitar sempre, "
            f"coloque {engine.KEY_ENV} no arquivo .env.",
        )
    st.caption(f"Modelo: `{engine.get_model()}`")

# ---------------------------------------------------------------- cabeçalho
st.title("📜 Tradutor Teológico Acadêmico")
st.caption(
    "Tradução de artigos e ensaios de teologia para o português, com terminologia "
    "técnica, tom acadêmico e citações preservadas."
)

# ---------------------------------------------------------------- entrada
PASTE, SHORT_PDF, BOOK = "✍️ Colar texto", "📄 PDF curto", "📚 Livro / PDF longo"
source = st.radio(
    "Origem do texto",
    [PASTE, SHORT_PDF, BOOK],
    horizontal=True,
    label_visibility="collapsed",
)

text_to_translate = ""
pdf_bytes: bytes | None = None
pdf_scanned = False
book_input = None

if source == BOOK:
    book_input = book_ui.render_input()
elif source == PASTE:
    text_to_translate = st.text_area(
        "Texto original",
        height=260,
        placeholder="Cole aqui o artigo, ensaio ou trecho a traduzir…",
    )
else:
    uploaded = st.file_uploader(
        "PDF curto (até 40 páginas; para livros, use 📚 Livro / PDF longo)",
        type=["pdf"],
        help="PDFs com texto selecionável funcionam melhor. PDFs digitalizados "
        "são lidos como imagem.",
    )
    if uploaded is not None:
        pdf_bytes = uploaded.getvalue()
        try:
            content = extract_text(pdf_bytes)
        except PdfError as exc:
            st.error(str(exc))
            pdf_bytes = None
        else:
            if content.likely_scanned:
                pdf_scanned = True
                st.info(
                    f"PDF de {content.pages} página(s) sem texto selecionável "
                    "(provavelmente digitalizado). Ele será enviado inteiro para "
                    "leitura das páginas como imagem."
                )
            else:
                st.success(f"Texto extraído de {content.pages} página(s).")
                text_to_translate = st.text_area(
                    "Texto extraído (revise ou corte o que não quiser traduzir)",
                    value=content.text,
                    height=260,
                    key=f"pdf_text_{uploaded.file_id}",
                )

language_label = st.selectbox("Idioma de origem", list(LANGUAGES))

with st.expander("Opções avançadas"):
    variant = st.radio(
        "Variante do português",
        ["português do Brasil", "português europeu"],
        horizontal=True,
    )
    bible_format = st.radio(
        "Referências bíblicas",
        ["ponto", "dois-pontos"],
        format_func=lambda v: "Rm 3.23 (padrão editorial brasileiro)" if v == "ponto" else "Rm 3:23",
        horizontal=True,
    )
    translator_notes = st.toggle(
        "Permitir notas do tradutor [N.T.]",
        help="Para jogos de palavras, ambiguidades do original ou termos sem equivalente exato.",
    )
    gloss_terms = st.toggle(
        "Mostrar o termo original na 1ª ocorrência",
        help="Ex.: justificação (*justification*).",
    )
    quality_label = st.radio("Qualidade", list(QUALITY), horizontal=True)
    workers = st.slider(
        "Capítulos ao mesmo tempo (modo livro)",
        1, 5, 3,
        help="Traduz vários capítulos em paralelo: mais rápido, mas o texto não aparece ao vivo. "
        "Se o Gemini reclamar de limite por minuto, o app espera sozinho. Use 1 para ver o texto "
        "enquanto é gerado.",
    )

options = TranslationOptions(
    source_language=LANGUAGES[language_label],
    variant=variant,
    bible_format=bible_format,
    translator_notes=translator_notes,
    gloss_terms=gloss_terms,
    effort=QUALITY[quality_label],
    engine=engine_name,
    workers=workers,
)

if source == BOOK:
    if book_input is not None:
        book_ui.render_run(book_input, options, resolve_api_key(engine))
    st.stop()

if text_to_translate.strip():
    words = len(text_to_translate.split())
    parts = len(split_into_chunks(text_to_translate))
    st.caption(
        f"≈ {words:,} palavras".replace(",", ".")
        + (f" · será traduzido em {parts} partes" if parts > 1 else "")
    )

translate = st.button("Traduzir artigo", type="primary", use_container_width=True)

# ---------------------------------------------------------------- tradução
if translate:
    api_key = resolve_api_key(engine)
    if not api_key:
        st.error(
            f"Falta a chave do {engine.LABEL}. Coloque {engine.KEY_ENV} no arquivo .env "
            "(veja .env.example) ou abra a barra lateral (›) e cole a chave."
        )
    elif not text_to_translate.strip() and not (pdf_bytes and pdf_scanned):
        st.warning("Cole um texto ou envie um PDF antes de traduzir.")
    else:
        waiting = st.empty()
        report = TranslationReport(notify=lambda msg: waiting.info(msg) if msg else waiting.empty())
        client = engine.make_client(api_key)
        if pdf_bytes and pdf_scanned:
            stream = translate_pdf_document(client, pdf_bytes, options, report)
        else:
            stream = translate_text(client, text_to_translate, options, report)

        status = st.caption("Traduzindo… o texto aparece à medida que é gerado.")
        live = st.empty()
        error = None
        result, shown = "", 0
        try:
            # Cada valor é o texto completo até agora; se o motor refizer um
            # trecho (limite de uso, RECITATION), o texto "volta" e é regerado.
            for result in stream:
                if abs(len(result) - shown) > 200:
                    shown = len(result)
                    live.markdown(result)
        except API_ERRORS as exc:
            error = engine.describe_error(exc)
        status.empty()
        live.empty()
        waiting.empty()

        if error:
            st.error(error)
        if isinstance(result, str) and result.strip():
            st.session_state["result"] = result
            st.session_state["warnings"] = report.warnings
            st.session_state["usage"] = (report.input_tokens, report.output_tokens)

# ---------------------------------------------------------------- resultado
if st.session_state.get("result"):
    result = st.session_state["result"]
    st.divider()
    st.subheader("Tradução")
    for warning in st.session_state.get("warnings", []):
        st.warning(warning)

    copy_button(result)
    st.download_button(
        "⬇️ Baixar (.md)",
        data=result.encode("utf-8"),
        file_name="traducao.md",
        mime="text/markdown",
        use_container_width=True,
        on_click="ignore",
    )

    with st.container(key="traducao"):
        st.markdown(result)

    inp, out = st.session_state.get("usage", (0, 0))
    if inp or out:
        st.caption(f"Tokens: {inp:,} de entrada · {out:,} de saída".replace(",", "."))

    if st.button("Limpar resultado", use_container_width=True):
        for key in ("result", "warnings", "usage"):
            st.session_state.pop(key, None)
        st.rerun()
