from __future__ import annotations

import json
from typing import Any

import pytest

from cortes.adapters.youtube.parsing import (
    LagTracker,
    PageFormatError,
    action_to_message,
    parse_chat_page,
    parse_chat_response,
    parse_live_page,
    runs_to_text,
)


def watch_page(player: dict[str, Any]) -> str:
    return (
        "<html><script>var ytInitialPlayerResponse = "
        + json.dumps(player)
        + ";var meta = document.createElement('meta');</script>"
        + '<script>var ytInitialData = {"contents": {}};</script></html>'
    )


LIVE_PLAYER = {
    "playabilityStatus": {"status": "OK"},
    "videoDetails": {"videoId": "abc123", "title": "GTA 6 RP", "author": "Canal", "isLive": True},
    "microformat": {
        "playerMicroformatRenderer": {
            "liveBroadcastDetails": {
                "isLiveNow": True,
                "startTimestamp": "2025-01-01T12:00:00+00:00",
            }
        }
    },
}


def test_live_page_detected() -> None:
    stream = parse_live_page(watch_page(LIVE_PLAYER), "youtube-canal")
    assert stream is not None
    assert stream.chat_id == "abc123"
    assert stream.title == "GTA 6 RP"
    assert stream.started_at is not None


def test_upcoming_stream_is_offline() -> None:
    player = {
        "videoDetails": {"videoId": "x", "isUpcoming": True},
        "playabilityStatus": {"status": "LIVE_STREAM_OFFLINE"},
    }
    assert parse_live_page(watch_page(player), "youtube-canal") is None


def test_ended_stream_is_offline() -> None:
    player = json.loads(json.dumps(LIVE_PLAYER))
    player["videoDetails"]["isLive"] = False
    broadcast = player["microformat"]["playerMicroformatRenderer"]["liveBroadcastDetails"]
    broadcast["isLiveNow"] = False
    broadcast["endTimestamp"] = "2025-01-01T15:00:00+00:00"
    assert parse_live_page(watch_page(player), "youtube-canal") is None


def test_channel_page_without_player_is_offline() -> None:
    data = {"contents": {"twoColumnBrowseResultsRenderer": {}}}
    html = f"<script>var ytInitialData = {json.dumps(data)};</script>"
    assert parse_live_page(html, "youtube-canal") is None


def test_unknown_page_raises() -> None:
    with pytest.raises(PageFormatError):
        parse_live_page("<html>Before you continue to YouTube</html>", "youtube-canal")


def reload(continuation: str) -> dict[str, Any]:
    return {"reloadContinuationData": {"continuation": continuation}}


def chat_page(*, with_selector: bool = True) -> str:
    default = {"invalidationContinuationData": {"continuation": "TOP", "timeoutMs": 10000}}
    renderer: dict[str, Any] = {"continuations": [default], "actions": []}
    if with_selector:
        renderer["header"] = {
            "liveChatHeaderRenderer": {
                "viewSelector": {
                    "sortFilterSubMenuRenderer": {
                        "subMenuItems": [
                            {"title": "Chat principal", "continuation": reload("TOP")},
                            {"title": "Chat ao vivo", "continuation": reload("ALL")},
                        ]
                    }
                }
            }
        }
    data = {"contents": {"liveChatRenderer": renderer}}
    cfg = {
        "INNERTUBE_API_KEY": "KEY1",
        "INNERTUBE_CONTEXT": {"client": {"clientName": "WEB", "clientVersion": "2.2025"}},
    }
    return (
        f'<script>window["ytInitialData"] = {json.dumps(data)};</script>'
        f"<script>ytcfg.set({json.dumps(cfg)});</script>"
    )


def test_chat_page_prefers_full_live_chat() -> None:
    boot = parse_chat_page(chat_page())
    assert boot.continuation == "ALL"
    assert boot.api_key == "KEY1"
    assert boot.context["client"]["clientVersion"] == "2.2025"


def test_chat_page_falls_back_to_default_continuation() -> None:
    assert parse_chat_page(chat_page(with_selector=False)).continuation == "TOP"


def test_chat_page_without_chat_raises() -> None:
    with pytest.raises(PageFormatError):
        parse_chat_page('<script>var ytInitialData = {"contents": {}};</script>')


def text_action(
    text_runs: list[dict[str, Any]], usec: int, author: str = "UCabc"
) -> dict[str, Any]:
    return {
        "addChatItemAction": {
            "item": {
                "liveChatTextMessageRenderer": {
                    "message": {"runs": text_runs},
                    "authorName": {"simpleText": "Pessoa"},
                    "authorExternalChannelId": author,
                    "timestampUsec": str(usec),
                }
            }
        }
    }


def test_chat_response() -> None:
    body = {
        "continuationContents": {
            "liveChatContinuation": {
                "continuations": [
                    {"timedContinuationData": {"continuation": "NEXT", "timeoutMs": 5000}}
                ],
                "actions": [text_action([{"text": "oi"}], 1_000_000)],
            }
        }
    }
    batch = parse_chat_response(body)
    assert batch.continuation == "NEXT"
    assert batch.timeout_ms == 5000
    assert len(batch.actions) == 1


def test_chat_response_ended() -> None:
    assert parse_chat_response({"responseContext": {}}).continuation is None


def test_runs_with_emojis() -> None:
    runs = [
        {"text": "kkkk"},
        {"emoji": {"emojiId": "😂", "shortcuts": [":face-with-tears-of-joy:"]}},
        {"emoji": {"emojiId": "UC/x", "isCustomEmoji": True, "shortcuts": [":_kekw:"]}},
        {"text": "mds"},
    ]
    assert runs_to_text(runs) == "kkkk 😂 kekw mds"


def test_message_uses_server_time_and_drops_backlog() -> None:
    lag = LagTracker()
    # Relógio do PC 5 s adiantado e rede com ~1 s de atraso.
    fresh = action_to_message(text_action([{"text": "oi"}], 100_000_000), "yt", 106.0, lag, 30.0)
    assert fresh is not None
    assert fresh.ts == pytest.approx(106.0)  # menor atraso visto até agora
    later = action_to_message(text_action([{"text": "b"}], 101_000_000), "yt", 108.0, lag, 30.0)
    assert later is not None and later.ts == pytest.approx(107.0)  # 101 + 6 (menor atraso)
    old = action_to_message(text_action([{"text": "velha"}], 50_000_000), "yt", 109.0, lag, 30.0)
    assert old is None  # 59 s de atraso a mais que o normal: histórico


def test_non_text_actions_ignored() -> None:
    lag = LagTracker()
    action = {"markChatItemAsDeletedAction": {"targetItemId": "x"}}
    assert action_to_message(action, "yt", 1.0, lag, 30.0) is None
