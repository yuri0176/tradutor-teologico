"""Tradução de livros: capítulo por capítulo, com progresso salvo em disco.

Cada trecho traduzido é gravado assim que termina. Se a página for fechada,
a conexão cair ou o usuário pausar, basta enviar o mesmo PDF com as mesmas
opções para continuar de onde parou, sem pagar de novo pelo que já foi feito.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable, Iterator

from .chapters import Book, Segment, segment_pdf_parts, segment_text
from .prompts import build_user_message
from .translator import (
    CONTEXT_TAIL_CHARS,
    Restart,
    TranslationOptions,
    TranslationReport,
    get_engine,
    split_into_chunks,
)


def jobs_dir() -> Path:
    path = Path(os.getenv("TRADUTOR_JOBS_DIR", ".traducoes"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_id(pdf_bytes: bytes, options: TranslationOptions, segments: list[Segment], scanned: bool) -> str:
    """Mesmo PDF + mesmas opções + mesma divisão = mesmo trabalho (retomável).

    O motor (Gemini/Claude) não entra na conta: um mesmo livro pode ter
    capítulos traduzidos por motores diferentes."""
    h = hashlib.sha256(pdf_bytes)
    opts = {k: v for k, v in asdict(options).items() if k != "engine"}
    h.update(json.dumps(opts, sort_keys=True).encode())
    h.update(json.dumps([(s.start, s.end) for s in segments]).encode())
    h.update(b"scanned" if scanned else b"text")
    return h.hexdigest()[:20]


@dataclass
class BookJob:
    id: str
    book_title: str
    segments: list[dict]  # Segment.to_dict()
    selected: list[int]
    scanned: bool
    # Tradução de cada trecho, por segmento: {"3": ["trecho 1", "trecho 2"]}
    translations: dict[str, list[str]] = field(default_factory=dict)
    # Número de trechos planejados por segmento (conhecido ao iniciar o segmento).
    planned: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    # --------------------------------------------------------------- persistência
    @property
    def path(self) -> Path:
        return jobs_dir() / f"{self.id}.json"

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @classmethod
    def load(cls, jid: str) -> "BookJob | None":
        path = jobs_dir() / f"{jid}.json"
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    def to_json(self) -> bytes:
        """Arquivo de progresso para o usuário guardar e reenviar depois."""
        return json.dumps(asdict(self), ensure_ascii=False, indent=1).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes | str) -> "BookJob":
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def same_division(self, segments: list[dict]) -> bool:
        """O progresso só encaixa se a divisão em capítulos for a mesma."""
        pages = lambda segs: [(s["start"], s["end"]) for s in segs]
        return pages(self.segments) == pages(segments)

    def adopt(self, other: "BookJob") -> None:
        """Assume a tradução de outro trabalho (arquivo de progresso importado),
        mantendo títulos e seleção atuais."""
        self.translations = other.translations
        self.planned = other.planned
        self.warnings = other.warnings
        self.input_tokens = other.input_tokens
        self.output_tokens = other.output_tokens
        self.save()

    # --------------------------------------------------------------- estado
    def segment(self, i: int) -> Segment:
        return Segment(**self.segments[i])

    def is_segment_done(self, i: int) -> bool:
        key = str(i)
        return key in self.planned and len(self.translations.get(key, [])) >= self.planned[key]

    @property
    def done_count(self) -> int:
        return sum(1 for i in self.selected if self.is_segment_done(i))

    @property
    def finished(self) -> bool:
        return all(self.is_segment_done(i) for i in self.selected)

    def reset_segments(self, indices: list[int]) -> None:
        """Apaga a tradução destes segmentos para refazê-los (ex.: com outro motor)."""
        for i in indices:
            self.translations.pop(str(i), None)
            self.planned.pop(str(i), None)
            title = self.segment(i).title
            self.warnings = [w for w in self.warnings if not w.startswith(f"{title} (parte")]
        self.save()

    def translated_segments(self) -> list[tuple[Segment, str]]:
        """Segmentos selecionados que já têm alguma tradução, na ordem do livro."""
        out = []
        for i in sorted(self.selected):
            parts = self.translations.get(str(i))
            if parts:
                out.append((self.segment(i), "\n\n".join(p.strip() for p in parts)))
        return out


@dataclass
class Event:
    kind: str  # "segment_start" | "chunk_start" | "text" | "restart" | "chunk_done" | "segment_done" | "skip"
    segment: int = 0
    chunk: int = 0
    chunks: int = 0
    text: str = ""


def _previous_tail(job: BookJob, upto_segment: int) -> str | None:
    """Final do último trecho já traduzido antes deste segmento (continuidade)."""
    for i in sorted((s for s in job.selected if s < upto_segment), reverse=True):
        parts = job.translations.get(str(i))
        if parts:
            return parts[-1][-CONTEXT_TAIL_CHARS:]
    return None


def run_book(
    client,
    pdf_bytes: bytes,
    book: Book,
    job: BookJob,
    options: TranslationOptions,
    notify: Callable[[str], None] | None = None,
) -> Iterator[Event]:
    """Traduz os segmentos selecionados em sequência, salvando após cada trecho."""
    engine = get_engine(options.engine)
    for i in sorted(job.selected):
        key = str(i)
        seg = job.segment(i)
        if job.is_segment_done(i):
            yield Event("skip", segment=i)
            continue

        # Plano de trechos do segmento: texto dividido por parágrafos ou,
        # em livro digitalizado, fatias de poucas páginas do próprio PDF.
        if job.scanned:
            units: list = segment_pdf_parts(pdf_bytes, seg)
        else:
            text = segment_text(book, seg)
            units = split_into_chunks(text) if text.strip() else []
        job.planned[key] = len(units)
        job.translations.setdefault(key, [])
        if not units:
            note = f"{seg.title}: sem texto para traduzir (página só com imagem ou em branco)."
            if note not in job.warnings:
                job.warnings.append(note)
        job.save()
        yield Event("segment_start", segment=i, chunks=len(units))

        previous = job.translations[key][-1][-CONTEXT_TAIL_CHARS:] if job.translations[key] else _previous_tail(job, i)
        for c in range(len(job.translations[key]), len(units)):
            yield Event("chunk_start", segment=i, chunk=c, chunks=len(units))
            message = build_user_message(
                None if job.scanned else units[c],
                source_language=options.source_language,
                variant=options.variant,
                bible_format=options.bible_format,
                translator_notes=options.translator_notes,
                gloss_terms=options.gloss_terms,
                part=c + 1,
                total_parts=len(units),
                previous_tail=previous,
                chapter_title=seg.title,
            )
            content = engine.document_content(units[c], message) if job.scanned else message

            report = TranslationReport(notify=notify)
            pieces = []
            label = f"{seg.title} (parte {c + 1})"
            for piece in engine.stream(client, content, options.effort, report, label):
                if isinstance(piece, Restart):  # o motor vai refazer o trecho do zero
                    pieces.clear()
                    yield Event("restart", segment=i, chunk=c)
                    continue
                pieces.append(piece)
                yield Event("text", segment=i, chunk=c, text=piece)

            translated = "".join(pieces)
            job.translations[key].append(translated)
            job.warnings.extend(report.warnings)
            job.input_tokens += report.input_tokens
            job.output_tokens += report.output_tokens
            job.save()
            previous = translated[-CONTEXT_TAIL_CHARS:]
            yield Event("chunk_done", segment=i, chunk=c, chunks=len(units))

        yield Event("segment_done", segment=i)
