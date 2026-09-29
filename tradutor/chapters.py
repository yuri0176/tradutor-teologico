"""Leitura de PDFs longos e divisão em capítulos.

A divisão tenta três estratégias, nesta ordem:
1. o sumário embutido no PDF (marcadores / outline);
2. títulos de capítulo no topo das páginas ("Chapter 3", "Kapitel IV", …);
3. blocos de N páginas.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import asdict, dataclass

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from .pdf_utils import MIN_CHARS_PER_PAGE, PdfError, clean_text

# Um PDF digitalizado é enviado ao modelo como documento; cada envio leva no
# máximo este número de páginas, para a resposta não estourar o limite de saída.
SCANNED_PAGES_PER_REQUEST = 12


@dataclass
class Segment:
    """Um capítulo (ou bloco) do livro: páginas [start, end), base 0."""

    title: str
    start: int
    end: int

    @property
    def pages_label(self) -> str:
        return f"{self.start + 1}–{self.end}" if self.end - self.start > 1 else f"{self.start + 1}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Book:
    pages: list[str]
    outline: list[tuple[int, str, int]]  # (nível, título, página)
    likely_scanned: bool

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def outline_depth(self) -> int:
        return max((lvl for lvl, _, _ in self.outline), default=0)


# ------------------------------------------------------------------- leitura

def _flatten_outline(reader: PdfReader) -> list[tuple[int, str, int]]:
    entries: list[tuple[int, str, int]] = []

    def walk(items, level: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            try:
                page = reader.get_destination_page_number(item)
            except Exception:
                continue
            title = " ".join(str(getattr(item, "title", "") or "").split())
            if title and page is not None and page >= 0:
                entries.append((level, title, page))

    try:
        walk(reader.outline, 1)
    except Exception:
        return []
    return entries


_PAGE_NUMBER = re.compile(r"^\s*(?:page\s+|p\.\s*)?[\divxlc]+\s*$", re.IGNORECASE)


def remove_running_lines(pages: list[str]) -> list[str]:
    """Remove cabeçalhos/rodapés repetidos e números de página soltos.

    Considera as duas primeiras e as duas últimas linhas de cada página; uma
    linha (com dígitos normalizados) que se repete em muitas páginas é
    tratada como cabeçalho ou rodapé corrido.
    """
    if len(pages) < 6:
        return pages

    def key(line: str) -> str:
        return re.sub(r"\d+", "#", line.strip().lower())

    counts: Counter[str] = Counter()
    for text in pages:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        for ln in set(lines[:2] + lines[-2:]):
            counts[key(ln)] += 1
    # Cabeçalho corrido aparece em boa parte das páginas; títulos de capítulo
    # ("Chapter #") nunca são removidos, mesmo que se repitam.
    threshold = max(4, int(len(pages) * 0.3))
    running = {k for k, n in counts.items() if n >= threshold and not _HEADING.match(k)}

    cleaned = []
    for text in pages:
        lines = text.splitlines()
        nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
        edges = set(nonempty[:2] + nonempty[-2:])
        kept = [
            ln
            for i, ln in enumerate(lines)
            if not (i in edges and (key(ln) in running or _PAGE_NUMBER.match(ln)))
        ]
        cleaned.append("\n".join(kept))
    return cleaned


def _extract_pages_fast(data: bytes) -> list[str] | None:
    """Texto de todas as páginas com pypdfium2, cerca de 15 vezes mais rápido que
    o pypdf (um livro de 1.900 páginas: ~8 s contra ~2 min). Devolve None se a
    biblioteca não estiver instalada ou falhar; aí vale o pypdf."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        try:
            pages = []
            for i in range(len(pdf)):
                page = pdf[i]
                textpage = page.get_textpage()
                try:
                    pages.append(textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))
                finally:
                    textpage.close()
                    page.close()
            return pages
        finally:
            pdf.close()
    except Exception:
        return None


