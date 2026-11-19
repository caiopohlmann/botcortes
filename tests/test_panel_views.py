from __future__ import annotations

from cortes.adapters.storage.sqlite_status import Heartbeat
from cortes.domain.models import Channel, ChatRef, Controls, Platform
from cortes.panel.views import (
    STARTING_GRACE_S,
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
        "supervisor": beat("supervisor", NOW - 1, {"processes": {"worker": {"restarts_1h": 3}}}),
    }
    monitor, worker = process_views(beats, NOW, stale_after_s=30)
    assert monitor.tone == "bad" and "parado" in monitor.status
    assert worker.tone == "muted" and worker.status == "pausado"
    assert worker.restarts_1h == 3 and worker.errors_1h == 2
    assert live_data(beats, "monitor", NOW, 30) == {}


def test_system_pause_is_not_reported_as_a_problem() -> None:
    # "Pausar tudo": monitor e worker desligados de propósito, com erros velhos no batimento.
    beats = {
        "monitor": beat("monitor", NOW - 600),
        "worker": beat("worker", NOW - 600, {"log": {"errors_1h": 4}}),
    }
    views = process_views(beats, NOW, 30, system_paused=True)
    assert [(v.tone, v.status) for v in views] == [("muted", "pausado (sistema)")] * 2
    assert all(v.errors_1h == 0 for v in views)
    verdict = health_verdict(views, {}, None, [], min_free_gb=20, now=NOW, system_paused=True)
    assert verdict.tone == "muted" and verdict.title == "Sistema pausado"
    assert "Iniciar tudo" in verdict.reasons[0][1]
    # Problema de verdade continua aparecendo durante a pausa.
    full = health_verdict(
        views, {"disk_free_gb": 5}, None, [], min_free_gb=20, now=NOW, system_paused=True
    )
    assert full.tone == "bad"
    # Monitor ainda saindo (batimento vivo): mostra que está saindo, sem acusar problema.
    alive = process_views({"monitor": beat("monitor", NOW)}, NOW, 30, system_paused=True)
    assert alive[0].status.startswith("saindo") and alive[0].tone == "info"

    rows = channel_rows([Channel(login="a")], {}, Controls(system_paused=True), {}, {}, NOW)
    assert rows[0]["state"] == "paused" and rows[0]["paused"]
    assert channel_rows([Channel(login="a")], {}, Controls(), {}, {}, NOW)[0]["state"] == (
        "unknown"
    )


def test_missing_processes_are_reported() -> None:
    views = process_views({}, NOW, 30)
    assert [v.status for v in views] == ["nunca iniciou", "nunca iniciou"]
    verdict = health_verdict(views, {}, None, [], min_free_gb=20, now=NOW)
    assert verdict.tone == "bad"


def test_verdict_ok_and_warnings() -> None:
    beats = {"monitor": beat("monitor", NOW), "worker": beat("worker", NOW)}
    views = process_views(beats, NOW, 30)
    ok = health_verdict(
        views, {"disk_free_gb": 500, "ram_pct": 40}, {"temp_c": 60}, [], min_free_gb=20, now=NOW
    )
    assert ok.tone == "ok" and ok.title == "Tudo estável"

    channels = [
        {
            "name": "A",
            "state": "recording",
            "session": {"started_at": NOW - 600, "last_segment_at": NOW - 300},
        },
        {"name": "B", "state": "retrying"},
    ]
    warn = health_verdict(
        views,
        {"disk_free_gb": 30, "ram_pct": 95},
        {"temp_c": 88},
        channels,
        min_free_gb=20,
        now=NOW,
    )
    texts = " | ".join(text for _, text in warn.reasons)
    assert warn.tone == "warn"
    for expected in ("Disco enchendo", "RAM", "GPU quente", "A: gravação", "B: sessão"):
        assert expected in texts

    full = health_verdict(views, {"disk_free_gb": 5}, None, [], min_free_gb=20, now=NOW)
    assert full.tone == "bad"


