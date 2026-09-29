"""Tradução em segundo plano.

Uma thread do servidor continua traduzindo com a página fechada (ou o celular
bloqueado). Se a cota diária do Gemini acabar, ela espera a cota zerar e segue
sozinha. O progresso é salvo em disco a cada trecho, como no modo normal.

Limites do Streamlit Community Cloud: o app "dorme" depois de muitas horas sem
nenhuma visita (cerca de 12 h) e o disco é apagado quando ele reinicia. Para
rodadas de várias horas isso basta; para vários dias, abra o app de vez em
quando e baixe o arquivo de progresso.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace

import httpx

from .book import BookJob, run_book
from .chapters import Book
from .engines import gemini
from .translator import API_ERRORS, TranslationOptions, get_engine

# Erros passageiros: espera e tenta de novo, em vez de desistir.
_TRANSIENT = (gemini.errors.ServerError, httpx.TransportError, ConnectionError)
_TRANSIENT_WAIT = 300  # segundos
_TRANSIENT_MAX = 8

ACTIVE_STATES = ("starting", "running", "waiting")


@dataclass
class Status:
    state: str = "starting"  # starting | running | waiting | done | stopped | error
    total: int = 0
    done: int = 0
    active: dict[int, str] = field(default_factory=dict)  # partes em andamento
    message: str = ""
    error: str = ""
    resume_at: float = 0.0  # instante (epoch) em que a espera termina
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class BackgroundRun:
    def __init__(self, job: BookJob, book: Book, pdf_bytes: bytes, options: TranslationOptions, api_key: str):
        self.job, self.book, self.pdf_bytes, self.options = job, book, pdf_bytes, options
        self._client = get_engine(options.engine).make_client(api_key)
        self._status = Status(total=len(job.selected), done=job.done_count)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"bg-{job.id}", daemon=True)

    # ------------------------------------------------------------------ controle
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def alive(self) -> bool:
        return self._thread.is_alive()

    def snapshot(self) -> Status:
        with self._lock:
            return replace(self._status, active=dict(self._status.active))

    def _set(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._status, key, value)
            self._status.updated_at = time.time()

    # ------------------------------------------------------------------ trabalho
    def _loop(self) -> None:
        transient = 0
        while not self._stop.is_set():
            try:
                self._run_once()
                if self._stop.is_set():
                    self._set(state="stopped", message="Parado. O progresso está salvo.")
                else:
                    self._set(state="done", done=self.job.done_count, active={}, message="Tradução concluída.")
                return
            except gemini.DailyQuotaExceeded as exc:
                # Todos os modelos sem cota hoje: espera a cota zerar e continua.
                self._wait_until(self._quota_reset_time(), f"Cota diária esgotada ({exc.detail}).")
            except _TRANSIENT as exc:
                transient += 1
                if transient > _TRANSIENT_MAX:
                    self._set(state="error", active={}, error=get_engine(self.options.engine).describe_error(exc))
                    return
                self._wait_until(time.time() + _TRANSIENT_WAIT, "Servidor do Gemini instável.")
            except API_ERRORS as exc:
                self._set(state="error", active={}, error=get_engine(self.options.engine).describe_error(exc))
                return
            except Exception as exc:  # erro de programação: não deixa a thread morrer calada
                self._set(state="error", active={}, error=f"Erro inesperado: {exc}")
                return
        self._set(state="stopped", message="Parado. O progresso está salvo.")

    def _run_once(self) -> None:
        self._set(state="running", message="", resume_at=0.0)
        events = run_book(
            self._client, self.pdf_bytes, self.book, self.job, self.options, notify=lambda m: self._set(message=m)
        )
        try:
            for ev in events:
                if self._stop.is_set():
                    return
                if ev.kind == "notify":
                    self._set(message=ev.text)
                    continue
                title = self.job.segment(ev.segment).title
                with self._lock:
                    active = self._status.active
                    if ev.kind == "chunk_start":
                        active[ev.segment] = f"{title[:45]} — trecho {ev.chunk + 1} de {ev.chunks}"
                    elif ev.kind == "segment_done":
                        active.pop(ev.segment, None)
                if ev.kind in ("chunk_start", "chunk_done", "segment_done", "skip"):
                    self._set(done=self.job.done_count)
        finally:
            events.close()  # para os trabalhadores em paralelo

    def _quota_reset_time(self) -> float:
        """Quando o primeiro modelo volta a ter cota (meia-noite do Pacífico)."""
        times = [gemini._UNAVAILABLE.get(m, 0) for m in gemini._model_chain()]
        future = [t for t in times if t > time.time()]
        return min(future) if future else gemini._next_quota_reset()

    def _wait_until(self, when: float, why: str) -> None:
        self._set(state="waiting", active={}, resume_at=when, message=why)
        while not self._stop.is_set() and time.time() < when:
            self._stop.wait(15)


# ---------------------------------------------------------------------- registro
# Vive no processo do servidor: sobrevive a recarregamentos da página e é
# compartilhado entre as sessões (por isso a tradução continua com a página fechada).
_RUNS: dict[str, BackgroundRun] = {}
_REGISTRY_LOCK = threading.Lock()


def get_run(job_id: str) -> BackgroundRun | None:
    with _REGISTRY_LOCK:
        return _RUNS.get(job_id)


def start_run(job: BookJob, book: Book, pdf_bytes: bytes, options: TranslationOptions, api_key: str) -> BackgroundRun:
    """Inicia (ou devolve, se já estiver rodando) a tradução em segundo plano deste trabalho."""
    with _REGISTRY_LOCK:
        run = _RUNS.get(job.id)
        if run and run.alive():
            return run
        run = BackgroundRun(job, book, pdf_bytes, options, api_key)
        _RUNS[job.id] = run
        run.start()
        return run


def clear_run(job_id: str) -> None:
    with _REGISTRY_LOCK:
        run = _RUNS.get(job_id)
        if run and not run.alive():
            _RUNS.pop(job_id, None)
