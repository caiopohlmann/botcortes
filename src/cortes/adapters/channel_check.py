"""Confere se um canal existe antes de o painel colocá-lo no channels.yaml.

Uma consulta só, como um espectador abrindo a página do canal. Se a plataforma não
responder (bloqueio, rede), o canal é aceito mesmo assim: o monitor mostra "Sem resposta".
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from cortes.config import Secrets
from cortes.domain.models import Channel, Platform

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChannelCheck:
    display_name: str | None  # nome como a plataforma mostra (None = não informado)
    verified: bool  # False = a plataforma não respondeu; não deu para conferir


async def check_channel(channel: Channel, secrets: Secrets) -> ChannelCheck:
    """Levanta LookupError se a plataforma disser que o canal não existe."""
    try:
        match channel.platform:
            case Platform.TWITCH:
                return await _check_twitch(channel, secrets)
            case Platform.KICK:
                return await _check_kick(channel)
            case Platform.YOUTUBE:
                return await _check_youtube(channel)
    except LookupError:
        raise
    except Exception as exc:
        log.warning("channel_check_failed", channel=channel.key, error=repr(exc))
        return ChannelCheck(display_name=None, verified=False)


async def _check_twitch(channel: Channel, secrets: Secrets) -> ChannelCheck:
    from cortes.adapters.twitch.helix import HelixClient

    helix = HelixClient(secrets.twitch_client_id, secrets.twitch_client_secret)
    try:
        name = await helix.get_display_name(channel.login)
    finally:
        await helix.aclose()
    if name is None:
        raise LookupError(f"a Twitch não tem o canal {channel.url}")
    return ChannelCheck(display_name=name, verified=True)


async def _check_kick(channel: Channel) -> ChannelCheck:
    from cortes.adapters.browser_http import browser_client
    from cortes.adapters.kick.api import fetch_public_channel

    http = browser_client()
    try:
        # 404 vira LookupError; bloqueio do Cloudflare vira outra exceção (aceita sem conferir).
        await fetch_public_channel(http, channel.login, channel.key)
    finally:
        await http.aclose()
    return ChannelCheck(display_name=None, verified=True)


async def _check_youtube(channel: Channel) -> ChannelCheck:
    from cortes.adapters.youtube.status import fetch_live, youtube_client

    http = youtube_client()
    try:
        await fetch_live(http, channel)  # 404 vira LookupError
    finally:
        await http.aclose()
    return ChannelCheck(display_name=None, verified=True)
