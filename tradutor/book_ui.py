"""Interface do modo "Livro / PDF longo"."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import streamlit as st

from .book import BookJob, job_id, run_book
from .chapters import (
    Book,
    Segment,
    auto_split,
    read_book,
    segment_text,
    split_by_headings,
    split_by_outline,
    split_fixed,
    SCANNED_PAGES_PER_REQUEST,
)
from .export import to_markdown, to_pdf, to_txt
from .pdf_utils import PdfError
from .translator import (
    API_ERRORS,
    TranslationOptions,
    estimate_tokens,
    get_engine,
    split_into_chunks,
)


@st.cache_data(max_entries=2, show_spinner="Lendo o PDF…")
def _read_book_cached(data: bytes, _digest: str) -> Book:
    return read_book(data)


@dataclass
class BookInput:
    pdf_bytes: bytes
    book: Book
    segments: list[Segment]
    selected: list[int]
    title: str


def render_input() -> BookInput | None:
    """Upload, divisão em capítulos e seleção. Retorna None enquanto não houver PDF."""
    uploaded = st.file_uploader(
        "Livro ou PDF longo",
        type=["pdf"],
        help="Até 200 MB. O livro é dividido em capítulos e traduzido em sequência.",
        key="book_upload",
    )
    if uploaded is None:
        return None

    data = uploaded.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    try:
        book = _read_book_cached(data, digest)
    except PdfError as exc:
        st.error(str(exc))
        return None

    if book.likely_scanned:
        st.info(
            f"{book.page_count} páginas sem texto selecionável (livro digitalizado). "
            f"As páginas serão enviadas como imagem, {SCANNED_PAGES_PER_REQUEST} por vez. "
            "Funciona, mas custa mais que um PDF com texto."
        )

    # ---------------------------------------------------------- divisão
    methods = ["Automático"]
    if book.outline:
        methods.append("Sumário do PDF")
    methods += ["Títulos no texto", "Blocos de páginas"]
    method = st.radio("Dividir em capítulos por", methods, horizontal=True)

    if method == "Automático":
        description, segments = auto_split(book)
    elif method == "Sumário do PDF":
        max_depth = min(book.outline_depth, 4)
        depth = (
            st.select_slider(
                "Nível do sumário",
                options=list(range(1, max_depth + 1)),
                value=min(2, max_depth),
                help="1 = só as divisões maiores (ex.: partes); 2 = também os capítulos; …",
            )
            if max_depth > 1
            else 1
        )
        segments = split_by_outline(book, depth)
        description = f"sumário do PDF (nível {depth})"
    elif method == "Títulos no texto":
        segments = split_by_headings(book)
        description = "títulos de capítulo encontrados no texto"
        if not segments:
            st.warning("Nenhum título de capítulo encontrado; usando blocos de 20 páginas.")
            segments = split_fixed(book.page_count, 20)
            description = "blocos de 20 páginas"
    else:
        size = st.number_input("Páginas por bloco", min_value=2, max_value=100, value=20, step=1)
        segments = split_fixed(book.page_count, int(size))
        description = f"blocos de {size} páginas"

    st.caption(f"{book.page_count} páginas · {len(segments)} partes · divisão por {description}")

    # ---------------------------------------------------------- seleção
    table = pd.DataFrame(
        {
            "Nº": list(range(1, len(segments) + 1)),
            "Traduzir": [True] * len(segments),
            "Capítulo": [s.title for s in segments],
            "Páginas": [s.pages_label for s in segments],
            "Palavras": [
                len(segment_text(book, s).split()) if not book.likely_scanned else None
                for s in segments
            ],
        }
    )
    edited = st.data_editor(
        table,
        key=f"segments_{digest[:12]}_{description}",
        hide_index=True,
        use_container_width=True,
        disabled=["Nº", "Páginas", "Palavras"],
        column_config={
            "Nº": st.column_config.NumberColumn(width="small"),
            "Traduzir": st.column_config.CheckboxColumn(width="small"),
            "Capítulo": st.column_config.TextColumn(width="large"),
        },
    )
    for seg, title in zip(segments, edited["Capítulo"]):
        seg.title = str(title).strip() or seg.title
    # Faixa de partes: mais fácil que desmarcar dezenas de caixas no celular.
    # A tradução vale para as partes que estão marcadas E dentro da faixa.
    total = len(segments)
    range_key = f"range_{digest[:12]}_{description}"
    col_from, col_to = st.columns(2)
    first = col_from.number_input("Traduzir da parte", 1, total, 1, key=f"{range_key}_from")
    last = col_to.number_input("até a parte", 1, total, total, key=f"{range_key}_to")
    if first > last:
        st.warning("A parte inicial é maior que a final; nenhuma parte será traduzida.")
    selected = [
        i for i, flag in enumerate(edited["Traduzir"]) if flag and first - 1 <= i <= last - 1
    ]
    if first != 1 or last != total:
        st.caption(f"Faixa {first}–{last}: {len(selected)} parte(s) marcada(s) dentro dela.")

    title = st.text_input("Título do livro (para a capa do PDF)", value=Path(uploaded.name).stem)
    return BookInput(data, book, segments, selected, title.strip())


def _estimate(inp: BookInput, engine_name: str) -> str:
    segs = [inp.segments[i] for i in inp.selected]
    if inp.book.likely_scanned:
        pages = sum(s.end - s.start for s in segs)
        requests = sum(-(-(s.end - s.start) // SCANNED_PAGES_PER_REQUEST) for s in segs)
        # ~450 palavras (~2.700 caracteres) por página; cada página como imagem
        # custa ~260 tokens de entrada no Gemini e ~1.600 no Claude.
        tokens_in, tokens_out = estimate_tokens(pages * 2700, requests)
        tokens_in += pages * (260 if engine_name == "gemini" else 1600)
    else:
        texts = [segment_text(inp.book, s) for s in segs]
        requests = sum(len(split_into_chunks(t)) for t in texts if t.strip())
        tokens_in, tokens_out = estimate_tokens(sum(len(t) for t in texts), requests)
    fmt = lambda n: f"{n:,}".replace(",", ".")
    line = (
        f"{len(segs)} partes selecionadas · **{requests} pedidos à API** · ≈ {fmt(tokens_in)} "
        f"tokens de entrada e {fmt(tokens_out)} de saída."
    )
    engine = get_engine(engine_name)
    if engine_name == "gemini":
        return line + (
            " **Gemini: grátis.** O plano gratuito limita pedidos por minuto e por dia: o app "
            "espera sozinho no limite por minuto; se a cota diária acabar, continue no dia seguinte."
        )
    price = engine.price_per_mtok()
    if price is None:
        return line + " **Claude: pago por uso.**"
    cost = (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
    brl = f"{cost:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return line + f" **Claude: ≈ US$ {brl}** (estimativa; o raciocínio interno do modelo pode aumentar o custo)."


def render_run(inp: BookInput, options: TranslationOptions, api_key: str | None) -> None:
    """Botões, progresso da tradução e downloads."""
    if not inp.selected:
        st.warning("Marque pelo menos um capítulo na tabela.")
        return

    jid = job_id(inp.pdf_bytes, options, inp.segments, inp.book.likely_scanned)
    job = BookJob.load(jid) or BookJob(
        id=jid,
        book_title=inp.title,
        segments=[s.to_dict() for s in inp.segments],
        selected=inp.selected,
        scanned=inp.book.likely_scanned,
    )
    # Títulos e seleção podem mudar sem perder o que já foi traduzido.
    job.book_title = inp.title
    job.segments = [s.to_dict() for s in inp.segments]
    job.selected = inp.selected

    engine = get_engine(options.engine)
    st.markdown(_estimate(inp, options.engine))
    if st.session_state.pop("book_finished_msg", False):
        st.success("Livro traduzido! Baixe o resultado abaixo.")
    started = any(job.translations.values())
    if started:
        st.info(f"Progresso salvo: {job.done_count} de {len(job.selected)} partes já traduzidas.")
    _render_progress_file(job, started)

    label = "Continuar tradução" if started and not job.finished else "Traduzir livro"
    start = st.button(label, type="primary", use_container_width=True, disabled=job.finished)
    if started and st.button("Recomeçar do zero", use_container_width=True):
        job.delete()
        st.rerun()
    if started:
        _render_redo(job)

    if start:
        if not api_key:
            st.error(f"Falta a chave do {engine.LABEL}. Coloque-a no .env ({engine.KEY_ENV}) ou na barra lateral.")
            return
        _run(engine.make_client(api_key), inp, job, options)

    _render_downloads(job)


def _run(client, inp: BookInput, job: BookJob, options: TranslationOptions) -> None:
    # Qualquer clique interrompe o script do Streamlit; como cada trecho é salvo
    # ao terminar, "Pausar" apenas para, e o botão "Continuar" retoma depois.
    st.button("⏸️ Pausar (o progresso fica salvo)", use_container_width=True)
    st.caption("Mantenha esta página aberta. Se ela fechar, envie o mesmo PDF e toque em Continuar.")

    total = len(job.selected)
    order = {seg: n for n, seg in enumerate(sorted(job.selected))}
    progress = st.progress(job.done_count / total, text="Preparando…")
    waiting = st.empty()
    log = st.container()
    live = st.empty()

    def notify(msg: str) -> None:
        if msg:
            waiting.info(msg)
        else:
            waiting.empty()

    current, shown = "", 0
    try:
        for ev in run_book(client, inp.pdf_bytes, inp.book, job, options, notify):
            seg = job.segment(ev.segment)
            n = order[ev.segment]
            if ev.kind == "chunk_start":
                current, shown = "", 0
                frac = (n + ev.chunk / max(ev.chunks, 1)) / total
                progress.progress(
                    min(frac, 1.0),
                    text=f"Parte {n + 1} de {total}: {seg.title} — trecho {ev.chunk + 1} de {ev.chunks}",
                )
            elif ev.kind == "restart":
                current, shown = "", 0
                live.empty()
            elif ev.kind == "text":
                current += ev.text
                if len(current) - shown > 400:  # atualiza a tela aos poucos
                    shown = len(current)
                    live.markdown(current[-2500:])
            elif ev.kind == "segment_done":
                log.write(f"✅ {seg.title}")
        progress.progress(1.0, text="Tradução concluída.")
        live.empty()
        # Redesenha a página com o estado final (downloads, "Refazer capítulos").
        st.session_state["book_finished_msg"] = True
        st.rerun()
    except API_ERRORS as exc:
        msg = get_engine(options.engine).describe_error(exc)
        st.error(msg + " O progresso está salvo: toque em Continuar para retomar.")


def _render_progress_file(job: BookJob, started: bool) -> None:
    """Baixar/retomar o progresso por arquivo.

    O disco do Streamlit é apagado quando o app hiberna; com esse arquivo o
    trabalho continua em outro dia ou em outro aparelho."""
    with st.expander("💾 Arquivo de progresso", expanded=not started):
        st.caption(
            "Baixe este arquivo ao parar por hoje. Da próxima vez, envie o mesmo PDF, "
            "escolha a mesma divisão de capítulos e reenvie o arquivo aqui: a tradução "
            "continua de onde parou, sem refazer nada."
        )
        if started:
            st.download_button(
                "⬇️ Baixar progresso (.json)",
                data=job.to_json(),
                file_name=f"progresso - {(job.book_title or 'livro').strip().replace('/', '-')}.json",
                mime="application/json",
                use_container_width=True,
                on_click="ignore",
            )
        upload = st.file_uploader(
            "Retomar de um arquivo de progresso",
            type=["json"],
            key=f"progress_upload_{job.id}",
        )
        if upload is None:
            return
        marker = (job.id, upload.file_id)
        if st.session_state.get("progress_imported") == marker:
            return
        try:
            other = BookJob.from_json(upload.getvalue())
        except (ValueError, TypeError):
            st.error("Arquivo de progresso inválido.")
            return
        if not other.same_division(job.segments):
            st.error(
                "Esse progresso é de outra divisão de capítulos. Escolha acima a mesma "
                "divisão usada antes (o mesmo método e o mesmo nível de sumário)."
            )
            return
        job.adopt(other)
        st.session_state["progress_imported"] = marker
        st.rerun()


def _render_redo(job: BookJob) -> None:
    """Apaga a tradução de alguns capítulos para refazê-los (ex.: com o Claude)."""
    done = [i for i in sorted(job.selected) if job.translations.get(str(i))]
    if not done:
        return
    key = f"redo_{job.id}"
    # Fica aberto enquanto houver capítulos escolhidos (a escolha recarrega a página).
    with st.expander("Refazer capítulos", expanded=bool(st.session_state.get(key))):
        st.caption(
            "Escolha capítulos já traduzidos para traduzir de novo, por exemplo com o outro "
            "motor (troque na barra lateral). Depois toque em Continuar tradução."
        )
        chosen = st.multiselect(
            "Capítulos",
            done,
            format_func=lambda i: job.segment(i).title,
            key=key,
        )
        if st.button("Apagar a tradução destes capítulos", disabled=not chosen, use_container_width=True):
            job.reset_segments(chosen)
            st.rerun()


def _render_downloads(job: BookJob) -> None:
    parts = job.translated_segments()
    if not parts:
        return
    st.divider()
    st.subheader("Livro traduzido")
    if not job.finished:
        st.caption(f"Tradução parcial: {job.done_count} de {len(job.selected)} partes. Você pode baixar o que já está pronto.")
    for warning in job.warnings:
        st.warning(warning)
    st.caption(
        f"Tokens usados até agora: {job.input_tokens:,} de entrada · {job.output_tokens:,} de saída".replace(",", ".")
    )

    base = (job.book_title or "livro").strip().replace("/", "-") + " - tradução"
    st.download_button(
        "⬇️ Baixar TXT",
        data=to_txt(job.book_title, parts).encode("utf-8"),
        file_name=f"{base}.txt",
        mime="text/plain",
        use_container_width=True,
        on_click="ignore",
    )
    st.download_button(
        "⬇️ Baixar Markdown (.md)",
        data=to_markdown(job.book_title, parts).encode("utf-8"),
        file_name=f"{base}.md",
        mime="text/markdown",
        use_container_width=True,
        on_click="ignore",
    )

    # O PDF leva alguns segundos por centena de páginas: só é gerado quando pedido.
    pdf_key = f"pdf_{job.id}_{sum(len(t) for _, t in parts)}_{job.book_title}"
    if pdf_key not in st.session_state:
        if st.button("📄 Gerar PDF", use_container_width=True):
            with st.spinner("Montando o PDF…"):
                st.session_state[pdf_key] = to_pdf(job.book_title, parts)
            st.rerun()
    else:
        st.download_button(
            "⬇️ Baixar PDF",
            data=st.session_state[pdf_key],
            file_name=f"{base}.pdf",
            mime="application/pdf",
            type="primary",
            use_container_width=True,
            on_click="ignore",
        )
