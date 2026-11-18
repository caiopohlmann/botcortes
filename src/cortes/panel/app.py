"""Painel web (FastAPI + templates Jinja2, sem dependências de front-end).

Lê o banco e as pastas de cortes. Escreve só:
  - as pausas (tabela `controls`), que o monitor e o worker consultam a cada poucos segundos;
  - os pedidos do atalho "Pedir ao Claude" (tabela `assistant_requests`), que o próprio
    painel executa em segundo plano, um por vez, com o Claude Code deste PC.

Segurança: por padrão só abre neste PC (127.0.0.1). Para abrir na rede (celular), defina
`panel.host: 0.0.0.0` e PANEL_PASSWORD no .env (login do navegador, usuário qualquer).
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets as pysecrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cortes.adapters.storage.sqlite_status import AssistantRequest, SqliteStatusStore
from cortes.application.assistant import ClipContext, assistant_loop
from cortes.application.edit_kit import RENDER_FILE, SOURCE_FILE, TRANSCRIPT_FILE
from cortes.config import Secrets, Settings
from cortes.domain.caption_quality import CaptionGrade
from cortes.panel.files import BadPathError, list_folder, resolve
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


def build_context(settings: Settings, store: SqliteStatusStore, now: float) -> dict[str, Any]:
    stale = settings.panel.stale_after_s
    beats = store.heartbeats()
    controls = store.get_controls()
    monitor = live_data(beats, "monitor", now, stale)
    worker = live_data(beats, "worker", now, stale)
    supervisor = live_data(beats, "supervisor", now, stale)
    processes = process_views(beats, now, stale)
    host: dict[str, Any] = monitor.get("host") or {}
    gpu: dict[str, Any] | None = worker.get("gpu")

    day = start_of_day(now)
    clips_today = store.clips_by_channel(day)
    moments_today = store.moments_by_channel(day)
    channels = channel_rows(settings.channels, monitor, controls, clips_today, moments_today, now)
    verdict = health_verdict(
        processes,
        host,
        gpu,
        monitor.get("channels") or [],
        min_free_gb=settings.paths.min_free_gb,
        now=now,
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
    channel_keys = {c.key for c in settings.channels}
    data_root = settings.paths.data_dir.resolve()
    clips_root = settings.paths.data_dir / "clips"
    grade_by_folder = {
        name: grade.value for grade, name in settings.publish.quality_folders.items()
    }
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
                    claim=store.claim_assistant_request,
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
        context = build_context(settings, store, time.time())
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/partials/{name}")
    def partial(request: Request, name: str) -> Any:
        if name not in _PARTIALS:
            raise HTTPException(status_code=404)
        context = build_context(settings, store, time.time())
        return templates.TemplateResponse(request, f"_{name}.html", context)

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        beats = store.heartbeats()
        controls = store.get_controls()
        return JSONResponse(
            {
                "now": time.time(),
                "controls": {
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
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    # ------------------------------------------------------------------ pausas
    def done() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

    @app.post("/control/all/{action}", dependencies=[Depends(same_origin)])
    def control_all(action: str) -> RedirectResponse:
        store.set_paused_all(_paused(action))
        return done()

    @app.post("/control/worker/{action}", dependencies=[Depends(same_origin)])
    def control_worker(action: str) -> RedirectResponse:
        store.set_worker_paused(_paused(action))
        return done()

    @app.post("/control/channel/{key}/{action}", dependencies=[Depends(same_origin)])
    def control_channel(key: str, action: str) -> RedirectResponse:
        if key not in channel_keys:
            raise HTTPException(status_code=404, detail="canal não configurado")
        store.set_channel_paused(key, _paused(action))
        return done()

    # ------------------------------------------------------------------ pastas de cortes
    @app.get("/arquivos")
    def browse(request: Request, p: str = "") -> Any:
        clips_root.mkdir(parents=True, exist_ok=True)
        try:
            listing = list_folder(clips_root, p, grade_by_folder)
        except BadPathError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        now = time.time()
        rows = store.clips_by_ids([f.clip_id for f in listing.files])
        requests_by_clip: dict[str, list[dict[str, Any]]] = {}
        for file in listing.files:
            requests_by_clip[file.clip_id] = [
                assistant_view(r, now)
                for r in store.assistant_requests(clip_id=file.clip_id, limit=3)
            ]
        context = build_context(settings, store, now)
        context.update(
            {
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
        raise SystemExit(
            f"panel.host={host} abre o painel na rede: defina PANEL_PASSWORD no .env"
        )


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
