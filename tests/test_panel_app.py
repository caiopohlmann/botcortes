"""Painel web de ponta a ponta (precisa de fastapi + httpx: roda no PC)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.config import PathsSettings, Secrets, Settings
from cortes.domain.models import Channel, Clip, Platform
from cortes.panel.app import check_bind, create_app


def make(tmp_path: Path, password: str = "") -> tuple[TestClient, SqliteStatusStore]:
    settings = Settings(
        paths=PathsSettings(data_dir=tmp_path),
        channels=[Channel(login="fulano"), Channel(login="ciclano", platform=Platform.KICK)],
    )
    secrets = Secrets(_env_file=None, panel_password=password)
    app = create_app(settings, secrets, run_assistant=False)
    return TestClient(app), SqliteStatusStore(settings.paths.database)


def add_clip_files(tmp_path: Path, store: SqliteStatusStore) -> Clip:
    folder = tmp_path / "clips" / "pending" / "fulano" / "2-legenda-revisar"
    folder.mkdir(parents=True)
    video = folder / "20261118-210000-twitch-fulano-aaaa_nota70.mp4"
    video.write_bytes(b"mp4")
    video.with_suffix(".txt").write_text("título\n", encoding="utf-8")
    (folder / "_LEIA-ME.txt").write_text("REVISAR A LEGENDA\n\nassista antes", encoding="utf-8")
    clip = Clip(
        id="20261118-210000-twitch-fulano-aaaa",
        moment_id="20261118-210000-twitch-fulano-aaaa",
        channel="twitch-fulano",
        display_name="fulano",
        video_path=video,
        description_path=video.with_suffix(".txt"),
        score=70,
        chat_score=70,
        audio_score=0.5,
        language="pt",
        transcript="olha o cara jogou o carro",
        duration_s=55,
        processing_s=20,
        created_at=time.time(),
        caption_grade="revisar",
        caption_score=79,
        caption_notes=("14% das palavras com confiança baixa",),
        doubtful_words=("jogou (31%)",),
    )
    store.add_clip(clip)
    return clip


def test_dashboard_renders_without_any_process(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "fulano" in resp.text and "Kick" in resp.text
    assert "nunca iniciou" in resp.text
    for name in ("overview", "channels", "clips", "system"):
        assert client.get(f"/partials/{name}").status_code == 200
    assert client.get("/partials/nada").status_code == 404


def test_dashboard_with_heartbeats(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    now = time.time()
    store.write_heartbeat(
        "monitor",
        1,
        now - 60,
        {
            "channels": [
                {
                    "key": "twitch-fulano",
                    "state": "recording",
                    "checked_at": now,
                    "stream": {"title": "GTA 6 RP", "game": "GTA VI", "viewers": 10},
                    "session": {"chat_rate": 5.0, "started_at": now - 30, "buffered_s": 30,
                                "last_segment_at": now},
                }
            ],
            "host": {"cpu_pct": 12.0, "ram_used_gb": 8.0, "ram_total_gb": 32.0,
                     "disk_free_gb": 400.0, "net_rx_mbps": 22.0, "boot_time": now - 7200},
        },
    )
    store.write_heartbeat("worker", 2, now - 60, {"gpu": {"name": "RTX", "util_pct": 30.0}})
    store.add_metric("host", {"cpu_pct": 10}, now - 120)
    store.add_metric("host", {"cpu_pct": 20}, now - 60)
    text = client.get("/").text
    assert "GTA 6 RP" in text and "Gravando" in text and "Tudo estável" in text
    assert "<polyline" in text


def test_pause_controls(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    assert client.post("/control/all/pause", follow_redirects=False).status_code == 303
    assert store.get_controls().paused_all
    client.post("/control/all/resume")
    client.post("/control/worker/pause")
    client.post("/control/channel/kick-ciclano/pause")
    controls = store.get_controls()
    assert not controls.paused_all and controls.worker_paused
    assert controls.paused_channels == frozenset({"kick-ciclano"})
    assert client.post("/control/channel/kick-outro/pause").status_code == 404
    assert client.post("/control/all/explodir").status_code == 404


def test_post_from_other_site_is_blocked(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    resp = client.post("/control/all/pause", headers={"origin": "https://site-malicioso.com"})
    assert resp.status_code == 403
    assert not store.get_controls().paused_all


def test_password(tmp_path: Path) -> None:
    client, _ = make(tmp_path, password="segredo")
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("qualquer", "errada")).status_code == 401
    assert client.get("/", auth=("qualquer", "segredo")).status_code == 200


def test_network_bind_requires_password() -> None:
    check_bind("127.0.0.1", "")
    with pytest.raises(SystemExit):
        check_bind("0.0.0.0", "")
    check_bind("0.0.0.0", "senha")


def test_folder_browser(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    add_clip_files(tmp_path, store)
    assert "fulano" in client.get("/arquivos?p=pending").text
    page = client.get("/arquivos?p=pending/fulano").text
    assert "2-legenda-revisar" in page and "Revisar legenda" in page
    page = client.get("/arquivos?p=pending/fulano/2-legenda-revisar").text
    assert "REVISAR A LEGENDA" in page  # o _LEIA-ME aparece no topo
    assert "14% das palavras com confiança baixa" in page and "jogou (31%)" in page
    assert "Pedir ao Claude" in page
    raw = client.get("/arquivos/raw?p=pending/fulano/2-legenda-revisar/"
                     "20261118-210000-twitch-fulano-aaaa_nota70.mp4")
    assert raw.status_code == 200 and raw.content == b"mp4"
    assert client.get("/arquivos?p=../..").status_code == 404
    assert client.get("/arquivos/raw?p=../cortes.db").status_code == 404


def test_ask_claude_queues_request(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    clip = add_clip_files(tmp_path, store)
    resp = client.post("/assistente", json={"clip_id": clip.id, "message": "troca jogou por pulou"})
    assert resp.status_code == 200 and resp.json()["status"] == "queued"
    [item] = store.assistant_requests()
    assert item.clip_id == clip.id and item.status == "queued"
    assert client.post("/assistente", json={"clip_id": clip.id, "message": " "}).status_code == 400
    assert client.post("/assistente", json={"clip_id": "x", "message": "a"}).status_code == 404
    page = client.get("/").text
    assert "troca jogou por pulou" in page and "Na fila" in page
    assert client.post(f"/assistente/{item.id}/terminal").status_code == 404  # sem sessão ainda

