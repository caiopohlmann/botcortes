from __future__ import annotations

from cortes.adapters.storage.sqlite_status import Heartbeat
from cortes.domain.models import Channel, ChatRef, Controls, Platform
from cortes.panel.views import (
    bucket_counts,
    channel_rows,
    fmt_ago,
    fmt_duration,
    fmt_num,
    health_verdict,
    live_data,
    process_views,
    sparkline,
)

NOW = 10_000.0


def beat(process: str, updated: float, data: dict[str, object] | None = None) -> Heartbeat:
    return Heartbeat(process, 1, updated - 3600, updated, data or {})


def test_formatting() -> None:
    assert fmt_duration(42) == "42s"
    assert fmt_duration(3 * 60) == "3min"
    assert fmt_duration(2 * 3600 + 5 * 60) == "2h 05min"
    assert fmt_duration(2 * 86400 + 3 * 3600) == "2d 3h"
    assert fmt_duration(None) == "—"
    assert fmt_ago(NOW - 2, NOW) == "agora"
    assert fmt_ago(NOW - 120, NOW) == "há 2min"
    assert fmt_num(12.345, 1, " GB") == "12,3 GB"
    assert fmt_num(None) == "—"
    assert fmt_num(True) == "—"


def test_sparkline_and_buckets() -> None:
    line = sparkline([0, 50, None, 100], width=30, height=10, vmax=100)
    assert not line.empty and line.peak == 100 and line.last == 100
    assert len(line.points.split()) == 3  # o None vira buraco
    assert sparkline([5]).empty
    assert bucket_counts([0.5, 1.5, 1.7, 9.9, 20], 0, 10, 10) == [1, 2, 0, 0, 0, 0, 0, 0, 0, 1]


def test_process_views_detect_stale_and_paused() -> None:
    beats = {
        "monitor": beat("monitor", NOW - 100),  # parou de mandar sinal
        "worker": beat("worker", NOW - 2, {"paused": True, "log": {"errors_1h": 2}}),
        "supervisor": beat(
            "supervisor", NOW - 1, {"processes": {"worker": {"restarts_1h": 3}}}
        ),
    }
    monitor, worker = process_views(beats, NOW, stale_after_s=30)
    assert monitor.tone == "bad" and "parado" in monitor.status
    assert worker.tone == "muted" and worker.status == "pausado"
    assert worker.restarts_1h == 3 and worker.errors_1h == 2
    assert live_data(beats, "monitor", NOW, 30) == {}


def test_missing_processes_are_reported() -> None:
    views = process_views({}, NOW, 30)
    assert [v.status for v in views] == ["nunca iniciou", "nunca iniciou"]
    verdict = health_verdict(views, {}, None, [], min_free_gb=20, now=NOW)
    assert verdict.tone == "bad"


def test_verdict_ok_and_warnings() -> None:
    beats = {"monitor": beat("monitor", NOW), "worker": beat("worker", NOW)}
    views = process_views(beats, NOW, 30)
    ok = health_verdict(views, {"disk_free_gb": 500, "ram_pct": 40}, {"temp_c": 60}, [],
                        min_free_gb=20, now=NOW)
    assert ok.tone == "ok" and ok.title == "Tudo estável"

    channels = [
        {"name": "A", "state": "recording",
         "session": {"started_at": NOW - 600, "last_segment_at": NOW - 300}},
        {"name": "B", "state": "retrying"},
    ]
    warn = health_verdict(views, {"disk_free_gb": 30, "ram_pct": 95}, {"temp_c": 88}, channels,
                          min_free_gb=20, now=NOW)
    texts = " | ".join(text for _, text in warn.reasons)
    assert warn.tone == "warn"
    for expected in ("Disco enchendo", "RAM", "GPU quente", "A: gravação", "B: sessão"):
        assert expected in texts

    full = health_verdict(views, {"disk_free_gb": 5}, None, [], min_free_gb=20, now=NOW)
    assert full.tone == "bad"


def test_channel_rows_merge_config_monitor_and_controls() -> None:
    configured = [
        Channel(login="a", display_name="Fulano",
                extra_chats=(ChatRef(Platform.KICK, "fulano"),)),
        Channel(login="b", platform=Platform.YOUTUBE),
    ]
    monitor = {
        "channels": [
            {
                "key": "twitch-a",
                "state": "recording",
                "checked_at": NOW - 10,
                "stream": {"title": "GTA RP", "game": "GTA VI", "viewers": 900,
                           "started_at": NOW - 3600},
                "session": {"chat_rate": 12.34, "chat_baseline": 8.0, "chat_score": 51.6,
                            "chat_warm": True, "started_at": NOW - 1800,
                            "last_segment_at": NOW - 1, "buffered_s": 180},
            },
            {"key": "youtube-b", "state": "paused", "stream": {"title": "Live"}},
        ]
    }
    controls = Controls(paused_channels=frozenset({"youtube-b"}))
    rows = channel_rows(configured, monitor, controls, {"twitch-a": 3}, {"twitch-a": 5}, NOW)
    a, b = rows
    assert a["state_label"] == "Gravando" and a["platform"] == "Twitch"
    assert a["chat_rate"] == "12,3" and a["clips_today"] == 3 and a["moments_today"] == 5
    assert a["extra_chats"] == ["Kick: fulano"]
    assert a["live_for"] == "1h 00min"
    assert b["paused"] and b["paused_individually"]
    assert b["state_label"] == "Pausado (ao vivo)"

    # Sem monitor rodando: todos desconhecidos, mas a lista de canais continua lá.
    rows = channel_rows(configured, {}, Controls(), {}, {}, NOW)
    assert [r["state"] for r in rows] == ["unknown", "unknown"]
