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
    default_checked,
    read_book,
    segment_text,
    split_by_headings,
    split_by_outline,
    split_fixed,
)
from tradutor.export import to_markdown, to_pdf, to_txt
from tradutor.engines import gemini
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
    p.add_argument("--capitulos", help="quais traduzir, ex.: 1-8,12 (padrão: o livro todo, sem capa, sumário e páginas vazias)")
    p.add_argument("--tudo", action="store_true", help="traduz também capa, sumário e páginas quase vazias")
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
    p.add_argument("--paralelo", type=int, default=3, metavar="N",
                   help="capítulos traduzidos ao mesmo tempo (padrão 3; 1 = um por vez)")
    p.add_argument("--cota-acabou", choices=["esperar", "sair"], default="esperar",
                   help="se a cota diária do Gemini acabar: esperar zerar e continuar (padrão) ou sair "
                   "(o progresso fica salvo; usado no GitHub Actions)")
    p.add_argument("--saidas-so-no-fim", action="store_true",
                   help="gera PDF/TXT/MD só ao terminar (e não a cada capítulo)")
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

    if args.capitulos:
        selected = parse_chapters(args.capitulos, len(segments))
    elif args.tudo:
        selected = list(range(len(segments)))
    else:  # o livro todo, menos capa, sumário, direitos e páginas quase vazias
        selected = [
            i for i, seg in enumerate(segments)
            if default_checked(seg.title, None if book.likely_scanned else len(segment_text(book, seg)))
        ]

    options = TranslationOptions(
        source_language=args.idioma,
        variant=args.variante,
        bible_format=args.referencias,
        translator_notes=args.notas_tradutor,
        gloss_terms=args.termo_original,
        effort=QUALIDADES[args.qualidade],
        engine=args.motor,
        workers=max(1, args.paralelo),
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
    interrupted = quota_stop = False
    while True:
        try:
            for ev in run_book(client, data, book, job, options, notify=lambda m: m and print(f"   … {m}")):
                if ev.kind == "notify":
                    if ev.text:
                        print(f"   … {ev.text}", flush=True)
                    continue
                seg = job.segment(ev.segment)
                n = order[ev.segment] + 1
                if ev.kind == "skip":
                    print(f"[{n}/{total}] {seg.title} — já traduzido")
                elif ev.kind == "segment_start":
                    print(f"[{n}/{total}] {seg.title} — {ev.chunks} trecho(s)", flush=True)
                elif ev.kind == "restart":
                    print("   … refazendo este trecho", flush=True)
                elif ev.kind == "chunk_done":
                    print(f"   [{n}/{total}] trecho {ev.chunk + 1}/{ev.chunks} pronto", flush=True)
                elif ev.kind == "segment_done" and not args.saidas_so_no_fim:
                    write_outputs(job, base)  # salva o livro a cada capítulo
            break
        except KeyboardInterrupt:
            interrupted = True
            print("\nInterrompido. O progresso está salvo.")
            break
        except gemini.DailyQuotaExceeded as exc:
            print(f"\nCota diária esgotada ({exc.detail}).", flush=True)
            if args.cota_acabou == "sair":
                quota_stop = True
                print("O progresso está salvo; a tradução continua na próxima execução, com a cota do dia seguinte.")
                break
            when = gemini.quota_reset_time()
            print(f"Espero a cota zerar e continuo sozinho ({time.strftime('%d/%m às %H:%M', time.localtime(when))}, "
                  "horário deste computador).", flush=True)
            try:
                while time.time() < when:
                    time.sleep(min(60, max(1.0, when - time.time())))
            except KeyboardInterrupt:
                interrupted = True
                print("\nInterrompido. O progresso está salvo.")
                break
        except API_ERRORS as exc:
            print(f"\n{engine.describe_error(exc)}", file=sys.stderr)
            write_outputs(job, base)
            write_summary(job, total, started)
            print("O progresso está salvo. Rode o mesmo comando de novo para continuar.", file=sys.stderr)
            return 1

    write_outputs(job, base)
    if job.finished and not interrupted:
        base.parent.mkdir(parents=True, exist_ok=True)
        (base.parent / "CONCLUIDO.txt").write_text(f"{job.book_title}: {total} partes traduzidas.\n", encoding="utf-8")
    minutes = (time.time() - started) / 60
    fmt = lambda n: f"{n:,}".replace(",", ".")
    print(f"\n{job.done_count}/{total} partes · {minutes:.0f} min · "
          f"{fmt(job.input_tokens)} tokens de entrada, {fmt(job.output_tokens)} de saída")
    for warning in job.warnings:
        print(f"  aviso: {warning}")
    for ext in (".pdf", ".txt", ".md"):
        print(f"  {base.with_suffix(ext)}")
    write_summary(job, total, started, quota_stop=quota_stop)
    if interrupted or not job.finished:
        print("\nRode o mesmo comando de novo para continuar de onde parou.")
    return 0


def write_summary(job: BookJob, total: int, started: float, quota_stop: bool = False) -> None:
    """No GitHub Actions, mostra um resumo na página da execução (dá para ler no celular)."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    words = sum(len(text.split()) for _, text in job.translated_segments())
    waited = sum(t.get("espera", 0) for t in job.timings)
    models = sorted({t.get("modelo", "") for t in job.timings if t.get("modelo")})
    lines = [
        f"## {job.book_title}",
        f"**{job.done_count} de {total} partes prontas** · {words:,} palavras traduzidas".replace(",", "."),
        f"Esta execução: {(time.time() - started) / 60:.0f} min, dos quais {waited / 60:.0f} min esperando o Google.",
        f"Modelos usados: {', '.join(models) or '—'}",
    ]
    if job.finished:
        lines.append("✅ **Livro completo.** Os arquivos estão na pasta `saida/`.")
    elif quota_stop:
        lines.append("⏳ A cota diária acabou. A execução de amanhã continua de onde parou.")
    lines += [f"- ⚠️ {w}" for w in job.warnings[:20]]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
