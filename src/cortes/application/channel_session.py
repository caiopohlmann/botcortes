"""Sessão de um canal ao vivo: gravação + chat(s) + detecção + captura de momentos.

Cada parte é uma task independente; gravação e chat se reconectam sozinhos. Se a
sessão inteira cair por um bug, o LiveMonitor a recria no próximo poll.

Multistream: além do chat do canal gravado, a sessão pode ler chats de outras
plataformas do mesmo streamer. Todos alimentam o mesmo analisador (um sinal só).
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog

from cortes.domain.detector import (
    ClipParams,
    ClipWindow,
    DetectedPeak,
    DetectorParams,
    MomentDetector,
    clip_window,
)
from cortes.domain.models import (
    Channel,
    ChatSignal,
    LiveStream,
    Moment,
    SessionStats,
)
from cortes.domain.ports import ChatSource, JobQueue, MomentStore, SegmentRecorder
from cortes.domain.scoring import ChatAnalyzer, ScoringParams

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    scoring: ScoringParams
    detector: DetectorParams
    clip: ClipParams
    work_dir: Path
    segment_s: float
    tick_s: float = 1.0
    signal_log_interval_s: float = 0.0
    # Avalia o chat N s no passado: chats que chegam em lotes (YouTube) precisam disso
    # para a janela já estar completa quando for medida. 0 para Twitch/Kick.
    chat_delay_s: float = 0.0
    min_free_bytes: int = 0  # disco quase cheio: não captura novos momentos (0 = sem limite)


def new_moment_id(channel_key: str, peak_ts: float) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(peak_ts))
    return f"{stamp}-{channel_key}-{secrets.token_hex(2)}"


class ChannelSession:
    def __init__(
        self,
        channel: Channel,
        stream: LiveStream,
        *,
        recorder: SegmentRecorder,
        chats: Sequence[ChatSource],
        store: MomentStore,
        queue: JobQueue,
        config: SessionConfig,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._channel = channel
        self._stream = stream
        self._recorder = recorder
        self._chats = list(chats)
        self._store = store
        self._queue = queue
        self._cfg = config
        self._clock = clock
        self._analyzer = ChatAnalyzer(config.scoring)
        self._detector = MomentDetector(config.detector)
        self._log = log.bind(channel=channel.key)
        # Estado para o painel (lido por snapshot(); só esta sessão escreve).
        self._started_at = clock()
        self._last_signal: ChatSignal | None = None
        self._last_message_at: float | None = None
        self._moments_detected = 0
        self._moments_queued = 0
        self._last_moment_at: float | None = None

    def snapshot(self) -> SessionStats:
        signal = self._last_signal
        return SessionStats(
            started_at=self._started_at,
            recorder=self._recorder.stats(),
            chat_rate=signal.rate if signal else 0.0,
            chat_baseline=signal.baseline if signal else 0.0,
            chat_score=signal.score if signal else 0.0,
            chat_warm=signal.warm if signal else False,
            last_message_at=self._last_message_at,
            moments_detected=self._moments_detected,
            moments_queued=self._moments_queued,
            last_moment_at=self._last_moment_at,
        )

    async def run(self) -> None:
        self._log.info(
            "session_started",
            title=self._stream.title,
            game=self._stream.game_name,
            chats=len(self._chats),
        )
        key = self._channel.key
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._recorder.run(), name=f"rec:{key}")
                for index, chat in enumerate(self._chats):
                    tg.create_task(self._consume_chat(chat), name=f"chat{index}:{key}")
                tg.create_task(self._tick_loop(tg), name=f"tick:{key}")
        finally:
            await self._recorder.cleanup()
            self._log.info("session_stopped")

    async def _consume_chat(self, chat: ChatSource) -> None:
        async for message in chat.messages():
            self._analyzer.add(message)
            self._last_message_at = self._clock()

    async def _tick_loop(self, tg: asyncio.TaskGroup) -> None:
        tick = self._cfg.tick_s
        next_at = self._clock()
        last_signal_log = 0.0
        try:
            while True:
                next_at += tick
                await asyncio.sleep(max(0.0, next_at - self._clock()))
                now = self._clock()
                if now - next_at > tick:  # event loop atrasou: não tenta "recuperar" ticks
                    next_at = now
                signal = self._analyzer.evaluate(now - self._cfg.chat_delay_s)
                self._last_signal = signal

                interval = self._cfg.signal_log_interval_s
                if interval > 0 and now - last_signal_log >= interval:
                    last_signal_log = now
                    self._log.info(
                        "chat_signal",
                        rate=round(signal.rate, 2),
                        baseline=round(signal.baseline, 2),
                        z=round(signal.z, 2),
                        hype=round(signal.hype, 2),
                        diversity=round(signal.diversity, 2),
                        score=round(signal.score, 1),
                        warm=signal.warm,
                    )

                peak = self._detector.feed(signal)
                if peak is not None:
                    tg.create_task(self._capture(peak))
        except asyncio.CancelledError:
            # Live acabou no meio de um hype: captura o que já temos.
            pending = self._detector.flush()
            if pending is not None:
                await self._store_moment(pending, self._window(pending))
            raise

    def _window(self, peak: DetectedPeak) -> ClipWindow:
        return clip_window(peak.peak_ts, self._cfg.clip)

    async def _capture(self, peak: DetectedPeak) -> None:
        window = self._window(peak)
        self._moments_detected += 1
        self._log.info(
            "moment_detected",
            score=round(peak.score, 1),
            z=round(peak.signal.z, 2),
            rate=round(peak.signal.rate, 2),
            hype=round(peak.signal.hype, 2),
        )
        # Espera o pós-roll ser gravado (+ folga para o último segmento fechar).
        ready_at = window.end_ts + 2 * self._cfg.segment_s + 1.0
        try:
            await asyncio.sleep(max(0.0, ready_at - self._clock()))
        except asyncio.CancelledError:
            await self._store_moment(peak, window)  # salva o trecho disponível
            raise
        await self._store_moment(peak, window)

    async def _store_moment(self, peak: DetectedPeak, window: ClipWindow) -> None:
        moment_id = new_moment_id(self._channel.key, peak.peak_ts)
        dest = self._cfg.work_dir / moment_id / "source.ts"
        try:
            if self._disk_full():
                return
            snapshot = await self._recorder.snapshot(window.start_ts, window.end_ts, dest)
            if snapshot is None:
                self._log.warning("moment_without_video", moment=moment_id)
                return
            moment = Moment(
                id=moment_id,
                channel=self._channel.key,
                display_name=self._channel.name,
                peak_ts=peak.peak_ts,
                event_ts=window.event_ts,
                start_ts=window.start_ts,
                end_ts=window.end_ts,
                chat_score=peak.score,
                source_path=snapshot.path,
                source_start_ts=snapshot.wall_start,
                source_end_ts=snapshot.wall_end,
                created_at=self._clock(),
            )
            await asyncio.to_thread(self._store.add_moment, moment)
            job_id = await asyncio.to_thread(self._queue.enqueue, moment_id)
            self._moments_queued += 1
            self._last_moment_at = self._clock()
            self._log.info(
                "moment_queued",
                moment=moment_id,
                job=job_id,
                duration_s=round(moment.clip_duration_s, 1),
                score=round(peak.score, 1),
            )
        except Exception as exc:
            # Um momento perdido não pode derrubar a sessão (gravação/chat continuam).
            self._log.exception("moment_capture_failed", moment=moment_id, error=repr(exc))

    def _disk_full(self) -> bool:
        if self._cfg.min_free_bytes <= 0:
            return False
        free = shutil.disk_usage(self._cfg.work_dir).free
        if free >= self._cfg.min_free_bytes:
            return False
        self._log.error(
            "moment_skipped_disk_full",
            free_gb=round(free / 1024**3, 1),
            min_free_gb=round(self._cfg.min_free_bytes / 1024**3, 1),
            hint="libere espaço (data/clips) ou ajuste paths.min_free_gb",
        )
        return True
