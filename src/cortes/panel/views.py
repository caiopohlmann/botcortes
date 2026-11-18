"""Monta o que o painel mostra a partir dos dados do banco (funções puras, sem web).

Entradas: batimentos dos processos (JSON), pausas, contagens e métricas do SQLite.
Saídas: dicionários simples para os templates. Testado em tests/test_panel_views.py.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from cortes.adapters.storage.sqlite_status import AssistantRequest, ClipRow, Heartbeat
from cortes.domain.models import Channel, Controls

STATE_LABELS: dict[str, tuple[str, str]] = {
    "recording": ("Gravando", "ok"),
    "starting": ("Iniciando", "info"),
    "paused": ("Pausado", "muted"),
    "offline": ("Offline", "muted"),
    "filtered": ("Outro jogo", "muted"),
    "retrying": ("Erro, tentando de novo", "bad"),
    "unknown": ("Sem resposta", "warn"),
}

# Qualidade da legenda: (rótulo, tom)
GRADE_LABELS: dict[str, tuple[str, str]] = {
    "boa": ("Legenda boa", "ok"),
    "revisar": ("Revisar legenda", "warn"),
    "ruim": ("Legenda ruim", "bad"),
}

ASSISTANT_LABELS: dict[str, tuple[str, str]] = {
    "queued": ("Na fila", "info"),
    "running": ("Claude trabalhando…", "info"),
    "done": ("Pronto", "ok"),
    "failed": ("Não deu certo", "bad"),
}

PROCESS_LABELS = {
    "supervisor": "Supervisor",
    "monitor": "Monitor (lives e gravação)",
    "worker": "Worker (cortes na GPU)",
    "panel": "Painel",
}

_TONE_ORDER = {"ok": 0, "info": 1, "warn": 2, "bad": 3}


# ---------------------------------------------------------------------- formatação
def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or math.isnan(seconds):
        return "—"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}min"
    if minutes:
        return f"{minutes}min"
    return f"{secs}s"


def fmt_ago(ts: float | None, now: float) -> str:
    if ts is None:
        return "—"
    delta = now - ts
    if delta < 5:
        return "agora"
    return f"há {fmt_duration(delta)}"


def fmt_num(value: Any, digits: int = 1, unit: str = "") -> str:
    if not isinstance(value, int | float) or isinstance(value, bool) or math.isnan(value):
        return "—"
    text = f"{value:.{digits}f}".replace(".", ",")
    return f"{text}{unit}"


def start_of_day(now: float) -> float:
    """Meia-noite de hoje no fuso do PC."""
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def worst_tone(tones: Sequence[str]) -> str:
    return max(tones, key=lambda tone: _TONE_ORDER.get(tone, 0), default="ok")


# ---------------------------------------------------------------------- gráficos
@dataclass(frozen=True, slots=True)
class Sparkline:
    points: str  # atributo "points" de um <polyline> SVG
    last: float | None
    peak: float | None
    width: int
    height: int

    @property
    def empty(self) -> bool:
        return not self.points


def sparkline(
    values: Sequence[float | None],
    *,
    width: int = 240,
    height: int = 40,
    vmax: float | None = None,
) -> Sparkline:
    """Linha SVG simples (sem JS). Valores None viram buracos ignorados."""
    numbers = [v for v in values if isinstance(v, int | float)]
    if len(numbers) < 2:
        last = numbers[-1] if numbers else None
        return Sparkline("", last, last, width, height)
    top = vmax if vmax is not None else max(numbers)
    top = top if top > 0 else 1.0
    step = width / (len(values) - 1)
    pad = 2
    points = []
    for index, value in enumerate(values):
        if not isinstance(value, int | float):
            continue
        y = height - pad - (min(value, top) / top) * (height - 2 * pad)
        points.append(f"{index * step:.1f},{y:.1f}")
    return Sparkline(" ".join(points), numbers[-1], max(numbers), width, height)


def bucket_counts(times: Sequence[float], start: float, end: float, buckets: int) -> list[int]:
    counts = [0] * buckets
    if end <= start or buckets <= 0:
        return counts
    size = (end - start) / buckets
    for ts in times:
        if start <= ts < end:
            counts[min(int((ts - start) / size), buckets - 1)] += 1
    return counts


def series(samples: Sequence[tuple[float, Mapping[str, Any]]], key: str) -> list[float | None]:
    out: list[float | None] = []
    for _, data in samples:
        value = data.get(key)
        out.append(float(value) if isinstance(value, int | float) else None)
    return out


# ---------------------------------------------------------------------- processos
@dataclass(frozen=True, slots=True)
class ProcessView:
    name: str
    label: str
    tone: str  # ok | warn | bad | muted
    status: str
    uptime: str
    restarts_1h: int | None
    warnings_1h: int
    errors_1h: int
    rss_mb: float | None


def _log_counts(beat: Heartbeat | None) -> tuple[int, int]:
    data = beat.data.get("log") if beat else None
    if not isinstance(data, dict):
        return 0, 0
    return int(data.get("warnings_1h") or 0), int(data.get("errors_1h") or 0)


def process_views(
    beats: Mapping[str, Heartbeat], now: float, stale_after_s: float
) -> list[ProcessView]:
    supervisor = beats.get("supervisor")
    supervised = {}
    if supervisor and now - supervisor.updated_at <= stale_after_s:
        procs = supervisor.data.get("processes")
        supervised = procs if isinstance(procs, dict) else {}
    views: list[ProcessView] = []
    for name in ("monitor", "worker"):
        beat = beats.get(name)
        info = supervised.get(name) if isinstance(supervised.get(name), dict) else None
        restarts = int(info.get("restarts_1h") or 0) if info else None
        warnings, errors = _log_counts(beat)
        rss: float | None = None
        if beat is None:
            tone, status, uptime = "bad", "nunca iniciou", "—"
        elif now - beat.updated_at > stale_after_s:
            tone, status = "bad", f"parado (último sinal {fmt_ago(beat.updated_at, now)})"
            uptime = "—"
        else:
            tone, status = "ok", "rodando"
            uptime = fmt_duration(now - beat.started_at)
            # monitor: RAM dele + streamlink/FFmpeg filhos; worker: RAM dele.
            source = beat.data.get("host") if name == "monitor" else beat.data
            if isinstance(source, dict):
                own = source.get("process_rss_mb")
                children = source.get("children_rss_mb") or 0
                if isinstance(own, int | float) and isinstance(children, int | float):
                    rss = float(own) + float(children)
            if name == "worker" and beat.data.get("paused"):
                tone, status = "muted", "pausado"
        views.append(
            ProcessView(
                name=name,
                label=PROCESS_LABELS[name],
                tone=tone,
                status=status,
                uptime=uptime,
                restarts_1h=restarts,
                warnings_1h=warnings,
                errors_1h=errors,
                rss_mb=rss,
            )
        )
    return views


def live_data(
    beats: Mapping[str, Heartbeat], name: str, now: float, stale_s: float
) -> dict[str, Any]:
    """Dados do batimento, ou {} se o processo estiver parado (não mostrar valor velho)."""
    beat = beats.get(name)
    if beat is None or now - beat.updated_at > stale_s:
        return {}
    return beat.data


# ---------------------------------------------------------------------- saúde geral
@dataclass(frozen=True, slots=True)
class Verdict:
    tone: str  # ok | warn | bad
    title: str
    reasons: list[tuple[str, str]] = field(default_factory=list)  # (tom, texto)


def health_verdict(
    processes: Sequence[ProcessView],
    host: Mapping[str, Any],
    gpu: Mapping[str, Any] | None,
    channels: Sequence[Mapping[str, Any]],
    *,
    min_free_gb: float,
    now: float,
) -> Verdict:
    reasons: list[tuple[str, str]] = []
    for proc in processes:
        if proc.tone == "bad":
            reasons.append(("bad", f"{proc.label}: {proc.status}"))
        if proc.restarts_1h:
            reasons.append(("warn", f"{proc.label} reiniciou {proc.restarts_1h}x na última hora"))
        if proc.errors_1h:
            reasons.append(("warn", f"{proc.label}: {proc.errors_1h} erro(s) na última hora"))

    disk_free = host.get("disk_free_gb")
    if isinstance(disk_free, int | float):
        if disk_free < min_free_gb:
            reasons.append(("bad", f"Disco cheio: {disk_free:.0f} GB livres (cortes parados)"))
        elif disk_free < 2 * min_free_gb:
            reasons.append(("warn", f"Disco enchendo: {disk_free:.0f} GB livres"))
    ram = host.get("ram_pct")
    if isinstance(ram, int | float) and ram >= 90:
        reasons.append(("warn", f"RAM em {ram:.0f}%"))
    temp = gpu.get("temp_c") if gpu else None
    if isinstance(temp, int | float) and temp >= 85:
        reasons.append(("warn", f"GPU quente: {temp:.0f} °C"))

    for channel in channels:
        name = channel.get("name") or channel.get("key")
        if channel.get("state") == "retrying":
            reasons.append(("warn", f"{name}: sessão caiu, tentando de novo"))
        session = channel.get("session")
        if channel.get("state") == "recording" and isinstance(session, dict):
            last = session.get("last_segment_at")
            started = session.get("started_at") or now
            if (last is None and now - started > 90) or (
                isinstance(last, int | float) and now - last > 90
            ):
                reasons.append(("warn", f"{name}: gravação sem vídeo novo (anúncio ou queda)"))

    tone = worst_tone([t for t, _ in reasons])
    title = {"ok": "Tudo estável", "warn": "Atenção", "bad": "Problema"}.get(tone, "Atenção")
    return Verdict(tone=tone, title=title, reasons=reasons)


# ---------------------------------------------------------------------- canais
def channel_rows(
    configured: Sequence[Channel],
    monitor: Mapping[str, Any],
    controls: Controls,
    clips_today: Mapping[str, int],
    moments_today: Mapping[str, int],
    now: float,
) -> list[dict[str, Any]]:
    """Uma linha por canal configurado. Sem monitor rodando, o estado fica desconhecido."""
    reported = {
        item.get("key"): item
        for item in monitor.get("channels") or []
        if isinstance(item, dict)
    }
    rows: list[dict[str, Any]] = []
    for channel in configured:
        item = reported.get(channel.key) or {}
        state = str(item.get("state") or "unknown")
        if not monitor:
            state = "unknown"
        label, tone = STATE_LABELS.get(state, (state, "muted"))
        stream = item.get("stream") if isinstance(item.get("stream"), dict) else None
        session = item.get("session") if isinstance(item.get("session"), dict) else None
        if state == "paused" and stream:
            label = "Pausado (ao vivo)"
        rows.append(
            {
                "key": channel.key,
                "name": channel.name,
                "platform": channel.platform.label,
                "platform_id": channel.platform.value,
                "url": channel.url,
                "extra_chats": [
                    f"{ref.platform.label}: {ref.login}" for ref in channel.extra_chats
                ],
                "state": state,
                "state_label": label,
                "tone": tone,
                "paused": controls.channel_paused(channel.key),
                "paused_individually": channel.key in controls.paused_channels,
                "title": stream.get("title") if stream else None,
                "game": stream.get("game") if stream else None,
                "viewers": stream.get("viewers") if stream else None,
                "live_for": fmt_duration(now - stream["started_at"])
                if stream and isinstance(stream.get("started_at"), int | float)
                else None,
                "chat_rate": fmt_num(session.get("chat_rate"), 1) if session else "—",
                "chat_baseline": fmt_num(session.get("chat_baseline"), 1) if session else "—",
                "chat_score": fmt_num(session.get("chat_score"), 0) if session else "—",
                "chat_warm": bool(session.get("chat_warm")) if session else False,
                "last_message": fmt_ago(session.get("last_message_at"), now)
                if session
                else "—",
                "last_segment": fmt_ago(session.get("last_segment_at"), now)
                if session
                else "—",
                "buffered": fmt_duration(session.get("buffered_s")) if session else "—",
                "recorder_restarts": session.get("recorder_restarts") if session else None,
                "recording_for": fmt_duration(now - session["started_at"])
                if session and isinstance(session.get("started_at"), int | float)
                else None,
                "moments_today": int(moments_today.get(channel.key, 0)),
                "clips_today": int(clips_today.get(channel.key, 0)),
                "checked": fmt_ago(item.get("checked_at"), now),
                "error": item.get("error"),
            }
        )
    return rows


# ---------------------------------------------------------------------- cortes
def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clip_view(clip: ClipRow, now: float) -> dict[str, Any]:
    grade_label, grade_tone = GRADE_LABELS.get(clip.caption_grade, (clip.caption_grade, "muted"))
    return {
        "id": clip.id,
        "grade": clip.caption_grade,
        "grade_label": grade_label,
        "grade_tone": grade_tone,
        "caption_score": round(clip.caption_score),
        "notes": list(clip.caption_notes),
        "doubts": list(clip.doubtful_words),
        # data/clips/<pending>/<criador>/<qualidade>/arquivo.mp4 -> "pending/criador/qualidade"
        "folder_rel": "/".join(clip.video_path.parts[-4:-1]),
        "channel": clip.display_name or clip.channel,
        "channel_key": clip.channel,
        "score": round(clip.score),
        "chat": round(clip.chat_score),
        "audio": round(clip.audio_score * 100),
        "duration": fmt_duration(clip.duration_s),
        "processing": fmt_num(clip.processing_s, 0, " s"),
        "ago": fmt_ago(clip.created_at, now),
        "created": time.strftime("%d/%m %H:%M", time.localtime(clip.created_at)),
        "transcript": _shorten(clip.transcript, 140),
        "file": f"{clip.video_path.parent.name}/{clip.video_path.name}",
    }


def assistant_view(item: AssistantRequest, now: float) -> dict[str, Any]:
    label, tone = ASSISTANT_LABELS.get(item.status, (item.status, "muted"))
    took = None
    if item.started_at and item.finished_at:
        took = fmt_duration(item.finished_at - item.started_at)
    return {
        "id": item.id,
        "clip_id": item.clip_id,
        "message": item.message,
        "status": item.status,
        "label": label,
        "tone": tone,
        "ago": fmt_ago(item.created_at, now),
        "took": took,
        "reply": item.reply or "",
        "error": item.error or "",
        "session_id": item.session_id,
        "active": item.status in ("queued", "running"),
    }
