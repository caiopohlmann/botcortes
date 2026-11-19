"""Chat do Kick pelo mesmo WebSocket (Centrifugo) que a página usa. Leitura anônima, sem conta.

Passos, iguais aos do navegador de quem assiste sem login:
  1. negociação: POST <realtime_api>/channels/<id>/chat/connection diz qual servidor usar
     (se falhar, usa `kick.realtime_url` do settings.yaml);
  2. token de visitante: POST <realtime_api>/auth/connection (vale ~45 min; renovado antes);
  3. WebSocket: `connect` com o token e `subscribe` no canal "chatrooms.<chatroom_id>.v2".

O chatroom_id vem, em ordem: do status da live, do `chat_id` do channels.yaml ou da API
pública do canal. O servidor manda ping ("{}") a cada ~25 s e espera "{}" de volta.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import structlog
from websockets.asyncio.client import ClientConnection, connect
from websockets.typing import Origin

from cortes.adapters.browser_http import USER_AGENT, browser_client
from cortes.adapters.kick.api import fetch_public_channel
from cortes.adapters.kick.parsing import (
    CentrifugoFrame,
    centrifugo_command,
    chat_channel,
    parse_centrifugo,
    parse_realtime_token,
    parse_realtime_url,
    to_chat_message,
)
from cortes.domain.models import ChatMessage, Platform, channel_key

log = structlog.get_logger(__name__)

_CONNECT_ID, _SUBSCRIBE_ID = 1, 2
_REPLY_TIMEOUT_S = 15.0
_SILENCE_MARGIN_S = 20.0  # sem nada (nem ping) por ping + margem: conexão morta, reconecta
_REFRESH_AT = 0.8  # renova o token com 80% da validade


class KickRealtimeChat:
    def __init__(
        self,
        slug: str,
        realtime_api: str,
        fallback_url: str,
        *,
        chatroom_id: str | None = None,
        clock: Callable[[], float] = time.time,
        max_backoff_s: float = 60.0,
    ) -> None:
        self._slug = slug
        self._api = realtime_api.rstrip("/")
        self._fallback_url = fallback_url
        self._chatroom_id = chatroom_id
        self._clock = clock
        self._max_backoff = max_backoff_s
        self._label = channel_key(Platform.KICK, slug)
        self._client_id = str(uuid.uuid4())  # como o navegador: um id por "aba"
        self._headers = {
            "Accept": "application/json",
            "Origin": "https://kick.com",
            "Referer": f"https://kick.com/{slug}",
        }

    async def _resolve_chatroom(self) -> str:
        if self._chatroom_id:
            return self._chatroom_id
        async with browser_client() as http:
            _, chatroom_id = await fetch_public_channel(http, self._slug, self._label)
        if not chatroom_id:
            raise LookupError("a API do Kick não informou o chatroom")
        self._chatroom_id = chatroom_id
        return chatroom_id

    async def _post(self, http: httpx.AsyncClient, path: str, body: dict[str, Any]) -> Any:
        resp = await http.post(f"{self._api}{path}", json=body, headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    async def _server_url(self, http: httpx.AsyncClient, chatroom_id: str) -> str:
        body = {
            "client": {"id": self._client_id, "type": "web"},
            "capabilities": {"accepted_providers": [{"provider": "centrifugo"}]},
        }
        try:
            url = parse_realtime_url(
                await self._post(http, f"/channels/{chatroom_id}/chat/connection", body)
            )
        except Exception as exc:
            log.warning("kick_realtime_negotiation_failed", channel=self._label, error=repr(exc))
            return self._fallback_url
        if url is None:
            log.warning("kick_realtime_no_centrifugo", channel=self._label)
            return self._fallback_url
        return url

    async def _token(self, http: httpx.AsyncClient) -> str:
        token = parse_realtime_token(
            await self._post(http, "/auth/connection", {"client_id": self._client_id})
        )
        if token is None:
            raise RuntimeError("o Kick não devolveu o token do chat")
        return token

    async def messages(self) -> AsyncIterator[ChatMessage]:
        backoff = 2.0
        while True:
            try:
                chatroom_id = await self._resolve_chatroom()
                async with browser_client() as http:
                    url = await self._server_url(http, chatroom_id)
                    token = await self._token(http)
                    async with connect(
                        url,
                        open_timeout=15,
                        max_size=2**20,
                        user_agent_header=USER_AGENT,
                        origin=Origin("https://kick.com"),
                    ) as ws:
                        await ws.send(
                            centrifugo_command(
                                _CONNECT_ID, "connect", {"token": token, "name": "js"}
                            )
                        )
                        session = _require(await _reply(ws, _CONNECT_ID), "conexão")
                        await ws.send(
                            centrifugo_command(
                                _SUBSCRIBE_ID, "subscribe", {"channel": chat_channel(chatroom_id)}
                            )
                        )
                        _require(await _reply(ws, _SUBSCRIBE_ID), "inscrição no chat")
                        log.info("chat_connected", channel=self._label, chatroom=chatroom_id)
                        backoff = 2.0
                        async for message in self._read(ws, http, chatroom_id, session):
                            yield message
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

    async def _read(
        self,
        ws: ClientConnection,
        http: httpx.AsyncClient,
        chatroom_id: str,
        session: dict[str, Any],
    ) -> AsyncIterator[ChatMessage]:
        expected = chat_channel(chatroom_id)
        pong = bool(session.get("pong"))
        silence_s = float(session.get("ping") or 25) + _SILENCE_MARGIN_S
        refresh_at = _refresh_deadline(session)
        next_id = _SUBSCRIBE_ID + 1
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), silence_s)
            except TimeoutError as exc:
                raise RuntimeError(f"chat do Kick mudo há {silence_s:.0f} s") from exc
            now = self._clock()
            for frame in parse_centrifugo(raw):
                if frame.kind == "ping":
                    if pong:
                        await ws.send("{}")
                elif frame.kind == "pub":
                    if frame.event is not None and frame.event.channel in (None, expected):
                        message = to_chat_message(frame.event, self._label, now)
                        if message is not None:
                            yield message
                elif frame.kind == "reply":
                    body = _require(frame, "renovação do token")
                    refresh_at = _refresh_deadline(body)
                elif frame.kind == "disconnect":
                    raise RuntimeError(f"o Kick encerrou a conexão do chat: {frame.body}")
            if refresh_at is not None and time.monotonic() >= refresh_at:
                refresh_at = None  # só agenda de novo quando a resposta chegar
                token = await self._token(http)
                await ws.send(centrifugo_command(next_id, "refresh", {"token": token}))
                next_id += 1
                log.debug("chat_token_refreshed", channel=self._label)


def _refresh_deadline(body: dict[str, Any]) -> float | None:
    ttl = body.get("ttl")
    if not body.get("expires") or not isinstance(ttl, (int, float)) or ttl <= 0:
        return None
    return time.monotonic() + max(10.0, float(ttl) * _REFRESH_AT)


async def _reply(ws: ClientConnection, command_id: int) -> CentrifugoFrame:
    """Espera a resposta de um comando (ignora o que chegar antes, como pings)."""
    async with asyncio.timeout(_REPLY_TIMEOUT_S):
        while True:
            for frame in parse_centrifugo(await ws.recv()):
                if frame.kind == "ping":
                    await ws.send("{}")
                elif frame.kind == "reply" and frame.id == command_id:
                    return frame


def _require(frame: CentrifugoFrame, what: str) -> dict[str, Any]:
    if frame.error:
        raise RuntimeError(f"o Kick recusou a {what}: {frame.error}")
    return frame.body or {}
