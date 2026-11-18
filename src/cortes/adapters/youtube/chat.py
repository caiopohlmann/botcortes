"""Chat do YouTube pelo mesmo caminho que a janela de chat do site usa (sem API/cota).

1. abre youtube.com/live_chat?v=<id> e pega a "continuation" do chat completo;
2. chama youtubei/v1/live_chat/get_live_chat repetidamente; cada resposta traz as
   mensagens novas e a próxima continuation.

O intervalo sugerido pelo YouTube varia (1-10 s). Limitamos a `chat_poll_max_s` para a
taxa de mensagens não chegar "em degraus", e o horário de cada mensagem é o do servidor
(ver LagTracker), não o da chegada do lote.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable

import httpx
import structlog

from cortes.adapters.youtube.parsing import (
    ChatBootstrap,
    LagTracker,
    action_to_message,
    parse_chat_page,
    parse_chat_response,
)
from cortes.adapters.youtube.status import fetch_live, youtube_client
from cortes.config import YouTubeSettings
from cortes.domain.models import Channel, ChatMessage

log = structlog.get_logger(__name__)

_CHAT_PAGE = "https://www.youtube.com/live_chat"
_GET_LIVE_CHAT = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat"


class ChatEndedError(RuntimeError):
    pass


class YouTubeLiveChat:
    def __init__(
        self,
        channel: Channel,
        settings: YouTubeSettings,
        *,
        video_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._channel = channel
        self._s = settings
        self._first_video_id = video_id
        self._clock = clock
        self._lag = LagTracker()

    async def _current_video(self, http: httpx.AsyncClient) -> str | None:
        if self._first_video_id:
            video_id, self._first_video_id = self._first_video_id, None
            return video_id
        stream = await fetch_live(http, self._channel)
        return stream.chat_id if stream else None

    async def messages(self) -> AsyncIterator[ChatMessage]:
        backoff = 5.0
        waiting_logged = False
        while True:
            try:
                async with youtube_client() as http:
                    video_id = await self._current_video(http)
                    if video_id is None:
                        # Chat extra (multistream) de um canal que não está ao vivo agora.
                        if not waiting_logged:
                            log.info("chat_waiting_live", channel=self._channel.key)
                            waiting_logged = True
                        await asyncio.sleep(self._s.check_interval_s)
                        continue
                    waiting_logged = False
                    boot = await self._bootstrap(http, video_id)
                    log.info("chat_connected", channel=self._channel.key, video=video_id)
                    backoff = 5.0
                    async for message in self._poll(http, boot):
                        yield message
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "chat_error", channel=self._channel.key, error=repr(exc), retry_in=backoff
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120.0)

    async def _bootstrap(self, http: httpx.AsyncClient, video_id: str) -> ChatBootstrap:
        resp = await http.get(_CHAT_PAGE, params={"is_popout": "1", "v": video_id})
        resp.raise_for_status()
        return parse_chat_page(resp.text)

    async def _poll(
        self, http: httpx.AsyncClient, boot: ChatBootstrap
    ) -> AsyncIterator[ChatMessage]:
        params = {"prettyPrint": "false"}
        if boot.api_key:
            params["key"] = boot.api_key
        headers = {"Origin": "https://www.youtube.com", "Referer": _CHAT_PAGE}
        continuation = boot.continuation
        s = self._s
        while True:
            resp = await http.post(
                _GET_LIVE_CHAT,
                params=params,
                headers=headers,
                json={"context": boot.context, "continuation": continuation},
            )
            resp.raise_for_status()
            received = self._clock()
            batch = parse_chat_response(resp.json())
            for action in batch.actions:
                message = action_to_message(
                    action, self._channel.key, received, self._lag, s.chat_max_age_s
                )
                if message is not None:
                    yield message
            if batch.continuation is None:
                raise ChatEndedError("o chat da live terminou")
            continuation = batch.continuation
            suggested = (batch.timeout_ms or 1000) / 1000
            await asyncio.sleep(min(max(suggested, s.chat_poll_min_s), s.chat_poll_max_s))
