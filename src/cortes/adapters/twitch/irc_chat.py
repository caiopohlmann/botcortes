"""Leitura do chat via IRC sobre WebSocket, login anônimo (somente leitura, sem token)."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Callable

import structlog
from websockets.asyncio.client import connect

from cortes.adapters.twitch.irc_parser import parse_line, to_chat_message
from cortes.domain.models import ChatMessage

log = structlog.get_logger(__name__)

_URL = "wss://irc-ws.chat.twitch.tv:443"


class TwitchIrcChat:
    def __init__(
        self,
        login: str,
        *,
        clock: Callable[[], float] = time.time,
        max_backoff_s: float = 60.0,
    ) -> None:
        self._login = login.casefold()
        self._clock = clock
        self._max_backoff = max_backoff_s

    async def messages(self) -> AsyncIterator[ChatMessage]:
        backoff = 1.0
        while True:
            try:
                async with connect(_URL, open_timeout=15, max_size=2**20) as ws:
                    await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                    await ws.send("PASS SCHMOOPIIE")
                    await ws.send(f"NICK justinfan{random.randint(10_000, 99_999)}")
                    await ws.send(f"JOIN #{self._login}")
                    log.info("chat_connected", channel=self._login)
                    backoff = 1.0
                    reconnect = False
                    async for frame in ws:
                        now = self._clock()
                        if isinstance(frame, bytes):
                            frame = frame.decode("utf-8", "replace")
                        for raw in frame.split("\r\n"):
                            parsed = parse_line(raw)
                            if parsed is None:
                                continue
                            if parsed.command == "PING":
                                await ws.send(f"PONG :{parsed.trailing or 'tmi.twitch.tv'}")
                            elif parsed.command == "RECONNECT":
                                reconnect = True
                            else:
                                message = to_chat_message(parsed, now)
                                if message is not None:
                                    yield message
                        if reconnect:
                            log.info("chat_reconnect_requested", channel=self._login)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("chat_error", channel=self._login, error=repr(exc), retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)
