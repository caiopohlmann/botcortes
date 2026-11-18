from __future__ import annotations

from pathlib import Path

import pytest

from cortes.adapters.recording.segment_timeline import SegmentTimeline


def test_uses_minimum_latency_offset() -> None:
    tl = SegmentTimeline(offset_window=10)
    tl.new_run()
    # Rajada inicial: 3 segmentos chegam juntos (atrasados); depois chegam "na hora".
    tl.observe(Path("s1"), 0, 2, observed_at=1006.0)
    tl.observe(Path("s2"), 2, 4, observed_at=1006.0)
    tl.observe(Path("s3"), 4, 6, observed_at=1006.0)
    seg = tl.observe(Path("s4"), 6, 8, observed_at=1008.0)
    # offset mínimo = 1008 - 8 = 1000 (s3 deu exatamente o mesmo)
    assert seg.wall_start == pytest.approx(1006.0)
    assert seg.wall_end == pytest.approx(1008.0)


def test_between_and_expire() -> None:
    tl = SegmentTimeline()
    tl.new_run()
    for i in range(10):
        tl.observe(Path(f"s{i}"), i * 2, i * 2 + 2, observed_at=100 + i * 2 + 2)
    hits = tl.between(105, 109)
    assert [s.path.name for s in hits] == ["s2", "s3", "s4"]
    expired = tl.expire(older_than_ts=106.5)
    assert [s.path.name for s in expired] == ["s0", "s1", "s2"]
    assert tl.segments[0].path.name == "s3"


def test_between_keeps_single_run() -> None:
    tl = SegmentTimeline()
    tl.new_run()
    tl.observe(Path("a1"), 0, 2, observed_at=102)
    tl.observe(Path("a2"), 2, 4, observed_at=104)
    tl.new_run()  # ffmpeg reiniciou: PTS volta a zero
    tl.observe(Path("b1"), 0, 2, observed_at=110)
    tl.observe(Path("b2"), 2, 4, observed_at=112)
    hits = tl.between(100, 112)
    assert [s.path.name for s in hits] == ["b1", "b2"]


def test_pts_reset_inside_run_recomputes_offset() -> None:
    tl = SegmentTimeline()
    tl.new_run()
    tl.observe(Path("x1"), 100, 102, observed_at=1002)
    seg = tl.observe(Path("x2"), 0, 2, observed_at=1010)  # descontinuidade
    assert seg.wall_end == pytest.approx(1010)
