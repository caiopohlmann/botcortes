"""Corte + render vertical 9:16 com FFmpeg em UMA passada.

Layout (1080x1920):
  - fundo: o próprio vídeo reduzido a 1/8, desfocado e ampliado (blur em 135x240 custa
    ~64x menos que em resolução cheia e o resultado visual é o mesmo);
  - gameplay 16:9 no centro (ou abaixo da facecam);
  - facecam opcional no topo (região configurável por streamer);
  - legendas ASS e crédito do canal queimados no mesmo filtergraph.

Performance: decodifica com NVDEC (-hwaccel cuda), filtros leves na CPU e encode NVENC.
Cortar e renderizar no mesmo comando evita gravar/ler um arquivo intermediário.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from cortes.adapters.media.ass import build_ass
from cortes.adapters.media.audio import audio_excitement
from cortes.adapters.media.ffmpeg_exec import run_ffmpeg
from cortes.config import AudioSettings, CaptionStyle, RenderSettings
from cortes.domain.captions import CaptionParams, caption_events
from cortes.domain.models import Region, RenderRequest

_ASS_NAME = "subs.ass"
_TMP_OUTPUT = "render.mp4"


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


@dataclass(frozen=True, slots=True)
class Layout:
    gameplay_y: int
    gameplay_h: int  # altura esperada para fonte 16:9
    facecam_y: int | None
    caption_top: int


def compute_layout(r: RenderSettings, has_facecam: bool) -> Layout:
    gameplay_h = _even(r.width * 9 / (16 * r.gameplay_width_fraction))
    if has_facecam:
        facecam_y: int | None = r.facecam_top
        gameplay_y = r.facecam_top + r.facecam_height + 20
    else:
        facecam_y = None
        gameplay_y = max(0, (r.height - gameplay_h) // 2 - r.gameplay_lift)
    caption_top = min(gameplay_y + gameplay_h + r.caption_gap, r.height - 400)
    return Layout(gameplay_y, gameplay_h, facecam_y, caption_top)


def filter_path(path: Path) -> str:
    """Caminho seguro dentro de um filtergraph (Windows: 'C\\:/pasta/arquivo')."""
    escaped = path.resolve().as_posix().replace("'", r"\'").replace(":", r"\:")
    return f"'{escaped}'"


def build_filtergraph(
    r: RenderSettings,
    layout: Layout,
    facecam: Region | None,
    *,
    ass_name: str = _ASS_NAME,
) -> str:
    w, h = r.width, r.height
    bw, bh = _even(w / 8), _even(h / 8)
    frac = f"{r.gameplay_width_fraction:g}"
    branches = "[bg_in][gp_in][fc_in]" if facecam else "[bg_in][gp_in]"

    graph = [
        f"[0:v]fps={r.fps},split={3 if facecam else 2}{branches}",
        f"[bg_in]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"boxblur=6:2,scale={w}:{h},setsar=1[bg]",
        f"[gp_in]crop=w=iw*{frac}:h=ih:x=(iw-ow)/2:y=0,scale={w}:-2,setsar=1[gp]",
    ]
    if facecam and layout.facecam_y is not None:
        fh = r.facecam_height
        graph += [
            f"[fc_in]crop=w=iw*{facecam.w:g}:h=ih*{facecam.h:g}:"
            f"x=iw*{facecam.x:g}:y=ih*{facecam.y:g},"
            f"scale={w}:{fh}:force_original_aspect_ratio=increase,crop={w}:{fh},setsar=1[fc]",
            f"[bg][fc]overlay=x=0:y={layout.facecam_y}[bgf]",
            f"[bgf][gp]overlay=x=(W-w)/2:y={layout.gameplay_y}[base]",
        ]
    else:
        graph.append(f"[bg][gp]overlay=x=(W-w)/2:y={layout.gameplay_y}[base]")

    ass_opts = f"filename={ass_name}"
    if r.fonts_dir is not None:
        ass_opts += f":fontsdir={filter_path(r.fonts_dir)}"
    graph.append(f"[base]ass={ass_opts},format=yuv420p[vout]")

    # loudnorm processa internamente a 192 kHz: reamostrar para 48 kHz depois.
    audio = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000" if r.loudnorm else "aresample=48000"
    graph.append(f"[0:a:0]{audio}[aout]")
    return ";".join(graph)


class FfmpegMedia:
    def __init__(
        self,
        ffmpeg_path: str,
        render: RenderSettings,
        style: CaptionStyle,
        captions: CaptionParams,
        audio: AudioSettings,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._r = render
        self._style = style
        self._captions = captions
        self._audio = audio
        self.sample_rate = audio.sample_rate

    async def extract_audio(self, source: Path, offset_s: float, duration_s: float) -> bytes:
        return await run_ffmpeg(
            [
                self._ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-ss", f"{offset_s:.3f}", "-t", f"{duration_s:.3f}", "-i", str(source),
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(self.sample_rate),
                "-f", "s16le", "pipe:1",
            ],
            timeout_s=120,
            capture_stdout=True,
        )  # fmt: skip

    def audio_excitement(self, pcm16: bytes, event_s: float) -> float:
        return audio_excitement(pcm16, self.sample_rate, event_s, self._audio)

    async def render_vertical(self, request: RenderRequest) -> None:
        r = self._r
        layout = compute_layout(r, request.facecam is not None)
        events = caption_events(request.captions, self._captions)
        ass_text = build_ass(
            events,
            self._style,
            width=r.width,
            height=r.height,
            caption_top=layout.caption_top,
            credit=request.credit,
            duration_s=request.duration_s,
        )
        work = request.work_dir
        work.mkdir(parents=True, exist_ok=True)
        (work / _ASS_NAME).write_text(ass_text, encoding="utf-8")

        args = [self._ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
        if r.hwaccel:
            args += ["-hwaccel", r.hwaccel]
        args += [
            "-ss", f"{request.offset_s:.3f}", "-t", f"{request.duration_s:.3f}",
            "-i", str(request.source.resolve()),
            "-filter_complex", build_filtergraph(r, layout, request.facecam),
            "-map", "[vout]", "-map", "[aout]",
            *r.video_args,
            "-c:a", "aac", "-b:a", r.audio_bitrate, "-ar", "48000",
            "-movflags", "+faststart",
            _TMP_OUTPUT,
        ]  # fmt: skip
        # cwd = pasta do job: o filtro ass recebe nome relativo (sem escapar "C:\" no Windows)
        await run_ffmpeg(args, cwd=work, timeout_s=r.timeout_s)

        request.output.parent.mkdir(parents=True, exist_ok=True)
        _replace(work / _TMP_OUTPUT, request.output)


def _replace(src: Path, dest: Path) -> None:
    """Move sobrescrevendo (no Windows, shutil.move falha se o destino já existe)."""
    try:
        src.replace(dest)  # mesmo disco: rename atômico
    except OSError:
        dest.unlink(missing_ok=True)  # discos diferentes: copia e apaga
        shutil.move(src, dest)
