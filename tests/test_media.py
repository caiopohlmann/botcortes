from __future__ import annotations

from pathlib import Path

import numpy as np

from cortes.adapters.media.ass import ass_color, ass_time, build_ass, escape_text
from cortes.adapters.media.audio import audio_excitement
from cortes.adapters.media.render import build_filtergraph, compute_layout, filter_path
from cortes.config import AudioSettings, CaptionStyle, RenderSettings
from cortes.domain.captions import CaptionEvent
from cortes.domain.models import Region


def test_ass_color_is_bgr() -> None:
    assert ass_color("#FFE600") == "&H0000E6FF"
    assert ass_color("#000000", alpha=0x64) == "&H64000000"


def test_ass_time() -> None:
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(61.234) == "0:01:01.23"
    assert ass_time(3600.5) == "1:00:00.50"


def test_escape() -> None:
    assert escape_text("a{b}\\c\nd") == "a(b)/c d"


def test_build_ass_highlights_current_word() -> None:
    events = [CaptionEvent(start=0.0, end=0.5, words=("OLHA", "ISSO"), highlight=1)]
    text = build_ass(
        events,
        CaptionStyle(),
        width=1080,
        height=1920,
        caption_top=1300,
        credit="twitch.tv/fulano",
        duration_s=55,
    )
    assert "PlayResY: 1920" in text
    assert "Credit,,0,0,0,,twitch.tv/fulano" in text
    assert "OLHA {\\c&H00E6FF&\\fscx112\\fscy112}ISSO{\\r}" in text
    assert ",8,60,60,1300," in text  # caption alinhada no topo em y=1300


def test_layout_without_facecam_centers_gameplay() -> None:
    layout = compute_layout(RenderSettings(), has_facecam=False)
    assert layout.gameplay_h == 608
    assert layout.gameplay_y == (1920 - 608) // 2 - 80
    assert layout.caption_top > layout.gameplay_y + layout.gameplay_h


def test_filtergraph_with_facecam() -> None:
    r = RenderSettings()
    layout = compute_layout(r, has_facecam=True)
    graph = build_filtergraph(r, layout, Region(x=0.0, y=0.7, w=0.2, h=0.3))
    assert "split=3[bg_in][gp_in][fc_in]" in graph
    assert "[bg][fc]overlay" in graph
    assert "ass=filename=subs.ass" in graph
    assert graph.endswith("[aout]")


def test_filtergraph_without_loudnorm() -> None:
    r = RenderSettings(loudnorm=False)
    graph = build_filtergraph(r, compute_layout(r, False), None)
    assert "loudnorm" not in graph
    assert "split=2[bg_in][gp_in]" in graph


def test_filter_path_escapes_colon() -> None:
    escaped = filter_path(Path("C:/fonts"))
    assert escaped.startswith("'") and escaped.endswith("'")
    assert "\\:" in escaped and "\\" not in escaped.replace("\\:", "")


def test_audio_excitement_detects_loud_event() -> None:
    sr = 16_000
    t = np.arange(sr * 30) / sr
    quiet = 0.02 * np.sin(2 * np.pi * 220 * t)
    loud = quiet.copy()
    loud[sr * 20 : sr * 23] *= 20  # grito em 20-23 s
    s = AudioSettings()
    pcm_quiet = (quiet * 32767).astype(np.int16).tobytes()
    pcm_loud = (loud * 32767).astype(np.int16).tobytes()
    assert audio_excitement(pcm_quiet, sr, event_s=21, s=s) < 0.1
    assert audio_excitement(pcm_loud, sr, event_s=21, s=s) > 0.9
