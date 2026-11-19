"""Painel web de ponta a ponta (precisa de fastapi + httpx: roda no PC)."""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.config import PathsSettings, Secrets, Settings
from cortes.domain.models import Channel, Clip, Moment, MomentStatus, Platform
from cortes.panel.app import check_bind, create_app


def make(tmp_path: Path, password: str = "") -> tuple[TestClient, SqliteStatusStore]:
    settings = Settings(
        paths=PathsSettings(data_dir=tmp_path),
        channels=[Channel(login="fulano"), Channel(login="ciclano", platform=Platform.KICK)],
    )
    # config_dir isolado: o painel relê channels.yaml de lá (não pode ler o config/ do projeto).
    secrets = Secrets(_env_file=None, panel_password=password, cortes_config_dir=tmp_path)
    app = create_app(settings, secrets, run_assistant=False)
    return TestClient(app), SqliteStatusStore(settings.paths.database)


def add_clip_files(tmp_path: Path, store: SqliteStatusStore) -> Clip:
    folder = tmp_path / "clips" / "fulano"
    folder.mkdir(parents=True)
    video = folder / "1.mp4"  # corte numerado: o painel acha no banco pelo caminho
    video.write_bytes(b"mp4")
    video.with_suffix(".txt").write_text("título\n", encoding="utf-8")
    now = time.time()
    # O clipe referencia o momento (chave estrangeira): o momento precisa existir antes.
    store.add_moment(
        Moment(
            id="20261118-210000-twitch-fulano-aaaa",
            channel="twitch-fulano",
            display_name="fulano",
            peak_ts=now,
            event_ts=now,
            start_ts=now - 40,
            end_ts=now + 15,
            chat_score=70.0,
            source_path=Path("x.ts"),
            source_start_ts=now - 50,
            source_end_ts=now + 20,
            created_at=now,
            status=MomentStatus.RENDERED,
        )
    )
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
    assert "PC e processos" in resp.text  # aba no topo
    system = client.get("/sistema")
    assert system.status_code == 200 and "nunca iniciou" in system.text
    assert 'href="/sistema" aria-current="page"' in system.text
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
                    "session": {
                        "chat_rate": 5.0,
                        "started_at": now - 30,
                        "buffered_s": 30,
                        "last_segment_at": now,
                    },
                }
            ],
            "host": {
                "cpu_pct": 12.0,
                "ram_used_gb": 8.0,
                "ram_total_gb": 32.0,
                "disk_free_gb": 400.0,
                "net_rx_mbps": 22.0,
                "boot_time": now - 7200,
            },
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
    client.post("/control/channel/kick-ciclano/pause")
    assert store.get_controls().paused_channels == frozenset({"kick-ciclano"})
    assert client.post("/control/channel/kick-outro/pause").status_code == 404
    assert client.post("/control/channel/kick-ciclano/explodir").status_code == 404
    # Os botões antigos viraram um só ("Pausar tudo" / "Iniciar tudo").
    assert client.post("/control/all/pause").status_code in (404, 405)
    assert client.post("/control/worker/pause").status_code in (404, 405)


