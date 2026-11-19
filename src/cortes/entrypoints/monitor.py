"""Processo leve, 24/7: status das lives, chat, buffer de gravação e detecção."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from cortes.adapters.recording.streamlink_ffmpeg import StreamlinkFfmpegRecorder
from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.application.channel_session import ChannelSession, SessionConfig
from cortes.application.live_monitor import LiveMonitor
from cortes.channel_config import ChannelList
from cortes.config import Secrets, Settings
from cortes.domain.models import Channel, ChatRef, Controls, LiveStream, Platform
from cortes.domain.ports import ChatSource, StreamStatusProvider
from cortes.entrypoints.restart import (
    SYSTEM,
    PauseRequested,
    RestartRequested,
    watch_pause,
    watch_restart,
)
from cortes.entrypoints.status import (
    MetricsRecorder,
    channel_status_dict,
    controls_dict,
    heartbeat_loop,
)
from cortes.observability import HostSampler, error_counter

log = structlog.get_logger(__name__)

_CHANNELS_CHECK_S = 3.0  # de quanto em quanto tempo confere se channels.yaml mudou


def _validate(settings: Settings) -> None:
    clip = settings.clip
    needed = clip.pre_roll_s + clip.post_roll_s + clip.reaction_offset_s + 30
    if settings.recording.buffer_s < needed:
        log.warning(
            "buffer_too_small",
            buffer_s=settings.recording.buffer_s,
            recommended_min_s=needed,
            hint="aumente recording.buffer_s ou reduza pre_roll/post_roll",
        )


def build_status_providers(
    settings: Settings, secrets: Secrets
) -> tuple[dict[Platform, StreamStatusProvider], list[Callable[[], Awaitable[None]]]]:
    """Um provedor por plataforma usada em channels.yaml (+ funções para fechá-los)."""
    platforms = {c.platform for c in settings.channels}
    providers: dict[Platform, StreamStatusProvider] = {}
    closers: list[Callable[[], Awaitable[None]]] = []
    if Platform.TWITCH in platforms:
        from cortes.adapters.twitch.helix import HelixClient

        helix = HelixClient(secrets.twitch_client_id, secrets.twitch_client_secret)
        providers[Platform.TWITCH] = helix
        closers.append(helix.aclose)
    if Platform.KICK in platforms:
        from cortes.adapters.kick.api import KickOfficialClient, KickStatusProvider

        official = None
        if secrets.kick_client_id and secrets.kick_client_secret:
            official = KickOfficialClient(secrets.kick_client_id, secrets.kick_client_secret)
        kick = KickStatusProvider(official)
        providers[Platform.KICK] = kick
        closers.append(kick.aclose)
    if Platform.YOUTUBE in platforms:
        from cortes.adapters.youtube.status import YouTubeStatusProvider

        youtube = YouTubeStatusProvider(settings.youtube.check_interval_s)
        providers[Platform.YOUTUBE] = youtube
        closers.append(youtube.aclose)
    return providers, closers


def build_chat(settings: Settings, ref: ChatRef, *, chat_hint: str | None = None) -> ChatSource:
    """Chat de uma plataforma. `chat_hint` = id já conhecido pelo status da live."""
    match ref.platform:
        case Platform.TWITCH:
            from cortes.adapters.twitch.irc_chat import TwitchIrcChat

            return TwitchIrcChat(ref.login)
        case Platform.KICK:
            from cortes.adapters.kick.chat import KickRealtimeChat

            return KickRealtimeChat(
                ref.login,
                settings.kick.realtime_api,
                settings.kick.realtime_url,
                chatroom_id=ref.chat_id or chat_hint,
            )
        case Platform.YOUTUBE:
            from cortes.adapters.youtube.chat import YouTubeLiveChat

            return YouTubeLiveChat(
                Channel(login=ref.login, platform=Platform.YOUTUBE),
                settings.youtube,
                video_id=chat_hint,
            )


async def run_monitor(settings: Settings, secrets: Secrets) -> None:
    started_at = time.time()
    _validate(settings)
    paths = settings.paths
    # Buffers de uma execução anterior não têm mais utilidade (timeline em memória se perdeu).
    await asyncio.to_thread(shutil.rmtree, paths.buffers, True)
    paths.buffers.mkdir(parents=True, exist_ok=True)
    paths.work.mkdir(parents=True, exist_ok=True)

    store = SqliteStatusStore(paths.database)
    providers, closers = build_status_providers(settings, secrets)

    def streamlink_args(platform: Platform) -> list[str]:
        args = settings.streamlink_args(platform)
        if platform is Platform.TWITCH and secrets.twitch_oauth_token:
            # Conta Turbo/inscrita: a Twitch não insere anúncios na gravação.
            token = secrets.twitch_oauth_token.removeprefix("OAuth ").strip()
            args += ["--twitch-api-header", f"Authorization=OAuth {token}"]
        return args

    def new_session(channel: Channel, stream: LiveStream) -> ChannelSession:
        main_chat = ChatRef(channel.platform, channel.login, channel.chat_id)
        refs = [main_chat, *channel.extra_chats]
        chats = [build_chat(settings, main_chat, chat_hint=stream.chat_id)]
        chats += [build_chat(settings, ref) for ref in channel.extra_chats]
        session_dir = paths.buffers / channel.key / time.strftime("%Y%m%d-%H%M%S")
        return ChannelSession(
            channel,
            stream,
            recorder=StreamlinkFfmpegRecorder(
                channel.key,
                channel.watch_url,
                session_dir,
                settings.recording,
                streamlink_args=streamlink_args(channel.platform),
                quality=channel.quality,
            ),
            chats=chats,
            store=store,
            queue=store,
            config=SessionConfig(
                scoring=settings.detection,
                detector=settings.detector,
                clip=settings.clip,
                work_dir=paths.work,
                segment_s=settings.recording.segment_s,
                signal_log_interval_s=settings.logging.signal_log_interval_s,
                chat_delay_s=max(settings.chat_delay_s(ref.platform) for ref in refs),
                min_free_bytes=paths.min_free_bytes,
            ),
        )

    async def read_controls() -> Controls:
        return await asyncio.to_thread(store.get_controls)

    monitor = LiveMonitor(
        settings.channels,
        providers,
        new_session,
        controls=read_controls,
        poll_interval_s=settings.monitor.poll_interval_s,
        offline_grace_polls=settings.monitor.offline_grace_polls,
        game_filter=settings.monitor.game_filter,
    )
    channel_list = ChannelList(
        settings.channels,
        secrets.cortes_config_dir.resolve() / "channels.yaml",
        name="monitor_channels",
    )

    async def watch_channels() -> None:
        """Canal novo (ou removido) no channels.yaml entra sem reiniciar o monitor."""
        known = tuple(settings.channels)
        while True:
            await asyncio.sleep(_CHANNELS_CHECK_S)
            channels = await asyncio.to_thread(channel_list.current)
            if channels == known:
                continue
            known = channels
            missing = sorted({c.platform for c in channels} - set(providers), key=str)
            for platform in missing:
                only = [c for c in channels if c.platform is platform]
                try:
                    extra, extra_closers = build_status_providers(
                        settings.model_copy(update={"channels": only}), secrets
                    )
                except Exception as exc:
                    log.error("status_provider_failed", platform=platform.value, error=repr(exc))
                    continue
                providers.update(extra)
                closers.extend(extra_closers)
                monitor.add_providers(extra)
            monitor.set_channels(channels)

    log.info(
        "monitor_started",
        channels=[c.key for c in settings.channels],
        profile=settings.profile,
        data_dir=str(paths.data_dir),
        disk_free_gb=round(shutil.disk_usage(paths.data_dir).free / 1024**3, 1),
    )
    sampler = HostSampler(paths.data_dir)
    metrics = MetricsRecorder(
        store, "host", settings.panel.metrics_interval_s, settings.panel.metrics_retention_days
    )

    async def collect() -> dict[str, Any]:
        host = sampler.sample()
        await metrics.maybe_record(
            {
                key: host[key]
                for key in (
                    "cpu_pct",
                    "ram_pct",
                    "ram_used_gb",
                    "disk_free_gb",
                    "net_rx_mbps",
                    "net_tx_mbps",
                    "children",
                    "children_rss_mb",
                )
            }
        )
        return {
            "channels": [channel_status_dict(status) for status in monitor.snapshot()],
            "controls": controls_dict(monitor.controls),
            "host": host,
            "log": error_counter.summary(),
            "min_free_gb": paths.min_free_gb,
            "poll_interval_s": settings.monitor.poll_interval_s,
            # Versão do channels.yaml em uso (o painel avisa se ela ficar para trás).
            "channels_loaded_at": channel_list.loaded_at,
            "channels_error": channel_list.error,
        }

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(monitor.run())
            tg.create_task(watch_channels())
            tg.create_task(
                heartbeat_loop(store, "monitor", collect, interval_s=settings.panel.heartbeat_s)
            )
            tg.create_task(watch_restart(lambda: store.restart_requested_at(SYSTEM), started_at))
            tg.create_task(watch_pause(lambda: store.get_controls().system_paused))
    except* PauseRequested:
        # "Pausar tudo" no painel: mesma saída limpa (sessões fechadas, momento em andamento
        # vai para a fila). O supervisor só sobe o monitor de novo no "Iniciar tudo".
        log.info("monitor_paused")
    except* RestartRequested:
        # "Reiniciar sistema" no painel. As sessões já pararam (o TaskGroup cancelou
        # monitor.run). Saída normal (código 0): o supervisor sobe o monitor de novo, relendo
        # config/channels.yaml.
        log.info("monitor_restarting")
    finally:
        for close in closers:
            with contextlib.suppress(Exception):
                await close()
