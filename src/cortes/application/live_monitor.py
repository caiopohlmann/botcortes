"""Acompanha quais canais estão ao vivo e mantém uma ChannelSession por canal online.

Ciclo (a cada `tick_s`, padrão 2 s):
  1. a cada `poll_interval_s`: consulta o status das plataformas;
  2. lê as pausas do painel;
  3. sincroniza: grava quem está ao vivo e não está pausado; para o resto.

Isolamento de falhas:
- cada sessão é uma task própria; se cair, é recriada com espera crescente (30 s, 60 s...);
- cada plataforma tem seu provedor. Se uma falhar, os canais dela ficam "desconhecidos":
  nada é iniciado nem parado por causa disso, e as outras seguem normalmente.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

from cortes.domain.models import Channel, Controls, LiveStream, Platform, SessionStats
from cortes.domain.ports import StreamStatusProvider

log = structlog.get_logger(__name__)

_MAX_SKIPPED_POLLS = 10  # backoff máximo de uma plataforma com erro (~5 min com poll de 30 s)
_SESSION_RETRY_S = 30.0
_SESSION_RETRY_MAX_S = 600.0


class SessionHandle(Protocol):
    async def run(self) -> None: ...

    def snapshot(self) -> SessionStats: ...


SessionFactory = Callable[[Channel, LiveStream], SessionHandle]
ControlsReader = Callable[[], Awaitable[Controls]]


class ChannelState(StrEnum):
    RECORDING = "recording"  # ao vivo e gravando
    STARTING = "starting"  # ao vivo, sessão prestes a iniciar
    PAUSED = "paused"  # pausado pelo painel
    OFFLINE = "offline"
    FILTERED = "filtered"  # ao vivo, mas fora do game_filter
    RETRYING = "retrying"  # a sessão caiu; nova tentativa em breve
    UNKNOWN = "unknown"  # a plataforma ainda não respondeu


@dataclass(frozen=True, slots=True)
class ChannelStatus:
    channel: Channel
    state: ChannelState
    stream: LiveStream | None  # info da live (também quando pausado/filtrado)
    session: SessionStats | None
    checked_at: float | None  # última vez que a plataforma respondeu sobre este canal
    error: str | None  # última queda da sessão


async def _no_controls() -> Controls:
    return Controls()


class LiveMonitor:
    def __init__(
        self,
        channels: Sequence[Channel],
        providers: Mapping[Platform, StreamStatusProvider],
        session_factory: SessionFactory,
        *,
        controls: ControlsReader = _no_controls,
        poll_interval_s: float = 30.0,
        tick_s: float = 2.0,
        offline_grace_polls: int = 2,
        game_filter: Sequence[str] = (),
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not channels:
            raise ValueError("Nenhum canal configurado em config/channels.yaml")
        missing = {c.platform for c in channels} - set(providers)
        if missing:
            names = ", ".join(sorted(p.value for p in missing))
            raise ValueError(f"sem provedor de status para: {names}")
        self._channels = {c.key: c for c in channels}
        self._providers = dict(providers)
        self._factory = session_factory
        self._read_controls = controls
        self._poll_s = poll_interval_s
        self._tick_s = tick_s
        self._grace = max(1, offline_grace_polls)
        self._games = frozenset(g.casefold() for g in game_filter)
        self._clock = clock
        self._mono = monotonic

        self._controls = Controls()
        self._online: dict[str, LiveStream] = {}  # ao vivo (e dentro do filtro de jogo)
        self._seen: dict[str, tuple[float, LiveStream | None]] = {}  # última resposta
        self._filtered: set[str] = set()
        self._misses: dict[str, int] = {}
        self._stop_reason: dict[str, str] = {}
        self._sessions: dict[str, tuple[asyncio.Task[None], SessionHandle]] = {}
        self._crashes: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._last_error: dict[str, str] = {}
        self._failures: dict[Platform, int] = {}
        self._skip: dict[Platform, int] = {}

    # ------------------------------------------------------------------ ciclo
    async def run(self) -> None:
        next_poll = 0.0
        try:
            while True:
                now = self._mono()
                poll = now >= next_poll
                if poll:
                    next_poll = now + self._poll_s
                await self.cycle(poll=poll)
                await asyncio.sleep(self._tick_s)
        finally:
            await self._stop_all()

    async def cycle(self, *, poll: bool = True) -> None:
        if poll:
            self._apply(await self._poll())
        try:
            self._controls = await self._read_controls()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # banco ocupado etc.: mantém as pausas anteriores
            log.warning("controls_read_failed", error=repr(exc))
        await self._sync()

    def _accepts(self, stream: LiveStream) -> bool:
        # Plataforma que não informa o jogo (YouTube) não é filtrada.
        if not self._games or not stream.game_name:
            return True
        return stream.game_name.casefold() in self._games

    # ------------------------------------------------------------------ status
    async def _poll(self) -> dict[str, LiveStream | None]:
        groups: dict[Platform, list[Channel]] = {}
        for channel in self._channels.values():
            groups.setdefault(channel.platform, []).append(channel)
        due: list[tuple[Platform, list[Channel]]] = []
        for platform, channels in groups.items():
            if self._skip.get(platform, 0) > 0:
                self._skip[platform] -= 1
                continue
            due.append((platform, channels))
        results = await asyncio.gather(*(self._fetch(p, cs) for p, cs in due))
        merged: dict[str, LiveStream | None] = {}
        for result in results:
            merged.update(result)
        return merged

    async def _fetch(
        self, platform: Platform, channels: list[Channel]
    ) -> Mapping[str, LiveStream | None]:
        try:
            result = await self._providers[platform].get_live_streams(channels)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures = self._failures.get(platform, 0) + 1
            self._failures[platform] = failures
            skip = min(2 ** (failures - 1) - 1, _MAX_SKIPPED_POLLS)  # 0, 1, 3, 7, 10...
            self._skip[platform] = skip
            log.warning(
                "status_poll_failed",
                platform=platform.value,
                error=repr(exc),
                retry_in_s=round((skip + 1) * self._poll_s),
            )
            return {}
        self._failures.pop(platform, None)
        wanted = {c.key for c in channels}
        return {key: value for key, value in result.items() if key in wanted}

    def _apply(self, status: Mapping[str, LiveStream | None]) -> None:
        """Atualiza quem está ao vivo. Canal ausente de `status` = desconhecido: não muda."""
        now = self._clock()
        for key, stream in status.items():
            if key not in self._channels:
                continue
            self._seen[key] = (now, stream)
            if stream is not None and self._accepts(stream):
                self._filtered.discard(key)
                self._online[key] = stream
                self._misses[key] = 0
                continue
            if stream is None:
                self._filtered.discard(key)
            else:
                self._filtered.add(key)
            if key not in self._online:
                continue
            self._misses[key] = self._misses.get(key, 0) + 1
            if self._misses[key] >= self._grace:
                self._online.pop(key)
                self._misses.pop(key, None)
                self._crashes.pop(key, None)
                self._retry_at.pop(key, None)
                self._stop_reason[key] = (
                    "offline" if stream is None else f"jogo: {stream.game_name}"
                )

    # ------------------------------------------------------------------ sessões
    async def _sync(self) -> None:
        self._reap()
        now = self._mono()
        for key, channel in self._channels.items():
            wanted = key in self._online and not self._controls.channel_paused(key)
            running = key in self._sessions
            if wanted and not running:
                if self._retry_at.get(key, 0.0) > now:
                    continue
                self._start(channel, self._online[key])
            elif running and not wanted:
                reason = "pausado" if key in self._online else self._stop_reason.pop(key, None)
                log.info("channel_stopping", channel=key, reason=reason or "offline")
                await self._stop(key)

    def _start(self, channel: Channel, stream: LiveStream) -> None:
        log.info("channel_live", channel=channel.key, game=stream.game_name, title=stream.title)
        handle = self._factory(channel, stream)
        task = asyncio.create_task(handle.run(), name=f"session:{channel.key}")
        self._sessions[channel.key] = (task, handle)
        self._retry_at.pop(channel.key, None)

    def _reap(self) -> None:
        """Sessões que terminaram sozinhas (erro) voltam com espera crescente."""
        for key, (task, _) in list(self._sessions.items()):
            if not task.done():
                continue
            del self._sessions[key]
            if task.cancelled():
                continue
            crashes = self._crashes.get(key, 0) + 1
            self._crashes[key] = crashes
            delay = min(_SESSION_RETRY_S * 2 ** (crashes - 1), _SESSION_RETRY_MAX_S)
            self._retry_at[key] = self._mono() + delay
            exc = task.exception()
            self._last_error[key] = repr(exc) if exc else "sessão terminou sem erro"
            log.error(
                "session_crashed",
                channel=key,
                error=self._last_error[key],
                retry_in_s=round(delay),
                exc_info=exc,
            )

    async def _stop(self, key: str) -> None:
        entry = self._sessions.pop(key, None)
        self._misses.pop(key, None)
        if entry is None:
            return
        task, _ = entry
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _stop_all(self) -> None:
        tasks = [task for task, _ in self._sessions.values()]
        self._sessions.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ painel
    @property
    def controls(self) -> Controls:
        return self._controls

    def snapshot(self) -> list[ChannelStatus]:
        statuses: list[ChannelStatus] = []
        now = self._mono()
        for key, channel in self._channels.items():
            seen = self._seen.get(key)
            entry = self._sessions.get(key)
            session = entry[1].snapshot() if entry else None
            if entry is not None:
                state = ChannelState.RECORDING
            elif self._controls.channel_paused(key):
                state = ChannelState.PAUSED
            elif self._retry_at.get(key, 0.0) > now:
                state = ChannelState.RETRYING
            elif key in self._online:
                state = ChannelState.STARTING
            elif key in self._filtered:
                state = ChannelState.FILTERED
            elif seen is None:
                state = ChannelState.UNKNOWN
            else:
                state = ChannelState.OFFLINE
            stream = self._online.get(key) or (seen[1] if seen else None)
            statuses.append(
                ChannelStatus(
                    channel=channel,
                    state=state,
                    stream=stream,
                    session=session,
                    checked_at=seen[0] if seen else None,
                    error=self._last_error.get(key),
                )
            )
        return statuses
