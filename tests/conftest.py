from __future__ import annotations

from cortes.domain.models import ChatMessage


def msg(ts: float, author: str = "a", text: str = "oi", channel: str = "canal") -> ChatMessage:
    return ChatMessage(channel=channel, author=author, text=text, ts=ts)