def test_channel_rows_merge_config_monitor_and_controls() -> None:
    configured = [
        Channel(login="a", display_name="Fulano", extra_chats=(ChatRef(Platform.KICK, "fulano"),)),
        Channel(login="b", platform=Platform.YOUTUBE),
    ]
    monitor = {
        "channels": [
            {
                "key": "twitch-a",
                "state": "recording",
                "checked_at": NOW - 10,
                "stream": {
                    "title": "GTA RP",
                    "game": "GTA VI",
                    "viewers": 900,
                    "started_at": NOW - 3600,
                },
                "session": {
                    "chat_rate": 12.34,
                    "chat_baseline": 8.0,
                    "chat_score": 51.6,
                    "chat_warm": True,
                    "started_at": NOW - 1800,
                    "last_segment_at": NOW - 1,
                    "buffered_s": 180,
                },
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
    assert a["live_for"] == "1h 00min" and a["viewers_fmt"] == "900"
    assert b["paused"] and b["paused_individually"]
    assert b["state_label"] == "Pausado"  # a coluna "Ao vivo" é que mostra a live

    assert (a["live"], a["live_label"], a["live_tone"], a["live_note"]) == (
        "yes",
        "Ao vivo",
        "ok",
        None,
    )
    # Pausado, mas a pessoa está ao vivo: destaque para o dono saber que ela voltou.
    assert (b["live"], b["live_tone"], b["live_note"]) == ("yes", "warn", "sem gravar (pausado)")

    # Sem monitor rodando: todos desconhecidos, mas a lista de canais continua lá.
    rows = channel_rows(configured, {}, Controls(), {}, {}, NOW)
    assert [r["state"] for r in rows] == ["unknown", "unknown"]
    assert [r["live_label"] for r in rows] == ["?", "?"]


def test_channel_rows_live_column_for_every_state() -> None:
    names = ["paused_off", "offline", "filtered", "starting", "retrying", "unknown"]
    configured = [Channel(login=name) for name in names]
    live = {"title": "Voltei", "started_at": NOW - 120}
    monitor = {
        "channels": [
            {"key": "twitch-paused_off", "state": "paused", "stream": None},
            {"key": "twitch-offline", "state": "offline", "stream": None},
            {"key": "twitch-filtered", "state": "filtered", "stream": live},
            {"key": "twitch-starting", "state": "starting", "stream": live},
            {"key": "twitch-retrying", "state": "retrying", "stream": live},
            {"key": "twitch-unknown", "state": "unknown", "stream": None},
        ]
    }
    controls = Controls(paused_channels=frozenset({"twitch-paused_off"}))
    rows = {r["key"]: r for r in channel_rows(configured, monitor, controls, {}, {}, NOW)}

    def col(name: str) -> tuple[object, ...]:
        r = rows[f"twitch-{name}"]
        return r["live"], r["live_label"], r["live_tone"], r["live_note"]

    assert col("paused_off") == ("no", "Offline", "muted", None)
    assert col("offline") == ("no", "Offline", "muted", None)
    assert col("filtered") == ("yes", "Ao vivo", "info", "sem gravar (outro jogo)")
    assert col("starting") == ("yes", "Ao vivo", "info", None)
    assert col("retrying") == ("yes", "Ao vivo", "info", None)
    assert col("unknown") == ("unknown", "?", "muted", "sem resposta")
    assert rows["twitch-filtered"]["live_for"] == "2min"


def test_channel_rows_quality_from_panel_or_config() -> None:
    configured = [
        Channel(login="a"),  # sem quality no YAML: vale o padrão global
        Channel(login="b", quality="720p60,720p,best"),
        Channel(login="c", quality="480p,best"),  # fora dos botões: mostra a 1ª
    ]
    controls = Controls(channel_quality={"twitch-a": "720p"})
    rows = channel_rows(
        configured, {}, controls, {}, {}, NOW, default_quality="1080p60,1080p,936p60,720p60,best"
    )
    a, b, c = rows
    assert (a["quality"], a["quality_from_panel"], a["quality_config"]) == ("720p", True, "1080p")
    assert (b["quality"], b["quality_from_panel"]) == ("720p", False)
    assert c["quality"] == "480p" and c["quality_options"] == ["1080p", "720p"]


def supervisor_beat(processes: dict[str, dict[str, object]]) -> Heartbeat:
    return beat("supervisor", NOW - 1, {"processes": processes, "system_paused": False})


def test_supervisor_shows_the_real_state_without_waiting_for_stale_beats() -> None:
    # Batimentos ainda "frescos" (10 s), mas o supervisor já viu os processos saírem.
    fresh = {"monitor": beat("monitor", NOW - 10), "worker": beat("worker", NOW - 10)}

    paused = {
        **fresh,
        "supervisor": supervisor_beat(
            {
                "monitor": {"running": False, "paused": True},
                "worker": {"running": False, "paused": True},
            }
        ),
    }
    views = process_views(paused, NOW, 30)
    assert [v.state for v in views] == ["paused", "paused"]
    assert live_data(paused, "monitor", NOW, 30) == {}  # não mostra canal "Gravando" velho
    rows = channel_rows([Channel(login="a")], {}, Controls(), {}, {}, NOW, monitor_state="paused")
    assert rows[0]["state"] == "paused"

    crashed = {
        **fresh,
        "supervisor": supervisor_beat(
            {
                "monitor": {"running": True, "started_at": NOW - 3600},
                "worker": {"running": False, "last_exit_code": 3},
            }
        ),
    }
    monitor, worker = process_views(crashed, NOW, 30)
    assert monitor.state == "running" and monitor.status == "rodando"
    assert worker.tone == "bad" and "código 3" in worker.status
    assert (
        health_verdict(process_views(crashed, NOW, 30), {}, None, [], min_free_gb=20, now=NOW).tone
        == "bad"
    )


def test_process_just_started_shows_starting_then_stuck() -> None:
    # "Iniciar tudo": o supervisor subiu o worker há 5 s; o batimento é da execução anterior.
    old = {"worker": beat("worker", NOW - 10), "monitor": beat("monitor", NOW)}
    starting = {
        **old,
        "supervisor": supervisor_beat(
            {
                "monitor": {"running": True, "started_at": NOW - 3600},
                "worker": {"running": True, "started_at": NOW - 5},
            }
        ),
    }
    views = process_views(starting, NOW, 30)
    assert views[1].state == "starting" and views[1].tone == "info"
    verdict = health_verdict(views, {}, None, [], min_free_gb=20, now=NOW)
    assert verdict.title == "Tudo estável" and "iniciando" in verdict.reasons[0][1]
    assert (
        channel_rows([Channel(login="a")], {}, Controls(), {}, {}, NOW, monitor_state="starting")[
            0
        ]["state"]
        == "starting"
    )

    stuck = {
        "supervisor": supervisor_beat(
            {
                "monitor": {"running": True, "started_at": NOW - STARTING_GRACE_S - 5},
                "worker": {"running": True, "started_at": NOW - 3600},
            }
        ),
        "worker": beat("worker", NOW - 100),
    }
    monitor, worker = process_views(stuck, NOW, 30)
    assert monitor.tone == "bad" and "sem sinal desde que iniciou" in monitor.status
    assert worker.tone == "bad" and "travado" in worker.status


def test_channel_rows_use_panel_check_when_newer() -> None:
    configured = [Channel(login="a"), Channel(login="b"), Channel(login="c")]
    checks = {
        "twitch-a": {"checked_at": NOW - 10, "stream": {"title": "Voltei", "started_at": NOW - 60}},
        "twitch-b": {"checked_at": NOW - 10, "stream": None},
    }
    # Sistema pausado (monitor desligado): só a verificação do painel diz quem está ao vivo.
    rows = channel_rows(configured, {}, Controls(system_paused=True), {}, {}, NOW, checks=checks)
    a, b, c = rows
    assert (a["live"], a["live_tone"], a["live_note"]) == ("yes", "warn", "sem gravar (pausado)")
    assert a["live_checked"] == "há 10s" and a["title"] == "Voltei" and a["live_for"] == "1min"
    assert a["state_label"] == "Pausado"
    assert (b["live"], b["live_checked"]) == ("no", "há 10s")
    assert (c["live"], c["live_checked"]) == ("unknown", None)  # não respondeu: segue "?"

    # Monitor rodando com dado mais novo que a verificação: vale o do monitor.
    monitor = {
        "channels": [{"key": "twitch-a", "state": "offline", "checked_at": NOW - 2, "stream": None}]
    }
    [a] = channel_rows(configured[:1], monitor, Controls(), {}, {}, NOW, checks=checks)
    assert (a["live"], a["live_checked"]) == ("no", None)
