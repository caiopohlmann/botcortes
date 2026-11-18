"""Parser mínimo de linhas IRC da Twitch (puro, sem I/O)."""

from __future__ import annotations

from dataclasses import dataclass

from cortes.domain.models import ChatMessage


@dataclass(frozen=True, slots=True)
class IrcLine:
    tags: dict[str, str]
    prefix: str
    command: str
    params: list[str]
    trailing: str | None


def parse_line(line: str) -> IrcLine | None:
    line = line.rstrip("\r\n")
    if not line:
        return None
    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for item in raw_tags.split(";"):
            key, _, value = item.partition("=")
            tags[key] = value
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing: str | None = None
    if " :" in line:
        line, _, trailing = line.partition(" :")
    elif line.startswith(":"):
        trailing, line = line[1:], ""
    parts = line.split()
    if not parts:
        return None
    return IrcLine(tags=tags, prefix=prefix, command=parts[0], params=parts[1:], trailing=trailing)


def to_chat_message(parsed: IrcLine, received_at: float) -> ChatMessage | None:
    if parsed.command != "PRIVMSG" or not parsed.params or parsed.trailing is None:
        return None
    text = parsed.trailing
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]
    author = parsed.prefix.partition("!")[0] or parsed.tags.get("login", "")
    return ChatMessage(
        channel=parsed.params[0].lstrip("#"),
        author=author.casefold(),
        text=text,
        # Hora local de recebimento: alinha com o relógio de parede usado na gravação.
        ts=received_at,
    )
