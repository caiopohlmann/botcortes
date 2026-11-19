"""Processo pesado: consome a fila de jobs e gera os clipes (GPU)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from cortes.adapters.media.render import FfmpegMedia
from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.adapters.storage.sqlite_store import SqliteStore
from cortes.adapters.transcription.whisper import FasterWhisperTranscriber
from cortes.application.channel_session import new_moment_id
from cortes.application.clip_pipeline import ClipPipeline, PipelineConfig
from cortes.application.edit_kit import SOURCE_FILE
from cortes.channel_config import ChannelList
from cortes.config import Settings, WorkerSettings
from cortes.domain.models import Channel, Moment, MomentStatus, Transcript
from cortes.domain.ports import Transcriber
from cortes.entrypoints.restart import (
    SYSTEM,
    PauseRequested,
    RestartRequested,
    watch_pause,
    watch_restart,
)
from cortes.entrypoints.status import Collector, MetricsRecorder, heartbeat_loop
from cortes.observability import error_counter, gpu_stats_loop, query_gpu

log = structlog.get_logger(__name__)


class _NoTranscriber:
    """Para o `rerender`: a legenda vem do kit de edição, o Whisper nem é carregado."""

    def transcribe(self, pcm16: bytes, sample_rate: int, language: str | None) -> Transcript:
        raise RuntimeError("transcrição desativada neste comando")


def reloading_channel_lookup(settings: Settings, channels_file: Path) -> Callable[[str], Channel]:
    """settings.channel(), mas com a lista do channels.yaml atual (canal novo sem reiniciar)."""
    channel_list = ChannelList(settings.channels, channels_file, name="worker_channels")

    def lookup(ref: str) -> Channel:
        current = list(channel_list.current())
        return settings.model_copy(update={"channels": current}).channel(ref)

    return lookup


async def build_pipeline(
    settings: Settings,
    store: SqliteStore,
    *,
    force: bool = False,
    whisper: bool = True,
    channels_file: Path | None = None,
) -> ClipPipeline:
    media = FfmpegMedia(
        settings.recording.ffmpeg_path,
        settings.render,
        settings.caption_style,
        settings.captions,
        settings.audio,
    )
    transcriber: Transcriber
    if whisper:
        log.info("whisper_loading", model=settings.transcription.model)
        # Carregar o modelo leva alguns segundos: fora do event loop.
        transcriber = await asyncio.to_thread(FasterWhisperTranscriber, settings.transcription)
    else:
        transcriber = _NoTranscriber()
    weights = settings.scoring
    if force:
        weights = dataclasses.replace(weights, min_final_score=0.0)
    paths = settings.paths
    return ClipPipeline(
        store=store,
        media=media,
        transcriber=transcriber,
        channel_lookup=reloading_channel_lookup(settings, channels_file)
        if channels_file
        else settings.channel,
        config=PipelineConfig(
            weights=weights,
            captions=settings.captions,
            output_dir=paths.clips,
            work_root=paths.work,
            hashtags=tuple(settings.publish.hashtags),
            description_template=settings.publish.description_template,
            title_max_chars=settings.publish.title_max_chars,
            keep_work_files=settings.worker.keep_work_files,
            keep_edit_kit=settings.worker.edit_kit_days > 0,
            quality=settings.caption_quality,
            short_clip=settings.short_clip,
            platform_hashtags={
                platform: tuple(tags)
                for platform, tags in settings.publish.platform_hashtags.items()
            },
        ),
        gpu_slots=asyncio.Semaphore(settings.worker.gpu_concurrency),
        encode_slots=asyncio.Semaphore(settings.worker.encode_concurrency),
    )


@dataclass(slots=True)
class WorkerState:
    """O que o worker está fazendo agora (vai no batimento para o painel)."""

    paused: bool = False
    active: dict[int, dict[str, Any]] = field(default_factory=dict)  # slot -> job
    done: int = 0
    failed: int = 0
    last_done_at: float | None = None


async def _job_loop(
    slot: int,
    store: SqliteStatusStore,
    pipeline: ClipPipeline,
    ws: WorkerSettings,
    state: WorkerState,
) -> None:
    lease_s = ws.job_timeout_s + 60
    while True:
        # Pausa pelo painel: termina o job atual e não pega o próximo (a fila espera).
        state.paused = (await asyncio.to_thread(store.get_controls)).worker_paused
        if state.paused:
            await asyncio.sleep(max(ws.poll_s, 2.0))
            continue
        job = await asyncio.to_thread(store.claim, lease_s)
        if job is None:
            await asyncio.sleep(ws.poll_s)
            continue
        bound = log.bind(job=job.id, moment=job.moment_id, attempt=job.attempts, slot=slot)
        bound.info("job_started")
        state.active[slot] = {
            "job": job.id,
            "moment": job.moment_id,
            "attempt": job.attempts,
            "started_at": time.time(),
        }
        try:
            await asyncio.wait_for(pipeline.process(job.moment_id), timeout=ws.job_timeout_s)
        except asyncio.CancelledError:
            await asyncio.to_thread(store.release, job.id)  # volta para a fila no próximo start
            raise
        except Exception as exc:
            state.failed += 1
            bound.exception("job_failed", error=repr(exc))
            await asyncio.to_thread(store.fail, job.id, repr(exc), ws.max_attempts)
            if job.attempts >= ws.max_attempts:
                await asyncio.to_thread(
                    store.set_moment_status,
                    job.moment_id,
                    MomentStatus.FAILED,
                    error=repr(exc)[:500],
                )
        else:
            await asyncio.to_thread(store.complete, job.id)
            state.done += 1
            state.last_done_at = time.time()
            bound.info("job_done")
        finally:
            state.active.pop(slot, None)


def clean_work_dirs(
    store: SqliteStore, work_root: Path, retention_s: float, edit_kit_s: float = 0.0
) -> list[Path]:
    """Apaga pastas antigas de data/work.

    - momento na fila (QUEUED): nunca apaga;
    - corte pronto (RENDERED): a pasta é o kit de edição, apagado após `edit_kit_s`;
    - o resto (erro, descartado, sobra de processo que morreu): após `retention_s`.
    """
    removed: list[Path] = []
    if not work_root.is_dir():
        return removed
    now = time.time()
    for directory in work_root.iterdir():
        try:
            if not directory.is_dir():
                continue
            age = now - directory.stat().st_mtime
        except OSError:
            continue
        moment = store.get_moment(directory.name)
        if moment is not None and moment.status is MomentStatus.QUEUED:
            continue
        limit = (
            edit_kit_s
            if moment is not None and moment.status is MomentStatus.RENDERED
            else (retention_s)
        )
        if limit <= 0 or age <= limit:
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed.append(directory)
    return removed


async def _janitor_loop(
    store: SqliteStore,
    work_root: Path,
    retention_h: float,
    edit_kit_days: float,
    min_free_bytes: int,
) -> None:
    """De hora em hora: limpa sobras de data/work e avisa se o disco estiver enchendo."""
    while True:
        try:
            if retention_h > 0 or edit_kit_days > 0:
                removed = await asyncio.to_thread(
                    clean_work_dirs, store, work_root, retention_h * 3600, edit_kit_days * 86400
                )
                if removed:
                    log.info(
                        "work_dirs_cleaned", count=len(removed), dirs=[p.name for p in removed]
                    )
            free = shutil.disk_usage(work_root).free
            if free < 2 * min_free_bytes:
                log.warning(
                    "disk_space_low",
                    free_gb=round(free / 1024**3, 1),
                    hint="mova/apague clipes antigos de data/clips",
                )
        except Exception as exc:
            log.warning("work_cleanup_failed", error=repr(exc))
        await asyncio.sleep(3600)


async def run_worker(settings: Settings, *, channels_file: Path | None = None) -> None:
    paths = settings.paths
    for directory in (paths.work, paths.clips):
        directory.mkdir(parents=True, exist_ok=True)
    store = SqliteStatusStore(paths.database)
    started_at = time.time()
    pipeline = await build_pipeline(settings, store, channels_file=channels_file)
    ws = settings.worker
    state = WorkerState()
    log.info(
        "worker_started",
        concurrency=ws.concurrency,
        gpu_slots=ws.gpu_concurrency,
        encode_slots=ws.encode_concurrency,
        profile=settings.profile,
    )
    collect = _worker_collector(settings, store, state)
    try:
        async with asyncio.TaskGroup() as tg:
            for slot in range(ws.concurrency):
                tg.create_task(_job_loop(slot, store, pipeline, ws, state))
            tg.create_task(gpu_stats_loop(ws.gpu_stats_interval_s))
            tg.create_task(
                heartbeat_loop(
                    store,
                    "worker",
                    collect,
                    interval_s=settings.panel.heartbeat_s,
                    started_at=started_at,
                )
            )
            tg.create_task(
                _janitor_loop(
                    store, paths.work, ws.work_retention_h, ws.edit_kit_days, paths.min_free_bytes
                )
            )
            tg.create_task(watch_restart(lambda: store.restart_requested_at(SYSTEM), started_at))
            tg.create_task(watch_pause(lambda: store.get_controls().system_paused))
    except* PauseRequested:
        # "Pausar tudo" no painel: o job em andamento voltou para a fila (_job_loop) e a GPU
        # fica livre. O supervisor só sobe o worker de novo no "Iniciar tudo".
        log.info("worker_paused")
    except* RestartRequested:
        # "Reiniciar sistema" no painel. O TaskGroup cancelou os jobs em andamento, e cada um
        # voltou para a fila (_job_loop). Saída normal (código 0): o supervisor sobe o worker.
        log.info("worker_restarting")


def _worker_collector(
    settings: Settings, store: SqliteStatusStore, state: WorkerState
) -> Collector:
    metrics = MetricsRecorder(
        store, "gpu", settings.panel.metrics_interval_s, settings.panel.metrics_retention_days
    )
    psutil_process: Any = None
    try:
        import psutil

        psutil_process = psutil.Process(os.getpid())
    except Exception:  # psutil ausente/erro: o painel só não mostra a RAM do worker
        psutil_process = None

    async def collect() -> dict[str, Any]:
        try:
            gpu = await query_gpu()
        except Exception:
            gpu = None
        if gpu is not None:
            await metrics.maybe_record(
                {k: gpu.get(k) for k in ("util_pct", "encoder_pct", "vram_used_mb", "temp_c")}
            )
        rss_mb = psutil_process.memory_info().rss / 1024**2 if psutil_process else None
        return {
            "paused": state.paused,
            "slots": settings.worker.concurrency,
            "active": list(state.active.values()),
            "done": state.done,
            "failed": state.failed,
            "last_done_at": state.last_done_at,
            "gpu": gpu,
            "process_rss_mb": rss_mb,
            "model": settings.transcription.model,
            "log": error_counter.summary(),
        }

    return collect


async def render_file(
    settings: Settings,
    video: Path,
    *,
    start_s: float,
    duration_s: float,
    channel_login: str,
    event_s: float | None,
    chat_score: float,
    keep_kit: bool = False,
) -> Path | None:
    """Gera um clipe a partir de um arquivo local (testa o pipeline sem Twitch)."""
    video = video.resolve()  # noqa: ASYNC240 (comando de teste, uma vez)
    if not video.is_file():
        raise FileNotFoundError(video)
    paths = settings.paths
    store = SqliteStore(paths.database)
    pipeline = await build_pipeline(settings, store, force=True)
    channel = settings.channel(channel_login)
    now = time.time()
    moment_id = new_moment_id(channel.key, now)
    if keep_kit:
        # Copia o vídeo para a pasta do corte: com a fonte em data/work/<id>/ o pipeline
        # guarda o kit de edição (transcript.json + render.json), e o corte pode ser
        # corrigido depois com `cortes rerender` ou pelo "Pedir ao Claude" do painel.
        kit_dir = paths.work / moment_id
        await asyncio.to_thread(kit_dir.mkdir, parents=True, exist_ok=True)
        video = await asyncio.to_thread(shutil.copyfile, video, kit_dir / SOURCE_FILE)
    # O arquivo é tratado como uma "fonte" cujo relógio começa em 0.
    event_at = start_s + (event_s if event_s is not None else max(0.0, duration_s - 15.0))
    moment = Moment(
        id=moment_id,
        channel=channel.key,
        display_name=channel.name,
        peak_ts=now,
        event_ts=event_at,
        start_ts=start_s,
        end_ts=start_s + duration_s,
        chat_score=chat_score,
        source_path=video,
        source_start_ts=0.0,
        source_end_ts=start_s + duration_s,
        created_at=now,
    )
    await asyncio.to_thread(store.add_moment, moment)
    clip = await pipeline.process(moment.id)
    return clip.video_path if clip else None


async def rerender_clip(settings: Settings, clip_id: str) -> Path:
    """`cortes rerender <id>`: gera o corte de novo a partir do kit de edição."""
    store = SqliteStatusStore(settings.paths.database)
    pipeline = await build_pipeline(settings, store, whisper=False)
    clip = await pipeline.rerender(clip_id)
    return clip.video_path
