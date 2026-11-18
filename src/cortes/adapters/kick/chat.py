"""Chat do Kick pelo mesmo WebSocket (Pusher) que a página usa. Leitura anônima, sem conta.

O canal Pusher do chat é "chatrooms.<chatroom_id>.v2". O chatroom_id vem, em ordem:
do status da live, do `chat_id` do channels.yaml ou da API pública do canal.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable

import structlog
from websockets.asyncio.client import ClientConnection, connect
from websockets.typing import Origin

from cortes.adapters.browser_http import USER_AGENT, browser_client
from cortes.adapters.kick.api import fetch_public_channel
from cortes.adapters.kick.parsing import (
    chat_channel,
    parse_frame,
    subscribe_message,
    to_chat_message,
)
from cortes.domain.models import ChatMessage, Platform, channel_key

log = structlog.get_logger(__name__)

_PING_EVERY_S = 60.0  # Pusher derruba conexões sem atividade (activity_timeout ~120 s)


class KickPusherChat:
    def __init__(
        self,
        slug: str,
        pusher_url: str,
        *,
        chatroom_id: str | None = None,
        clock: Callable[[], float] = time.time,
        max_backoff_s: float = 60.0,
    ) -> None:
        self._slug = slug
        self._url = pusher_url
        self._chatroom_id = chatroom_id
        self._clock = clock
        self._max_backoff = max_backoff_s
        self._label = channel_key(Platform.KICK, slug)

    async def _resolve_chatroom(self) -> str:
        if self._chatroom_id:
            return self._chatroom_id
        async with browser_client() as http:
            _, chatroom_id = await fetch_public_channel(http, self._slug, self._label)
        if not chatroom_id:
            raise LookupError("a API do Kick não informou o chatroom")
        self._chatroom_id = chatroom_id
        return chatroom_id

    async def messages(self) -> AsyncIterator[ChatMessage]:
        backoff = 2.0
        while True:
            try:
                chatroom_id = await self._resolve_chatroom()
                async with connect(
                    self._url,
                    open_timeout=15,
                    max_size=2**20,
                    user_agent_header=USER_AGENT,
                    origin=Origin("https://kick.com"),
                ) as ws:
                    await ws.send(subscribe_message(chatroom_id))
                    log.info("chat_connected", channel=self._label, chatroom=chatroom_id)
                    backoff = 2.0
                    pinger = asyncio.create_task(_keepalive(ws))
                    try:
                        async for message in self._read(ws, chatroom_id):
                            yield message
                    finally:
                        pinger.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await pinger
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                hint = None
                if self._chatroom_id is None:
                    hint = "defina chat_id (id do chatroom) no channels.yaml"
                log.warning(
                    "chat_error", channel=self._label, error=repr(exc), retry_in=backoff, hint=hint
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _read(self, ws: ClientConnection, chatroom_id: str) -> AsyncIterator[ChatMessage]:
        expected = chat_channel(chatroom_id)
        async for raw in ws:
            now = self._clock()
            frame = parse_frame(raw)
            if frame is None:
                continue
            if frame.event == "pusher:ping":
                await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
            elif frame.event == "pusher:error":
                # 4000-4099: erro de configuração (ex.: chave do app trocada). Reconecta
                # com backoff e deixa a mensagem no log para ajustar kick.pusher_url.
                raise RuntimeError(f"Pusher recusou: {frame.data}")
            elif frame.event == "pusher_internal:subscription_succeeded":
                log.debug("chat_subscribed", channel=self._label)
            elif frame.channel == expected:
                message = to_chat_message(frame, self._label, now)
                if message is not None:
                    yield message


async def _keepalive(ws: ClientConnection) -> None:
    while True:
        await asyncio.sleep(_PING_EVERY_S)
        await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
