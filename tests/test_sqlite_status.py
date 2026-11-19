from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.domain.models import QUALITY_PRESETS, Clip, Moment, MomentStatus


def new_store() -> SqliteStatusStore:
    folder = Path(tempfile.mkdtemp(prefix="cortes-test-"))
    return SqliteStatusStore(folder / "cortes.db")


def test_controls_roundtrip() -> None:
    store = new_store()
    assert not store.get_controls().paused_all
    store.set_paused_all(True)
    store.set_worker_paused(True)
    store.set_channel_paused("kick-fulano", True)
    store.set_channel_paused("twitch-ciclano", True)
    store.set_channel_paused("twitch-ciclano", False)
    controls = store.get_controls()
    assert controls.paused_all and controls.worker_paused
    assert controls.paused_channels == frozenset({"kick-fulano"})
    store.set_paused_all(False)
    assert not store.get_controls().paused_all
    assert store.get_controls().channel_paused("kick-fulano")


def test_channel_quality_roundtrip() -> None:
    store = new_store()
    assert store.get_controls().quality_for("kick-fulano") is None
    store.set_channel_quality("kick-fulano", "720p")
    store.set_channel_quality("kick-fulano", "1080p")  # troca
    store.set_channel_quality("twitch-ciclano", "720p")
    store.set_channel_paused("kick-fulano", True)  # pausa e qualidade não se misturam
    controls = store.get_controls()
    assert dict(controls.channel_quality) == {"kick-fulano": "1080p", "twitch-ciclano": "720p"}
    assert controls.quality_for("twitch-ciclano") == QUALITY_PRESETS["720p"]
    assert controls.paused_channels == frozenset({"kick-fulano"})

    store.resume_all()  # "Iniciar tudo" não apaga a escolha de qualidade
    store.set_channel_quality("twitch-ciclano", None)  # "usar o config"
    assert dict(store.get_controls().channel_quality) == {"kick-fulano": "1080p"}
    with pytest.raises(ValueError):
        store.set_channel_quality("kick-fulano", "4k")


def test_system_pause_and_resume_all() -> None:
    store = new_store()
    store.set_system_paused(True)
    store.set_paused_all(True)
    store.set_worker_paused(True)
    store.set_channel_paused("kick-fulano", True)
    controls = store.get_controls()
    assert controls.system_paused and controls.anything_paused
    assert controls.channel_paused("twitch-qualquer")  # pausa do sistema vale para todos
    store.resume_all()
    controls = store.get_controls()
    assert not controls.anything_paused
    assert controls.paused_channels == frozenset({"kick-fulano"})  # a do canal fica


def test_restart_request_keeps_latest_time_and_ignores_pauses() -> None:
    store = new_store()
    assert store.restart_requested_at("monitor") is None
    store.request_restart("monitor", 100.5)
    store.request_restart("monitor", 200.25)
    assert store.restart_requested_at("monitor") == 200.25
    assert store.restart_requested_at("worker") is None
    # O pedido fica na mesma tabela das pausas, mas não vira pausa.
    controls = store.get_controls()
    assert not controls.paused_all and not controls.worker_paused
    assert controls.paused_channels == frozenset()


def test_heartbeat_upsert() -> None:
    store = new_store()
    store.write_heartbeat("monitor", 10, 100.0, {"a": 1})
    store.write_heartbeat("monitor", 11, 200.0, {"a": 2, "texto": "ação"})
    beats = store.heartbeats()
    assert list(beats) == ["monitor"]
    beat = beats["monitor"]
    assert beat.pid == 11 and beat.started_at == 200.0
    assert beat.data == {"a": 2, "texto": "ação"}


def test_metrics_window_and_prune() -> None:
    store = new_store()
    for ts in (100.0, 200.0, 300.0):
        store.add_metric("host", {"cpu_pct": ts / 10}, ts)
    store.add_metric("gpu", {"util_pct": 50}, 250.0)
    assert [ts for ts, _ in store.metrics_since("host", 150.0)] == [200.0, 300.0]
    assert store.prune_metrics(250.0) == 2  # apaga ts < 250: host 100 e 200
    assert [ts for ts, _ in store.metrics_since("host", 0)] == [300.0]
    assert [ts for ts, _ in store.metrics_since("gpu", 0)] == [250.0]


