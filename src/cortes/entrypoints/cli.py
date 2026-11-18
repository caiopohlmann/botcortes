"""Linha de comando: cortes run | monitor | worker | panel | check | render-file."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import structlog

from cortes.config import Secrets, Settings, load_secrets, load_settings
from cortes.domain.models import Platform
from cortes.entrypoints.process_guard import kill_children_on_exit
from cortes.observability import configure_logging

log = structlog.get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortes", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="sobe monitor + worker + painel e os reinicia se caírem")
    sub.add_parser("monitor", help="só o monitor (lives, chat, buffer, detecção)")
    sub.add_parser("worker", help="só o worker (transcrição + render na GPU)")
    sub.add_parser("panel", help="só o painel web (http://127.0.0.1:8080)")
    sub.add_parser("check", help="verifica FFmpeg, NVENC, CUDA, streamlink e credenciais")

    rr = sub.add_parser(
        "rerender", help="gera um corte de novo a partir do kit de edição (legenda corrigida)"
    )
    rr.add_argument("clip_id", help="id do corte (nome do arquivo sem _notaNN.mp4)")

    rf = sub.add_parser("render-file", help="gera um clipe de um vídeo local (teste sem Twitch)")
    rf.add_argument("video", type=Path)
    rf.add_argument("--start", type=float, default=0.0, help="início do trecho (s)")
    rf.add_argument("--duration", type=float, default=55.0, help="duração do trecho (s)")
    rf.add_argument(
        "--channel",
        default="teste",
        help="canal dos créditos/facecam: login ou plataforma-login (ex.: kick-fulano)",
    )
    rf.add_argument("--event", type=float, default=None, help="lance, em s dentro do trecho")
    rf.add_argument("--chat-score", type=float, default=80.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    secrets = load_secrets()
    settings = load_settings(secrets)
    log_file = settings.paths.logs / f"{args.command}.log"
    configure_logging(settings.logging.level, log_file)
    guard_error = kill_children_on_exit()
    if guard_error:
        log.warning("process_guard_unavailable", error=guard_error)

    try:
        match args.command:
            case "run":
                from cortes.entrypoints.supervisor import run_all

                asyncio.run(run_all(settings))
            case "monitor":
                from cortes.entrypoints.monitor import run_monitor

                asyncio.run(run_monitor(settings, secrets))
            case "worker":
                from cortes.entrypoints.worker import run_worker

                asyncio.run(run_worker(settings))
            case "panel":
                from cortes.panel.app import run_panel

                log.info(
                    "panel_started",
                    url=f"http://{settings.panel.host}:{settings.panel.port}",
                    password=bool(secrets.panel_password),
                )
                asyncio.run(run_panel(settings, secrets))
            case "check":
                return run_check(settings, secrets)
            case "rerender":
                from cortes.entrypoints.worker import rerender_clip

                clip_id = str(args.clip_id).split("_nota")[0].removesuffix(".mp4")
                path = asyncio.run(rerender_clip(settings, clip_id))
                print(f"Corte gerado de novo: {path}")
            case "render-file":
                from cortes.entrypoints.worker import render_file

                output = asyncio.run(
                    render_file(
                        settings,
                        args.video,
                        start_s=args.start,
                        duration_s=args.duration,
                        channel_login=args.channel,
                        event_s=args.event,
                        chat_score=args.chat_score,
                    )
                )
                print(f"Clipe gerado: {output}" if output else "Nenhum clipe gerado.")
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130
    return 0


# ---------------------------------------------------------------------- check
def _run(cmd: Sequence[str], timeout: float = 60) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, repr(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


def _video_encoder(settings: Settings) -> str:
    video_args = settings.render.video_args
    for flag in ("-c:v", "-vcodec", "-codec:v"):
        if flag in video_args:
            index = video_args.index(flag)
            if index + 1 < len(video_args):
                return video_args[index + 1]
    return "libx264"


def run_check(settings: Settings, secrets: Secrets) -> int:
    results: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail.strip().splitlines()[0][:110] if detail.strip() else ""))

    add("perfil", True, f"{settings.profile} | dados em {settings.paths.data_dir}")
    data_dir = settings.paths.data_dir
    probe_dir = data_dir if data_dir.exists() else data_dir.parent
    free_gb = shutil.disk_usage(probe_dir).free / 1024**3
    add(
        "espaço em disco",
        free_gb >= settings.paths.min_free_gb,
        f"{free_gb:.0f} GB livres (mínimo configurado: {settings.paths.min_free_gb:g} GB)",
    )
    keys = ", ".join(c.key for c in settings.channels)
    add("canais", bool(settings.channels), keys or "nenhum")

    ffmpeg = shutil.which(settings.recording.ffmpeg_path)
    add("ffmpeg no PATH", ffmpeg is not None, ffmpeg or "instale o FFmpeg (build full)")
    if ffmpeg:
        ok, out = _run([ffmpeg, "-hide_banner", "-version"])
        add("ffmpeg versão", ok, out)
        _, filters = _run([ffmpeg, "-hide_banner", "-filters"])
        add("filtro ass (libass)", " ass " in filters, "necessário para legendas")
        encoder = _video_encoder(settings)
        ok, out = _run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=1",
                "-c:v", encoder, "-f", "null", "-",
            ]
        )  # fmt: skip
        add(f"encoder {encoder}", ok, "funcionando" if ok else out)
        if settings.render.hwaccel:
            _, hw = _run([ffmpeg, "-hide_banner", "-hwaccels"])
            add(f"hwaccel {settings.render.hwaccel}", settings.render.hwaccel in hw.split(), "")

    ok, out = _run([*settings.recording.streamlink_cmd(), "--version"])
    add("streamlink", ok, out)

    try:
        import fastapi
        import psutil
        import uvicorn

        versions = f"fastapi {fastapi.__version__} | uvicorn {uvicorn.__version__}"
        add("painel (fastapi/uvicorn/psutil)", True, f"{versions} | psutil {psutil.__version__}")
    except Exception as exc:
        add("painel (fastapi/uvicorn/psutil)", False, repr(exc))
    if settings.assistant.enabled:
        claude = shutil.which(settings.assistant.claude_path)
        if claude:
            ok, out = _run([claude, "--version"], timeout=30)
            add("Claude Code (atalho do painel)", ok, out)
        else:
            add(
                "Claude Code (atalho do painel)",
                False,
                "não encontrado: instale e faça login (`claude`), ou assistant.enabled: false",
            )
    if settings.panel.host not in ("127.0.0.1", "localhost", "::1"):
        add(
            "senha do painel",
            bool(secrets.panel_password),
            "ok" if secrets.panel_password else "painel aberto na rede: defina PANEL_PASSWORD",
        )

    smi = shutil.which("nvidia-smi")
    if smi:
        query = "--query-gpu=name,driver_version,memory.total"
        ok, out = _run([smi, query, "--format=csv,noheader"])
        add("GPU NVIDIA", ok, out)
    else:
        add("GPU NVIDIA", settings.transcription.device != "cuda", "nvidia-smi não encontrado")

    try:
        from cortes.adapters.transcription.whisper import register_cuda_dlls

        if settings.transcription.device == "cuda":
            register_cuda_dlls()
        import ctranslate2

        cuda_devices = int(ctranslate2.get_cuda_device_count())
        need_cuda = settings.transcription.device == "cuda"
        add(
            "faster-whisper / CUDA",
            cuda_devices > 0 or not need_cuda,
            f"{cuda_devices} GPU(s) CUDA | modelo {settings.transcription.model} "
            f"({settings.transcription.device}/{settings.transcription.compute_type})",
        )
    except Exception as exc:
        add("faster-whisper / CUDA", False, repr(exc))

    platforms = {c.platform for c in settings.channels}
    if Platform.TWITCH in platforms:
        if secrets.twitch_client_id and secrets.twitch_client_secret:
            add("Twitch (status)", *_check_platform(settings, secrets, Platform.TWITCH))
        else:
            add("Twitch (status)", False, "defina TWITCH_CLIENT_ID/SECRET no .env")
    for platform in (Platform.KICK, Platform.YOUTUBE):
        if platform in platforms:
            add(f"{platform.label} (status)", *_check_platform(settings, secrets, platform))

    width = max(len(name) for name, _, _ in results)
    for name, ok, detail in results:
        print(f"  {'OK ' if ok else 'ERRO'}  {name.ljust(width)}  {detail}")
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - failed}/{len(results)} verificações OK")
    return 1 if failed else 0


def _check_platform(
    settings: Settings, secrets: Secrets, platform: Platform
) -> tuple[bool, str]:
    """Consulta o status real dos canais da plataforma (mesmo caminho do monitor)."""
    from cortes.entrypoints.monitor import build_status_providers

    channels = [c for c in settings.channels if c.platform is platform]
    only = settings.model_copy(update={"channels": channels})

    async def probe() -> tuple[bool, str]:
        providers, closers = build_status_providers(only, secrets)
        try:
            status = await providers[platform].get_live_streams(channels)
        finally:
            for close in closers:
                with contextlib.suppress(Exception):
                    await close()
        unknown = [c.key for c in channels if c.key not in status]
        live = [key for key, stream in status.items() if stream is not None]
        detail = f"ao vivo agora: {', '.join(live) or 'nenhum'}"
        if unknown:
            detail += f" | sem resposta: {', '.join(unknown)}"
        return not unknown, detail

    try:
        return asyncio.run(probe())
    except Exception as exc:
        return False, repr(exc)