def test_channel_quality_control(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    assert client.post("/control/channel/kick-ciclano/quality/720p").status_code == 200
    assert dict(store.get_controls().channel_quality) == {"kick-ciclano": "720p"}
    text = client.get("/").text
    assert "/control/channel/kick-ciclano/quality/1080p" in text
    assert "usar o config" in text  # escolhido no painel: dá para voltar ao config

    assert client.post("/control/channel/kick-ciclano/quality/config").status_code == 200
    assert dict(store.get_controls().channel_quality) == {}
    assert client.post("/control/channel/kick-ciclano/quality/4k").status_code == 404
    assert client.post("/control/channel/kick-outro/quality/720p").status_code == 404


def test_pause_all_needs_a_supervisor_that_knows_it(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    now = time.time()
    resp = client.post("/control/system/pause", follow_redirects=False)
    assert resp.status_code == 409 and "cortes run" in resp.json()["detail"]
    assert "Pausar tudo" not in client.get("/").text  # sem supervisor, nem aparece

    # Supervisor de antes desta versão (sem "system_paused" no batimento): pede para reabrir.
    store.write_heartbeat("supervisor", 2, now - 60, {"processes": {}})
    resp = client.post("/control/system/pause", follow_redirects=False)
    assert resp.status_code == 409 and "feche e abra" in resp.json()["detail"]
    assert not store.get_controls().system_paused

    store.write_heartbeat("supervisor", 2, now - 60, {"processes": {}, "system_paused": False})
    assert "Pausar tudo" in client.get("/").text
    assert client.post("/control/system/pause", follow_redirects=False).status_code == 303
    assert store.get_controls().system_paused


def test_resume_all_turns_everything_back_on(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    store.set_system_paused(True)
    store.set_worker_paused(True)  # pausa interna que tenha ficado ligada
    store.set_channel_paused("kick-ciclano", True)
    text = client.get("/").text
    assert "Sistema pausado" in text and "Iniciar tudo" in text  # aparece mesmo sem supervisor
    assert client.post("/control/system/resume", follow_redirects=False).status_code == 303
    controls = store.get_controls()
    assert not controls.anything_paused
    assert controls.paused_channels == frozenset({"kick-ciclano"})  # escolha por canal fica


def test_restart_system_needs_supervisor(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    now = time.time()
    # Sem o supervisor os processos sairiam e ninguém os subiria de novo: recusa.
    store.write_heartbeat("monitor", 1, now - 60, {"channels": []})
    resp = client.post("/control/system/restart", follow_redirects=False)
    assert resp.status_code == 409 and "cortes run" in resp.json()["detail"]
    assert store.restart_requested_at("system") is None

    store.write_heartbeat("supervisor", 2, now - 60, {"processes": {}})
    assert "Reiniciar sistema" in client.get("/").text
    assert client.post("/control/system/restart", follow_redirects=False).status_code == 303
    requested = store.restart_requested_at("system")
    assert requested is not None and requested >= now


def test_restart_system_works_with_the_monitor_down(tmp_path: Path) -> None:
    # Monitor travado/parado é justamente quando forçar o reinício ajuda.
    client, store = make(tmp_path)
    store.write_heartbeat("supervisor", 2, time.time() - 60, {"processes": {}})
    assert "Reiniciar sistema" in client.get("/").text
    assert client.post("/control/system/restart", follow_redirects=False).status_code == 303
    assert store.restart_requested_at("system") is not None


def test_restart_button_hidden_without_supervisor(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    store.write_heartbeat("monitor", 1, time.time() - 60, {"channels": []})
    assert "Reiniciar sistema" not in client.get("/").text


def test_healthz_reports_the_panel_pid(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    body = client.get("/healthz").json()
    assert body["ok"] is True and isinstance(body["pid"], int)


def test_panel_rereads_channels_yaml_and_warns_monitor_is_outdated(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    store.write_heartbeat("monitor", 1, time.time() - 600, {"channels": []})
    store.write_heartbeat("supervisor", 2, time.time() - 600, {"processes": {}})
    assert "ciclano" in client.get("/").text

    (tmp_path / "channels.yaml").write_text(
        "channels:\n  - {login: beltrano, platform: youtube, display_name: Beltrano}\n",
        encoding="utf-8",
    )
    text = client.get("/").text
    assert "Beltrano" in text and "ciclano" not in text
    # Monitor antigo (não informa a versão do arquivo): só pega a mudança ao reiniciar.
    assert "monitor ainda não pegou a versão nova" in text
    # A pausa aceita o canal novo e recusa o que saiu da lista.
    assert client.post("/control/channel/youtube-beltrano/pause").status_code == 200
    assert client.post("/control/channel/kick-ciclano/pause").status_code == 404

    # Monitor reiniciado depois da mudança: some o aviso.
    store.write_heartbeat("monitor", 3, time.time() + 1, {"channels": []})
    assert "channels.yaml mudou" not in client.get("/").text

    # Monitor novo, que relê sozinho: sem aviso enquanto está em dia...
    mtime = (tmp_path / "channels.yaml").stat().st_mtime
    store.write_heartbeat("monitor", 4, time.time() - 600, {"channels_loaded_at": mtime})
    assert "channels.yaml mudou" not in client.get("/").text
    # ...e com aviso se ficar para trás por mais que alguns segundos.
    store.write_heartbeat("monitor", 4, time.time() - 600, {"channels_loaded_at": mtime - 60})
    os.utime(tmp_path / "channels.yaml", (mtime - 30, mtime - 30))
    assert "monitor ainda não pegou a versão nova" in client.get("/").text


def test_broken_channels_yaml_keeps_previous_list(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    (tmp_path / "channels.yaml").write_text("channels: [ {login: ", encoding="utf-8")
    text = client.get("/").text
    assert "ciclano" in text
    assert "tem um erro e foi ignorado" in text  # o painel avisa do erro no arquivo


CHANNELS_YAML = (
    "# Canais monitorados.\r\n"
    "channels:\r\n"
    "  - login: fulano\r\n"
    "    platform: twitch\r\n"
    "\r\n"
    "  # Exemplo (comentário no fim do arquivo)\r\n"
)


@pytest.fixture
def fake_check(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Troca a consulta à plataforma: nenhum teste fala com Twitch/Kick/YouTube."""
    from cortes.adapters.channel_check import ChannelCheck

    checked: list[str] = []

    async def check(channel: Channel, secrets: Secrets) -> ChannelCheck:
        checked.append(channel.key)
        if channel.login == "naoexiste":
            raise LookupError(f"o Kick não tem o canal {channel.url}")
        name = "Fulano Oficial" if channel.platform is Platform.TWITCH else None
        return ChannelCheck(display_name=name, verified=channel.login != "semresposta")

    monkeypatch.setattr("cortes.panel.app.check_channel", check)
    return checked


def test_add_channel_by_link(tmp_path: Path, fake_check: list[str]) -> None:
    path = tmp_path / "channels.yaml"
    path.write_bytes(CHANNELS_YAML.encode("utf-8"))
    client, _ = make(tmp_path)

    resp = client.post(
        "/canais",
        json={
            "link": "https://www.kick.com/Sicrano/videos",
            "display_name": "Sicrano",
            "language": "pt",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == "kick-sicrano" and "adicionado" in resp.json()["message"]
    text = path.read_bytes().decode("utf-8")
    assert text.startswith(CHANNELS_YAML.rstrip("\r\n"))  # o resto do arquivo fica igual
    assert '  - login: "sicrano"\r\n    platform: kick\r\n    display_name: "Sicrano"' in text
    assert "\n" not in text.replace("\r\n", "")  # mantém o CRLF do arquivo
    assert "Adicionado pelo painel" in text
    assert "Sicrano" in client.get("/").text  # o painel já mostra o canal novo

    # Sem nome: usa o que a plataforma informa. Idioma vazio = detectar sozinho.
    resp = client.post("/canais", json={"link": "youtube.com/@beltrano/live", "language": ""})
    assert resp.status_code == 200, resp.text
    from cortes.config import read_channels

    channels = {c.key: c for c in read_channels(path)}
    assert channels["youtube-beltrano"].language is None
    assert channels["kick-sicrano"].language == "pt"


def test_add_channel_rejects_bad_links(tmp_path: Path, fake_check: list[str]) -> None:
    path = tmp_path / "channels.yaml"
    path.write_bytes(CHANNELS_YAML.encode("utf-8"))
    client, _ = make(tmp_path)

    def post(link: str, **extra: str) -> tuple[int, str]:
        resp = client.post("/canais", json={"link": link, "language": "pt", **extra})
        return resp.status_code, resp.json().get("detail", "")

    assert post("twitch.tv/Fulano")[0] == 409  # já está na lista
    assert post("https://instagram.com/fulano")[0] == 400
    assert post("twitch.tv/directory")[0] == 400
    code, detail = post("kick.com/naoexiste")
    assert code == 404 and "não tem o canal" in detail
    assert post("kick.com/ciclano", language="xx")[0] == 400
    assert path.read_bytes().decode("utf-8") == CHANNELS_YAML  # nada foi gravado
    assert "twitch-fulano" not in fake_check  # repetido nem consulta a plataforma

    # Plataforma sem resposta: aceita, mas avisa que não conferiu.
    resp = client.post("/canais", json={"link": "kick.com/semresposta", "language": "pt"})
    assert resp.status_code == 200 and "não foi conferido" in resp.json()["message"]
    assert "não deu para conferir" in path.read_text(encoding="utf-8")


def test_verify_live_now_without_the_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cortes.panel.live_check import LiveCheck

    calls: list[list[str]] = []

    async def fake(channels: Sequence[Channel], settings: Settings, secrets: Secrets) -> LiveCheck:
        calls.append([c.key for c in channels])
        return LiveCheck(
            checked_at=time.time(),
            results={
                "twitch-fulano": {"title": "GTA 6 RP", "game": "", "viewers": 9, "started_at": None}
            },
            failed=["kick-ciclano"],
            message="Ao vivo agora: fulano (1 de 2). Sem resposta: ciclano.",
        )

    monkeypatch.setattr("cortes.panel.app.check_live", fake)
    client, store = make(tmp_path)
    store.set_system_paused(True)  # tudo pausado: monitor desligado
    resp = client.post("/canais/verificar")
    assert resp.status_code == 200 and "1 de 2" in resp.json()["message"]
    text = client.get("/").text
    assert "Última verificação" in text and "verificado agora" in text and "GTA 6 RP" in text
    # Clique repetido logo em seguida: reaproveita a consulta (não bate nas plataformas de novo).
    client.post("/canais/verificar")
    assert len(calls) == 1
    assert store.get_controls().system_paused  # não liga nada


def test_post_from_other_site_is_blocked(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    store.write_heartbeat("supervisor", 2, time.time(), {"processes": {}, "system_paused": False})
    resp = client.post("/control/system/pause", headers={"origin": "https://site-malicioso.com"})
    assert resp.status_code == 403
    assert not store.get_controls().system_paused


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
    assert "fulano" in client.get("/arquivos").text
    page = client.get("/arquivos?p=fulano").text
    assert "1.mp4" in page and "Revisar legenda" in page  # dados do banco, pelo caminho
    assert "14% das palavras com confiança baixa" in page and "jogou (31%)" in page
    assert "Pedir ao Claude" in page
    raw = client.get("/arquivos/raw?p=fulano/1.mp4")
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
    # O card "Pedir ao Claude" saiu do painel: o pedido aparece no próprio corte (pastas).
    assert "Enviar para o Claude" not in client.get("/").text
    page = client.get(f"/arquivos?p={FOLDER}").text
    assert "troca jogou por pulou" in page and "Na fila" in page
    assert client.post(f"/assistente/{item.id}/terminal").status_code == 404  # sem sessão ainda


FOLDER = "fulano"
VIDEO_REL = f"{FOLDER}/1.mp4"


def test_delete_clip_from_folder(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    clip = add_clip_files(tmp_path, store)
    assert "Excluir corte" in client.get(f"/arquivos?p={FOLDER}").text

    resp = client.post(f"/arquivos/excluir?p={VIDEO_REL}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/arquivos?p={FOLDER}"
    assert not clip.video_path.exists() and not clip.description_path.exists()
    assert clip.video_path.parent.is_dir()  # a pasta do criador continua
    assert store.get_clip_row(clip.id) is not None  # só os arquivos: o banco não muda
    assert "Pasta vazia" in client.get(f"/arquivos?p={FOLDER}").text
    assert client.post(f"/arquivos/excluir?p={VIDEO_REL}").status_code == 404  # já apagado


def simulate_lock(
    monkeypatch: pytest.MonkeyPatch, store: SqliteStatusStore, *, free_when_paused: bool
) -> list[tuple[bool, bool]]:
    """Simula o Windows com o vídeo "em uso": solta só com o bot pausado (ou nunca)."""
    import cortes.panel.app as panel_app

    monkeypatch.setattr(panel_app, "_DELETE_WAIT_S", 0.05)
    monkeypatch.setattr(panel_app, "_DELETE_WAIT_PAUSED_S", 0.2)
    monkeypatch.setattr(panel_app, "_DELETE_RETRY_S", 0.01)
    real = panel_app.delete_clip_files
    seen: list[tuple[bool, bool]] = []  # (paused_all, worker_paused) a cada tentativa

    def fake(root: Path, rel: str) -> Path:
        controls = store.get_controls()
        seen.append((controls.paused_all, controls.worker_paused))
        if free_when_paused and controls.paused_all and controls.worker_paused:
            return real(root, rel)
        raise PermissionError("arquivo em uso")

    monkeypatch.setattr(panel_app, "delete_clip_files", fake)
    return seen


def test_delete_locked_clip_pauses_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = make(tmp_path)
    clip = add_clip_files(tmp_path, store)
    seen = simulate_lock(monkeypatch, store, free_when_paused=True)
    resp = client.post(f"/arquivos/excluir?p={VIDEO_REL}", follow_redirects=False)
    assert resp.status_code == 303 and not clip.video_path.exists()
    assert seen[0] == (False, False) and seen[-1] == (True, True)  # pausou para apagar
    controls = store.get_controls()
    assert not controls.paused_all and not controls.worker_paused  # e voltou tudo


def test_delete_locked_clip_keeps_pauses_you_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = make(tmp_path)
    clip = add_clip_files(tmp_path, store)
    store.set_worker_paused(True)  # pausado por você antes
    simulate_lock(monkeypatch, store, free_when_paused=False)  # nunca solta
    resp = client.post(f"/arquivos/excluir?p={VIDEO_REL}")
    assert clip.video_path.exists()
    assert resp.status_code == 409 and "aberto em outro programa" in resp.json()["detail"]
    controls = store.get_controls()
    assert not controls.paused_all  # a pausa do painel foi desfeita
    assert controls.worker_paused  # e a sua continua


def test_delete_clip_refuses_bad_paths(tmp_path: Path) -> None:
    client, store = make(tmp_path)
    clip = add_clip_files(tmp_path, store)
    for bad in ("../cortes.db", f"{FOLDER}/_LEIA-ME.txt", FOLDER):
        assert client.post(f"/arquivos/excluir?p={bad}").status_code == 404
    assert clip.video_path.exists()
    other = client.post(
        f"/arquivos/excluir?p={VIDEO_REL}", headers={"origin": "http://outro-site.com"}
    )
    assert other.status_code == 403 and clip.video_path.exists()


def test_page_shows_the_pause_right_away(tmp_path: Path) -> None:
    # O monitor saiu pela pausa há 3 s: o batimento dele ainda diz "Gravando", o supervisor não.
    client, store = make(tmp_path)
    now = time.time()
    channel = {"key": "twitch-fulano", "state": "recording", "session": {"started_at": now - 60}}
    store.write_heartbeat("monitor", 1, now - 600, {"channels": [channel]})
    store.write_heartbeat("worker", 2, now - 600, {})
    store.set_system_paused(True)
    store.write_heartbeat(
        "supervisor",
        3,
        now - 3600,
        {
            "processes": {
                "monitor": {"running": False, "paused": True},
                "worker": {"running": False, "paused": True},
            },
            "system_paused": True,
        },
    )
    text = client.get("/").text
    assert "Sistema pausado" in text
    assert "Gravando" not in text
    # O estado de cada processo fica na aba "PC e processos".
    assert "pausado (sistema)" in client.get("/sistema").text
