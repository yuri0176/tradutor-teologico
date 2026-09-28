#!/usr/bin/env python3
"""Tradução de livros pela linha de comando, sem navegador.

Roda sozinho por horas: traduz capítulo por capítulo, salva o progresso depois
de cada trecho, espera quando a API pede e gera o livro em PDF, TXT e Markdown.

Exemplos:

    # ver os capítulos que o programa encontrou
    python traduzir_livro.py livro.pdf --listar

    # traduzir tudo (pode parar e recomeçar quando quiser)
    python traduzir_livro.py livro.pdf --titulo "Ética Cristã"

    # só alguns capítulos, do inglês, com o Claude
    python traduzir_livro.py livro.pdf --capitulos 1-8,12 --idioma inglês --motor claude
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from tradutor.book import BookJob, job_id, run_book
from tradutor.chapters import (
    auto_split,
    read_book,
    split_by_headings,
    split_by_outline,
    split_fixed,
)
from tradutor.export import to_markdown, to_pdf, to_txt
from tradutor.pdf_utils import PdfError
from tradutor.translator import API_ERRORS, TranslationOptions, get_engine

IDIOMAS = ["auto", "inglês", "espanhol", "alemão", "francês", "italiano", "holandês", "latim"]
QUALIDADES = {"rapida": "medium", "alta": "high", "maxima": "xhigh"}


def parse_chapters(spec: str, total: int) -> list[int]:
    """"1-8,12" -> índices base 0. Os números seguem a lista de --listar."""
    chosen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
            else:
                a = b = int(part)
        except ValueError:
            raise SystemExit(f"Não entendi o capítulo '{part}'. Use algo como 1-8,12.")
        chosen.update(range(a - 1, b))
    fora = [i + 1 for i in chosen if not 0 <= i < total]
    if fora:
        raise SystemExit(f"O livro tem {total} partes; não existe a parte {fora[0]}.")
    return sorted(chosen)


def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Traduz um livro em PDF para o português, capítulo por capítulo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("pdf", type=Path, help="o livro em PDF")
    p.add_argument("--listar", action="store_true", help="mostra os capítulos encontrados e sai")
    p.add_argument("--capitulos", help="quais traduzir, ex.: 1-8,12 (padrão: todos)")
    p.add_argument("--titulo", help="título na capa do PDF (padrão: nome do arquivo)")
    p.add_argument("--saida", type=Path, help="nome-base dos arquivos gerados")
    p.add_argument("--idioma", choices=IDIOMAS, default="auto", help="idioma de origem")
    p.add_argument("--motor", choices=["gemini", "claude"], default="gemini")
    p.add_argument("--qualidade", choices=list(QUALIDADES), default="alta")
    p.add_argument(
        "--dividir",
        choices=["auto", "sumario", "titulos", "paginas"],
        default="auto",
        help="como separar os capítulos",
    )
    p.add_argument("--nivel", type=int, default=2, help="nível do sumário, com --dividir sumario")
    p.add_argument("--paginas", type=int, default=20, help="páginas por bloco, com --dividir paginas")
    p.add_argument("--variante", default="português do Brasil")
    p.add_argument("--referencias", choices=["ponto", "dois-pontos"], default="ponto")
    p.add_argument("--notas-tradutor", action="store_true", help="permite notas [N.T.]")
    p.add_argument("--termo-original", action="store_true", help="termo original na 1ª ocorrência")
    return p.parse_args()


def choose_split(book, args) -> tuple[str, list]:
    if args.dividir == "auto":
        return auto_split(book, args.paginas)
    if args.dividir == "sumario":
        segments = split_by_outline(book, args.nivel)
        if not segments:
            raise SystemExit("Este PDF não tem sumário embutido. Use --dividir titulos ou paginas.")
        return f"sumário do PDF (nível {args.nivel})", segments
    if args.dividir == "titulos":
        segments = split_by_headings(book)
        if not segments:
            raise SystemExit("Nenhum título de capítulo encontrado. Use --dividir paginas.")
        return "títulos encontrados no texto", segments
    return f"blocos de {args.paginas} páginas", split_fixed(book.page_count, args.paginas)


def write_outputs(job: BookJob, base: Path) -> None:
    parts = job.translated_segments()
    if not parts:
        return
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".txt").write_text(to_txt(job.book_title, parts), encoding="utf-8")
    base.with_suffix(".md").write_text(to_markdown(job.book_title, parts), encoding="utf-8")
    base.with_suffix(".pdf").write_bytes(to_pdf(job.book_title, parts))


def main() -> int:
    load_dotenv()
    args = build_args()
    if not args.pdf.exists():
        raise SystemExit(f"Arquivo não encontrado: {args.pdf}")

    data = args.pdf.read_bytes()
    try:
        book = read_book(data)
    except PdfError as exc:
        raise SystemExit(str(exc))

    description, segments = choose_split(book, args)
    print(f"{book.page_count} páginas · {len(segments)} partes · divisão por {description}")
    if book.likely_scanned:
        print("PDF sem texto selecionável: as páginas serão enviadas como imagem.")

    if args.listar:
        for i, seg in enumerate(segments, start=1):
            print(f"{i:>4}. {seg.title}  (págs. {seg.pages_label})")
        return 0

    selected = parse_chapters(args.capitulos, len(segments)) if args.capitulos else list(range(len(segments)))

    options = TranslationOptions(
        source_language=args.idioma,
        variant=args.variante,
        bible_format=args.referencias,
        translator_notes=args.notas_tradutor,
        gloss_terms=args.termo_original,
        effort=QUALIDADES[args.qualidade],
        engine=args.motor,
    )
    engine = get_engine(args.motor)
    api_key = os.getenv(engine.KEY_ENV)
    if not api_key:
        raise SystemExit(f"Falta a chave: coloque {engine.KEY_ENV} no arquivo .env. {engine.KEY_HELP}.")

    title = args.titulo or args.pdf.stem
    base = args.saida or Path(f"{title} - tradução")
    jid = job_id(data, options, segments, book.likely_scanned)
    job = BookJob.load(jid) or BookJob(
        id=jid,
        book_title=title,
        segments=[s.to_dict() for s in segments],
        selected=selected,
        scanned=book.likely_scanned,
    )
    job.book_title = title
    job.segments = [s.to_dict() for s in segments]
    job.selected = selected
    job.save()

    total = len(selected)
    print(f"Motor: {engine.LABEL} · modelo {engine.get_model()}")
    print(f"{total} partes selecionadas · {job.done_count} já traduzidas")
    print(f"Progresso salvo em {job.path}")
    print("Pode interromper com Ctrl+C: nada do que já foi traduzido se perde.\n")

    client = engine.make_client(api_key)
    started = time.time()
    order = {seg: n for n, seg in enumerate(sorted(selected))}
    interrupted = False
    try:
        for ev in run_book(client, data, book, job, options, notify=lambda m: m and print(f"   … {m}")):
            seg = job.segment(ev.segment)
            n = order[ev.segment] + 1
            if ev.kind == "skip":
                print(f"[{n}/{total}] {seg.title} — já traduzido")
            elif ev.kind == "segment_start":
                print(f"[{n}/{total}] {seg.title} — {ev.chunks} trecho(s)", flush=True)
            elif ev.kind == "restart":
                print("   … refazendo este trecho", flush=True)
            elif ev.kind == "chunk_done":
                print(f"   trecho {ev.chunk + 1}/{ev.chunks} pronto", flush=True)
            elif ev.kind == "segment_done":
                write_outputs(job, base)  # salva o livro a cada capítulo
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrompido. O progresso está salvo.")
    except API_ERRORS as exc:
        print(f"\n{engine.describe_error(exc)}", file=sys.stderr)
        write_outputs(job, base)
        print("O progresso está salvo. Rode o mesmo comando de novo para continuar.", file=sys.stderr)
        return 1

    write_outputs(job, base)
    minutes = (time.time() - started) / 60
    fmt = lambda n: f"{n:,}".replace(",", ".")
    print(f"\n{job.done_count}/{total} partes · {minutes:.0f} min · "
          f"{fmt(job.input_tokens)} tokens de entrada, {fmt(job.output_tokens)} de saída")
    for warning in job.warnings:
        print(f"  aviso: {warning}")
    for ext in (".pdf", ".txt", ".md"):
        print(f"  {base.with_suffix(ext)}")
    if interrupted or not job.finished:
        print("\nRode o mesmo comando de novo para continuar de onde parou.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
