"""Peças de interface: CSS mobile-first e botão de copiar."""

from __future__ import annotations

import json

import streamlit as st
import streamlit.components.v1 as components

MOBILE_CSS = """
<style>
/* Mobile-first: margens pequenas no celular, coluna central no desktop. */
.block-container {
    padding: 1.2rem 1rem 4rem;
    max-width: 760px;
}
@media (min-width: 768px) {
    .block-container { padding: 2.5rem 2rem 4rem; }
}
h1 { font-size: 1.6rem !important; line-height: 1.25 !important; }

/* Campos e botões com alvo de toque confortável (>= 48px). */
textarea { font-size: 16px !important; line-height: 1.5 !important; }
div.stButton > button, div.stDownloadButton > button {
    min-height: 3.2rem;
    font-size: 1.05rem;
    border-radius: 0.7rem;
}
div.stButton > button[kind="primary"] {
    min-height: 3.8rem;
    font-size: 1.2rem;
    font-weight: 600;
}

/* Resultado com leitura confortável. */
.st-key-traducao p, .st-key-traducao li, .st-key-traducao blockquote {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1.05rem;
    line-height: 1.65;
}
</style>
"""


def copy_button(text: str, label: str = "📋 Copiar texto") -> None:
    """Botão grande que copia `text` para a área de transferência.

    Usa a Clipboard API e, se o navegador a bloquear (comum em iframes no
    celular), cai para document.execCommand('copy') com um textarea oculto.
    """
    # json.dumps gera um literal JS seguro; "</" é escapado para não fechar a tag <script>.
    payload = json.dumps(text).replace("</", "<\\/")
    html = f"""
<button id="copy" type="button">{label}</button>
<textarea id="buf" readonly aria-hidden="true"></textarea>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  #copy {{
    width: 100%; min-height: 3.2rem; font-size: 1.05rem; font-weight: 600;
    border: 0; border-radius: 0.7rem; background: #7a2e2e; color: #fff;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
  }}
  #copy:active {{ opacity: .85; }}
  #buf {{ position: absolute; left: -9999px; top: 0; }}
</style>
<script>
  const text = {payload};
  const btn = document.getElementById("copy");
  const original = btn.textContent;
  function done(ok) {{
    btn.textContent = ok ? "✅ Copiado!" : "Não foi possível copiar — use o download";
    setTimeout(() => (btn.textContent = original), 2500);
  }}
  function fallback() {{
    const buf = document.getElementById("buf");
    buf.value = text;
    buf.focus();
    buf.select();
    buf.setSelectionRange(0, text.length);
    let ok = false;
    try {{ ok = document.execCommand("copy"); }} catch (e) {{ ok = false; }}
    done(ok);
  }}
  btn.addEventListener("click", () => {{
    if (navigator.clipboard && window.isSecureContext) {{
      navigator.clipboard.writeText(text).then(() => done(true), fallback);
    }} else {{
      fallback();
    }}
  }});
</script>
"""
    # st.iframe substitui components.html nas versões novas do Streamlit.
    if hasattr(st, "iframe"):
        st.iframe(html, height=60)
    else:
        components.html(html, height=60)