def read_book(data: bytes) -> Book:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise PdfError("O PDF está protegido por senha.") from exc
            fast = None  # o pypdfium2 não recebeu a senha vazia: usa o pypdf
        else:
            fast = _extract_pages_fast(data)
        if fast is not None and len(fast) == len(reader.pages):
            raw_pages = fast
        else:
            raw_pages = []
            for page in reader.pages:
                try:
                    raw_pages.append(page.extract_text() or "")
                except Exception:
                    raw_pages.append("")
    except PdfReadError as exc:
        raise PdfError("Não foi possível ler o PDF. O arquivo pode estar corrompido.") from exc
    if not raw_pages:
        raise PdfError("O PDF não tem páginas.")

    total_chars = sum(len(p.strip()) for p in raw_pages)
    pages = [clean_text(p) for p in remove_running_lines(raw_pages)]
    return Book(
        pages=pages,
        outline=_flatten_outline(reader),
        likely_scanned=total_chars < MIN_CHARS_PER_PAGE * len(raw_pages),
    )


# ------------------------------------------------------------------- divisão

def _segments_from_starts(starts: list[tuple[int, str]], page_count: int) -> list[Segment]:
    """Converte (página inicial, título) em segmentos contíguos que cobrem o livro todo."""
    by_page: dict[int, str] = {}
    for page, title in sorted(starts, key=lambda x: x[0]):
        if 0 <= page < page_count:
            # Vários marcadores na mesma página: junta os títulos.
            by_page[page] = f"{by_page[page]} / {title}" if page in by_page else title

    ordered = sorted(by_page.items())
    if not ordered:
        return []
    segments = []
    if ordered[0][0] > 0:
        segments.append(Segment("Páginas iniciais", 0, ordered[0][0]))
    for i, (page, title) in enumerate(ordered):
        end = ordered[i + 1][0] if i + 1 < len(ordered) else page_count
        segments.append(Segment(title, page, end))
    return segments


def split_by_outline(book: Book, depth: int) -> list[Segment]:
    starts = [(page, title) for level, title, page in book.outline if level <= depth]
    return _segments_from_starts(starts, book.page_count)


_HEADING = re.compile(
    r"^\s*(chapter|capítulo|capitulo|kapitel|chapitre|capitolo|hoofdstuk|"
    r"part|parte|teil|partie|book|livro|libro|buch|appendix|apêndice|anhang|anexo)"
    r"\s+([0-9]{1,3}|[ivxlc]{1,7}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|[a-z]{3,12})\b[.:]?\s*(.*)$",
    re.IGNORECASE,
)


def split_by_headings(book: Book) -> list[Segment]:
    """Procura um título de capítulo nas primeiras linhas de cada página."""
    starts = []
    for i, text in enumerate(book.pages):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:4]
        for j, line in enumerate(lines):
            m = _HEADING.match(line)
            if m and len(line) <= 90:
                title = line
                # "Chapter 3" sozinho na linha: o título costuma vir na linha seguinte.
                if not m.group(3) and j + 1 < len(lines) and len(lines[j + 1]) <= 90:
                    title = f"{line}: {lines[j + 1]}"
                starts.append((i, title))
                break
    if len(starts) < 2:
        return []
    return _segments_from_starts(starts, book.page_count)


def split_fixed(page_count: int, size: int) -> list[Segment]:
    size = max(1, size)
    return [
        Segment(f"Páginas {s + 1}–{min(s + size, page_count)}", s, min(s + size, page_count))
        for s in range(0, page_count, size)
    ]


def auto_split(book: Book, block_size: int = 20) -> tuple[str, list[Segment]]:
    """Escolhe a melhor estratégia disponível. Retorna (descrição, segmentos)."""
    if book.outline:
        depth = min(2, book.outline_depth)
        segments = split_by_outline(book, depth)
        if len(segments) >= 2:
            return f"sumário do PDF (nível {depth})", segments
    segments = split_by_headings(book)
    if segments:
        return "títulos de capítulo encontrados no texto", segments
    return f"blocos de {block_size} páginas", split_fixed(book.page_count, block_size)


# ------------------------------------------------------------------- conteúdo

def segment_text(book: Book, segment: Segment) -> str:
    return "\n\n".join(p for p in book.pages[segment.start : segment.end] if p.strip())


def segment_pdf_parts(data: bytes, segment: Segment) -> list[bytes]:
    """Para livros digitalizados: recorta as páginas do segmento em PDFs menores."""
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for s in range(segment.start, segment.end, SCANNED_PAGES_PER_REQUEST):
        writer = PdfWriter()
        for p in range(s, min(s + SCANNED_PAGES_PER_REQUEST, segment.end)):
            writer.add_page(reader.pages[p])
        buf = io.BytesIO()
        writer.write(buf)
        parts.append(buf.getvalue())
    return parts
