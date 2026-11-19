"""Painel web (FastAPI + templates Jinja2, sem dependências de front-end).

Lê o banco e as pastas de cortes. Escreve só:
  - as pausas (tabela `controls`), que o monitor e o worker consultam a cada poucos segundos;
  - o pedido "Reiniciar sistema" (também em `controls`, ver entrypoints/restart.py): o
    supervisor para monitor, worker e o próprio painel e sobe os três de novo, com o código e
    a configuração atuais (força quem não sair sozinho no prazo);
  - os pedidos do atalho "Pedir ao Claude" (tabela `assistant_requests`), que o próprio
    painel executa em segundo plano, um por vez, com o Claude Code deste PC.

Na página de pastas, o botão "Excluir corte" apaga o .mp4 e o .txt do disco (só os arquivos).
Se o arquivo estiver em uso, pausa o bot por alguns segundos e volta como estava.

Segurança: por padrão só abre neste PC (127.0.0.1). Para abrir na rede (celular), defina
`panel.host: 0.0.0.0` e PANEL_PASSWORD no .env (login do navegador, usuário qualquer).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import secrets as pysecrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cortes.adapters.channel_check import check_channel
from cortes.adapters.storage.sqlite_status import AssistantRequest, SqliteStatusStore
from cortes.application.assistant import ClipContext, assistant_loop
from cortes.application.edit_kit import RENDER_FILE, SOURCE_FILE, TRANSCRIPT_FILE
from cortes.channel_config import ChannelList, append_channel
from cortes.config import Secrets, Settings
from cortes.domain.caption_quality import CaptionGrade
from cortes.domain.models import QUALITY_PRESETS, Channel, channel_key, parse_channel_link
from cortes.entrypoints.restart import SYSTEM
from cortes.panel.files import BadPathError, FileItem, delete_clip_files, list_folder, resolve
from cortes.panel.live_check import LiveCheck, check_live
from cortes.panel.views import (
    GRADE_LABELS,
    assistant_view,
    bucket_counts,
    channel_rows,
    clip_view,
    fmt_ago,
    fmt_duration,
    fmt_num,
    health_verdict,
    live_data,
    process_views,
    series,
    sparkline,
    start_of_day,
)

log = structlog.get_logger(__name__)

_HERE = Path(__file__).resolve().parent
_PARTIALS = ("overview", "channels", "clips", "assistant", "system")
_basic = HTTPBasic(auto_error=False)
# Excluir corte: quanto tempo insistir com o arquivo em uso (antes e depois de pausar o bot).
_DELETE_WAIT_S = 3.0
_DELETE_WAIT_PAUSED_S = 15.0
_DELETE_RETRY_S = 0.5
# O monitor confere channels.yaml a cada poucos segundos: antes disso, não é "pendente".
_CHANNELS_RELOAD_GRACE_S = 15.0
# "Verificar ao vivo agora": cliques seguidos reaproveitam a última consulta.
_LIVE_CHECK_COOLDOWN_S = 15.0
# Idioma do canal novo: "" = o Whisper detecta sozinho.
_LANGUAGES = ("pt", "en", "")


@dataclass
class _PanelState:
    """Estado em memória do painel (some ao reiniciar o painel)."""

    live_check: LiveCheck | None = None
    check_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def build_context(
    settings: Settings,
    store: SqliteStatusStore,
    now: float,
    channel_list: ChannelList | None = None,
    live_check: LiveCheck | None = None,
) -> dict[str, Any]:
    stale = settings.panel.stale_after_s
    configured = channel_list.current() if channel_list else tuple(settings.channels)
    beats = store.heartbeats()
    controls = store.get_controls()
    monitor = live_data(beats, "monitor", now, stale)
    worker = live_data(beats, "worker", now, stale)
    supervisor = live_data(beats, "supervisor", now, stale)
    processes = process_views(beats, now, stale, system_paused=controls.system_paused)
    host: dict[str, Any] = monitor.get("host") or {}
    gpu: dict[str, Any] | None = worker.get("gpu")

    day = start_of_day(now)
    clips_today = store.clips_by_channel(day)
    moments_today = store.moments_by_channel(day)
    channels = channel_rows(
        configured,
        monitor,
        controls,
        clips_today,
        moments_today,
        now,
        monitor_state=next((p.state for p in processes if p.name == "monitor"), "down"),
        default_quality=settings.recording.quality,
        checks=live_check.as_rows() if live_check else None,
    )
    # channels.yaml mudou e o monitor ainda não pegou a versão nova. O monitor relê sozinho
    # em poucos segundos; o aviso só aparece se ele não conseguir (arquivo com erro) ou se for
    # um monitor antigo, que só relê ao reiniciar.
    monitor_beat = beats.get("monitor")
    changed_at = channel_list.changed_at if channel_list else None
    if not (monitor and monitor_beat and changed_at):
        config_pending = False
    elif "channels_loaded_at" in monitor:
        loaded = monitor.get("channels_loaded_at") or 0.0
        config_pending = loaded < changed_at and now - changed_at > _CHANNELS_RELOAD_GRACE_S
    else:
        config_pending = monitor_beat.started_at < changed_at
    channels_error = (channel_list.error if channel_list else None) or monitor.get("channels_error")
    verdict = health_verdict(
        processes,
        host,
        gpu,
        monitor.get("channels") or [],
        min_free_gb=settings.paths.min_free_gb,
        now=now,
        system_paused=controls.system_paused,
    )

    since = now - 86400
    host_samples = store.metrics_since("host", since)
    gpu_samples = store.metrics_since("gpu", since)
    charts = {
        "cpu": sparkline(series(host_samples, "cpu_pct"), vmax=100),
        "ram": sparkline(series(host_samples, "ram_pct"), vmax=100),
        "net": sparkline(series(host_samples, "net_rx_mbps")),
        "gpu": sparkline(series(gpu_samples, "util_pct"), vmax=100),
        "temp": sparkline(series(gpu_samples, "temp_c"), vmax=100),
        "clips": sparkline(
            [float(n) for n in bucket_counts(store.clip_times(since), since, now, 24)]
        ),
    }

    moments = store.moment_counts(day)
    jobs = store.job_counts()
    captions_today = store.caption_counts(day)
    recording = sum(1 for row in channels if row["state"] == "recording")
    panel_info = (supervisor.get("processes") or {}).get("panel") or {}
    boot = host.get("boot_time")
    recent = [clip_view(clip, now) for clip in store.recent_clips(30)]

    return {
        "now": now,
        "updated": time.strftime("%H:%M:%S", time.localtime(now)),
        "refresh_ms": int(settings.panel.refresh_s * 1000),
        "verdict": verdict,
        "controls": controls,
        "processes": processes,
        "supervised": bool(supervisor),
        "config_pending": config_pending,
        "channels_error": channels_error,
        "live_check": {"message": live_check.message, "ago": fmt_ago(live_check.checked_at, now)}
        if live_check
        else None,
        "panel_restarts_1h": panel_info.get("restarts_1h"),
        "host": host,
        "gpu": gpu,
        "worker": worker,
        "charts": charts,
        "channels": channels,
        "recording": recording,
        "counts": {
            "today": sum(clips_today.values()),
            "week": store.count_clips_since(now - 7 * 86400),
            "total": store.count_clips_since(0),
            "rejected_today": moments.get("rejected", 0),
            "failed_today": moments.get("failed", 0),
            "moments_today": sum(moments.values()),
            "queue": jobs.get("pending", 0),
            "running": jobs.get("running", 0),
            "caption_good": captions_today.get(CaptionGrade.GOOD.value, 0),
            "caption_review": captions_today.get(CaptionGrade.REVIEW.value, 0),
            "caption_bad": captions_today.get(CaptionGrade.BAD.value, 0),
        },
        "recent": recent[:12],
        "recent_all": recent,
        "assistant_enabled": settings.assistant.enabled,
        "assistant": [assistant_view(r, now) for r in store.assistant_requests(limit=8)],
        "grade_labels": GRADE_LABELS,
        "monitor_log": (monitor.get("log") or {}).get("last") or [],
        "worker_log": (worker.get("log") or {}).get("last") or [],
        "pc_uptime": fmt_duration(now - boot) if isinstance(boot, int | float) else "—",
        "min_free_gb": settings.paths.min_free_gb,
        "fmt_num": fmt_num,
        "fmt_ago": fmt_ago,
        "fmt_duration": fmt_duration,
        "q": lambda rel: quote(rel, safe="/"),
    }


def clip_context(settings: Settings, store: SqliteStatusStore, clip_id: str) -> ClipContext | None:
    row = store.get_clip_row(clip_id)
    if row is None:
        return None
    channel = settings.channel(row.channel)
    kit = settings.paths.work / clip_id
    return ClipContext(
        clip_id=clip_id,
        creator=channel.name,
        channel_url=channel.url,
        video_path=row.video_path,
        description_path=row.description_path,
        kit_dir=kit,
        kit_available=all(
            (kit / name).is_file() for name in (SOURCE_FILE, TRANSCRIPT_FILE, RENDER_FILE)
        ),
        caption_grade=row.caption_grade,
        caption_notes=row.caption_notes,
        transcript=row.transcript,
    )


def create_app(settings: Settings, secrets: Secrets, *, run_assistant: bool = True) -> FastAPI:
    store = SqliteStatusStore(settings.paths.database)
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    password = secrets.panel_password
    channel_list = ChannelList(
        settings.channels,
        secrets.cortes_config_dir.resolve() / "channels.yaml",
        name="panel_channels",
    )
    state = _PanelState()
    data_root = settings.paths.data_dir.resolve()
    clips_root = settings.paths.clips
    project_dir = Path.cwd()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if run_assistant and settings.assistant.enabled:
            from cortes.adapters.assistant.claude_code import ClaudeCodeRunner

            interrupted = await asyncio.to_thread(store.fail_interrupted_requests)
            if interrupted:
                log.warning("assistant_requests_interrupted", count=interrupted)
            task = asyncio.create_task(
                assistant_loop(
                    # "Pausar tudo": os pedidos esperam (o que já estava rodando termina).
                    claim=lambda: (
                        None
                        if store.get_controls().system_paused
                        else store.claim_assistant_request()
                    ),
                    finish=store.finish_assistant_request,
                    context_for=lambda clip_id: clip_context(settings, store, clip_id),
                    runner=ClaudeCodeRunner(settings.assistant, project_dir),
                )
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def auth(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
    ) -> None:
        if not password:
            return
        ok = credentials is not None and pysecrets.compare_digest(
            credentials.password.encode(), password.encode()
        )
        if not ok:
            raise HTTPException(
                status_code=401,
                detail="senha do painel",
                headers={"WWW-Authenticate": 'Basic realm="cortesgta6"'},
            )

    def same_origin(request: Request) -> None:
        """Bloqueia POST vindo de outro site (o navegador mandaria a senha junto)."""
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(status_code=403, detail="origem não permitida")

    app = FastAPI(
        title="cortesgta6",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(auth)],
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    # ------------------------------------------------------------------ painel
    @app.get("/")
    def index(request: Request) -> Any:
        context = build_context(settings, store, time.time(), channel_list, state.live_check)
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/sistema")
    def system_page(request: Request) -> Any:
        """Aba "PC e processos": saúde do PC, processos e gráficos de 24 h."""
        context = build_context(settings, store, time.time(), channel_list, state.live_check)
        context["page"] = "sistema"
        return templates.TemplateResponse(request, "system.html", context)

    @app.get("/partials/{name}")
    def partial(request: Request, name: str) -> Any:
        if name not in _PARTIALS:
            raise HTTPException(status_code=404)
        context = build_context(settings, store, time.time(), channel_list, state.live_check)
        return templates.TemplateResponse(request, f"_{name}.html", context)

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        beats = store.heartbeats()
        controls = store.get_controls()
        return JSONResponse(
            {
                "now": time.time(),
                "controls": {
                    "system_paused": controls.system_paused,
                    "paused_all": controls.paused_all,
                    "worker_paused": controls.worker_paused,
                    "paused_channels": sorted(controls.paused_channels),
                },
                "heartbeats": {
                    name: {
                        "pid": beat.pid,
                        "started_at": beat.started_at,
                        "updated_at": beat.updated_at,
                        "data": beat.data,
                    }
                    for name, beat in beats.items()
                },
            }
        )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        # pid: o "Reiniciar sistema" na página espera mudar para saber que o painel voltou.
        return {"ok": True, "pid": os.getpid()}

    # ------------------------------------------------------------------ pausas
    def done() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

    @app.post("/control/system/pause", dependencies=[Depends(same_origin)])
    def pause_system() -> RedirectResponse:
        """Botão "Pausar tudo": o supervisor desliga monitor e worker em ~2 s (painel fica)."""
        supervisor = live_data(
            store.heartbeats(), "supervisor", time.time(), settings.panel.stale_after_s
        )
        if not supervisor:
            # Sem o `cortes run`, ninguém subiria monitor e worker de novo no "Iniciar tudo".
            raise HTTPException(
                status_code=409,
                detail="só funciona com o bot aberto por `cortes run` (o supervisor)",
            )
        if "system_paused" not in supervisor:
            # Supervisor aberto antes desta versão: ficaria subindo de novo quem sai pela pausa.
            raise HTTPException(
                status_code=409,
                detail="feche e abra o `cortes run` uma vez para ativar o “Pausar tudo”",
            )
        store.set_system_paused(True)
        log.info("system_pause_requested_by_panel")
        return done()

    @app.post("/control/system/resume", dependencies=[Depends(same_origin)])
    def resume_system() -> RedirectResponse:
        """Botão "Iniciar tudo": o supervisor sobe monitor e worker (pausas de canal ficam)."""
        store.resume_all()
        log.info("system_resume_requested_by_panel")
        return done()

    @app.post("/control/channel/{key}/{action}", dependencies=[Depends(same_origin)])
    def control_channel(key: str, action: str) -> RedirectResponse:
        if key not in {c.key for c in channel_list.current()}:
            raise HTTPException(status_code=404, detail="canal não configurado")
        store.set_channel_paused(key, _paused(action))
        return done()

    @app.post("/control/channel/{key}/quality/{preset}", dependencies=[Depends(same_origin)])
    def channel_quality(key: str, preset: str) -> RedirectResponse:
        """Qualidade do canal (1080p/720p; "config" volta à do channels.yaml).

        O monitor lê em até ~2 s: se o canal estiver gravando, reinicia só a gravação dele.
        """
        if key not in {c.key for c in channel_list.current()}:
            raise HTTPException(status_code=404, detail="canal não configurado")
        if preset != "config" and preset not in QUALITY_PRESETS:
            raise HTTPException(status_code=404, detail="qualidade desconhecida")
        store.set_channel_quality(key, None if preset == "config" else preset)
        log.info("channel_quality_set_by_panel", channel=key, quality=preset)
        return done()

    @app.post("/control/system/restart", dependencies=[Depends(same_origin)])
    def restart_system() -> RedirectResponse:
        """O supervisor vê o pedido em até ~2 s e reinicia monitor, worker e painel.

        Funciona mesmo com o monitor ou o worker travado (o supervisor força o encerramento).
        """
        now = time.time()
        if not live_data(store.heartbeats(), "supervisor", now, settings.panel.stale_after_s):
            # Sem o `cortes run`, os processos sairiam e ninguém os subiria de novo.
            raise HTTPException(
                status_code=409,
                detail="só funciona com o bot aberto por `cortes run` (o supervisor)",
            )
        store.request_restart(SYSTEM, now)
        log.info("system_restart_requested_by_panel")
        return done()

    # ------------------------------------------------------------------ pastas de cortes
    @app.get("/arquivos")
    def browse(request: Request, p: str = "") -> Any:
        clips_root.mkdir(parents=True, exist_ok=True)
        try:
            listing = list_folder(clips_root, p)
        except BadPathError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        now = time.time()

        # Corte numerado ("3.mp4"): acha no banco pelo caminho; formato antigo, pelo id no nome.
        # O banco guarda o caminho como o worker montou (data_dir + clips/...): procura nas
        # duas formas, a do disco (resolvida) e a da configuração.
        def candidates(file: FileItem) -> list[str]:
            return [str(file.path), str(clips_root.joinpath(*file.rel.split("/")))]

        by_path = store.clips_by_paths([c for f in listing.files for c in candidates(f)])
        by_id = store.clips_by_ids([f.clip_id for f in listing.files])
        files = []
        rows = {}
        for file in listing.files:
            found = [by_path[c] for c in candidates(file) if c in by_path]
            row = found[0] if found else by_id.get(file.clip_id)
            if row is not None:
                file = dataclasses.replace(file, clip_id=row.id)
                rows[row.id] = row
            files.append(file)
        listing = dataclasses.replace(listing, files=files)
        requests_by_clip: dict[str, list[dict[str, Any]]] = {}
        for file in listing.files:
            requests_by_clip[file.clip_id] = [
                assistant_view(r, now)
                for r in store.assistant_requests(clip_id=file.clip_id, limit=3)
            ]
        context = build_context(settings, store, now, channel_list, state.live_check)
        context.update(
            {
                "page": "pastas",
                "listing": listing,
                "clip_rows": {cid: clip_view(row, now) for cid, row in rows.items()},
                "requests_by_clip": requests_by_clip,
                "has_active": any(
                    item["active"] for items in requests_by_clip.values() for item in items
                ),
            }
        )
        return templates.TemplateResponse(request, "files.html", context)

    @app.get("/arquivos/raw")
    def raw_file(p: str) -> FileResponse:
        try:
            path = resolve(clips_root, p)
        except BadPathError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not path.is_file() or path.suffix.lower() not in (".mp4", ".txt"):
            raise HTTPException(status_code=404, detail="arquivo não encontrado")
        media = "video/mp4" if path.suffix.lower() == ".mp4" else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media)

    def delete_with_retry(rel: str, seconds: float) -> Path:
        """No Windows, arquivo aberto (player, render) não apaga: tenta de novo até `seconds`."""
        deadline = time.monotonic() + seconds
        while True:
            try:
                return delete_clip_files(clips_root, rel)
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_DELETE_RETRY_S)

    @app.post("/arquivos/excluir", dependencies=[Depends(same_origin)])
    def delete_clip(p: str) -> RedirectResponse:
        """Apaga o corte da pasta (.mp4 + .txt ao lado). Banco e kit de edição ficam como estão.

        Se o arquivo estiver em uso, pausa a gravação e o worker (só o que estava rodando),
        tenta de novo e volta tudo como estava, mesmo se der erro.
        """
        try:
            try:
                video = delete_with_retry(p, _DELETE_WAIT_S)
            except PermissionError:
                before = store.get_controls()
                log.info("clip_delete_pausing", path=p)
                if not before.paused_all:
                    store.set_paused_all(True)
                if not before.worker_paused:
                    store.set_worker_paused(True)
                try:
                    video = delete_with_retry(p, _DELETE_WAIT_PAUSED_S)
                finally:
                    if not before.paused_all:
                        store.set_paused_all(False)
                    if not before.worker_paused:
                        store.set_worker_paused(False)
                    log.info("clip_delete_resumed", path=p)
        except BadPathError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OSError as exc:
            log.warning("clip_delete_failed", path=p, error=repr(exc))
            raise HTTPException(
                status_code=409,
                detail="não deu para apagar: o vídeo está aberto em outro programa?",
            ) from exc
        log.info("clip_deleted", path=str(video))
        parent = video.parent.relative_to(clips_root.resolve()).as_posix()
        return RedirectResponse(f"/arquivos?p={quote(parent, safe='/')}", status_code=303)

    def clip_file(clip_id: str, kind: str) -> Path:
        clip = store.get_clip_row(clip_id)
        if clip is None:
            raise HTTPException(status_code=404)
        path = (clip.video_path if kind == "video" else clip.description_path).resolve()
        if not path.is_relative_to(data_root) or not path.is_file():
            raise HTTPException(status_code=404, detail="arquivo não encontrado")
        return path

    @app.get("/clips/{clip_id}/video")
    def clip_video(clip_id: str) -> FileResponse:
        path = clip_file(clip_id, "video")
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    @app.get("/clips/{clip_id}/description")
    def clip_description(clip_id: str) -> PlainTextResponse:
        path = clip_file(clip_id, "description")
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ verificar ao vivo
    @app.post("/canais/verificar", dependencies=[Depends(same_origin)])
    async def verify_live() -> JSONResponse:
        """Botão "Verificar ao vivo agora": consulta as plataformas sem ligar nada."""
        async with state.check_lock:
            last = state.live_check
            if last and time.time() - last.checked_at < _LIVE_CHECK_COOLDOWN_S:
                return JSONResponse({"message": last.message})  # clique repetido: o mesmo
            channels = await asyncio.to_thread(channel_list.current)
            state.live_check = await check_live(channels, settings, secrets)
            return JSONResponse({"message": state.live_check.message})

    # ------------------------------------------------------------------ canal novo
    @app.post("/canais", dependencies=[Depends(same_origin)])
    async def add_channel(request: Request) -> JSONResponse:
        """Acrescenta um canal pelo link no channels.yaml.

        Monitor e worker releem o arquivo sozinhos: o canal entra em poucos segundos, sem
        reiniciar nada e sem parar as gravações em andamento.
        """
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="corpo inválido") from exc
        body = body if isinstance(body, dict) else {}
        link = str(body.get("link") or "").strip()
        name = " ".join(str(body.get("display_name") or "").split())[:60]
        language = str(body.get("language") or "").strip()
        if language not in _LANGUAGES:
            raise HTTPException(status_code=400, detail="idioma inválido")
        try:
            platform, login = parse_channel_link(link)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        channel = Channel(login=login, platform=platform)
        current = await asyncio.to_thread(channel_list.current)
        for existing in current:
            if existing.key == channel.key:
                raise HTTPException(
                    status_code=409, detail=f"{existing.name} ({channel.url}) já está na lista"
                )
            if channel.key in {channel_key(r.platform, r.login) for r in existing.extra_chats}:
                raise HTTPException(
                    status_code=409,
                    detail=f"{channel.url} já entra como chat extra de {existing.name}",
                )
        try:
            check = await check_channel(channel, secrets)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        channel = Channel(
            login=login,
            platform=platform,
            display_name=name or check.display_name or "",
            language=language or None,
        )
        note = "" if check.verified else " (não deu para conferir na plataforma agora)"
        comment = f"Adicionado pelo painel em {time.strftime('%d/%m/%Y %H:%M')}{note}."
        try:
            await asyncio.to_thread(append_channel, channel_list.path, channel, comment)
        except (OSError, ValueError) as exc:
            log.warning("channel_add_failed", channel=channel.key, error=repr(exc))
            raise HTTPException(
                status_code=400, detail=f"não consegui salvar no channels.yaml: {exc}"
            ) from exc
        await asyncio.to_thread(channel_list.current)
        message = (
            f"{channel.name} ({channel.url}) adicionado. O bot começa a acompanhar em "
            "alguns segundos; se estiver ao vivo, já começa a gravar."
        )
        if not check.verified:
            message += " A plataforma não respondeu agora, então o canal não foi conferido."
        return JSONResponse({"key": channel.key, "message": message})

    # ------------------------------------------------------------------ atalho do Claude
    @app.post("/assistente", dependencies=[Depends(same_origin)])
    async def ask_claude(request: Request) -> JSONResponse:
        if not settings.assistant.enabled:
            raise HTTPException(status_code=404, detail="atalho do Claude desativado")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="corpo inválido") from exc
        clip_id = str((body or {}).get("clip_id") or "").strip()
        message = str((body or {}).get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="escreva o que o Claude deve fazer")
        if len(message) > settings.assistant.max_message_chars:
            raise HTTPException(status_code=400, detail="pedido longo demais")
        if await asyncio.to_thread(store.get_clip_row, clip_id) is None:
            raise HTTPException(status_code=404, detail="corte não encontrado")
        request_id = await asyncio.to_thread(store.add_assistant_request, clip_id, message)
        log.info("assistant_requested", request=request_id, clip=clip_id)
        return JSONResponse({"id": request_id, "status": "queued"})

    @app.post("/assistente/{request_id}/terminal", dependencies=[Depends(same_origin)])
    def open_terminal(request_id: int) -> JSONResponse:
        item: AssistantRequest | None = store.get_assistant_request(request_id)
        if item is None or not item.session_id:
            raise HTTPException(status_code=404, detail="esse pedido não tem conversa salva")
        from cortes.adapters.assistant.claude_code import ClaudeCodeRunner

        try:
            ClaudeCodeRunner(settings.assistant, project_dir).open_terminal(item.session_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True})

    return app


def _paused(action: str) -> bool:
    if action not in ("pause", "resume"):
        raise HTTPException(status_code=404)
    return action == "pause"


def check_bind(host: str, password: str) -> None:
    """Abrir na rede sem senha deixaria qualquer um da rede pausar o bot."""
    if host not in ("127.0.0.1", "localhost", "::1") and not password:
        raise SystemExit(f"panel.host={host} abre o painel na rede: defina PANEL_PASSWORD no .env")


async def run_panel(settings: Settings, secrets: Secrets) -> None:
    import uvicorn

    check_bind(settings.panel.host, secrets.panel_password)
    app = create_app(settings, secrets)
    config = uvicorn.Config(
        app,
        host=settings.panel.host,
        port=settings.panel.port,
        log_config=None,  # usa o logging do projeto
        access_log=False,
    )
    await uvicorn.Server(config).serve()
