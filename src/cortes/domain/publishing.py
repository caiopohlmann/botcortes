"""Sugestões de título, hashtags e descrição (sempre com crédito ao canal original)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from cortes.domain.models import Channel, is_youtube_channel_id

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_NOT_HASHTAG = re.compile(r"\W")


def suggest_title(transcript: str, channel: Channel, max_chars: int) -> str:
    text = " ".join(transcript.split())
    if not text:
        return f"{channel.name} no GTA 6 😂"
    first = _SENTENCE_SPLIT.split(text, maxsplit=1)[0]
    if len(first) > max_chars:
        cut = first[: max_chars - 1]
        first = (cut.rsplit(" ", 1)[0] if " " in cut else cut) + "…"
    return f'"{first}" - {channel.name}'


def _channel_hashtag(channel: Channel) -> str:
    if is_youtube_channel_id(channel.login):
        return ""  # "#UCxxxx" não é hashtag útil
    return _NOT_HASHTAG.sub("", channel.login)  # kick: "fulano-gta" -> "fulanogta"


def build_hashtags(
    base: Iterable[str], channel: Channel, platform_tags: Iterable[str] = ()
) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in (*base, *platform_tags, *channel.hashtags, _channel_hashtag(channel)):
        tag = tag.strip()
        if not tag or tag == "#":
            continue
        if not tag.startswith("#"):
            tag = f"#{tag}"
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def build_description(template: str, *, title: str, channel: Channel, hashtags: list[str]) -> str:
    text = template.format(
        title=title,
        display_name=channel.name,
        login=channel.login,
        platform=channel.platform.label,
        channel_url=channel.url,
        hashtags=" ".join(hashtags),
    )
    # Garantia: o crédito aparece mesmo se alguém editar o template e esquecer.
    if channel.url not in text:
        text = f"{text.rstrip()}\n\nCréditos: {channel.name} ({channel.url})\n"
    return text
