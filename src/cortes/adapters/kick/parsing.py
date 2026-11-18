"""Parsing puro (sem I/O) das respostas do Kick: API do canal e eventos do chat (Pusher)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cortes.domain.models import ChatMessage, LiveStream

# O conteúdo das mensagens traz emotes como "[emote:37226:KEKW]".
_EMOTE = re.compile(r"\[emote:\d+:([^\]\s]+)\]")

CHAT_EVENT = "App\\Events\\ChatMessageEvent"


@dataclass(frozen=True, slots=True)
class PusherFrame:
    event: str
    channel: str | None
    data: dict[str, Any]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_content(content: str) -> str:
    """Troca os emotes pelo nome (com espaços), para as palavras de hype baterem."""
    return " ".join(_EMOTE.sub(r" \1 ", content).split())


def parse_frame(raw: str | bytes) -> PusherFrame | None:
    """Pusher manda {"event", "channel", "data"}; "data" costuma vir como JSON em string."""
    try:
        frame = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(frame, dict) or not isinstance(frame.get("event"), str):
        return None
    data: Any = frame.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data) if data else {}
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    channel = frame.get("channel")
    return PusherFrame(
        event=frame["event"], channel=channel if isinstance(channel, str) else None, data=data
    )


def to_chat_message(frame: PusherFrame, channel: str, received_at: float) -> ChatMessage | None:
    if frame.event != CHAT_EVENT:
        return None
    data = frame.data
    if data.get("type") not in (None, "message", "reply"):
        return None
    content = data.get("content")
    if not isinstance(content, str):
        return None
    sender = _dict(data.get("sender"))
    author = str(sender.get("slug") or sender.get("username") or sender.get("id") or "")
    return ChatMessage(
        channel=channel,
        author=author.casefold(),
        text=normalize_content(content),
        ts=received_at,  # entrega em tempo real: hora local de chegada, como na Twitch
    )


def subscribe_message(chatroom_id: str) -> str:
    return json.dumps(
        {"event": "pusher:subscribe", "data": {"auth": "", "channel": chat_channel(chatroom_id)}}
    )


def chat_channel(chatroom_id: str) -> str:
    return f"chatrooms.{chatroom_id}.v2"


def _parse_kick_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _category(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or "")
    return ""


def parse_public_channel(body: Any, key: str) -> tuple[LiveStream | None, str | None]:
    """Resposta de kick.com/api/v2/channels/<slug> -> (live ou None, chatroom_id)."""
    if not isinstance(body, dict):
        raise ValueError("resposta inesperada da API do Kick")
    chatroom = _dict(body.get("chatroom"))
    chatroom_id = str(chatroom["id"]) if chatroom.get("id") is not None else None
    live = body.get("livestream")
    if not isinstance(live, dict) or live.get("is_live") is False:
        return None, chatroom_id
    categories = live.get("categories") or []
    user = _dict(body.get("user"))
    stream = LiveStream(
        key=key,
        user_name=str(user.get("username") or body.get("slug") or ""),
        title=str(live.get("session_title") or ""),
        game_name=_category(categories[0]) if categories else "",
        started_at=_parse_kick_time(live.get("created_at") or live.get("start_time")),
        viewer_count=int(live.get("viewer_count") or 0),
        chat_id=chatroom_id,
    )
    return stream, chatroom_id


def parse_official_channels(body: Any, keys: dict[str, str]) -> dict[str, LiveStream | None]:
    """Resposta de api.kick.com/public/v1/channels -> status por Channel.key.

    `keys` mapeia slug (minúsculo) -> Channel.key. Só canais presentes na resposta ganham
    status; um slug ausente fica "desconhecido".
    """
    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ValueError("resposta inesperada da API oficial do Kick")
    status: dict[str, LiveStream | None] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = keys.get(str(item.get("slug") or "").casefold())
        if key is None:
            continue
        stream = _dict(item.get("stream"))
        if not stream.get("is_live"):
            status[key] = None
            continue
        status[key] = LiveStream(
            key=key,
            user_name=str(item.get("slug") or ""),
            title=str(item.get("stream_title") or ""),
            game_name=_category(item.get("category")),
            started_at=_parse_kick_time(stream.get("start_time")),
            viewer_count=int(stream.get("viewer_count") or 0),
        )
    return status
