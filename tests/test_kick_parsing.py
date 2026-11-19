from __future__ import annotations

import json

from cortes.adapters.kick.parsing import (
    CHAT_EVENT,
    centrifugo_command,
    chat_channel,
    normalize_content,
    parse_centrifugo,
    parse_frame,
    parse_official_channels,
    parse_public_channel,
    parse_realtime_token,
    parse_realtime_url,
    to_chat_message,
)


def chat_frame(content: str, *, slug: str = "Fulano", type_: str = "message") -> str:
    data = {
        "id": "abc",
        "chatroom_id": 668,
        "content": content,
        "type": type_,
        "created_at": "2025-01-01T12:00:00+00:00",
        "sender": {"id": 1, "username": slug, "slug": slug.lower()},
    }
    # O envelope traz "data" como JSON dentro de string.
    frame = {"event": CHAT_EVENT, "channel": "chatrooms.668.v2", "data": json.dumps(data)}
    return json.dumps(frame)


def test_emotes_become_names() -> None:
    assert normalize_content("kkkk[emote:37226:KEKW][emote:1:LUL]") == "kkkk KEKW LUL"


def test_parse_chat_message() -> None:
    frame = parse_frame(chat_frame("que isso [emote:37226:KEKW]"))
    assert frame is not None and frame.channel == chat_channel("668")
    message = to_chat_message(frame, "kick-canal", 123.0)
    assert message is not None
    assert message.text == "que isso KEKW"
    assert message.author == "fulano"
    assert message.ts == 123.0


def test_non_chat_events_ignored() -> None:
    raw = json.dumps({"event": "pusher:connection_established", "data": '{"socket_id":"1"}'})
    frame = parse_frame(raw)
    assert frame is not None and frame.data == {"socket_id": "1"}
    assert to_chat_message(frame, "kick-canal", 1.0) is None


def test_garbage_frames() -> None:
    assert parse_frame("not json") is None
    assert parse_frame("[]") is None
    frame = parse_frame('{"event":"x","data":"not json"}')
    assert frame is not None and frame.data == {}


def centrifugo_pub(content: str, channel: str = "chatrooms.668.v2") -> str:
    envelope = json.loads(chat_frame(content))
    del envelope["channel"]  # no Centrifugo o canal vem fora do envelope
    return json.dumps({"push": {"channel": channel, "pub": {"data": envelope, "offset": 7}}})


def test_centrifugo_publication_becomes_chat_message() -> None:
    (frame,) = parse_centrifugo(centrifugo_pub("[emote:1:LUL] kkkk"))
    assert frame.kind == "pub" and frame.event is not None
    assert frame.event.channel == chat_channel("668")
    message = to_chat_message(frame.event, "kick-canal", 50.0)
    assert message is not None and message.text == "LUL kkkk" and message.author == "fulano"


def test_centrifugo_batch_ping_replies_and_disconnect() -> None:
    raw = "\n".join(
        [
            "{}",
            '{"id":1,"connect":{"client":"x","expires":true,"ttl":2699,"ping":25,"pong":true}}',
            '{"id":2,"error":{"code":103,"message":"permission denied"}}',
            centrifugo_pub("oi"),
            '{"push":{"disconnect":{"code":3501,"reason":"bad request"}}}',
            "lixo",
            "",
        ]
    )
    frames = parse_centrifugo(raw.encode())
    assert [f.kind for f in frames] == ["ping", "reply", "reply", "pub", "disconnect"]
    assert frames[1].id == 1 and frames[1].error is None
    assert frames[1].body is not None and frames[1].body["ttl"] == 2699
    assert frames[2].error == {"code": 103, "message": "permission denied"}
    assert frames[4].body == {"code": 3501, "reason": "bad request"}


def test_centrifugo_command() -> None:
    body = json.loads(centrifugo_command(2, "subscribe", {"channel": "chatrooms.668.v2"}))
    assert body == {"id": 2, "subscribe": {"channel": "chatrooms.668.v2"}}


def test_realtime_negotiation_and_token() -> None:
    negotiation = {
        "data": {
            "connections": [
                {"credentials": {"app_key": "k", "cluster": "us2"}, "provider": "pusher"},
                {"credentials": {"url": "wss://realtime.exemplo/ws"}, "provider": "centrifugo"},
            ],
            "mode": "websocket",
        },
        "message": "success",
    }
    assert parse_realtime_url(negotiation) == "wss://realtime.exemplo/ws"
    assert parse_realtime_url({"data": {"connections": [{"provider": "pusher"}]}}) is None
    assert parse_realtime_url("lixo") is None
    assert parse_realtime_token({"data": {"token": "abc"}, "message": "success"}) == "abc"
    assert parse_realtime_token({"data": {}}) is None


PUBLIC_LIVE = {
    "id": 10,
    "slug": "fulano",
    "chatroom": {"id": 668},
    "user": {"username": "Fulano"},
    "livestream": {
        "is_live": True,
        "session_title": "GTA RP",
        "created_at": "2025-01-01 12:00:00",
        "viewer_count": 321,
        "categories": [{"name": "Grand Theft Auto VI"}],
    },
}


def test_public_channel_live() -> None:
    stream, chatroom = parse_public_channel(PUBLIC_LIVE, "kick-fulano")
    assert chatroom == "668"
    assert stream is not None
    assert stream.key == "kick-fulano"
    assert stream.game_name == "Grand Theft Auto VI"
    assert stream.viewer_count == 321
    assert stream.chat_id == "668"
    assert stream.started_at is not None and stream.started_at.tzinfo is not None


def test_public_channel_offline_keeps_chatroom() -> None:
    body = {**PUBLIC_LIVE, "livestream": None}
    stream, chatroom = parse_public_channel(body, "kick-fulano")
    assert stream is None and chatroom == "668"


def test_official_channels() -> None:
    body = {
        "data": [
            {
                "slug": "fulano",
                "stream_title": "GTA",
                "category": {"name": "Grand Theft Auto VI"},
                "stream": {
                    "is_live": True,
                    "viewer_count": 5,
                    "start_time": "2025-01-01T12:00:00Z",
                },
            },
            {"slug": "ciclano", "stream": {"is_live": False}},
        ]
    }
    status = parse_official_channels(
        body, {"fulano": "kick-fulano", "ciclano": "kick-ciclano", "beltrano": "kick-beltrano"}
    )
    assert status["kick-ciclano"] is None
    live = status["kick-fulano"]
    assert live is not None and live.game_name == "Grand Theft Auto VI"
    assert "kick-beltrano" not in status  # sem resposta = desconhecido
