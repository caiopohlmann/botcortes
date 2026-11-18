from __future__ import annotations

from cortes.adapters.twitch.irc_parser import parse_line, to_chat_message


def test_privmsg_with_tags() -> None:
    line = (
        "@badge-info=;badges=;color=#FF0000;display-name=Fulano;tmi-sent-ts=1700000000000 "
        ":fulano!fulano@fulano.tmi.twitch.tv PRIVMSG #canal :KEKW que isso :) kkk"
    )
    parsed = parse_line(line)
    assert parsed is not None
    m = to_chat_message(parsed, received_at=123.0)
    assert m is not None
    assert m.channel == "canal"
    assert m.author == "fulano"
    assert m.text == "KEKW que isso :) kkk"
    assert m.ts == 123.0


def test_action_message() -> None:
    parsed = parse_line(":a!a@a.tmi.twitch.tv PRIVMSG #c :\x01ACTION dança\x01")
    assert parsed is not None
    m = to_chat_message(parsed, 0)
    assert m is not None and m.text == "dança"


def test_ping() -> None:
    parsed = parse_line("PING :tmi.twitch.tv")
    assert parsed is not None
    assert parsed.command == "PING"
    assert parsed.trailing == "tmi.twitch.tv"


def test_non_privmsg_ignored() -> None:
    parsed = parse_line(":tmi.twitch.tv 001 justinfan123 :Welcome, GLHF!")
    assert parsed is not None
    assert to_chat_message(parsed, 0) is None


def test_empty_line() -> None:
    assert parse_line("") is None
    assert parse_line("\r\n") is None