def moment(moment_id: str, channel: str, created: float, status: MomentStatus) -> Moment:
    return Moment(
        id=moment_id,
        channel=channel,
        display_name=channel,
        peak_ts=created,
        event_ts=created,
        start_ts=created - 40,
        end_ts=created + 15,
        chat_score=60.0,
        source_path=Path("x.ts"),
        source_start_ts=created - 50,
        source_end_ts=created + 20,
        created_at=created,
        status=status,
    )


def clip(moment_id: str, channel: str, created: float) -> Clip:
    return Clip(
        id=moment_id,
        moment_id=moment_id,
        channel=channel,
        display_name=channel.upper(),
        video_path=Path(f"{moment_id}.mp4"),
        description_path=Path(f"{moment_id}.txt"),
        score=70.0,
        chat_score=60.0,
        audio_score=0.5,
        language="pt",
        transcript="olha isso",
        duration_s=55.0,
        processing_s=20.0,
        created_at=created,
    )


def test_counts_and_recent() -> None:
    store = new_store()
    now = time.time()
    store.add_moment(moment("m1", "twitch-a", now - 10, MomentStatus.RENDERED))
    store.add_moment(moment("m2", "twitch-a", now - 5, MomentStatus.REJECTED))
    store.add_moment(moment("m3", "kick-b", now - 2 * 86400, MomentStatus.RENDERED))
    store.add_clip(clip("m1", "twitch-a", now - 9))
    store.add_clip(clip("m3", "kick-b", now - 2 * 86400))
    store.enqueue("m2")

    assert store.clips_by_channel(now - 3600) == {"twitch-a": 1}
    assert store.count_clips_since(now - 7 * 86400) == 2
    assert store.count_clips_since(0) == 2
    assert store.moment_counts(now - 3600) == {"rendered": 1, "rejected": 1}
    assert store.moments_by_channel(now - 3600) == {"twitch-a": 2}
    assert store.job_counts() == {"pending": 1}
    recent = store.recent_clips(10)
    assert [c.id for c in recent] == ["m1", "m3"]
    row = store.get_clip_row("m1")
    assert row is not None and row.display_name == "TWITCH-A"
    assert store.get_clip_row("nao-existe") is None


def test_old_database_gets_the_new_column_and_keeps_its_data() -> None:
    import sqlite3

    from cortes.adapters.storage.sqlite_store import SqliteStore
    from cortes.domain.models import Moment

    folder = Path(tempfile.mkdtemp(prefix="cortes-old-"))
    db = folder / "cortes.db"
    with sqlite3.connect(db) as conn:  # banco de antes da coluna hype_s
        conn.execute(
            "CREATE TABLE moments (id TEXT PRIMARY KEY, channel TEXT NOT NULL, "
            "display_name TEXT NOT NULL, peak_ts REAL NOT NULL, event_ts REAL NOT NULL, "
            "start_ts REAL NOT NULL, end_ts REAL NOT NULL, chat_score REAL NOT NULL, "
            "source_path TEXT NOT NULL, source_start_ts REAL NOT NULL, "
            "source_end_ts REAL NOT NULL, status TEXT NOT NULL, final_score REAL, "
            "error TEXT, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO moments VALUES ('m-velho', 'twitch-a', 'a', 1, 1, 0, 2, 50, 'x.ts', "
            "0, 2, 'queued', NULL, NULL, 1)"
        )
    store = SqliteStore(db)
    store2 = SqliteStore(db)  # abrir de novo (outro processo) não quebra
    old = store.get_moment("m-velho")
    assert old is not None and old.hype_s is None
    new = Moment(
        id="m-novo", channel="twitch-a", display_name="a", peak_ts=1, event_ts=1, start_ts=0,
        end_ts=2, chat_score=50, source_path=Path("y.ts"), source_start_ts=0,
        source_end_ts=2, created_at=1, hype_s=6.5,
    )  # fmt: skip
    store2.add_moment(new)
    loaded = store.get_moment("m-novo")
    assert loaded is not None and loaded.hype_s == 6.5
