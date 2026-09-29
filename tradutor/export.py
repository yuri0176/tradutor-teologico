"""Reconstrução do livro traduzido em Markdown, TXT e PDF."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path

import markdown as md
from fpdf import FPDF
from fpdf.fonts import TextStyle

from .chapters import Segment

FONTS_DIR = Path(__file__).parent / "fonts"


def _has_heading(text: str) -> bool:
    """O trecho começa (nas primeiras linhas) com um título Markdown?"""
    return any(re.match(r"^#{1,3}\s+\S", line) for line in text.splitlines()[:8])


# ------------------------------------------------------------------- Markdown / TXT

def to_markdown(book_title: str, parts: list[tuple[Segment, str]]) -> str:
    chunks = [f"% {book_title}\n"] if book_title else []
    chunks += [text.strip() for _, text in parts]
    return "\n\n---\n\n".join(chunks) + "\n"


def markdown_to_plain(text: str) -> str:
    out = []
    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            title = heading.group(2).strip()
            out.append(title.upper() if len(heading.group(1)) == 1 else title)
            continue
        line = re.sub(r"^\s*>\s?", "    ", line)            # citação em bloco -> recuo
        line = re.sub(r"\[\^(\w+)\]:", r"[\1]", line)        # definição de nota
        line = re.sub(r"\[\^(\w+)\]", r"[\1]", line)         # chamada de nota
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line) # links
        line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)      # negrito
        line = re.sub(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", r"\1", line)  # itálico
        line = re.sub(r"<[^>]+>", "", line)                  # HTML solto (<sup> etc.)
        out.append(line)
    return "\n".join(out)


def to_txt(book_title: str, parts: list[tuple[Segment, str]]) -> str:
    sep = "\n\n" + "=" * 60 + "\n\n"
    body = sep.join(markdown_to_plain(text.strip()) for _, text in parts)
    head = f"{book_title.upper()}\n\n" if book_title else ""
    return head + body + "\n"


# ------------------------------------------------------------------- PDF

_RTL = re.compile("[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]")  # hebraico, árabe e afins


class _BookPDF(FPDF):
    def __init__(self, book_title: str, rtl: bool = False):
        super().__init__(format=(148, 210))  # A5 em mm
        self.book_title = book_title
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(True, margin=18)
        # FreeSerif cobre latim estendido, grego politônico e hebraico.
        self.add_font("Serif", "", FONTS_DIR / "FreeSerif.ttf")
        self.add_font("Serif", "B", FONTS_DIR / "FreeSerifBold.ttf")
        self.add_font("Serif", "I", FONTS_DIR / "FreeSerifItalic.ttf")
        self.add_font("Serif", "BI", FONTS_DIR / "FreeSerifBoldItalic.ttf")
        if rtl:  # só o hebraico/árabe precisa de modelagem; para latim e grego ela só
            try:  # atrapalha a camada de texto (copiar/pesquisar) em alguns servidores
                self.set_text_shaping(True)
            except Exception:
                pass
        self.set_title(book_title)
        self.set_creator("Tradutor Teológico Acadêmico")

    def footer(self) -> None:
        if self.page_no() == 1:
            return
        self.set_y(-12)
        self.set_font("Serif", "", 8)
        self.set_text_color(120)
        self.cell(0, 6, str(self.page_no()), align="C")
        self.set_text_color(0)


BODY_PT = 10.5
_DARK = (40, 30, 30)
_TAG_STYLES = {
    "h1": TextStyle(font_family="Serif", font_style="B", font_size_pt=17, color=_DARK, t_margin=2, b_margin=0.4),
    "h2": TextStyle(font_family="Serif", font_style="B", font_size_pt=13.5, color=_DARK, t_margin=5, b_margin=0.4),
    "h3": TextStyle(font_family="Serif", font_style="B", font_size_pt=11.5, color=_DARK, t_margin=4, b_margin=0.4),
    "h4": TextStyle(font_family="Serif", font_style="B", font_size_pt=BODY_PT, color=_DARK, t_margin=4, b_margin=0.4),
    "blockquote": TextStyle(font_size_pt=9.5, color=(60, 60, 60), l_margin=8, t_margin=1, b_margin=1),
}


_FOOTNOTE_DEF = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")


def _to_html(text: str) -> str:
    """Markdown -> HTML simples, no subconjunto que o fpdf2 desenha bem.

    As notas de rodapé ([^1] no texto e "[^1]: …" no fim) viram números
    sobrescritos e uma seção "Notas" no fim do capítulo.
    """
    body, notes = [], []
    for line in text.splitlines():
        m = _FOOTNOTE_DEF.match(line.strip())
        if m:
            notes.append(f"<sup>{m.group(1)}</sup> {m.group(2)}")
        elif notes and line.startswith(("    ", "\t")) and line.strip():
            notes[-1] += " " + line.strip()  # continuação de nota
        else:
            body.append(line)
    content = re.sub(r"\[\^([^\]]+)\]", r"<sup>\1</sup>", "\n".join(body))
    if notes:
        content += "\n\n#### Notas\n\n" + "\n\n".join(notes)
    html = md.markdown(content, extensions=["sane_lists"])
    return _flatten_lists(html)


def _flatten_lists(html: str) -> str:
    """Listas viram parágrafos com o marcador escrito ("1.", "–"): o fpdf2
    desenha os marcadores automáticos num tamanho desproporcional."""

    def ordered(m: re.Match) -> str:
        items = re.findall(r"<li>(.*?)</li>", m.group(2), flags=re.S)
        start = int(m.group(1) or 1)
        return "".join(
            f"<p>&nbsp;&nbsp;{start + i}. {_strip_p(item)}</p>" for i, item in enumerate(items)
        )

    def unordered(m: re.Match) -> str:
        items = re.findall(r"<li>(.*?)</li>", m.group(1), flags=re.S)
        return "".join(f"<p>&nbsp;&nbsp;– {_strip_p(item)}</p>" for item in items)

    html = re.sub(r'<ol(?: start="(\d+)")?>(.*?)</ol>', ordered, html, flags=re.S)
    return re.sub(r"<ul>(.*?)</ul>", unordered, html, flags=re.S)


def _strip_p(item: str) -> str:
    return re.sub(r"</?p>", " ", item).strip()


def to_pdf(book_title: str, parts: list[tuple[Segment, str]]) -> bytes:
    # NFC: letras acentuadas em um único caractere (o FreeSerif desenha bem assim).
    parts = [(seg, unicodedata.normalize("NFC", text)) for seg, text in parts]
    pdf = _BookPDF(book_title, rtl=any(_RTL.search(text) for _, text in parts))

    # Folha de rosto
    pdf.add_page()
    pdf.set_y(70)
    pdf.set_font("Serif", "B", 20)
    pdf.multi_cell(0, 10, book_title or "Tradução", align="C")
    pdf.ln(6)
    pdf.set_font("Serif", "I", 10)
    pdf.multi_cell(0, 6, f"Tradução para o português\n{date.today():%d/%m/%Y}", align="C")

    for segment, text in parts:
        pdf.add_page()
        # Títulos Markdown (#, ##) viram marcadores do PDF automaticamente no
        # write_html; sem título no texto, cria o marcador com o nome do segmento.
        if not _has_heading(text):
            pdf.start_section(segment.title, level=0)
        pdf.set_font("Serif", "", BODY_PT)
        pdf.write_html(
            _to_html(text),
            font_family="Serif",
            tag_styles=_TAG_STYLES,
        )

    return bytes(pdf.output())
