"""Publicação do estado de cada processo para o painel (batimentos + métricas).

O painel lê só o banco. Se um processo travar, o batimento dele "envelhece" e o painel
mostra o processo como parado.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.application.live_monitor import ChannelStatus
from cortes.domain.models import Controls, LiveStream, SessionStats

log = structlog.get_logger(__name__)

Collector = Callable[[], Awaitable[dict[str, Any]]]


def controls_dict(controls: Controls) -> dict[str, Any]:
    return {
        "system_paused": controls.system_paused,
        "paused_all": controls.paused_all,
        "worker_paused": controls.worker_paused,
        "paused_channels": sorted(controls.paused_channels),
    }


def stream_dict(stream: LiveStream | None) -> dict[str, Any] | None:
    if stream is None:
        return None
    return {
        "title": stream.title,
        "game": stream.game_name,
        "viewers": stream.viewer_count,
        "started_at": stream.started_at.timestamp() if stream.started_at else None,
    }


def _session_dict(stats: SessionStats | None) -> dict[str, Any] | None:
    if stats is None:
        return None
    return {
        "started_at": stats.started_at,
        "recorder_restarts": stats.recorder.restarts,
        "last_segment_at": stats.recorder.last_segment_at,
        "buffered_s": stats.recorder.buffered_s,
        "chat_rate": stats.chat_rate,
        "chat_baseline": stats.chat_baseline,
        "chat_score": stats.chat_score,
        "chat_warm": stats.chat_warm,
        "last_message_at": stats.last_message_at,
        "moments_detected": stats.moments_detected,
        "moments_queued": stats.moments_queued,
        "last_moment_at": stats.last_moment_at,
    }


def channel_status_dict(status: ChannelStatus) -> dict[str, Any]:
    channel = status.channel
    return {
        "key": channel.key,
        "platform": channel.platform.value,
        "login": channel.login,
        "name": channel.name,
        "url": channel.url,
        "quality": channel.quality,
        "extra_chats": [f"{ref.platform.value}:{ref.login}" for ref in channel.extra_chats],
        "state": status.state.value,
        "checked_at": status.checked_at,
        "error": status.error,
        "stream": stream_dict(status.stream),
        "session": _session_dict(status.session),
    }


async def heartbeat_loop(
    store: SqliteStatusStore,
    process: str,
    collect: Collector,
    *,
    interval_s: float,
    started_at: float | None = None,
) -> None:
    """Grava o batimento do processo a cada `interval_s` (falhas só viram aviso)."""
    started = started_at or time.time()
    pid = os.getpid()
    while True:
        try:
            data = await collect()
            await asyncio.to_thread(store.write_heartbeat, process, pid, started, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("heartbeat_failed", process=process, error=repr(exc))
        await asyncio.sleep(interval_s)


class MetricsRecorder:
    """Guarda uma amostra a cada `interval_s` e apaga o histórico antigo de hora em hora."""

    def __init__(
        self, store: SqliteStatusStore, source: str, interval_s: float, retention_days: float
    ) -> None:
        self._store = store
        self._source = source
        self._interval = interval_s
        self._retention_s = retention_days * 86400
        self._next_at = 0.0
        self._next_prune = 0.0

    async def maybe_record(self, data: dict[str, Any]) -> None:
        now = time.time()
        if self._interval <= 0 or now < self._next_at:
            return
        self._next_at = now + self._interval
        await asyncio.to_thread(self._store.add_metric, self._source, data, now)
        if now >= self._next_prune:
            self._next_prune = now + 3600
            await asyncio.to_thread(self._store.prune_metrics, now - self._retention_s)
