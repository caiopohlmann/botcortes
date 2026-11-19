"""Parsing puro (sem I/O) das páginas e respostas internas do YouTube.

São os mesmos dados que o site usa para montar a página da live e o chat:
- `ytInitialPlayerResponse` na página /live do canal (está ao vivo? qual vídeo?);
- `ytInitialData` + `ytcfg` na página do chat (primeira "continuation" e contexto);
- respostas de youtubei/v1/live_chat/get_live_chat (mensagens novas).

Não é API oficial: se o YouTube mudar o formato, só este arquivo precisa de ajuste.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cortes.domain.models import ChatMessage, LiveStream

_PLAYER_RESPONSE = re.compile(r"ytInitialPlayerResponse\s*=\s*")
_INITIAL_DATA = re.compile(r"""(?:window\s*\[\s*["']ytInitialData["']\s*\]|ytInitialData)\s*=\s*""")
_INNERTUBE_CONTEXT = re.compile(r'"INNERTUBE_CONTEXT"\s*:\s*')
_API_KEY = re.compile(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"')
_CLIENT_VERSION = re.compile(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"')

_CONTINUATION_KINDS = (
    "invalidationContinuationData",
    "timedContinuationData",
    "reloadContinuationData",
)

DEFAULT_CLIENT_VERSION = "2.20241101.00.00"


class PageFormatError(ValueError):
    """A página não tem o formato esperado (consentimento, captcha ou layout novo)."""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def extract_json(html: str, pattern: re.Pattern[str]) -> dict[str, Any] | None:
    """Primeiro objeto JSON logo depois de `pattern` (ex.: `var ytInitialData = {...};`)."""
    decoder = json.JSONDecoder()
    for match in pattern.finditer(html):
        start = match.end()
        if start >= len(html) or html[start] != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(html, start)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_live_page(html: str, key: str) -> LiveStream | None:
    """Página youtube.com/@canal/live -> LiveStream se há uma live acontecendo agora.

    Sem live, o YouTube mostra a página do canal, uma live agendada ou o último VOD:
    todos viram None. Página sem nenhum dos blocos de dados levanta PageFormatError
    (melhor "desconhecido" do que achar que está offline por engano).
    """
    player = extract_json(html, _PLAYER_RESPONSE)
    if player is None:
        if extract_json(html, _INITIAL_DATA) is None:
            raise PageFormatError("página do YouTube sem ytInitialPlayerResponse/ytInitialData")
        return None  # página do canal: não está ao vivo

    details = _dict(player.get("videoDetails"))
    micro = _dict(_dict(player.get("microformat")).get("playerMicroformatRenderer"))
    broadcast = _dict(micro.get("liveBroadcastDetails"))
    video_id = details.get("videoId")
    is_live = details.get("isLive") is True or broadcast.get("isLiveNow") is True
    if not is_live or not isinstance(video_id, str) or broadcast.get("endTimestamp"):
        return None
    return LiveStream(
        key=key,
        user_name=str(details.get("author") or ""),
        title=str(details.get("title") or ""),
        game_name="",  # a página não informa o jogo de forma confiável
        started_at=_parse_time(broadcast.get("startTimestamp")),
        chat_id=video_id,
    )


@dataclass(frozen=True, slots=True)
class ChatBootstrap:
    continuation: str
    context: dict[str, Any]
    api_key: str | None


def _continuation_from(data: dict[str, Any]) -> str | None:
    for kind in _CONTINUATION_KINDS:
        value = _dict(data.get(kind)).get("continuation")
        if isinstance(value, str) and value:
            return value
    return None


def parse_chat_page(html: str) -> ChatBootstrap:
    """Página youtube.com/live_chat?v=<id> -> continuation do chat completo + contexto."""
    data = extract_json(html, _INITIAL_DATA)
    if data is None:
        raise PageFormatError("página do chat sem ytInitialData (live sem chat?)")
    renderer = _dict(_dict(data.get("contents")).get("liveChatRenderer"))
    if not renderer:
        raise PageFormatError("chat indisponível para este vídeo")

    continuation: str | None = None
    # O padrão é "Chat principal" (filtrado). Para medir a taxa queremos "Chat ao vivo"
    # (todas as mensagens): é o último item do seletor no cabeçalho.
    header = _dict(_dict(renderer.get("header")).get("liveChatHeaderRenderer"))
    selector = _dict(_dict(header.get("viewSelector")).get("sortFilterSubMenuRenderer"))
    items = _list(selector.get("subMenuItems"))
    if items:
        continuation = _continuation_from(_dict(_dict(items[-1]).get("continuation")))
    if continuation is None:
        for item in _list(renderer.get("continuations")):
            continuation = _continuation_from(_dict(item))
            if continuation:
                break
    if continuation is None:
        raise PageFormatError("chat sem continuation")

    context = extract_json(html, _INNERTUBE_CONTEXT)
    if context is None:
        version_match = _CLIENT_VERSION.search(html)
        version = version_match.group(1) if version_match else DEFAULT_CLIENT_VERSION
        context = {"client": {"clientName": "WEB", "clientVersion": version}}
    key_match = _API_KEY.search(html)
    return ChatBootstrap(
        continuation=continuation,
        context=context,
        api_key=key_match.group(1) if key_match else None,
    )


@dataclass(frozen=True, slots=True)
class ChatBatch:
    actions: list[dict[str, Any]]
    continuation: str | None  # None = chat encerrado (live acabou)
    timeout_ms: int | None


def parse_chat_response(body: Any) -> ChatBatch:
    live = _dict(_dict(_dict(body).get("continuationContents")).get("liveChatContinuation"))
    if not live:
        return ChatBatch(actions=[], continuation=None, timeout_ms=None)
    continuation: str | None = None
    timeout_ms: int | None = None
    for item in _list(live.get("continuations")):
        for kind in _CONTINUATION_KINDS:
            data = _dict(_dict(item).get(kind))
            if isinstance(data.get("continuation"), str):
                continuation = data["continuation"]
                raw_timeout = data.get("timeoutMs")
                timeout_ms = int(raw_timeout) if isinstance(raw_timeout, int | str) else None
                break
        if continuation:
            break
    actions = [a for a in _list(live.get("actions")) if isinstance(a, dict)]
    return ChatBatch(actions=actions, continuation=continuation, timeout_ms=timeout_ms)


def runs_to_text(runs: Any) -> str:
    """Junta os "runs" da mensagem; emojis viram tokens separados (😂 ou nome do emote)."""
    parts: list[str] = []
    for run in _list(runs):
        run = _dict(run)
        text = run.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        emoji = _dict(run.get("emoji"))
        if not emoji:
            continue
        token = ""
        if emoji.get("isCustomEmoji"):
            shortcuts = _list(emoji.get("shortcuts"))
            if shortcuts and isinstance(shortcuts[0], str):
                token = shortcuts[0].strip(":").lstrip("_")  # ":_kekw:" -> "kekw"
        else:
            token = str(emoji.get("emojiId") or "")
        if token:
            parts.append(f" {token} ")
    return " ".join("".join(parts).split())


@dataclass(slots=True)
class LagTracker:
    """Converte o horário do servidor do YouTube para o relógio local.

    Usa o menor atraso observado (chegada - envio) numa janela recente: cobre o atraso
    de rede e qualquer diferença entre o relógio do PC e o do YouTube.
    """

    window: int = 300
    _lags: deque[float] = field(default_factory=deque)

    def to_local(self, server_ts: float, received_at: float) -> tuple[float, float]:
        """-> (horário local estimado do envio, "idade" da mensagem ao chegar, em s)."""
        lag = received_at - server_ts
        self._lags.append(lag)
        if len(self._lags) > self.window:
            self._lags.popleft()
        base = min(self._lags)
        return server_ts + base, lag - base


def action_to_message(
    action: dict[str, Any],
    channel: str,
    received_at: float,
    lag: LagTracker,
    max_age_s: float,
) -> ChatMessage | None:
    item = _dict(_dict(action.get("addChatItemAction")).get("item"))
    renderer = _dict(
        item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
    )
    if not renderer:
        return None
    author = renderer.get("authorExternalChannelId") or _dict(renderer.get("authorName")).get(
        "simpleText"
    )
    raw_ts = renderer.get("timestampUsec")
    try:
        server_ts = int(str(raw_ts)) / 1_000_000  # None/lixo -> ValueError -> hora local
    except (TypeError, ValueError):
        server_ts = received_at
    local_ts, age = lag.to_local(server_ts, received_at)
    if age > max_age_s:
        return None  # histórico (reconexão): não é reação ao que está passando agora
    return ChatMessage(
        channel=channel,
        author=str(author or "").casefold(),
        text=runs_to_text(_dict(renderer.get("message")).get("runs")),
        ts=local_ts,
    )
