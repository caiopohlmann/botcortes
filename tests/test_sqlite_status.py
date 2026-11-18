from __future__ import annotations

import tempfile
import time
from pathlib import Path

from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.domain.models import Clip, Moment, MomentStatus


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
